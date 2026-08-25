#!/usr/bin/env python3
"""The live loop: evaluate all detectors on every 5-minute bar close,
journal every cluster, and alert subject to the budget.

Default mode is log-only (ConsoleAlerter). --live pushes to Telegram and
is required for that. --replay-date YYYY-MM-DD runs the exact same
pipeline fast-forwarded against a cached session instead of waiting on
real market hours — see run_replay() below.

IMPORTANT: run_live() has only been verified to construct and wire
together correctly; it has not been exercised against real live market
conditions in this build (that requires actually running it during
market hours). run_replay() is the path that's actually been run
end-to-end and demonstrated. Treat --live as unverified until someone
runs it live and confirms.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exchange_calendars as ecals
import requests

from tradebot.alerts import (
    AlertBudget,
    Cluster,
    ConsoleAlerter,
    Decision,
    SuppressionCategory,
    TelegramAlerter,
    suppression_category_for_decision,
)
from tradebot.config import MARKET_PROXY_SYMBOLS, WATCHLIST
from tradebot.costs import select_contract
from tradebot import dedup
from tradebot.events import active_event_window, events_for_date, has_earnings_before
from tradebot import evaluations as evaluations_mod
from tradebot.rendering import templates
from tradebot.guard import extreme_mover_evidence, validate_alert_data
from tradebot import metrics
from tradebot.telegram_bot import heartbeat as bot_liveness
from tradebot.telegram_bot import outbox
from tradebot.telegram_bot.performance import track_record, weekly_recap
from tradebot.detectors import (
    CONTEXT_DETECTORS,
    DETECTORS,
    Bar,
    DailyAnchors,
    atr,
    bar_close_ts,
    build_anchors,
    score_cluster,
    tier_for_score,
)
from tradebot.journal import (
    CLOSE_MARK_OFFSET_MIN,
    OUTCOME_OFFSETS_MIN,
    ProductionJournalRefused,
    RUN_MODE_LIVE,
    RUN_MODE_REPLAY,
    RUN_MODE_UNKNOWN,
    UNATTRIBUTED_RUN_ID,
    backfill_marks,
    code_version,
    connect,
    detected_symbols_for_session,
    historical_performance,
    iv_rank,
    new_run_id,
    pending_contract_backfills,
    pending_contract_close_backfills,
    pending_contract_day_range_backfills,
    record_contract_day_range,
    record_contract_forward_mid,
    record_contract_selection,
    record_decision_event,
    record_iv_sample,
    resolve_replay_db_path,
    set_extreme_mover,
    set_news_driven,
    set_no_trade,
    tier_performance,
)
from tradebot.journal import write_cluster as journal_write_cluster
from tradebot.features import pct_from_prior_close
from tradebot.marketdata import (
    PLAUSIBILITY_WINDOW_SESSIONS,
    LiveMarketData,
    Quote,
    ReplayMarketData,
    _is_rth,
    filter_plausible_sessions,
    implausible_session_reason,
    median_session_volume,
    write_bars_csv,
)

SIMILAR_SETUPS_LOOKBACK = 200  # deep enough to realistically reach costs.MIN_SIMILAR_SETUPS_SAMPLE (50)

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "cache"
HALT_FILE = REPO_ROOT / "data" / "HALT"
HEARTBEAT_FILE = REPO_ROOT / "data" / "heartbeat.json"
WEEKLY_RECAP_STATE_FILE = REPO_ROOT / "data" / "weekly_recap_state.json"
ET = ZoneInfo("America/New_York")
STALENESS_SECONDS = 90
BAR_MINUTES = 5
# How long run_live() idles before returning on a non-trading day, so a
# process supervisor with no restart backoff (Docker's `restart:
# unless-stopped`, see docker-compose.yml) doesn't spin in a tight
# restart loop all weekend. Bare-metal deployments don't hit this path
# at all — scripts/watchdog.sh simply never starts the runner outside
# market hours in the first place.
OFF_SESSION_IDLE_SECONDS = 1800

CALENDAR = ecals.get_calendar("XNYS")

logger = logging.getLogger("watchtower.runner")


def configure_logging(level: str | None = None, stream=None) -> None:
    """Attaches (or replaces) a single stream handler on the "watchtower"
    parent logger -- not the root logger -- so INFO-level operational
    logs from watchtower.runner, and any future child such as
    watchtower.vendors.alpaca, actually reach Docker's captured
    stdout/stderr instead of being silently dropped. Without this, the
    process has no handler anywhere in its logger hierarchy, so Python
    falls back to logging.lastResort (WARNING-only) -- this is exactly
    why PR #63's broad_scan_shadow_counts INFO logs were never visible
    in production.

    Scoped to "watchtower", not the root logger: watchtower.runner and
    watchtower.vendors.alpaca are both children of it and inherit this
    handler/level via normal propagation, while third-party loggers
    (urllib3, alpaca, websockets, ...) live outside this namespace
    entirely and are completely unaffected -- their volume doesn't
    change just because Perch's own operational logs became visible.

    Only called from main(), never at module import time -- importing
    this module as a library (as every test in this file does) must
    never configure logging as a side effect. This is also why LOG_LEVEL
    is read here, not at module import time: reading it eagerly at
    import would bake in whatever the environment happened to be when
    the module was first imported, not when the process actually starts.

    Idempotent: assigns a fresh single-element handler list rather than
    appending, so calling this more than once (accidentally, or from a
    test) never produces duplicate log lines.

    level/stream: only ever overridden by tests -- real callers rely on
    the LOG_LEVEL env var (default "INFO") and sys.stderr."""
    resolved_level = (level if level is not None else os.environ.get("LOG_LEVEL", "INFO")).upper()
    watchtower_logger = logging.getLogger("watchtower")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    watchtower_logger.handlers = [handler]
    watchtower_logger.setLevel(resolved_level)
    watchtower_logger.propagate = False


# --------------------------------------------------------------------------
# Pure helpers — testable without real time or network.
# --------------------------------------------------------------------------


def is_stale(latest_bar_close: datetime, now: datetime, max_seconds: int = STALENESS_SECONDS) -> bool:
    return (now - latest_bar_close).total_seconds() > max_seconds


def only_closed_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    """Live-only lookahead guard (CLAUDE.md: never make a decision
    timestamped before the bar it used has closed). LiveMarketData.
    session_bars() can include the currently-forming bar — Alpaca's
    intraday-bars response, queried through "now", may report the
    in-progress candle with whatever partial OHLCV has accumulated so
    far — and nothing in that read path checks a bar's own close time
    against the wall clock. is_stale() doesn't catch this either: it
    only flags a bar that's too OLD (now - close > max_seconds), and a
    still-forming bar's bar_close_ts is in the future, so that
    subtraction is negative and trivially "not stale".

    Filters rather than truncates-from-the-first-bad-one, so a genuinely
    completed bar is never dropped just because a later, still-forming
    one was also returned in the same response — correct even if a
    vendor response were ever out of chronological order, not just in
    the common case where only the last element can possibly be
    incomplete. Exactly at its own close (bar_close_ts(bar) == now) a
    bar is included, not excluded — "closed", not "closed and a beat
    late".

    Live only: ReplayMarketData's bars are cached historical data, real
    and fully closed by construction, so run_replay() has no equivalent
    call and no use for this — see its own separate loop in run_replay().
    """
    return [b for b in bars if bar_close_ts(b) <= now]


def latest_required_bar_close(
    session_open: datetime,
    now: datetime,
    grace_seconds: int = STALENESS_SECONDS,
    session_close: datetime | None = None,
    bar_minutes: int = BAR_MINUTES,
) -> datetime | None:
    """The most recent bar boundary that must ALREADY be available by
    `now`, given `grace_seconds` of tolerance past its own nominal close
    — None if no boundary has become required yet.

    This is deliberately NOT "the most recent boundary at or before
    now" (that reads as stale for most of every healthy candle — a bar
    that closed 4 minutes ago while its successor is still forming is
    completely normal, not delayed data). It's also not "gap from the
    single latest boundary" (that under-reports: if we're missing
    several bars in a row, an EARLIER boundary can already be overdue
    even while the very latest one is still inside its own grace
    window — checking only the latest boundary would miss that). This
    finds the latest boundary B such that `now - B > grace_seconds`
    (strict, matching is_stale()'s own strict `>` discipline — a bar
    exactly `grace_seconds` late is not yet stale, only one MORE than
    that late is) by counting how many bar_minutes-aligned boundaries
    fall strictly before `now - grace_seconds`.

    session_close (session_bounds()'s own close_ts, not a hardcoded
    16:00) caps the RESULT, not the input `now` — clamping `now` first
    would subtract grace_seconds from an already-capped value, so the
    final session's own bar could never earn its own grace window and
    would never become required no matter how long it stayed missing
    after the close (real bug, caught in review: clamping-then-grace
    stuck the required boundary one bar early forever). Grace is applied
    to the real, unclamped `now` so the final bar gets exactly the same
    grace period every other bar does; only the boundary this produces
    is then capped at session_close, so a sufficiently delayed loop
    iteration can still never demand a fictional post-close bar —
    early-close safe because session_close itself already reflects a
    real early close, not assumed from the caller's timing alone.
    """
    bar_seconds = bar_minutes * 60
    elapsed_past_grace = (now - timedelta(seconds=grace_seconds) - session_open).total_seconds()
    if elapsed_past_grace <= 0:
        return None
    completed = math.ceil(elapsed_past_grace / bar_seconds) - 1
    if completed < 1:
        return None
    required = session_open + timedelta(seconds=bar_seconds * completed)
    if session_close is not None and required > session_close:
        required = session_close
    return required


def is_halted_bar(bar: Bar) -> bool:
    """A zero-volume bar means no trades happened — treat it as no new
    information rather than feeding it to the detectors, so a halted
    symbol (common for BE and IONQ) doesn't produce garbage on reopen."""
    return bar.volume == 0


def bar_gap_minutes(bars: list[Bar], bar_minutes: int = BAR_MINUTES) -> float | None:
    """Minutes between the two most recent bars' OPEN timestamps, or None
    with fewer than 2 bars. A vendor that silently drops a bar (unlike
    is_halted_bar's zero-volume case, which is an explicit bar the vendor
    DID return) shows up here as a gap wider than bar_minutes."""
    if len(bars) < 2:
        return None
    return (bars[-1].ts - bars[-2].ts).total_seconds() / 60


def is_bar_gap(bars: list[Bar], bar_minutes: int = BAR_MINUTES, tolerance_minutes: float = 0.0) -> bool:
    """True when the most recent bar arrived later than expected — a
    silently-missing mid-session bar, not caught by is_halted_bar since
    no zero-volume bar was ever returned for the gap to begin with.
    tolerance_minutes defaults to 0 (strict); loosen only after a real
    replay run shows benign vendor timestamp jitter, not by guessing."""
    gap = bar_gap_minutes(bars, bar_minutes)
    return gap is not None and gap > bar_minutes + tolerance_minutes


def session_bounds(session_date: date, calendar=CALENDAR) -> tuple[datetime, datetime]:
    """(open, close) in UTC for session_date, honoring early closes (e.g.
    13:00 ET) — never hardcode 09:30-16:00 ET."""
    if not calendar.is_session(session_date):
        raise ValueError(f"{session_date} is not a trading session")
    open_ts = calendar.session_open(session_date).to_pydatetime().astimezone(timezone.utc)
    close_ts = calendar.session_close(session_date).to_pydatetime().astimezone(timezone.utc)
    return open_ts, close_ts


def cached_session_dates(cache_dir: Path, symbols: list[str]) -> list[date]:
    common: set[date] | None = None
    for symbol in symbols:
        dates = {date.fromisoformat(p.stem.removeprefix("intraday_")) for p in (cache_dir / symbol).glob("intraday_*.csv")}
        common = dates if common is None else (common & dates)
    return sorted(common or set())


def full_session_rth_bars(symbol: str, session_date: date, cache_dir: Path = CACHE_DIR) -> list[Bar]:
    md = ReplayMarketData(cache_dir, symbol, session_date)
    while md.advance():
        pass
    return list(md.session_bars(symbol, session_date))


def expected_rth_bar_count(session_date: date, calendar=CALENDAR, bar_minutes: int = BAR_MINUTES) -> int:
    """The calendar-expected number of RTH bars for session_date, honoring
    early closes -- the plausibility floor's (Proposal 5c) bar-count
    reference, so a 13:00 ET early close is never mistaken for a runt."""
    open_ts, close_ts = session_bounds(session_date, calendar)
    return int((close_ts - open_ts).total_seconds() // (bar_minutes * 60))


def _build_history_by_symbol(
    cache_dir: Path, symbols: list[str], session_date: date, stats: "HeartbeatStats | None" = None,
) -> dict[str, list[list[Bar]]]:
    """The baseline-building half of Proposal 5c's plausibility floor:
    every cached session that feeds a symbol's avg_cum_volume_by_bar (and
    any future TR profile) is run through filter_plausible_sessions
    first, so a runt file can never join a baseline. A rejection is never
    silent -- ERROR log, a metrics counter, and (when stats is given, as
    both run_live and run_replay do) a heartbeat data_gaps line."""
    historical_sessions = [s for s in cached_session_dates(cache_dir, symbols) if s < session_date]
    history_by_symbol: dict[str, list[list[Bar]]] = {}
    for symbol in symbols:
        sessions = [(d, full_session_rth_bars(symbol, d, cache_dir)) for d in historical_sessions]
        accepted, rejections = filter_plausible_sessions(sessions, expected_rth_bar_count)
        history_by_symbol[symbol] = accepted
        for rejected_date, reason in rejections:
            logger.error(
                "plausibility floor rejected %s %s from the baseline: %s", symbol, rejected_date.isoformat(), reason,
            )
            metrics.increment("plausibility_floor_rejection", stage="baseline", symbol=symbol, rule=reason.split(":")[0])
            if stats is not None:
                stats.data_gaps.append(f"{symbol} {rejected_date.isoformat()}: {reason}")
    return history_by_symbol


@dataclass
class HeartbeatStats:
    start_time: datetime
    session_date: date
    tier_counts: Counter = field(default_factory=Counter)
    suppression_counts: Counter = field(default_factory=Counter)
    data_gaps: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def record_cluster(self, tier: str, decision: Decision) -> None:
        self.tier_counts[tier] += 1
        if decision != Decision.SEND:
            self.suppression_counts[decision.value] += 1


def evaluate_bar(
    symbol: str, bars: list[Bar], anchors: DailyAnchors, market_bars: dict[str, list[Bar]] | None = None
) -> dict | None:
    core = [d for d in (detector(bars, anchors) for detector in DETECTORS) if d is not None]
    context = [d for d in (detector(bars, anchors, market_bars) for detector in CONTEXT_DETECTORS) if d is not None]
    detections = core + context
    if not detections:
        return None
    last = bars[-1]
    expected_close = bar_close_ts(last)
    for d in detections:
        assert d.ts >= expected_close, (
            f"lookahead violation: {d.kind} detection ts={d.ts} precedes bar close={expected_close} for {symbol}"
        )
    primary = max(detections, key=lambda d: d.score)
    # Root cause of the dual-ATR bug: level_break/range_expansion/vwap_break/
    # round_number_break each compute their OWN ATR14 window (inconsistently
    # — range_expansion uses bars[:-1], the others use bars) and print it
    # directly in their headline ("...42.10x ATR(14)=0.88"). Independently
    # recomputing atr(bars) here for the cluster-level ATR14 stat meant the
    # alert could show a second, different number for the same concept —
    # e.g. headline "ATR(14)=0.88" next to a stats-block "ATR14  1.77".
    # Reuse whatever ATR the primary (headline) detector actually used, so
    # the two can never disagree — this changes zero detector scoring, only
    # which number the cluster reports alongside it. Detectors that don't
    # reference ATR in their headline at all (gap, rvol_spike) have no
    # context["atr14"], so this falls back to the general bars-window ATR,
    # which nothing in their headline could contradict anyway.
    atr14 = primary.context.get("atr14")
    if atr14 is None:
        atr14 = atr(bars)
    # A1 (docs/open-awareness-proposals-2026-08.md): prior-close
    # displacement recorded alongside every cluster this evaluation
    # already produces -- a RECORDED FEATURE ONLY, never a scoring or
    # tiering input. See tradebot.features.pct_from_prior_close.
    displacement = pct_from_prior_close(last.close, anchors.prior_close)
    return {
        "ts": expected_close,
        "close": last.close,
        "atr14": atr14,
        "kinds": ",".join(d.kind for d in detections),
        "primary_kind": primary.kind,
        "primary_headline": primary.headline,
        "primary_detection": primary,
        "headlines": "; ".join(d.headline for d in detections),
        "score": score_cluster(detections),
        "trend": "up" if last.close >= anchors.prior_close else "down",
        "detections": detections,
        "pct_from_prior_close": displacement.value,
        "pct_from_prior_close_status": displacement.status,
    }


class TelegramHaltChecker:
    """Polls Telegram for a '/halt' message. Never lets a network blip
    crash the loop — a failed check just means 'not halted this tick'."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self._offset: int | None = None

    def check(self) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": 0}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except requests.RequestException:
            return False
        halted = False
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message", {})
            if str(msg.get("chat", {}).get("id")) == self.chat_id and msg.get("text", "").strip() == "/halt":
                halted = True
        return halted


# --------------------------------------------------------------------------
# Shared per-bar pipeline: evaluate -> journal -> budget -> alert
# --------------------------------------------------------------------------


def _commit_then_send(conn, alerter, text: str, *, priority: int, alert_id: str | None = None) -> None:
    """Commit journal.db, THEN send. The only way process_new_bar is
    allowed to alert.

    journal.db and users.db are separate SQLite files with no
    cross-database transaction. `alerter.send()` writes to users.db's
    outbox and commits there immediately, and a separate worker process
    delivers from that outbox within seconds — so the moment this
    function's send returns, the alert is durable and on its way,
    whether or not journal.db's own transaction ever closes. Before this
    helper existed, a HIGH alert on the NO TRADE path went out at a
    point where the detection row it references (INSERTed at the top of
    process_new_bar) was still uncommitted: a SIGKILL/OOM/power-loss in
    that window rolled the detection back and left a real subscriber
    alert pointing at a detection_id that never existed. That breaks
    CLAUDE.md's "every detection is journaled before any alert is sent",
    and concretely it breaks the alert's own inline keyboard — tapping
    "I took this" resolves the id against journal.db
    (telegram_bot/handlers.py:_resolve_detection) and answers "I don't
    recognize alert id ...", refusing to log a trade the subscriber
    really took.

    The trade path happened not to be exposed: record_contract_selection()
    commits, which flushes the pending detection INSERT with it. That's
    an accident of an unrelated function's commit, not a guarantee —
    hence a helper rather than one more bare commit() that the next
    edit can forget to keep ahead of a new send().

    Deliberately does NOT carry the post-alert writes (the alerted flag,
    suppression reasons). Committing `alerted=1` before the send would
    risk recording an alert we never actually sent; leaving it after
    risks not recording one we did. For a product whose whole positioning
    is an unedited track record, only the undercount direction is
    acceptable — see docs/full-code-review.md finding #5."""
    conn.commit()
    alerter.send(text, priority=priority, alert_id=alert_id)


def _record_decision(
    conn, detection_id, *, stage, decision, run_mode, run_id, version,
    reason=None, detail=None,
) -> None:
    """Append one decision_events row for a decision process_new_bar has
    already taken. Instrumentation, and instrumentation only.

    Two properties this wrapper exists to guarantee at every call site,
    rather than leaving each of them to remember:

    commit=False. process_new_bar owns a transaction whose boundary is
    load-bearing: _commit_then_send() commits precisely so that a
    detection is durable before any alert referencing it exists, and
    nothing before that point may commit on its own (see that function's
    docstring for the bug that produced the rule). journal's helper
    commits by default; every call from here overrides it. The events
    still land — flushed by whichever commit the caller was already
    going to make, which on the alerting path is _commit_then_send's,
    i.e. still ahead of the send.

    Swallowed failures. Recording that a decision was taken must never
    change which decision was taken, and must never turn a bar that
    would have alerted into a bar that raised instead. A broken ledger
    write is worth an ERROR line and a counter; it is not worth the
    alert. (SQLite doesn't roll back a transaction over one failed
    statement, so the pending detection writes survive this too.)"""
    try:
        record_decision_event(
            conn, detection_id, stage=stage, decision=decision, reason=reason, detail=detail,
            code_version_str=version, run_mode=run_mode, run_id=run_id, commit=False,
        )
    except Exception:
        logger.error(
            "decision event write failed: stage=%s decision=%s detection_id=%s",
            stage, decision, detection_id, exc_info=True,
        )
        metrics.increment("decision_event_write_failed", stage=stage)


def _anchors_as_dict(anchors) -> dict | None:
    """DailyAnchors as plain JSON-able data.

    Frozen for the session (CLAUDE.md), so this is written once per
    symbol-session and is the other half of what a detector needs to be
    re-run offline against a stored bar -- rvol_spike in particular is
    unreproducible without avg_cum_volume_by_bar. Its int keys become
    strings in JSON, which is a lossless round trip for a reader that
    expects it."""
    try:
        return {
            "symbol": anchors.symbol,
            "session_date": anchors.session_date.isoformat(),
            "prior_close": anchors.prior_close,
            "prior_high": anchors.prior_high,
            "prior_low": anchors.prior_low,
            "opening_range_high": anchors.opening_range_high,
            "opening_range_low": anchors.opening_range_low,
            "opening_range_volume": anchors.opening_range_volume,
            "swing_high": anchors.swing_high,
            "swing_low": anchors.swing_low,
            "avg_cum_volume_by_bar": dict(anchors.avg_cum_volume_by_bar),
        }
    except Exception:
        return None


def _record_evaluation(
    eval_conn, bars, *, symbol, session_date, outcome, run_mode, run_id, version,
    anchors=None, origin=None, atr14=None, kinds=None, cluster_score=None,
    tier=None, detection_id=None, error=None,
) -> None:
    """Record one Stage 2 bar evaluation. Instrumentation, and
    instrumentation only.

    eval_conn=None means recording is off, and returns before doing any
    work at all -- the default for every caller that doesn't opt in,
    which is every existing test and every replay. So this layer is
    completely inert unless run_live hands it a connection.

    Swallows its own failures, like _record_decision: knowing what the
    detectors saw must never change what they decided, and must never
    turn a bar that would have alerted into a bar that raised.

    Writes to evaluations.db on its own connection. Two of the outcomes
    are recorded BEFORE process_new_bar opens its journal.db
    transaction, so writing them into journal.db would leave one open
    that this function then abandons -- a separate file makes that
    impossible rather than merely avoided, and keeps _commit_then_send's
    ordering untouchable from here.

    atr14 is computed here, lazily, only when a caller didn't already
    have one: on the NO_DETECTION path evaluate_bar returned None and so
    resolved no ATR, but "how big was this bar really" is the first
    question anyone asks of a missed mover. Inside the guard, so a
    failure costs the row, never the bar."""
    if eval_conn is None:
        return
    try:
        last = bars[-1]
        if atr14 is None:
            atr14 = atr(bars)
        evaluations_mod.record_bar_evaluation(
            eval_conn,
            session=session_date.isoformat(), symbol=symbol,
            run_id=run_id, run_mode=run_mode,
            now_utc=datetime.now(timezone.utc).isoformat(),
            bar_ts_utc=last.ts.isoformat(), outcome=outcome,
            open=last.open, high=last.high, low=last.low, close=last.close, volume=last.volume,
            atr14=atr14, kinds=kinds, cluster_score=cluster_score, tier=tier,
            detection_id=detection_id, error=error,
            code_version=version, origin=origin,
            anchors=_anchors_as_dict(anchors) if anchors is not None else None,
        )
    except Exception:
        logger.error("bar evaluation write failed: symbol=%s outcome=%s", symbol, outcome, exc_info=True)
        metrics.increment("evaluation_write_failed", outcome=outcome)


def process_new_bar(
    conn, budget, alerter, version, symbol, session_date, bars, anchors, quote_fn, chain_fn, stats,
    subscriber_hook=None, validation_now_fn=None, market_bars=None, data_feed=None, origin="watchlist",
    # Defaults for direct/test/legacy callers only -- run_live() and
    # run_replay() always pass both explicitly. 'unknown' means the
    # caller did not say, and is never to be read as live. See the
    # docstring's run_mode/run_id paragraphs.
    run_mode=RUN_MODE_UNKNOWN, run_id=UNATTRIBUTED_RUN_ID,
    # Stage 2 observability. None (the default) disables recording
    # entirely, so every existing caller and test is unaffected;
    # run_live opens the connection once and passes it in.
    eval_conn=None,
) -> None:
    """subscriber_hook(cluster, rendered_text, entry_mid), if given, is
    called right after a HIGH alert is sent to the ops channel/console —
    it's how the live per-user DM fan-out (tradebot.telegram_bot.delivery)
    plugs in without this module needing to know anything about Telegram
    users. entry_mid is the real per-contract debit select_contract()
    computed (None on a NO TRADE), passed through so delivery.py can size
    a position per-subscriber without this module knowing account sizes
    or risk tolerances either. None (the default) preserves the exact
    prior behavior — no fan-out, used by replay and by every existing
    test.

    validation_now_fn: zero-argument callable returning real wall-clock
    time, tz-aware, for the guard's quote-staleness check. Called (if
    given) immediately after quote_fn(symbol) returns, so the captured
    timestamp reflects when the quote actually arrived — not when this
    function, or the caller's loop iteration, started. An iteration can
    be delayed by unrelated earlier work (other symbols, a broad scan),
    so a timestamp captured any earlier would understate the quote's true
    age, in the worst case making a genuinely stale quote look fresh.
    Only run_live() passes this — a replayed historical quote is
    definitionally hours/days "stale" relative to real now, so replay
    passes None and the guard skips that one check, the same way
    STALENESS_SECONDS/is_stale() is already a live-only concept here.

    market_bars: {proxy_symbol: bars}, for detectors.relative_strength_break
    via evaluate_bar's CONTEXT_DETECTORS pass. None (the default) means
    that detector simply never fires — same fail-conservative behavior
    as any other missing-data case, not an error.

    data_feed/origin: passed straight through to journal.write_cluster()
    and the Cluster this builds — see that function's docstring. Both
    callers (run_replay/run_live) resolve these once per invocation/tick
    and pass them in; this function has no way to know either on its own.

    run_mode/run_id: stamped onto every decision_events row this function
    appends, so a replay's decisions can never be mistaken for the live
    ones from the same session (the detection_id is a hash of
    symbol/session/ts/kinds and is therefore identical across runs —
    these two columns are the only thing that separates them). The
    production entry points both resolve them once per call and pass them
    explicitly — run_live() sends RUN_MODE_LIVE, run_replay() sends
    RUN_MODE_REPLAY, each with its own new_run_id() — exactly the way
    they already pass data_feed/origin. No production path relies on the
    defaults.

    The defaults exist for the other kind of caller: a direct one — a
    test, a script, a REPL session, anything predating these parameters —
    that has no run to attribute its events to. For those,
    'unknown'/'unattributed' is the honest answer, and it is deliberately
    a loud one rather than a NULL or an empty string.

    Read them as 'this row did not say', NEVER as live. An event stamped
    RUN_MODE_UNKNOWN is not evidence of a live decision and must not be
    counted as one; the only rows that assert live are the ones that say
    RUN_MODE_LIVE. Nothing else about this function's behavior reads
    either parameter — they are carried to the ledger and nowhere
    else."""
    last = bars[-1]

    def _evaluation(outcome, **fields):
        """Stage 2 observability for THIS bar. Every field it needs about
        the call is already fixed by this point; only the outcome and the
        detection details vary."""
        _record_evaluation(
            eval_conn, bars, symbol=symbol, session_date=session_date, outcome=outcome,
            run_mode=run_mode, run_id=run_id, version=version, anchors=anchors,
            origin=origin, **fields,
        )

    # Two flat try blocks rather than nested ones, so each outcome is
    # recorded exactly once: a detector crash is DETECTOR_ERROR and is
    # never also relabelled EVALUATION_ERROR on its way out.
    try:
        if is_halted_bar(last):
            stats.data_gaps.append(f"{symbol} zero-volume bar at {last.ts.isoformat()} (halted?)")
            metrics.increment("data_health_suppression", reason="halted")
            _evaluation(evaluations_mod.OUTCOME_HALTED_BAR)
            return
        if is_bar_gap(bars):
            stats.data_gaps.append(f"{symbol}: bar gap ({bar_gap_minutes(bars):.0f}min) at {last.ts.isoformat()}")
            metrics.increment("data_health_suppression", reason="bar_gap")
            _evaluation(evaluations_mod.OUTCOME_BAR_GAP)
            return
    except Exception as exc:
        # A data-health guard itself failed. Recorded and RE-RAISED
        # unchanged -- before this layer existed the exception propagated
        # to the caller's own handler, and swallowing it here would be a
        # behavior change, not instrumentation.
        _evaluation(evaluations_mod.OUTCOME_EVALUATION_ERROR, error=f"{type(exc).__name__}: {exc}"[:500])
        raise

    try:
        result = evaluate_bar(symbol, bars, anchors, market_bars)
    except Exception as exc:
        # A detector crashed, or the lookahead assertion tripped. Same
        # discipline: record, then re-raise untouched.
        _evaluation(evaluations_mod.OUTCOME_DETECTOR_ERROR, error=f"{type(exc).__name__}: {exc}"[:500])
        raise

    if result is None:
        # The black hole this layer exists for: every detector ran, none
        # fired, and until now nothing anywhere recorded that it happened.
        _evaluation(evaluations_mod.OUTCOME_NO_DETECTION)
        return

    detection_id = journal_write_cluster(
        conn,
        session=session_date.isoformat(),
        symbol=symbol,
        ts_utc=result["ts"].isoformat(),
        kinds=result["kinds"],
        headlines=result["headlines"],
        score=result["score"],
        close=result["close"],
        atr14=result["atr14"],
        trend=result["trend"],
        detections=result["detections"],
        code_version_str=version,
        primary_kind=result["primary_kind"],
        data_feed=data_feed,
        origin=origin,
        # .get(): test doubles that monkeypatch evaluate_bar with a
        # hand-built result dict (see test_integration_pipeline.py)
        # predate these two keys -- None/None there is the correct
        # "primitive never ran" default, same as every other pre-A1 row.
        pct_from_prior_close=result.get("pct_from_prior_close"),
        pct_from_prior_close_status=result.get("pct_from_prior_close_status"),
    )
    tier = tier_for_score(result["score"]).value
    raw_tier_is_high = tier == "high"
    # Recorded too, so the funnel is complete: "was this bar evaluated at
    # all?" becomes a direct lookup rather than an inference from absence,
    # and detection_id makes the join to journal.db's detections total.
    # The tier here is the true score-based one, before any event-window
    # routing downgrade -- same ground truth detections.tier holds.
    _evaluation(
        evaluations_mod.OUTCOME_DETECTED, atr14=result["atr14"], kinds=result["kinds"],
        cluster_score=result["score"], tier=tier, detection_id=detection_id,
    )

    # News/macro tagging — see tradebot.events module docstring: news is
    # suppression and context, never an alert source. Any overlapping
    # event window, regardless of severity or tier, marks this cluster
    # news-driven: continuation stats don't transfer to event-driven
    # moves, so a reader (and historical_performance()'s own sample pool)
    # needs to know this one doesn't count as a clean technical setup.
    event_window = active_event_window(conn, symbol, result["ts"])
    news_driven = event_window is not None
    if news_driven:
        set_news_driven(conn, detection_id, True, kind=event_window.kind, severity=event_window.severity)

    # Cross-time dedup — see tradebot.dedup module docstring. A crash in
    # this lookup itself is treated as WATCH (not a duplicate), a
    # deliberate exception to "always suppress on failure": a dedup-check
    # bug says nothing about whether the underlying SIGNAL is
    # trustworthy (unlike a stale quote or missing bar), so the safer
    # failure mode is to let it through the normal budget pipeline rather
    # than silently zero out all HIGH alerting on a transient bug here.
    try:
        dedup_result = dedup.evaluate_dedup(conn, symbol, result["ts"], result["score"])
    except Exception as exc:
        logger.error("dedup check failed: symbol=%s detection_id=%s", symbol, detection_id, exc_info=True)
        metrics.increment("dedup_check_failed")
        dedup_result = dedup.DedupResult(dedup.LifecycleState.WATCH, None, False)
        # The forced WATCH above is indistinguishable, in `detections`,
        # from a WATCH the dedup logic actually decided on: both write
        # lifecycle_state='watch' and related_detection_id=NULL. Only
        # the ledger can say which one this row was.
        _record_decision(
            conn, detection_id, stage="dedup", decision="WATCH_ON_LOOKUP_FAILURE",
            reason="dedup_lookup_failed", run_mode=run_mode, run_id=run_id, version=version,
            detail={"error_type": type(exc).__name__, "error": str(exc)[:500], "symbol": symbol},
        )
    conn.execute(
        "UPDATE detections SET lifecycle_state=?, related_detection_id=? WHERE id=?",
        (dedup_result.lifecycle_state.value, dedup_result.related_detection_id, detection_id),
    )

    # HIGH-only blackout action — suppress or downgrade, stating why.
    # MEDIUM/LOG are already batched, so there's no immediate-publish race
    # for a blackout window to protect against; they're tagged above but
    # not rerouted. The journal's own `tier` column (written above by
    # journal_write_cluster) always reflects the true score-based tier —
    # what changes below is only the tier used to route/publish this
    # alert, which is exactly what "downgrade" and "suppress" mean.
    if raw_tier_is_high and news_driven and event_window.severity == "downgrade":
        logger.info(
            "HIGH alert downgraded to MEDIUM by event window: symbol=%s kind=%s detail=%s source=%s",
            symbol, event_window.kind, event_window.detail, event_window.source,
        )
        metrics.increment("event_window_downgrade", kind=event_window.kind)
        tier = "medium"
        # detections.tier keeps the true score-based tier (see the block
        # comment above) — deliberately not mutated here. So the fact
        # that THIS alert was routed as medium instead of high, and why,
        # exists nowhere in the journal but this row.
        _record_decision(
            conn, detection_id, stage="event_window_routing", decision="DOWNGRADE_HIGH_TO_MEDIUM",
            reason=event_window.kind, run_mode=run_mode, run_id=run_id, version=version,
            detail={
                "journaled_tier": "high", "routed_tier": "medium", "score": result["score"],
                "event_kind": event_window.kind, "event_severity": event_window.severity,
                "event_detail": event_window.detail, "event_source": event_window.source,
            },
        )

    cluster = Cluster(
        id=detection_id,
        ts_utc=result["ts"].isoformat(),
        session=session_date.isoformat(),
        symbol=symbol,
        kinds=result["kinds"],
        headlines=result["headlines"],
        primary_headline=result["primary_headline"],
        score=result["score"],
        tier=tier,
        close=result["close"],
        atr14=result["atr14"],
        trend=result["trend"],
        code_version=version,
        origin=origin,
    )

    if raw_tier_is_high and news_driven and event_window.severity == "suppress":
        decision = Decision.SUPPRESS_NEWS_BLACKOUT
        decided_by = "event_window"
        logger.info(
            "HIGH alert suppressed by event window: symbol=%s kind=%s detail=%s source=%s",
            symbol, event_window.kind, event_window.detail, event_window.source,
        )
        metrics.increment("event_window_suppression", kind=event_window.kind)
    elif (
        raw_tier_is_high
        and dedup_result.lifecycle_state == dedup.LifecycleState.CONFIRMED
        and not dedup_result.is_escalation
    ):
        decision = Decision.SUPPRESS_DUPLICATE
        decided_by = "dedup"
        logger.info(
            "HIGH alert suppressed as duplicate: symbol=%s related_id=%s",
            symbol, dedup_result.related_detection_id,
        )
        metrics.increment("duplicate_suppression", symbol=symbol)
    else:
        decision = budget.evaluate(cluster)
        decided_by = "alert_budget"
    stats.record_cluster(tier, decision)
    # The routing decision, recorded once, whichever of the three
    # branches above produced it. AlertBudget itself stays pure — it is
    # handed no connection and gains no I/O; this is its caller writing
    # down the answer it already returned. `decided_by` is carried
    # because SUPPRESS_NEWS_BLACKOUT/SUPPRESS_DUPLICATE never reach
    # budget.evaluate() at all, and a ledger that implied they did would
    # be wrong about where the decision came from.
    _record_decision(
        conn, detection_id, stage="alert_routing", decision=decision.value,
        reason=decided_by, run_mode=run_mode, run_id=run_id, version=version,
        detail={"tier": tier, "journaled_tier": tier_for_score(result["score"]).value, "score": result["score"]},
    )

    if decision in (Decision.SEND, Decision.CAP_REACHED_NOTICE):
        quote = quote_fn(symbol)
        # Captured here, after the quote request returns, not earlier --
        # see validation_now_fn's docstring above.
        validation_now = validation_now_fn() if validation_now_fn is not None else None
        guard_reason = validate_alert_data(
            cluster, anchors, quote, bars=bars, now=validation_now, primary_detection=result["primary_detection"],
        )
        if guard_reason is None:
            # Proposal 3: re-derive the same evidence validate_alert_data
            # just used internally to decide whether to suppress -- guard.py
            # keeps its str|None contract unchanged (see its module
            # docstring), so tagging a PASS is the caller's job, using the
            # same pure inputs it already has at this exact call site.
            mover = extreme_mover_evidence(bars, anchors, quote)
            if mover is not None:
                set_extreme_mover(conn, detection_id, gap_pct=mover.gap_pct, verified_volume=mover.verified_volume)
                metrics.increment("extreme_mover_verified", symbol=symbol)

            similar_setups = historical_performance(
                conn, kind=result["primary_kind"], trend=result["trend"], exclude_id=detection_id,
                lookback=SIMILAR_SETUPS_LOOKBACK,
            )

            def bound_chain_fn(expiry, _sym=symbol):
                try:
                    return chain_fn(_sym, expiry)
                except NotImplementedError:
                    return None

            def bound_iv_rank_fn(current_iv, _sym=symbol):
                return iv_rank(conn, _sym, current_iv)

            def bound_earnings_check_fn(expiry, _sym=symbol):
                return has_earnings_before(conn, _sym, session_date, expiry)

            selection = select_contract(
                bound_chain_fn, symbol, spot=result["close"], direction=result["trend"], atr14=result["atr14"],
                similar_setups=similar_setups, today=session_date,
                iv_rank_fn=bound_iv_rank_fn, earnings_check_fn=bound_earnings_check_fn,
            )
            set_no_trade(conn, detection_id, not selection.is_tradable)
            # detections.no_trade is a bare 0/1: it records THAT there
            # was no tradable contract, never which gate refused it.
            # select_contract() already returns that reason and detail;
            # this is the only place either is written down.
            _record_decision(
                conn, detection_id,
                stage="contract_selection",
                decision="TRADABLE" if selection.is_tradable else "NO_TRADE",
                reason=None if selection.is_tradable else (
                    selection.no_trade.reason if selection.no_trade else "unknown"
                ),
                run_mode=run_mode, run_id=run_id, version=version,
                detail={
                    "expiry": selection.expiry.isoformat() if selection.expiry else None,
                    "dte": selection.dte,
                    "similar_setups_sample": selection.similar_setups_sample,
                    "insufficient_sample": selection.insufficient_sample,
                    "no_trade_detail": selection.no_trade.detail if selection.no_trade else None,
                    "is_vertical": selection.breakeven.is_vertical if selection.breakeven else None,
                    "strike": selection.breakeven.legs[0].contract.strike if selection.breakeven else None,
                    "right": selection.breakeven.legs[0].contract.right if selection.breakeven else None,
                },
            )

            entry_mid = None  # stays None on a NO TRADE — nothing to size a position against
            if selection.breakeven is not None:
                primary_contract = selection.breakeven.legs[0].contract
                if primary_contract.implied_volatility is not None:
                    record_iv_sample(conn, symbol, session_date, primary_contract.implied_volatility)
                short_leg = selection.breakeven.legs[1].contract if selection.breakeven.is_vertical else None
                entry_mid = (primary_contract.bid + primary_contract.ask) / 2
                if short_leg is not None:
                    entry_mid -= (short_leg.bid + short_leg.ask) / 2
                record_contract_selection(
                    conn, detection_id, symbol=symbol, right=primary_contract.right, strike=primary_contract.strike,
                    expiry=selection.expiry, dte=selection.dte, delta=primary_contract.delta, entry_mid=entry_mid,
                    entry_ts=result["ts"], is_vertical=selection.breakeven.is_vertical,
                    short_strike=short_leg.strike if short_leg else None,
                    short_delta=short_leg.delta if short_leg else None,
                )
                logger.info(
                    "contract selected: symbol=%s right=%s strike=%s expiry=%s dte=%s delta=%s vertical=%s insufficient_sample=%s",
                    symbol, primary_contract.right, primary_contract.strike, selection.expiry, selection.dte,
                    primary_contract.delta, selection.breakeven.is_vertical, selection.insufficient_sample,
                )
            else:
                logger.info(
                    "NO TRADE: symbol=%s reason=%s detail=%s expiry=%s",
                    symbol, selection.no_trade.reason if selection.no_trade else "unknown",
                    selection.no_trade.detail if selection.no_trade else "", selection.expiry,
                )

            text = templates.render_high_alert(
                cluster, anchors, quote, selection, similar_setups, news_driven=news_driven, extreme_mover=mover,
            )
            # Commits the detection (and everything enriched onto it
            # above: news_driven, lifecycle_state, no_trade) before the
            # alert referencing it can exist. See _commit_then_send.
            _commit_then_send(conn, alerter, text, priority=outbox.PRIORITY_HIGH, alert_id=detection_id)
            conn.execute("UPDATE detections SET alerted=1 WHERE id=?", (detection_id,))
            if subscriber_hook is not None:
                try:
                    subscriber_hook(cluster, text, entry_mid)
                except Exception:
                    stats.errors.append(f"{symbol}: subscriber fan-out failed — {traceback.format_exc()}")
        else:
            rule_name = guard_reason.split(":", 1)[0]
            logger.error(
                "alert suppressed by data guard: rule=%s symbol=%s detection_id=%s reason=%r "
                "cluster=%r anchors=%r quote=%r",
                rule_name, symbol, detection_id, guard_reason, cluster, anchors, quote,
            )
            metrics.increment("validator_rejection", rule=rule_name)
            metrics.increment("suppression", category=SuppressionCategory.DATA_INTEGRITY.value)
            stats.errors.append(f"{symbol}: alert suppressed by data guard — {guard_reason}")
            conn.execute(
                "UPDATE detections SET suppress_reason=?, suppress_category=? WHERE id=?",
                (f"data_integrity_failed: {guard_reason}", SuppressionCategory.DATA_INTEGRITY.value, detection_id),
            )
            # Rejections only. There is deliberately no matching event
            # for a guard that passed: a ledger of everything that did
            # not happen buries the decisions that did, and "the guard
            # raised no objection" is already implied by the routing
            # event above being followed by a send.
            _record_decision(
                conn, detection_id, stage="data_guard", decision="REJECT",
                reason=guard_reason, run_mode=run_mode, run_id=run_id, version=version,
                detail={"rule": rule_name, "category": SuppressionCategory.DATA_INTEGRITY.value},
            )
        if decision == Decision.CAP_REACHED_NOTICE:
            # Carries no detection_id, so it can't dangle the way the
            # HIGH alert above could — routed through the same helper
            # anyway so that every send inside process_new_bar commits
            # first, and "send with journal writes still pending" isn't
            # a shape this function can express.
            _commit_then_send(
                conn,
                alerter,
                templates.render_system_notice(
                    f"Daily high-tier alert cap ({budget.max_high_per_day}) reached. "
                    "Suppressing further HIGH alerts today.",
                    result["ts"],
                ),
                priority=outbox.PRIORITY_HIGH,
            )
            if guard_reason is None:
                category = suppression_category_for_decision(decision)
                metrics.increment("suppression", category=category.value if category else "unknown")
                conn.execute(
                    "UPDATE detections SET suppress_reason=?, suppress_category=? WHERE id=?",
                    (decision.value, category.value if category else None, detection_id),
                )
    elif decision in (
        Decision.SUPPRESS_CAP, Decision.SUPPRESS_COOLDOWN, Decision.SUPPRESS_NEWS_BLACKOUT, Decision.SUPPRESS_DUPLICATE,
    ):
        reason = decision.value
        if decision == Decision.SUPPRESS_NEWS_BLACKOUT:
            reason = f"{decision.value}:{event_window.kind}:{event_window.detail or event_window.source}"
        elif decision == Decision.SUPPRESS_DUPLICATE:
            reason = f"{decision.value}:{dedup_result.related_detection_id}"
        category = suppression_category_for_decision(decision)
        metrics.increment("suppression", category=category.value if category else "unknown")
        conn.execute(
            "UPDATE detections SET suppress_reason=?, suppress_category=? WHERE id=?",
            (reason, category.value if category else None, detection_id),
        )
    conn.commit()


def send_medium_digest_if_due(
    budget: AlertBudget, alerter, conn, when: datetime, personal_fanout_fn=None,
) -> None:
    """personal_fanout_fn(clusters, text, when), if given, is called right
    after the ops-channel digest is sent — it's how 'aggressive'-
    sensitivity subscribers get this same digest personally (see
    tradebot.telegram_bot.delivery.make_medium_fanout_fn), without this
    module needing to know anything about per-user sensitivity settings.
    None (the default) preserves the exact prior behavior — no personal
    fan-out, used by replay and by every existing test."""
    digest = budget.pop_medium_digest_if_due()
    if not digest:
        return
    tier_perf = tier_performance(conn).get("medium")
    text = templates.render_digest("Medium Digest", "medium", digest, tier_perf, when)
    alerter.send(text, priority=outbox.PRIORITY_NORMAL)
    if personal_fanout_fn is not None:
        personal_fanout_fn(digest, text, when)


def send_log_summary(budget: AlertBudget, alerter, conn, when: datetime) -> None:
    summary = budget.pop_log_summary()
    if not summary:
        return
    tier_perf = tier_performance(conn).get("log")
    alerter.send(templates.render_log_summary(summary, tier_perf, when), priority=outbox.PRIORITY_LOG)


def _alert_if_backfill_implausible(
    alerter, stats: "HeartbeatStats", marks_written: int, session_date: date, when: datetime,
) -> None:
    """2026-08-12 incident: backfill_marks() wrote 0 marks for ~160 real
    detections at a session close, with no error, no log line, no alert
    -- nothing surfaced it until a subscriber noticed the dashboard stuck
    on placeholder text hours later. Every detection that has any bars at
    all gets at least a CLOSE_MARK_OFFSET_MIN row (see backfill_marks()'s
    own docstring), so a healthy day writes at least one mark per
    detection -- marks_written < total_detections is the loud, mechanism-
    agnostic tripwire: it doesn't matter WHY the cache/vendor/fetch chain
    failed upstream, only that the one thing backfill_marks() promises
    (an outcome per detection) didn't happen. Silence at close is now
    itself the alarm."""
    total_detections = sum(stats.tier_counts.values())
    if total_detections == 0 or marks_written >= total_detections:
        return
    logger.error(
        "backfill_marks wrote implausibly few marks: %d marks for %d detections (session=%s)",
        marks_written, total_detections, session_date.isoformat(),
    )
    try:
        alerter.send(
            templates.render_failure_notice(
                f"backfill_marks wrote only {marks_written} mark(s) for {total_detections} "
                f"detection(s) on {session_date.isoformat()} -- today's AFTER DETECTION outcomes "
                f"are likely missing or incomplete. See runner logs.",
                when,
            ),
            priority=outbox.PRIORITY_HIGH,
        )
    except Exception:
        logger.error("also failed to send the backfill-marks alert itself", exc_info=True)


def _cache_todays_intraday_bars(
    cache_dir: Path, symbols: list[str], session_date: date, fetch_fn=None,
) -> tuple[list[str], list[str]]:
    """The actual structural fix behind the 2026-08-12 incident: nothing
    in the live pipeline has ever written today's own intraday bars to
    the cache directory (LiveMarketData fetches straight into memory,
    never to disk; scripts/fetch_cache.py's walk-back starts at
    date.today() - 1, so it can never target today either) -- meaning
    backfill_marks() reading from cache has been structurally unable to
    succeed on a live close, on any day, since the live path was built.
    Called once, at close, right before backfill_marks(): fetches every
    symbol that had a detection this session (watchlist AND screening,
    via journal.detected_symbols_for_session -- the old manual patch only
    covered watchlist) directly from the vendor and writes it to disk in
    the same shape backfill_marks() already reads.

    fetch_fn defaults to vendors.alpaca.fetch_intraday_bars (deferred
    import, same "tests inject a fake, no vendor SDK required" pattern
    as run_broad_scan's fetch_bars_fn above).

    Feed consistency: the default fetch_fn uses DETECTOR_DATA_FEED, the
    same module-level constant resolved once at process start that every
    other bar this session came from used -- called once per symbol in
    one loop within one process invocation, never re-resolved per
    symbol, so one date's file can never mix feeds within a single call.
    That guarantee holds for the normal same-day/same-process case; a
    deliberate, delayed, cross-process re-run for a past session (a
    manual repair) must set DETECTOR_DATA_FEED to match whatever feed
    that past session actually ran under -- same discipline
    scripts/fetch_cache.py already requires for any historical-date
    operation, not a new gap this introduces.

    Returns (succeeded, failed) symbol lists -- never raises; a single
    symbol's vendor error must not block caching the rest."""
    feed_label = "injected fetch_fn"
    if fetch_fn is None:
        from tradebot.vendors.alpaca import DETECTOR_DATA_FEED, fetch_intraday_bars  # deferred: avoid importing the vendor SDK for every runner.py import

        fetch_fn = fetch_intraday_bars
        feed_label = DETECTOR_DATA_FEED

    logger.info("close-time intraday cache fetch: %d symbol(s), feed=%s, session=%s", len(symbols), feed_label, session_date.isoformat())
    succeeded: list[str] = []
    failed: list[str] = []
    for symbol in symbols:
        try:
            bars = fetch_fn(symbol, session_date)
        except Exception:
            logger.error("close-time cache fetch failed for %s (session=%s)", symbol, session_date.isoformat(), exc_info=True)
            failed.append(symbol)
            continue
        if not bars:
            # Unlike scripts/fetch_cache.py's historical walk-back (which
            # must tell a real holiday from a real failure), every symbol
            # here is known to have fired a detection today -- there is
            # no legitimate "holiday" explanation for empty bars, so this
            # is always loud.
            logger.error("fetch_intraday_bars returned no bars for %s on %s despite a detection firing today", symbol, session_date.isoformat())
            failed.append(symbol)
            continue

        # Proposal 5c's plausibility floor, close-time-write half: the
        # 2026-08-11/12 runts (~1M vs ~40M normal SPY volume) reached the
        # cache this exact way, with nothing to catch them. A rejection
        # here means the fetch itself is untrustworthy -- treated the same
        # as a fetch failure (no file written, symbol counted `failed`),
        # never silently cached as if it were a real session.
        reference_sessions = [
            d for d in cached_session_dates(cache_dir, [symbol]) if d < session_date
        ][-PLAUSIBILITY_WINDOW_SESSIONS:]
        median_volume = median_session_volume(
            [sum(b.volume for b in full_session_rth_bars(symbol, d, cache_dir)) for d in reference_sessions]
        )
        reason = implausible_session_reason(
            [b for b in bars if _is_rth(b)],
            median_volume=median_volume,
            expected_bar_count=expected_rth_bar_count(session_date),
        )
        if reason is not None:
            logger.error(
                "plausibility floor rejected close-time cache write for %s on %s: %s",
                symbol, session_date.isoformat(), reason,
            )
            metrics.increment("plausibility_floor_rejection", stage="close_write", symbol=symbol, rule=reason.split(":")[0])
            failed.append(symbol)
            continue

        write_bars_csv(cache_dir / symbol / f"intraday_{session_date.isoformat()}.csv", bars)
        succeeded.append(symbol)
    return succeeded, failed


def _alert_if_cache_fetch_failed(
    alerter, succeeded: list[str], failed: list[str], session_date: date, when: datetime,
) -> None:
    """Separate from _alert_if_backfill_implausible on purpose -- an
    operator seeing only "backfill wrote too few marks" doesn't know
    which stage broke. This fires from the FETCH stage specifically
    (vendor/auth trouble), independent of and before backfill_marks()
    ever runs, so the two alerts together say WHERE it broke, not just
    THAT it broke.

    Two severities, not one: ANY failed symbol gets an ERROR log line
    (still worth knowing, still visible in docker compose logs), but the
    loud Telegram page is reserved for TOTAL failure -- succeeded is
    empty, i.e. 0 of N -- the "systemic vendor/auth outage" shape this
    was actually designed for. A single symbol's transient vendor hiccup
    must never page: backfill_marks()'s own missing-cache-file log for
    that one symbol, plus _alert_if_backfill_implausible if it turns out
    to matter for the day's overall count, remain the safety net for a
    partial miss without escalating every isolated failure to a page."""
    if not failed:
        return
    total = len(succeeded) + len(failed)
    logger.error(
        "close-time intraday cache fetch failed for %d/%d symbol(s) on %s: %s",
        len(failed), total, session_date.isoformat(), ", ".join(failed),
    )
    if succeeded:
        return  # partial failure -- logged above, not paged
    try:
        alerter.send(
            templates.render_failure_notice(
                f"Close-time cache fetch failed for ALL {total} symbol(s) on "
                f"{session_date.isoformat()} ({', '.join(failed)}) -- likely a vendor/auth problem, "
                f"not a backfill problem. AFTER DETECTION outcomes cannot be computed until this "
                f"is fixed. See runner logs.",
                when,
            ),
            priority=outbox.PRIORITY_HIGH,
        )
    except Exception:
        logger.error("also failed to send the cache-fetch-failure alert itself", exc_info=True)


def _last_recap_week_end(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(json.loads(path.read_text())["week_end"])
    except (json.JSONDecodeError, KeyError, OSError, ValueError):
        return None


def _mark_recap_sent(path: Path, week_end: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"week_end": week_end.isoformat()}))


def maybe_send_weekly_recap(conn, alerter, now: datetime, state_path: Path | None = None) -> None:
    """Fires once per calendar week, at the first live session on or
    after that week's Monday — covers [last week sent, this Monday's
    midnight UTC). A cursor (not a "is today Monday" check) so a holiday
    or a day the bot wasn't running never silently drops a week: it just
    catches up on the next run. See tradebot.rendering.templates.
    render_weekly_recap — the SAME template runs whether the week was
    good or bad."""
    state_path = state_path or WEEKLY_RECAP_STATE_FILE
    session_date = now.astimezone(ET).date()
    this_monday = session_date - timedelta(days=session_date.weekday())
    week_end = datetime.combine(this_monday, datetime.min.time(), tzinfo=timezone.utc)

    last_sent = _last_recap_week_end(state_path)
    if last_sent is not None and last_sent >= week_end:
        return  # already covered up through this week

    week_start = last_sent if last_sent is not None else week_end - timedelta(days=7)
    if week_start >= week_end:
        return  # nothing new to cover yet

    recap = weekly_recap(conn, week_start.isoformat(), week_end.isoformat())
    alerter.send(templates.render_weekly_recap(recap, now), priority=outbox.PRIORITY_NORMAL)
    _mark_recap_sent(state_path, week_end)


PINNED_STATUS_STATE_FILE = REPO_ROOT / "data" / "pinned_status_state.json"


def _telegram_api_call(token: str, method: str, payload: dict) -> dict:
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()["result"]


def _load_pinned_status_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_pinned_status_state(path: Path, chat_id, message_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chat_id": chat_id, "message_id": message_id}))


def maybe_update_pinned_status(token: str, chat_id, conn, now: datetime, state_path: Path | None = None) -> None:
    """Keeps ONE pinned message in the ops channel showing the live
    sample size and today's significance verdict (see
    performance.significance_check) — edited in place on every call
    rather than re-sent, so it never spams a fresh pin. Deliberately a
    direct Telegram API call, not the outbox: like TelegramHaltChecker
    above, this is a control-plane operation on one fixed chat, not a
    per-subscriber alert delivery."""
    state_path = state_path or PINNED_STATUS_STATE_FILE
    tr = track_record(conn, tier="high")
    text = templates.render_pinned_status(tr, now)

    state = _load_pinned_status_state(state_path)
    if state is not None:
        try:
            _telegram_api_call(
                token, "editMessageText",
                {"chat_id": state["chat_id"], "message_id": state["message_id"], "text": text, "parse_mode": "HTML"},
            )
            return
        except requests.RequestException:
            pass  # pinned message may have been deleted/unpinned by hand -> fall through and recreate

    sent = _telegram_api_call(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    _telegram_api_call(
        token, "pinChatMessage", {"chat_id": chat_id, "message_id": sent["message_id"], "disable_notification": True}
    )
    _save_pinned_status_state(state_path, chat_id, sent["message_id"])


# --------------------------------------------------------------------------
# Contract forward-mid backfill — live only (see journal.py's schema
# comment: no cached historical options data to replay against). Fills in
# what select_contract() couldn't know at entry time: what the SAME
# contract is actually worth 15/30/60m later, and at the session close.
# Automatic and unconditional, exactly like backfill_marks() for the
# underlying — never gated on whether the contract gained or lost value.
# --------------------------------------------------------------------------


def _contract_mid(chain, right: str, strike: float) -> float | None:
    for c in chain.contracts:
        if c.right == right and c.strike == strike:
            return (c.bid + c.ask) / 2
    return None  # contract not found in today's chain — never fabricated


def _contract_occ_symbol(chain, right: str, strike: float) -> str | None:
    for c in chain.contracts:
        if c.right == right and c.strike == strike:
            return c.symbol
    return None


def _forward_mid(md, symbol: str, right: str, strike: float, expiry: str, is_vertical: bool, short_strike: float | None) -> float | None:
    """The long leg's mid, minus the short leg's mid for a vertical — the
    same formula runner.py used to compute entry_mid, so a forward mid is
    comparable to it. None (not fetched) if either leg's contract can't
    be found in the current chain, rather than reporting a partial mid."""
    try:
        chain = md[symbol].chain(symbol, expiry=date.fromisoformat(expiry))
    except Exception:
        return None
    long_mid = _contract_mid(chain, right, strike)
    if long_mid is None:
        return None
    if not is_vertical:
        return long_mid
    short_mid = _contract_mid(chain, right, short_strike)
    if short_mid is None:
        return None
    return long_mid - short_mid


def backfill_pending_contract_mids(conn, md, now: datetime) -> None:
    """Called once per live-loop iteration: for each of the 15/30/60m
    checkpoints, fetch and record the forward mid for every selection
    that's now old enough but doesn't have it yet. A single vendor
    hiccup on one contract must never stop the others from backfilling —
    see the per-selection try/except."""
    for offset_min in OUTCOME_OFFSETS_MIN:
        for detection_id, symbol, right, strike, expiry, is_vertical, short_strike in pending_contract_backfills(conn, now, offset_min):
            try:
                mid = _forward_mid(md, symbol, right, strike, expiry, bool(is_vertical), short_strike)
                if mid is not None:
                    record_contract_forward_mid(conn, detection_id, offset_min, mid)
            except Exception:
                logger.error("contract forward-mid backfill failed: detection_id=%s offset_min=%s", detection_id, offset_min, exc_info=True)


def backfill_pending_contract_close_mids(conn, md, session_date: date) -> None:
    """Called once at the end of a live session: the close-mid equivalent
    of backfill_pending_contract_mids, using the same best-effort, never-
    fabricated fetch. Options typically stop trading close to the
    session's own close, so this is 'as close to the real close as
    practically fetchable', the same honest limitation session close
    prices for the underlying don't have."""
    for detection_id, symbol, right, strike, expiry, is_vertical, short_strike in pending_contract_close_backfills(conn, session_date):
        try:
            mid = _forward_mid(md, symbol, right, strike, expiry, bool(is_vertical), short_strike)
            if mid is not None:
                record_contract_forward_mid(conn, detection_id, CLOSE_MARK_OFFSET_MIN, mid)
        except Exception:
            logger.error("contract close-mid backfill failed: detection_id=%s", detection_id, exc_info=True)


def backfill_contract_day_ranges(conn, md, session_date: date) -> None:
    """Called once at the end of a live session, alongside
    backfill_pending_contract_close_mids: records each contract's own
    real intraday trade low/high for the day (see
    journal.record_contract_day_range) — the "what was the most anyone
    could have made on this contract today" context shown alongside our
    actual entry's outcome. Vertical spreads are skipped: a spread's day
    range isn't the sum of its two legs' independent ranges (they don't
    hit their extremes at the same moment), so reporting one would imply
    a number nobody could have actually captured."""
    from tradebot.vendors.alpaca import fetch_option_day_range

    for detection_id, symbol, right, strike, expiry in pending_contract_day_range_backfills(conn, session_date):
        try:
            chain = md[symbol].chain(symbol, expiry=date.fromisoformat(expiry))
            occ_symbol = _contract_occ_symbol(chain, right, strike)
            if occ_symbol is None:
                continue
            day_range = fetch_option_day_range(occ_symbol, session_date)
            if day_range is not None:
                record_contract_day_range(conn, detection_id, day_range[0], day_range[1])
        except Exception:
            logger.error("contract day-range backfill failed: detection_id=%s", detection_id, exc_info=True)


# --------------------------------------------------------------------------
# Replay mode — fast-forwards through a cached session bar by bar.
# --------------------------------------------------------------------------


def run_replay(
    session_date: date, alerter, db_path=None, cache_dir: Path = None,
    allow_production_db: bool = False, metrics_path=None,
) -> HeartbeatStats:
    """Replay one cached session. This is the boundary: it decides where
    this run's side effects land, then does the work.

    metrics_path: which counter file this replay writes. None (the
    default) means metrics.REPLAY_METRICS_PATH -- data/metrics_replay.json
    -- NOT the live data/metrics.json. process_new_bar increments thirteen
    counters a replay can reach (validator_rejection, suppression,
    dedup_check_failed, ...), and those exist to answer "how often is this
    happening in production right now"; a replay of an old session adding
    to them makes that unanswerable, invisibly, because a counter is just a
    number with no record of who added to it.

    Same boundary philosophy as db_path above: the redirect is established
    ONCE here, where the mode is known, rather than teaching thirteen
    increment() call sites -- and the fourteenth someone adds later -- to
    recognise a replay. See metrics.redirect_to.

    Every other argument is passed straight through; see
    _run_replay_session for what they do."""
    destination = Path(metrics_path) if metrics_path is not None else metrics.REPLAY_METRICS_PATH
    logger.info("replay metrics -> %s", destination)
    with metrics.redirect_to(destination):
        return _run_replay_session(
            session_date, alerter, db_path=db_path, cache_dir=cache_dir,
            allow_production_db=allow_production_db,
        )


def _run_replay_session(
    session_date: date, alerter, db_path=None, cache_dir: Path = None,
    allow_production_db: bool = False,
) -> HeartbeatStats:
    """The replay itself. Split from run_replay so the boundary above can
    wrap the whole run in one metrics.redirect_to() without indenting this
    body under it -- and so "where do this run's side effects go" is
    answered in one short readable place instead of being buried at the top
    of a long function.

    db_path: which journal to write. None (the default) means
    journal.REPLAY_DB_PATH — data/journal_replay.db — NOT the production
    journal. A replay reproduces live detection ids exactly (cluster_id
    hashes symbol/session/ts/kinds), so a replay aimed at the live
    journal upserts onto the live rows and then keeps mutating them
    through the writes that follow write_cluster: lifecycle_state,
    suppress_reason/category, news_driven, alerted, and set_no_trade —
    which in replay ALWAYS writes no_trade=1, since ReplayMarketData has
    no options chain to select from. Rather than teach each of those
    writes to recognise a replay, the boundary is the connection itself;
    see journal.resolve_replay_db_path.

    An explicit path is honoured unchanged, which is what
    scripts/compare_replay.py uses to run two versions of the detection
    logic against the same session into two separate DB files (see that
    script's module docstring for why this is a separate-file design
    rather than an in-place A/B split), and what the SIP Phase 1 backtest
    used for its IEX-vs-SIP pair.

    allow_production_db: the deliberate escape hatch, and the only way to
    reach DEFAULT_DB_PATH. Off by default here and not merely in the CLI,
    because run_replay is called programmatically too and a guard that
    lived only in argparse would leave every other caller unprotected.
    Passing the production path without it raises
    journal.ProductionJournalRefused.

    cache_dir: override which cache tree (see scripts/fetch_cache.py) is
    replayed against (default CACHE_DIR, i.e. data/cache/). Used the
    same way as db_path, but for comparing two DATA sources (e.g. IEX
    vs. SIP -- see docs/sip-migration-proposal.md's Phase 1) instead of
    two code versions. Only affects this replay call -- run_live never
    passes this, so live mode is untouched."""
    from tradebot.vendors.alpaca import DETECTOR_DATA_FEED  # deferred: avoid importing the vendor SDK for every runner.py import

    cache_dir = cache_dir if cache_dir is not None else CACHE_DIR
    open_ts, _close_ts = session_bounds(session_date)

    # Enforced here, not only in main(): run_replay is called
    # programmatically (tests, scripts, a REPL), and a guard that lived
    # only in argparse would protect the command line while leaving every
    # other caller pointed at the production journal. db_path=None
    # resolves to journal.REPLAY_DB_PATH, never DEFAULT_DB_PATH.
    conn = connect(resolve_replay_db_path(db_path, allow_production_db=allow_production_db))
    version = code_version()
    # One id for this replay, generated per call: replaying a session
    # more than once is normal, the decision ledger is append-only, and
    # detection ids repeat across runs (cluster_id hashes
    # symbol/session/ts/kinds). This is what keeps the second replay's
    # events from reading as a later revision of the first's — or of the
    # live session's.
    run_id = new_run_id()
    clock = {"t": open_ts}
    budget = AlertBudget(now=lambda: clock["t"])
    stats = HeartbeatStats(start_time=open_ts, session_date=session_date)

    try:
        alerter.send(templates.render_morning_briefing(tier_performance(conn).get("high"), clock["t"]), priority=outbox.PRIORITY_NORMAL)
        alerter.send(
            templates.render_pre_open_card(events_for_date(conn, session_date), session_date, clock["t"]),
            priority=outbox.PRIORITY_NORMAL,
        )
    except Exception:
        stats.errors.append(traceback.format_exc())

    history_by_symbol = _build_history_by_symbol(cache_dir, WATCHLIST, session_date, stats)

    md = {symbol: ReplayMarketData(cache_dir, symbol, session_date) for symbol in WATCHLIST}
    anchors: dict[str, DailyAnchors] = {}
    rth_bar_count = {symbol: 0 for symbol in WATCHLIST}

    def quote_fn(symbol: str) -> Quote:
        last = list(md[symbol].session_bars(symbol, session_date))[-1]
        # ReplayMarketData has no real quotes — synthesize a tight one
        # around the last close purely so the alert has something to show.
        return Quote(symbol=symbol, ts=last.ts, bid=last.close - 0.02, ask=last.close + 0.02, last=last.close)

    def chain_fn(symbol: str, expiry: date):
        return md[symbol].chain(symbol, expiry=expiry)  # always raises — no chain in replay

    halted = False
    while not halted:
        any_advanced = False
        for symbol in WATCHLIST:
            try:
                if not md[symbol].advance():
                    continue
                any_advanced = True
                rth_bars = list(md[symbol].session_bars(symbol, session_date))
                if not rth_bars or len(rth_bars) == rth_bar_count[symbol]:
                    continue
                rth_bar_count[symbol] = len(rth_bars)
                clock["t"] = bar_close_ts(rth_bars[-1])

                if symbol not in anchors:
                    daily = md[symbol].daily_bars(symbol, 20)
                    if not daily:
                        stats.data_gaps.append(f"{symbol}: no prior daily bar cached for {session_date}")
                        continue
                    anchors[symbol] = build_anchors(
                        symbol=symbol,
                        session_date=session_date,
                        prior_daily_bars=daily,
                        opening_range_bars=rth_bars[:1],
                        historical_session_bars=history_by_symbol[symbol],
                    )

                market_bars = {
                    proxy: list(md[proxy].session_bars(proxy, session_date))
                    for proxy in MARKET_PROXY_SYMBOLS if proxy in md
                }
                process_new_bar(
                    conn, budget, alerter, version, symbol, session_date, rth_bars,
                    anchors[symbol], quote_fn, chain_fn, stats, market_bars=market_bars,
                    data_feed=DETECTOR_DATA_FEED,
                    run_mode=RUN_MODE_REPLAY, run_id=run_id,
                )
                send_medium_digest_if_due(budget, alerter, conn, clock["t"])
            except Exception:
                stats.errors.append(traceback.format_exc())
                try:
                    alerter.send(
                        templates.render_system_notice(
                            f"{symbol}: exception during evaluation. See logs. Continuing.", clock["t"]
                        ),
                        priority=outbox.PRIORITY_HIGH,
                    )
                except Exception:
                    pass
                continue

        if HALT_FILE.exists():
            alerter.send(templates.render_system_notice("HALT file present. Stopping replay.", clock["t"]), priority=outbox.PRIORITY_HIGH)
            halted = True
        if not any_advanced:
            break

    send_log_summary(budget, alerter, conn, clock["t"])
    marks_written = backfill_marks(conn, session_date)
    print(f"backfilled {marks_written} forward-price marks")
    heartbeat = templates.render_heartbeat(
        session_date, clock["t"] - open_ts, stats.tier_counts, stats.suppression_counts,
        stats.data_gaps, stats.errors, tier_performance(conn), clock["t"],
    )
    alerter.send(heartbeat, priority=outbox.PRIORITY_LOG)
    conn.close()
    return stats


# --------------------------------------------------------------------------
# Live mode — real 5-minute loop. See module docstring: unverified against
# real market conditions in this build.
# --------------------------------------------------------------------------


# Stage 1 broad-scan cadence (see tradebot.broad_scan / tradebot.universe)
# — deliberately much coarser than the 5-minute Stage 2 bar cadence:
# daily-bar-level volume/range doesn't meaningfully change every 5
# minutes, and a bulk fetch across the ~13,000-symbol active universe
# (live-verified 2026-08-08: ~9 chunked requests, ~20s total — see
# vendors.alpaca.fetch_daily_bars_bulk) is real API load that has no
# business repeating on the same cadence as the fixed 17-symbol loop.
BROAD_SCAN_INTERVAL_MINUTES = 30
# Caps how many EXTRA symbols beyond WATCHLIST get pulled into live
# Stage 2 evaluation per scan — the explicit "higher coverage, NOT higher
# alert volume" bound: a bigger universe can surface more candidates, but
# only this many of the strongest ones ever reach the real detector suite
# and the daily HIGH cap/cooldowns in alerts.AlertBudget still apply on
# top of that.
BROAD_SCAN_PROMOTION_LIMIT = 25
BROAD_SCAN_LOOKBACK_DAYS = 30


def _broad_scan_due(
    loop_start: datetime,
    session_open: datetime,
    last_broad_scan: datetime | None,
) -> bool:
    """True only after the first RTH bar closes and then on cadence.

    Stage 1 consumes a still-forming daily aggregate. Before RTH that
    aggregate can still be the prior session, so a premarket promotion
    can survive into the opening Stage-2 pass. Waiting for the first
    closed bar removes that carry path; run_broad_scan independently
    enforces the daily bar's session date before promotion.
    """
    first_safe_scan = session_open + timedelta(minutes=BAR_MINUTES)
    if loop_start < first_safe_scan:
        return False
    return (
        last_broad_scan is None
        or (loop_start - last_broad_scan).total_seconds() >= BROAD_SCAN_INTERVAL_MINUTES * 60
    )


def _log_broad_scan_shadow_counts(
    symbols: list[str],
    bars_by_symbol: dict,
    snapshots: list,
    promoted: list,
    selected: list,
    promotion_limit: int,
    *,
    session_date: date | None = None,
) -> None:
    """Decision Ledger measurement gate — SHADOW COUNTS ONLY. Read-only
    instrumentation: computes and logs the Stage-1 funnel's
    conservation-invariant counts from data run_broad_scan() already
    holds. Never persists anything, never calls a detector or vendor a
    second time, and cannot affect the returned symbol list — every
    input here (`symbols`, `bars_by_symbol`, `snapshots`, `promoted`,
    `selected`) is read, never mutated, and the whole body is wrapped in
    its own try/except so a bug in this function is exactly as harmless
    to run_broad_scan()'s real behavior as a failed metrics.increment
    call would be.

    The requested-universe conservation invariant is scoped STRICTLY to
    `symbols` (what active_symbols() actually asked for) at every stage
    — never to the raw fetch response, which the vendor could in
    principle pad with a symbol nobody requested. Nothing in
    build_snapshots_from_daily_bars/screen_snapshot/promote_candidates
    filters by "was this symbol requested", so the current production
    pipeline WOULD process and could even select such a symbol if the
    vendor ever sent one — that's real behavior, not a data-integrity
    bug this function's job is to hide. So every stage is reported
    twice: once scoped to `requested_symbols` (feeds the invariant) and
    once as the true, unfiltered production total (`candidate`,
    `selected_top_n`) — plus a parallel `unexpected_*` count at every
    stage an unrequested symbol could reach, so vendor extras are
    visible, never silently folded in or silently dropped.

    Each count maps to one specific, already-existing code path — no
    invented categories:
      - missing_from_fetch: requested symbol absent from the fetch
        response (vendors.alpaca.fetch_daily_bars_bulk never pads a
        missing symbol with a fabricated entry — see its own docstring)
      - unexpected_from_fetch / unexpected_snapshot / unexpected_candidate
        / unexpected_selected: a symbol that was NEVER requested but
        appeared at that stage anyway — expected to always be 0 under
        Alpaca's API contract, cheap to verify rather than assume, and
        reported at every stage it could reach rather than only at the
        first one
      - insufficient_history: a REQUESTED symbol present in the fetch
        response but dropped by broad_scan.build_snapshots_from_daily_bars's
        own min_history skip
      - stale_session_bar: enough history was returned, but the newest
        daily bar is not from the requested live session, so the symbol
        is barred from Stage 2 rather than screening prior-session data
      - invalid_baseline: a REQUESTED symbol's Snapshot that would fail
        screen_snapshot's own guard (broad_scan.py:69-70) — the one
        deliberate, small duplication of that single boolean condition
        against Snapshot's own public fields, not a re-derivation of
        any ratio formula
      - evaluated_quiet: the REQUESTED remainder, derived from the
        conservation invariant itself, never independently computed
      - candidate / selected_top_n: the TRUE production totals, read
        directly off `promoted` / `selected` — never a separately
        recomputed count, so these can never diverge from what was
        actually selected
    """
    try:
        requested_symbols = set(symbols)
        returned_symbols = set(bars_by_symbol.keys())
        requested_fetched_symbols = requested_symbols & returned_symbols
        fetched_count = len(requested_fetched_symbols)
        missing_from_fetch_count = len(requested_symbols - returned_symbols)
        unexpected_from_fetch_count = len(returned_symbols - requested_symbols)

        snapshot_symbols = {s.symbol for s in snapshots}
        requested_snapshot_symbols = snapshot_symbols & requested_symbols
        excluded_snapshot_symbols = requested_fetched_symbols - requested_snapshot_symbols
        if session_date is None:
            stale_session_symbols: set[str] = set()
        else:
            from tradebot import broad_scan

            stale_session_symbols = {
                symbol for symbol in excluded_snapshot_symbols
                if len(bars_by_symbol[symbol]) >= broad_scan.MIN_HISTORY_BARS
                and max(bars_by_symbol[symbol], key=lambda b: b.ts).ts.astimezone(ET).date() != session_date
            }
        stale_session_count = len(stale_session_symbols)
        insufficient_history_count = len(excluded_snapshot_symbols - stale_session_symbols)
        requested_snapshot_count = len(requested_snapshot_symbols)
        unexpected_snapshot_count = len(snapshot_symbols - requested_symbols)

        requested_snapshots = [s for s in snapshots if s.symbol in requested_symbols]
        invalid_baseline_count = sum(1 for s in requested_snapshots if s.avg_volume <= 0 or s.prior_close <= 0)

        requested_candidate_count = sum(1 for c in promoted if c.symbol in requested_symbols)
        unexpected_candidate_count = len(promoted) - requested_candidate_count
        evaluated_quiet_count = requested_snapshot_count - invalid_baseline_count - requested_candidate_count

        # True production totals — unfiltered, exactly what run_broad_scan()
        # actually processed/returned. promote_candidates' own default
        # threshold (1.0) is already screen_snapshot's own construction
        # floor (broad_scan.py:65-96), so candidate_count ==
        # eligible_for_top_n_count is provably true under current code —
        # logging both so a future threshold change would show up as a
        # divergence instead of silently disappearing, the same reasoning
        # the universe_candidates_promoted naming issue exists to avoid
        # repeating.
        candidate_count = len(promoted)
        eligible_for_top_n_count = candidate_count
        selected_top_n_count = len(selected)
        unexpected_selected_count = sum(1 for c in selected if c.symbol not in requested_symbols)

        requested_universe_count = len(requested_symbols)
        requested_check = (
            missing_from_fetch_count + insufficient_history_count
            + stale_session_count + requested_snapshot_count
        )
        snapshot_check = invalid_baseline_count + evaluated_quiet_count + requested_candidate_count
        invariant_ok = (requested_check == requested_universe_count) and (snapshot_check == requested_snapshot_count)

        logger.info(
            "broad_scan_shadow_counts requested=%d fetched=%d missing_from_fetch=%d "
            "insufficient_history=%d stale_session_bar=%d requested_snapshot=%d invalid_baseline=%d "
            "evaluated_quiet=%d requested_candidate=%d candidate=%d "
            "eligible_for_top_n=%d selected_top_n=%d promotion_limit=%d "
            "unexpected_from_fetch=%d unexpected_snapshot=%d unexpected_candidate=%d "
            "unexpected_selected=%d invariant_ok=%s",
            requested_universe_count, fetched_count, missing_from_fetch_count,
            insufficient_history_count, stale_session_count, requested_snapshot_count, invalid_baseline_count,
            evaluated_quiet_count, requested_candidate_count, candidate_count,
            eligible_for_top_n_count, selected_top_n_count, promotion_limit,
            unexpected_from_fetch_count, unexpected_snapshot_count, unexpected_candidate_count,
            unexpected_selected_count, invariant_ok,
        )
    except Exception:
        logger.error("broad_scan shadow-count instrumentation failed (non-fatal): %s", traceback.format_exc())


def _screening_audit_enabled() -> bool:
    """Verbose Stage 1 audit — per-symbol QUIET rows as well as the
    interesting ones. Off unless WATCHTOWER_SCREEN_AUDIT is set.

    An env var rather than a CLI flag on purpose: this is an
    investigation setting, and threading a flag through run_live() would
    mean editing the live loop for something that is not a live
    behavior. A --screen-audit flag is a cheap follow-up if it needs to
    be discoverable; it is deliberately not in the change that first
    creates the table.

    Volume is why it is opt-in: roughly 185k rows and 25-30 MB per
    session at full universe size, against a few hundred rows with it
    off. Meant for a bounded window while chasing a specific miss."""
    return os.environ.get("WATCHTOWER_SCREEN_AUDIT", "").strip().lower() in {"1", "true", "yes", "on"}


def _persist_broad_scan_screening(
    universe_conn, symbols, bars_by_symbol, snapshots, promoted, selected,
    promotion_limit, *, session_date, tick_utc, run_id, run_mode, latency_ms,
) -> None:
    """Persist this Stage 1 pass — the sibling of
    _log_broad_scan_shadow_counts, which stays log-only and untouched.

    Same inputs, same read-only discipline, same swallow-everything
    guard: every value is read off objects run_broad_scan already holds,
    nothing is recomputed, no vendor is called again, and a failure here
    is exactly as harmless to the returned selection as a failed
    metrics.increment. The classification is done by the PURE
    broad_scan.classify_screen_outcomes; this only hands the result to
    universe.record_screening_tick.

    Writes to universe.db on its own connection, so it cannot interact
    with journal.db's transaction boundary -- process_new_bar's
    commit-then-send ordering is untouchable from here by construction.

    Deliberately duplicates the bucket derivation that
    _log_broad_scan_shadow_counts already does, rather than rewriting
    that proven-inert function to share one. The duplication is guarded
    by a test asserting the two agree, so a future edit that made them
    disagree fails loudly instead of drifting."""
    try:
        from tradebot import broad_scan
        from tradebot import universe as universe_mod

        tick, events = broad_scan.classify_screen_outcomes(
            symbols, bars_by_symbol, snapshots, promoted, selected, promotion_limit,
            verbose_audit=_screening_audit_enabled(),
            session_date=session_date,
        )
        universe_mod.record_screening_tick(
            universe_conn, tick, events,
            session=session_date.isoformat(), tick_utc=tick_utc.isoformat(),
            run_id=run_id, run_mode=run_mode,
            screen_version=broad_scan.SCREEN_VERSION, code_version=code_version(),
            audit_mode=_screening_audit_enabled(), latency_ms=latency_ms,
        )
    except Exception:
        logger.error("screening_events persistence failed (non-fatal): %s", traceback.format_exc())
        metrics.increment("screening_persist_failed")


def run_broad_scan(
    universe_conn, fetch_bars_fn=None, promotion_limit: int = BROAD_SCAN_PROMOTION_LIMIT,
    *, session_date=None, tick_utc=None, run_id=None, run_mode=RUN_MODE_UNKNOWN,
) -> list[str]:
    """One Stage 1 pass: the active universe (tradebot.universe) -> one
    bulk daily-bars fetch -> the cheap screen (tradebot.broad_scan) ->
    the strongest `promotion_limit` symbols. fetch_bars_fn defaults to
    vendors.alpaca.fetch_daily_bars_bulk (deferred import — this module
    stays usable without the Alpaca SDK installed for anything that
    doesn't call this); tests inject a fake.

    Also logs Decision Ledger shadow counts (see
    _log_broad_scan_shadow_counts) — read-only measurement instrumentation
    ahead of a possible future persistence layer, never affecting the
    selection this function returns."""
    from tradebot import broad_scan
    from tradebot import universe as universe_mod

    if fetch_bars_fn is None:
        from tradebot.vendors.alpaca import fetch_daily_bars_bulk

        fetch_bars_fn = fetch_daily_bars_bulk

    scan_started = time.monotonic()
    symbols = universe_mod.active_symbols(universe_conn)
    bars_by_symbol = fetch_bars_fn(symbols, BROAD_SCAN_LOOKBACK_DAYS)
    snapshots = broad_scan.build_snapshots_from_daily_bars(
        bars_by_symbol, session_date=session_date,
    )
    promoted = broad_scan.run_stage1_screen(snapshots)
    selected = promoted[:promotion_limit]
    latency_ms = int((time.monotonic() - scan_started) * 1000)

    # Stage 1 observability. Both calls are instrumentation only, both
    # swallow their own failures, and both run AFTER `selected` is final
    # -- the returned list below is computed from it and cannot be
    # affected by either.
    now = datetime.now(timezone.utc)
    _persist_broad_scan_screening(
        universe_conn, symbols, bars_by_symbol, snapshots, promoted, selected, promotion_limit,
        session_date=session_date if session_date is not None else now.astimezone(ET).date(),
        tick_utc=tick_utc if tick_utc is not None else now,
        run_id=run_id if run_id is not None else UNATTRIBUTED_RUN_ID,
        run_mode=run_mode, latency_ms=latency_ms,
    )

    try:
        _log_broad_scan_shadow_counts(
            symbols, bars_by_symbol, snapshots, promoted, selected, promotion_limit,
            session_date=session_date,
        )
    except Exception:
        # Belt-and-suspenders: _log_broad_scan_shadow_counts already
        # guards its own body, but this outer boundary means a future
        # edit that weakens or removes that internal guard still can't
        # take the real selection down with it.
        logger.error("broad_scan shadow-count instrumentation failed (non-fatal, outer boundary): %s", traceback.format_exc())

    return [c.symbol for c in selected]


SESSION_OPEN_STATE_FILE = REPO_ROOT / "data" / "session_open_state.json"


def _load_session_open_state(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["session_date"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _mark_session_open_sent(path: Path, session_date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_date": session_date.isoformat()}))


SESSION_CLOSE_STATE_FILE = REPO_ROOT / "data" / "session_close_state.json"


def _load_session_close_state(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["session_date"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _mark_session_close_sent(path: Path, session_date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_date": session_date.isoformat()}))


def maybe_send_session_open_messages(conn, alerter, session_date, now: datetime, state_path: Path | None = None) -> None:
    """The morning briefing + pre-open card are once-per-session
    announcements, guarded the same way maybe_send_weekly_recap/
    maybe_update_pinned_status guard theirs — run_live() itself has no
    "have I already opened today" memory of its own, and under
    supervised restart (Docker `restart: unless-stopped`, or a human
    re-running scripts/start.sh mid-morning after a crash) it WILL be
    called more than once for the same session_date. Without this guard
    that means duplicate briefing/pre-open sends on every restart —
    including, worst case, a restart loop after a clean end-of-session
    exit repeatedly resending both until the next session."""
    state_path = state_path or SESSION_OPEN_STATE_FILE
    if _load_session_open_state(state_path) == session_date.isoformat():
        return
    alerter.send(templates.render_morning_briefing(tier_performance(conn).get("high"), now), priority=outbox.PRIORITY_NORMAL)
    alerter.send(
        templates.render_pre_open_card(events_for_date(conn, session_date), session_date, now),
        priority=outbox.PRIORITY_NORMAL,
    )
    _mark_session_open_sent(state_path, session_date)


def run_live(
    alerter, subscriber_hook=None, medium_fanout_fn=None, enable_broad_scan: bool = False, db_path=None,
) -> HeartbeatStats:
    from tradebot.vendors.alpaca import DETECTOR_DATA_FEED  # deferred: avoid importing the vendor SDK for every runner.py import

    now = datetime.now(timezone.utc)
    session_date = now.astimezone(ET).date()
    if not CALENDAR.is_session(session_date):
        # Weekend/holiday: nothing to scan. session_bounds() would raise
        # ValueError for a date with no session — that's correct for
        # callers who only ever pass a real trading day (run_replay), but
        # run_live() itself gets started unconditionally by `docker
        # compose`'s restart policy, every day, including non-trading
        # ones. Idle and return cleanly instead of crashing.
        logger.info("not a trading session (%s) — idling %ds", session_date, OFF_SESSION_IDLE_SECONDS)
        time.sleep(OFF_SESSION_IDLE_SECONDS)
        return HeartbeatStats(start_time=now, session_date=session_date)
    open_ts, close_ts = session_bounds(session_date)

    # Today is a trading day, but if we're already past its close AND
    # we've already sent this session's close report, this call is a
    # restart landing after today's market hours (Docker's `restart:
    # unless-stopped` fires on ANY exit, including the clean one at the
    # bottom of this function) — not a fresh session. Without this check
    # every restart falls straight through to the while loop below, sees
    # loop_start >= close_ts on its very first iteration, and re-runs the
    # entire end-of-session send (log summary, heartbeat, re-pinning the
    # status message) again — a fast, indefinite restart loop that spams
    # Telegram once per restart until the next real trading session
    # replaces session_date. See maybe_send_session_open_messages'
    # docstring for the symmetric guard on the OPEN side of this same
    # class of bug.
    if now >= close_ts and _load_session_close_state(SESSION_CLOSE_STATE_FILE) == session_date.isoformat():
        logger.info("already sent today's (%s) close report — idling %ds", session_date, OFF_SESSION_IDLE_SECONDS)
        time.sleep(OFF_SESSION_IDLE_SECONDS)
        return HeartbeatStats(start_time=now, session_date=session_date)

    # A halt has no in-process "resume" moment (see tradebot.incidents'
    # module docstring) — reaching a fresh run_live() call at all is
    # itself proof the system came back, so any still-open halt incident
    # closes right here, unconditionally (a safe no-op if none is open).
    from tradebot import incidents

    incidents.close_incident("halt", now)

    conn = connect(db_path) if db_path is not None else connect()
    version = code_version()
    # Per call, not per process and not per session date: a restart
    # mid-session starts a genuinely separate execution, and the ledger
    # should say so rather than blur the two together. See run_replay's
    # matching comment.
    run_id = new_run_id()
    budget = AlertBudget(now=lambda: datetime.now(timezone.utc))
    stats = HeartbeatStats(start_time=now, session_date=session_date)

    try:
        maybe_send_session_open_messages(conn, alerter, session_date, now)
        maybe_send_weekly_recap(conn, alerter, now)
        if isinstance(alerter, TelegramAlerter):
            maybe_update_pinned_status(alerter.token, alerter.chat_id, conn, now)
    except Exception:
        stats.errors.append(traceback.format_exc())

    history_by_symbol = _build_history_by_symbol(CACHE_DIR, WATCHLIST, session_date, stats)

    # Stage 2 observability (tradebot.evaluations). Opened once for the
    # session and passed to process_new_bar, which records what the
    # detectors saw on every bar -- including the bars where nothing
    # fired, which until now left no trace anywhere.
    #
    # A failure to open it costs the observability and nothing else:
    # eval_conn stays None, process_new_bar's recording is then inert,
    # and the session scans exactly as it did before this existed. Same
    # never-fatal treatment as the universe connection above.
    eval_conn = None
    try:
        from tradebot import evaluations as evaluations_module

        eval_conn = evaluations_module.connect()
    except Exception:
        stats.errors.append(traceback.format_exc())

    md = {symbol: LiveMarketData(symbol, session_date) for symbol in WATCHLIST}
    anchors: dict[str, DailyAnchors] = {}
    rth_bar_count = {symbol: 0 for symbol in WATCHLIST}
    stale_notified = False

    halt_checker = None
    if isinstance(alerter, TelegramAlerter):
        halt_checker = TelegramHaltChecker(alerter.token, alerter.chat_id)

    # Stage 1 wiring — see run_broad_scan's docstring. Entirely opt-in
    # (enable_broad_scan=False preserves the exact fixed-WATCHLIST
    # behavior this loop always had) and never allowed to take the
    # session down: a universe-refresh or bulk-fetch failure is logged
    # like any other per-iteration exception, not fatal.
    universe_conn = None
    dynamic_symbols: list[str] = []
    last_broad_scan: datetime | None = None
    if enable_broad_scan:
        try:
            from tradebot import universe as universe_mod
            from tradebot.vendors.alpaca import fetch_us_equity_assets

            universe_conn = universe_mod.connect()
            universe_mod.refresh_universe(universe_conn, fetch_us_equity_assets, now)
        except Exception:
            stats.errors.append(traceback.format_exc())
            universe_conn = None

    while True:
        loop_start = datetime.now(timezone.utc)
        if loop_start >= close_ts:
            break
        if HALT_FILE.exists() or (halt_checker is not None and halt_checker.check()):
            alerter.send(templates.render_system_notice("Halt requested. Stopping the live session.", loop_start), priority=outbox.PRIORITY_HIGH)
            break

        if universe_conn is not None and _broad_scan_due(loop_start, open_ts, last_broad_scan):
            try:
                dynamic_symbols = run_broad_scan(
                    universe_conn, session_date=session_date, tick_utc=loop_start,
                    run_id=run_id, run_mode=RUN_MODE_LIVE,
                )
                for symbol in dynamic_symbols:
                    if symbol not in md:
                        md[symbol] = LiveMarketData(symbol, session_date)
                        rth_bar_count[symbol] = 0
                logger.info("broad scan promoted %d symbol(s): %s", len(dynamic_symbols), dynamic_symbols)
            except Exception:
                stats.errors.append(traceback.format_exc())
            last_broad_scan = loop_start

        scan_symbols = WATCHLIST + [s for s in dynamic_symbols if s not in WATCHLIST]
        # Shared across every symbol THIS iteration: relative_strength_break
        # (the only market_bars consumer, see detectors.py) only needs one
        # proxy snapshot per tick, not one freshly re-fetched per symbol --
        # its own alignment is positional (proxy_bars[len(bars)-1]) with an
        # explicit len(proxy_bars) < len(bars) abstention guard, so a single
        # shared, closed-bar-filtered fetch is exactly as safe to read from
        # every symbol's evaluation as a per-symbol refetch was, at 1/17th
        # the vendor calls. shared_market_bars_attempted (not just checking
        # "is shared_market_bars truthy") is what stops a failed or
        # legitimately-empty first attempt from silently retrying on every
        # later symbol in the same iteration.
        shared_market_bars: dict[str, list[Bar]] | None = None
        shared_market_bars_attempted = False
        for symbol in scan_symbols:
            origin = "watchlist" if symbol in WATCHLIST else "screening"
            try:
                # Captured before the fetch, not after: a request can
                # straddle a bar boundary (start at 13:34:59.8, Alpaca
                # forms the response from data available at THAT instant,
                # network returns at 13:35:00.15) — bar_close_ts(bar) <=
                # some later post-fetch timestamp only proves the wall
                # clock has passed the boundary by the time we looked, not
                # that the specific response we're holding was assembled
                # after it. Using the pre-fetch instant as the eligibility
                # cutoff is the conservative choice: a bar whose nominal
                # close falls in the request's own window is deferred to
                # the next poll rather than trusted as final from a
                # response that might have been mid-formation when Alpaca
                # built it.
                pre_fetch_time = datetime.now(timezone.utc)
                rth_bars = list(md[symbol].session_bars(symbol, session_date))
                if not rth_bars:
                    continue
                rth_bars = only_closed_bars(rth_bars, pre_fetch_time)
                if not rth_bars:
                    continue
                # Staleness wants the freshest possible "now" — the fetch
                # has already completed by this point, and using an older
                # timestamp here would only make the check less sensitive,
                # never wrongly reject good data (the direction eligibility
                # above needs to be conservative in; this direction is
                # safe to be as fresh as possible instead). Not
                # loop_start: see the eligibility comment above — the
                # same staleness applies to it.
                post_fetch_time = datetime.now(timezone.utc)
                required_close = latest_required_bar_close(
                    open_ts, post_fetch_time, STALENESS_SECONDS, session_close=close_ts
                )
                if required_close is not None and bar_close_ts(rth_bars[-1]) < required_close:
                    metrics.increment("data_health_suppression", reason="stale")
                    if not stale_notified:
                        alerter.send(
                            templates.render_system_notice(
                                f"{symbol} data is stale (>{STALENESS_SECONDS}s). "
                                "Suppressing alerts until fresh.",
                                loop_start,
                            ),
                            priority=outbox.PRIORITY_HIGH,
                        )
                        stale_notified = True
                    continue
                if len(rth_bars) == rth_bar_count[symbol]:
                    continue
                rth_bar_count[symbol] = len(rth_bars)

                if symbol not in anchors:
                    daily = md[symbol].daily_bars(symbol, 20)
                    if not daily:
                        stats.data_gaps.append(f"{symbol}: no prior daily bar cached for {session_date}")
                        continue
                    anchors[symbol] = build_anchors(
                        symbol=symbol,
                        session_date=session_date,
                        prior_daily_bars=daily,
                        opening_range_bars=rth_bars[:1],
                        # .get(..., []): a symbol Stage 1 just promoted mid-session
                        # has no cached replay history (see cached_session_dates'
                        # WATCHLIST-only scope above) — an empty history just
                        # means rvol_spike (the one detector that reads
                        # avg_cum_volume_by_bar) never fires for it; every other
                        # detector works fine off this session's own bars alone.
                        historical_session_bars=history_by_symbol.get(symbol, []),
                    )

                # Lazy: only the first symbol THIS iteration that reaches
                # this point pays for the shared proxy fetch -- an
                # iteration where no symbol advances this far (no bars, not
                # closed, stale, or no new bar) makes zero proxy calls.
                # Isolated in its own try/except, separate from the outer
                # per-symbol handler below: a shared-fetch failure must
                # degrade only relative_strength_break's context for the
                # rest of this iteration, never abort this (or any later)
                # symbol's ordinary detector processing the way an
                # unguarded exception here would.
                if not shared_market_bars_attempted:
                    shared_market_bars_attempted = True
                    try:
                        proxy_pre_fetch_time = datetime.now(timezone.utc)
                        shared_market_bars = {
                            proxy: only_closed_bars(
                                list(md[proxy].session_bars(proxy, session_date)), proxy_pre_fetch_time
                            )
                            for proxy in MARKET_PROXY_SYMBOLS if proxy in md
                        }
                    except Exception:
                        stats.errors.append(traceback.format_exc())
                        shared_market_bars = None

                process_new_bar(
                    conn, budget, alerter, version, symbol, session_date, rth_bars,
                    anchors[symbol], md[symbol].quote,
                    lambda s, expiry, _sym=symbol: md[_sym].chain(s, expiry=expiry),
                    stats, subscriber_hook,
                    validation_now_fn=lambda: datetime.now(timezone.utc),
                    market_bars=shared_market_bars,
                    data_feed=DETECTOR_DATA_FEED, origin=origin,
                    run_mode=RUN_MODE_LIVE, run_id=run_id,
                    eval_conn=eval_conn,
                )
                send_medium_digest_if_due(budget, alerter, conn, loop_start, medium_fanout_fn)
            except Exception:
                stats.errors.append(traceback.format_exc())
                try:
                    alerter.send(
                        templates.render_system_notice(
                            f"{symbol}: exception during evaluation. See logs. Continuing.", loop_start
                        ),
                        priority=outbox.PRIORITY_HIGH,
                    )
                except Exception:
                    pass
                continue

        backfill_pending_contract_mids(conn, md, loop_start)
        bot_liveness.write_heartbeat(HEARTBEAT_FILE, loop_start)
        elapsed = (datetime.now(timezone.utc) - loop_start).total_seconds()
        time.sleep(max(0.0, BAR_MINUTES * 60 - elapsed))

    end_time = datetime.now(timezone.utc)
    send_log_summary(budget, alerter, conn, end_time)
    todays_symbols = detected_symbols_for_session(conn, session_date)
    fetched_ok, fetch_failed = _cache_todays_intraday_bars(CACHE_DIR, todays_symbols, session_date)
    _alert_if_cache_fetch_failed(alerter, fetched_ok, fetch_failed, session_date, end_time)
    marks_written = backfill_marks(conn, session_date)
    _alert_if_backfill_implausible(alerter, stats, marks_written, session_date, end_time)
    backfill_pending_contract_close_mids(conn, md, session_date)
    backfill_contract_day_ranges(conn, md, session_date)
    heartbeat = templates.render_heartbeat(
        session_date, end_time - now, stats.tier_counts, stats.suppression_counts,
        stats.data_gaps, stats.errors, tier_performance(conn), end_time,
        cache_fetch_failed=fetch_failed,
    )
    alerter.send(heartbeat, priority=outbox.PRIORITY_LOG)
    _mark_session_close_sent(SESSION_CLOSE_STATE_FILE, session_date)
    conn.close()
    return stats


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="push real alerts to Telegram; default is console/log-only")
    parser.add_argument(
        "--replay-date", type=str, default=None,
        help="YYYY-MM-DD — run against a cached session instead of live market hours",
    )
    parser.add_argument(
        "--no-personal-alerts", action="store_true",
        help="with --live, skip the per-user DM fan-out (tradebot.telegram_bot) and only push the ops channel",
    )
    parser.add_argument(
        "--broad-scan", action="store_true",
        help="with --live, also run Stage 1 (tradebot.broad_scan) across the full active universe "
        "(tradebot.universe) every 30 minutes, promoting up to 25 extra symbols into live Stage 2 "
        "evaluation on top of the fixed WATCHLIST. Off by default.",
    )
    parser.add_argument(
        "--db-path", type=str, default=None,
        help="override the journal DB path — for running two versions of the detection logic against the "
        "same --replay-date into separate files, see scripts/compare_replay.py. Default: data/journal.db "
        "for a live run, data/journal_replay.db for --replay-date (a replay never defaults to the "
        "production journal)",
    )
    parser.add_argument(
        "--metrics-path", type=str, default=None,
        help="override where --replay-date writes its counters (default data/metrics_replay.json -- a "
        "replay never writes the live data/metrics.json). Ignored for a live run, which always uses "
        "data/metrics.json",
    )
    parser.add_argument(
        "--allow-production-replay-db", action="store_true",
        help="DANGEROUS: permit --replay-date to write to the production journal (data/journal.db). A "
        "replay reproduces live detection ids and will overwrite the live record's decision state. "
        "Without this, naming the production journal is refused",
    )
    parser.add_argument(
        "--cache-dir", type=str, default=None,
        help="override which cache tree (default data/cache/) --replay-date replays against — for comparing "
        "two DATA sources (e.g. IEX vs. SIP) into separate --db-path files, see docs/sip-migration-proposal.md",
    )
    args = parser.parse_args()

    # The alerter is deliberately NOT built before this branch. Replaying
    # a historical session must never be able to select the live Telegram
    # alerter -- a replay's alerts are hours or days stale, and
    # run_replay() opens by sending a morning briefing and pre-open card,
    # so a --replay-date --live run would push those to real subscribers
    # before evaluating a single bar. Rejecting the combination here, and
    # constructing ConsoleAlerter inside the replay branch rather than
    # from args.live above it, means "a replay holding a TelegramAlerter"
    # is not a state this function can reach. There is deliberately no
    # override: --no-personal-alerts only skips the per-user DM fan-out
    # and still pushes the ops channel, so it is not one either.
    if args.replay_date:
        if args.live:
            parser.error(
                "--replay-date cannot be combined with --live: a replay of a historical session "
                "must not push real alerts to Telegram. Drop --live to replay to the console."
            )
        alerter = ConsoleAlerter()
        cache_dir = Path(args.cache_dir) if args.cache_dir else None
        try:
            run_replay(
                date.fromisoformat(args.replay_date), alerter, db_path=args.db_path,
                cache_dir=cache_dir, allow_production_db=args.allow_production_replay_db,
                metrics_path=args.metrics_path,
            )
        except ProductionJournalRefused as exc:
            parser.error(str(exc))
    else:
        alerter = TelegramAlerter() if args.live else ConsoleAlerter()
        subscriber_hook = None
        medium_fanout_fn = None
        if args.live and not args.no_personal_alerts:
            # Deferred import: the command layer (and its own DB) is only
            # needed here, so replay/console-only runs stay decoupled from it.
            # No BotClient here anymore — the hooks only enqueue to the
            # outbox now; tradebot.telegram_bot.worker is the only thing
            # that actually calls the Telegram API.
            from tradebot.telegram_bot.db import connect as users_connect
            from tradebot.telegram_bot.delivery import make_medium_fanout_fn, make_subscriber_hook

            users_conn = users_connect()
            session_date_fn = lambda now: now.astimezone(ET).date()
            subscriber_hook = make_subscriber_hook(users_conn, session_date_fn, WATCHLIST)
            medium_fanout_fn = make_medium_fanout_fn(users_conn, session_date_fn, WATCHLIST)
        run_live(alerter, subscriber_hook, medium_fanout_fn, enable_broad_scan=args.broad_scan, db_path=args.db_path)


if __name__ == "__main__":
    main()
