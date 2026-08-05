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

from tradebot.alerts import AlertBudget, Cluster, ConsoleAlerter, Decision, TelegramAlerter, format_alert
from tradebot.costs import breakeven_move
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
from tradebot.journal import backfill_marks, code_version, connect
from tradebot.journal import write_cluster as journal_write_cluster
from tradebot.marketdata import LiveMarketData, Quote, ReplayMarketData

WATCHLIST = ["SPY", "QQQ", "GOOGL", "TSLA", "BE", "IONQ"]
REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "cache"
HALT_FILE = REPO_ROOT / "data" / "HALT"
ET = ZoneInfo("America/New_York")
STALENESS_SECONDS = 90
BAR_MINUTES = 5

CALENDAR = ecals.get_calendar("XNYS")


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

    def summary_text(self, end_time: datetime) -> str:
        uptime = end_time - self.start_time
        lines = [
            f"=== heartbeat: {self.session_date} ===",
            f"uptime: {uptime}",
            f"detections by tier: {dict(self.tier_counts)}",
            f"suppressions: {dict(self.suppression_counts)}",
            f"data gaps: {len(self.data_gaps)}",
        ]
        lines.extend(f"  - {g}" for g in self.data_gaps)
        lines.append(f"errors: {len(self.errors)}")
        return "\n".join(lines)


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
    return {
        "ts": expected_close,
        "close": last.close,
        "atr14": atr(bars),
        "kinds": ",".join(d.kind for d in detections),
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
    conn, budget, alerter, version, symbol, session_date, bars, anchors, quote_fn, chain_fn, stats
) -> None:
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
    cluster = Cluster(
        id=detection_id,
        ts_utc=result["ts"].isoformat(),
        session=session_date.isoformat(),
        symbol=symbol,
        kinds=result["kinds"],
        headlines=result["headlines"],
        score=result["score"],
        tier=tier,
        close=result["close"],
        atr14=result["atr14"],
        trend=result["trend"],
        code_version=version,
    )

    decision = budget.evaluate(cluster)
    stats.record_cluster(tier, decision)

    if decision in (Decision.SEND, Decision.CAP_REACHED_NOTICE):
        quote = quote_fn(symbol)
        try:
            chain = chain_fn(symbol)
        except NotImplementedError:
            chain = None
        breakeven = breakeven_move(chain, spot=result["close"], atr14=result["atr14"])
        alerter.send(format_alert(cluster, anchors, quote, breakeven))
        conn.execute("UPDATE detections SET alerted=1 WHERE id=?", (detection_id,))
        if decision == Decision.CAP_REACHED_NOTICE:
            alerter.send(
                f"[SYSTEM] daily high-tier alert cap ({budget.max_high_per_day}) reached — "
                "suppressing further HIGH alerts today."
            )
            conn.execute(
                "UPDATE detections SET suppress_reason=? WHERE id=?", (decision.value, detection_id)
            )
    elif decision in (Decision.SUPPRESS_CAP, Decision.SUPPRESS_COOLDOWN):
        conn.execute("UPDATE detections SET suppress_reason=? WHERE id=?", (decision.value, detection_id))
    conn.commit()


def send_medium_digest_if_due(budget: AlertBudget, alerter) -> None:
    digest = budget.pop_medium_digest_if_due()
    if not digest:
        return
    lines = [f"[MEDIUM DIGEST] {len(digest)} cluster(s)"]
    lines += [f"  {c.symbol} {c.kinds} score={c.score:.2f} — {c.headlines}" for c in digest]
    alerter.send("\n".join(lines))


def send_log_summary(budget: AlertBudget, alerter) -> None:
    summary = budget.pop_log_summary()
    if not summary:
        return
    lines = [f"[LOG SUMMARY] {len(summary)} sub-threshold detection(s) today"]
    by_symbol = Counter(c.symbol for c in summary)
    lines += [f"  {symbol}: {count}" for symbol, count in by_symbol.most_common()]
    alerter.send("\n".join(lines))


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

    def chain_fn(symbol: str):
        return md[symbol].chain(symbol, expiry=session_date)  # always raises — no chain in replay

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
                    daily = md[symbol].daily_bars(symbol, 2)
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
                send_medium_digest_if_due(budget, alerter)
            except Exception:
                stats.errors.append(traceback.format_exc())
                alerter.send(f"[ERROR] {symbol}: exception during evaluation — see logs. Continuing.")
                continue

        if HALT_FILE.exists():
            alerter.send("[SYSTEM] HALT file present — stopping replay.")
            halted = True
        if not any_advanced:
            break

    send_log_summary(budget, alerter)
    marks_written = backfill_marks(conn, session_date)
    print(f"backfilled {marks_written} forward-price marks")
    heartbeat = stats.summary_text(clock["t"])
    alerter.send(heartbeat)
    conn.close()
    return stats


# --------------------------------------------------------------------------
# Live mode — real 5-minute loop. See module docstring: unverified against
# real market conditions in this build.
# --------------------------------------------------------------------------


def run_live(alerter) -> HeartbeatStats:
    now = datetime.now(timezone.utc)
    session_date = now.astimezone(ET).date()
    open_ts, close_ts = session_bounds(session_date)

    conn = connect()
    version = code_version()
    budget = AlertBudget(now=lambda: datetime.now(timezone.utc))
    stats = HeartbeatStats(start_time=now, session_date=session_date)

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
            alerter.send("[SYSTEM] halt requested — stopping.")
            break

        for symbol in WATCHLIST:
            try:
                rth_bars = list(md[symbol].session_bars(symbol, session_date))
                if not rth_bars:
                    continue
                if is_stale(bar_close_ts(rth_bars[-1]), loop_start):
                    if not stale_notified:
                        alerter.send(
                            f"[SYSTEM] {symbol} data is stale (>{STALENESS_SECONDS}s) — "
                            "suppressing alerts until fresh."
                        )
                        stale_notified = True
                    continue
                if len(rth_bars) == rth_bar_count[symbol]:
                    continue
                rth_bar_count[symbol] = len(rth_bars)

                if symbol not in anchors:
                    daily = md[symbol].daily_bars(symbol, 2)
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
                    lambda s, _sym=symbol: md[_sym].chain(s, expiry=session_date),
                    stats,
                )
                send_medium_digest_if_due(budget, alerter)
            except Exception:
                stats.errors.append(traceback.format_exc())
                try:
                    alerter.send(f"[ERROR] {symbol}: exception during evaluation — see logs. Continuing.")
                except Exception:
                    pass
                continue

        elapsed = (datetime.now(timezone.utc) - loop_start).total_seconds()
        time.sleep(max(0.0, BAR_MINUTES * 60 - elapsed))

    send_log_summary(budget, alerter)
    backfill_marks(conn, session_date)
    alerter.send(stats.summary_text(datetime.now(timezone.utc)))
    conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="push real alerts to Telegram; default is console/log-only")
    parser.add_argument(
        "--replay-date", type=str, default=None,
        help="YYYY-MM-DD — run against a cached session instead of live market hours",
    )
    args = parser.parse_args()

    alerter = TelegramAlerter() if args.live else ConsoleAlerter()

    if args.replay_date:
        run_replay(date.fromisoformat(args.replay_date), alerter)
    else:
        run_live(alerter)


if __name__ == "__main__":
    main()
