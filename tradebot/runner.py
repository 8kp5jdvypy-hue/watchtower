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
import logging
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
    TelegramAlerter,
)
from tradebot.config import WATCHLIST
from tradebot.costs import select_contract
from tradebot.events import active_event_window, events_for_date, has_earnings_before
from tradebot.rendering import templates
from tradebot.guard import validate_alert_data
from tradebot import metrics
from tradebot.telegram_bot import heartbeat as bot_liveness
from tradebot.detectors import (
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
    backfill_marks,
    code_version,
    connect,
    historical_performance,
    iv_rank,
    pending_contract_backfills,
    record_contract_forward_mid,
    record_contract_selection,
    record_iv_sample,
    set_news_driven,
    set_no_trade,
    tier_performance,
)
from tradebot.journal import write_cluster as journal_write_cluster
from tradebot.marketdata import LiveMarketData, Quote, ReplayMarketData

SIMILAR_SETUPS_LOOKBACK = 200  # deep enough to realistically reach costs.MIN_SIMILAR_SETUPS_SAMPLE (50)

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "cache"
HALT_FILE = REPO_ROOT / "data" / "HALT"
HEARTBEAT_FILE = REPO_ROOT / "data" / "heartbeat.json"
ET = ZoneInfo("America/New_York")
STALENESS_SECONDS = 90
BAR_MINUTES = 5

CALENDAR = ecals.get_calendar("XNYS")

logger = logging.getLogger("watchtower.runner")


# --------------------------------------------------------------------------
# Pure helpers — testable without real time or network.
# --------------------------------------------------------------------------


def is_stale(latest_bar_close: datetime, now: datetime, max_seconds: int = STALENESS_SECONDS) -> bool:
    return (now - latest_bar_close).total_seconds() > max_seconds


def is_halted_bar(bar: Bar) -> bool:
    """A zero-volume bar means no trades happened — treat it as no new
    information rather than feeding it to the detectors, so a halted
    symbol (common for BE and IONQ) doesn't produce garbage on reopen."""
    return bar.volume == 0


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


def full_session_rth_bars(symbol: str, session_date: date) -> list[Bar]:
    md = ReplayMarketData(CACHE_DIR, symbol, session_date)
    while md.advance():
        pass
    return list(md.session_bars(symbol, session_date))


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


def evaluate_bar(symbol: str, bars: list[Bar], anchors: DailyAnchors) -> dict | None:
    detections = [d for d in (detector(bars, anchors) for detector in DETECTORS) if d is not None]
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


def process_new_bar(
    conn, budget, alerter, version, symbol, session_date, bars, anchors, quote_fn, chain_fn, stats,
    subscriber_hook=None, now=None,
) -> None:
    """subscriber_hook(cluster, rendered_text), if given, is called right
    after a HIGH alert is sent to the ops channel/console — it's how the
    live per-user DM fan-out (tradebot.telegram_bot.delivery) plugs in
    without this module needing to know anything about Telegram users.
    None (the default) preserves the exact prior behavior — no fan-out,
    used by replay and by every existing test.

    now: real wall-clock time, tz-aware, for the guard's quote-staleness
    check. Only run_live() passes this — a replayed historical quote is
    definitionally hours/days "stale" relative to real now, so replay
    passes None and the guard skips that one check, the same way
    STALENESS_SECONDS/is_stale() is already a live-only concept here."""
    last = bars[-1]
    if is_halted_bar(last):
        stats.data_gaps.append(f"{symbol} zero-volume bar at {last.ts.isoformat()} (halted?)")
        return

    result = evaluate_bar(symbol, bars, anchors)
    if result is None:
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
    )
    tier = tier_for_score(result["score"]).value
    raw_tier_is_high = tier == "high"

    # News/macro tagging — see tradebot.events module docstring: news is
    # suppression and context, never an alert source. Any overlapping
    # event window, regardless of severity or tier, marks this cluster
    # news-driven: continuation stats don't transfer to event-driven
    # moves, so a reader (and historical_performance()'s own sample pool)
    # needs to know this one doesn't count as a clean technical setup.
    event_window = active_event_window(conn, symbol, result["ts"])
    news_driven = event_window is not None
    if news_driven:
        set_news_driven(conn, detection_id, True)

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
    )

    if raw_tier_is_high and news_driven and event_window.severity == "suppress":
        decision = Decision.SUPPRESS_NEWS_BLACKOUT
        logger.info(
            "HIGH alert suppressed by event window: symbol=%s kind=%s detail=%s source=%s",
            symbol, event_window.kind, event_window.detail, event_window.source,
        )
        metrics.increment("event_window_suppression", kind=event_window.kind)
    else:
        decision = budget.evaluate(cluster)
    stats.record_cluster(tier, decision)

    if decision in (Decision.SEND, Decision.CAP_REACHED_NOTICE):
        quote = quote_fn(symbol)
        guard_reason = validate_alert_data(
            cluster, anchors, quote, bars=bars, now=now, primary_detection=result["primary_detection"],
        )
        if guard_reason is None:
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

            text = templates.render_high_alert(cluster, anchors, quote, selection, similar_setups, news_driven=news_driven)
            alerter.send(text)
            conn.execute("UPDATE detections SET alerted=1 WHERE id=?", (detection_id,))
            if subscriber_hook is not None:
                try:
                    subscriber_hook(cluster, text)
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
            stats.errors.append(f"{symbol}: alert suppressed by data guard — {guard_reason}")
            conn.execute(
                "UPDATE detections SET suppress_reason=? WHERE id=?",
                (f"data_integrity_failed: {guard_reason}", detection_id),
            )
        if decision == Decision.CAP_REACHED_NOTICE:
            alerter.send(
                templates.render_system_notice(
                    f"Daily high-tier alert cap ({budget.max_high_per_day}) reached. "
                    "Suppressing further HIGH alerts today.",
                    result["ts"],
                )
            )
            if guard_reason is None:
                conn.execute(
                    "UPDATE detections SET suppress_reason=? WHERE id=?", (decision.value, detection_id)
                )
    elif decision in (Decision.SUPPRESS_CAP, Decision.SUPPRESS_COOLDOWN, Decision.SUPPRESS_NEWS_BLACKOUT):
        reason = decision.value
        if decision == Decision.SUPPRESS_NEWS_BLACKOUT:
            reason = f"{decision.value}:{event_window.kind}:{event_window.detail or event_window.source}"
        conn.execute("UPDATE detections SET suppress_reason=? WHERE id=?", (reason, detection_id))
    conn.commit()


def send_medium_digest_if_due(budget: AlertBudget, alerter, conn, when: datetime) -> None:
    digest = budget.pop_medium_digest_if_due()
    if not digest:
        return
    tier_perf = tier_performance(conn).get("medium")
    alerter.send(templates.render_digest("Medium Digest", "medium", digest, tier_perf, when))


def send_log_summary(budget: AlertBudget, alerter, conn, when: datetime) -> None:
    summary = budget.pop_log_summary()
    if not summary:
        return
    tier_perf = tier_performance(conn).get("log")
    alerter.send(templates.render_log_summary(summary, tier_perf, when))


# --------------------------------------------------------------------------
# Replay mode — fast-forwards through a cached session bar by bar.
# --------------------------------------------------------------------------


def run_replay(session_date: date, alerter) -> HeartbeatStats:
    open_ts, _close_ts = session_bounds(session_date)

    conn = connect()
    version = code_version()
    clock = {"t": open_ts}
    budget = AlertBudget(now=lambda: clock["t"])
    stats = HeartbeatStats(start_time=open_ts, session_date=session_date)

    try:
        alerter.send(templates.render_morning_briefing(tier_performance(conn).get("high"), clock["t"]))
        alerter.send(templates.render_pre_open_card(events_for_date(conn, session_date), session_date, clock["t"]))
    except Exception:
        stats.errors.append(traceback.format_exc())

    historical_sessions = [s for s in cached_session_dates(CACHE_DIR, WATCHLIST) if s < session_date]
    history_by_symbol = {
        symbol: [full_session_rth_bars(symbol, s) for s in historical_sessions] for symbol in WATCHLIST
    }

    md = {symbol: ReplayMarketData(CACHE_DIR, symbol, session_date) for symbol in WATCHLIST}
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

                process_new_bar(
                    conn, budget, alerter, version, symbol, session_date, rth_bars,
                    anchors[symbol], quote_fn, chain_fn, stats,
                )
                send_medium_digest_if_due(budget, alerter, conn, clock["t"])
            except Exception:
                stats.errors.append(traceback.format_exc())
                try:
                    alerter.send(
                        templates.render_system_notice(
                            f"{symbol}: exception during evaluation. See logs. Continuing.", clock["t"]
                        )
                    )
                except Exception:
                    pass
                continue

        if HALT_FILE.exists():
            alerter.send(templates.render_system_notice("HALT file present. Stopping replay.", clock["t"]))
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
    alerter.send(heartbeat)
    conn.close()
    return stats


# --------------------------------------------------------------------------
# Live mode — real 5-minute loop. See module docstring: unverified against
# real market conditions in this build.
# --------------------------------------------------------------------------


def run_live(alerter, subscriber_hook=None) -> HeartbeatStats:
    now = datetime.now(timezone.utc)
    session_date = now.astimezone(ET).date()
    open_ts, close_ts = session_bounds(session_date)

    conn = connect()
    version = code_version()
    budget = AlertBudget(now=lambda: datetime.now(timezone.utc))
    stats = HeartbeatStats(start_time=now, session_date=session_date)

    try:
        alerter.send(templates.render_morning_briefing(tier_performance(conn).get("high"), now))
        alerter.send(templates.render_pre_open_card(events_for_date(conn, session_date), session_date, now))
    except Exception:
        stats.errors.append(traceback.format_exc())

    historical_sessions = [s for s in cached_session_dates(CACHE_DIR, WATCHLIST) if s < session_date]
    history_by_symbol = {
        symbol: [full_session_rth_bars(symbol, s) for s in historical_sessions] for symbol in WATCHLIST
    }

    md = {symbol: LiveMarketData(symbol, session_date) for symbol in WATCHLIST}
    anchors: dict[str, DailyAnchors] = {}
    rth_bar_count = {symbol: 0 for symbol in WATCHLIST}
    stale_notified = False

    halt_checker = None
    if isinstance(alerter, TelegramAlerter):
        halt_checker = TelegramHaltChecker(alerter.token, alerter.chat_id)

    while True:
        loop_start = datetime.now(timezone.utc)
        if loop_start >= close_ts:
            break
        if HALT_FILE.exists() or (halt_checker is not None and halt_checker.check()):
            alerter.send(templates.render_system_notice("halt requested. Stopping.", loop_start))
            break

        for symbol in WATCHLIST:
            try:
                rth_bars = list(md[symbol].session_bars(symbol, session_date))
                if not rth_bars:
                    continue
                if is_stale(bar_close_ts(rth_bars[-1]), loop_start):
                    if not stale_notified:
                        alerter.send(
                            templates.render_system_notice(
                                f"{symbol} data is stale (>{STALENESS_SECONDS}s). "
                                "Suppressing alerts until fresh.",
                                loop_start,
                            )
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
                        historical_session_bars=history_by_symbol[symbol],
                    )

                process_new_bar(
                    conn, budget, alerter, version, symbol, session_date, rth_bars,
                    anchors[symbol], md[symbol].quote,
                    lambda s, expiry, _sym=symbol: md[_sym].chain(s, expiry=expiry),
                    stats, subscriber_hook, now=loop_start,
                )
                send_medium_digest_if_due(budget, alerter, conn, loop_start)
            except Exception:
                stats.errors.append(traceback.format_exc())
                try:
                    alerter.send(
                        templates.render_system_notice(
                            f"{symbol}: exception during evaluation. See logs. Continuing.", loop_start
                        )
                    )
                except Exception:
                    pass
                continue

        bot_liveness.write_heartbeat(HEARTBEAT_FILE, loop_start)
        elapsed = (datetime.now(timezone.utc) - loop_start).total_seconds()
        time.sleep(max(0.0, BAR_MINUTES * 60 - elapsed))

    end_time = datetime.now(timezone.utc)
    send_log_summary(budget, alerter, conn, end_time)
    backfill_marks(conn, session_date)
    heartbeat = templates.render_heartbeat(
        session_date, end_time - now, stats.tier_counts, stats.suppression_counts,
        stats.data_gaps, stats.errors, tier_performance(conn), end_time,
    )
    alerter.send(heartbeat)
    conn.close()
    return stats


def main() -> None:
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
    args = parser.parse_args()

    alerter = TelegramAlerter() if args.live else ConsoleAlerter()

    if args.replay_date:
        run_replay(date.fromisoformat(args.replay_date), alerter)
    else:
        subscriber_hook = None
        if args.live and not args.no_personal_alerts:
            # Deferred import: the command layer (and its own DB) is only
            # needed here, so replay/console-only runs stay decoupled from it.
            from tradebot.telegram_bot.client import BotClient
            from tradebot.telegram_bot.db import connect as users_connect
            from tradebot.telegram_bot.delivery import make_subscriber_hook

            client = BotClient(alerter.token)
            users_conn = users_connect()
            subscriber_hook = make_subscriber_hook(
                client, users_conn, lambda now: now.astimezone(ET).date(), WATCHLIST
            )
        run_live(alerter, subscriber_hook)


if __name__ == "__main__":
    main()
