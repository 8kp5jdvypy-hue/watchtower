#!/usr/bin/env python3
"""Replay every cached session through the detectors and score what fires.

For each (symbol, session) pair, walks bars one at a time via
ReplayMarketData, freezes DailyAnchors once the first RTH bar (09:30-09:35
ET) closes, then evaluates every detector in DETECTORS (level_break,
rvol_spike, range_expansion, vwap_break, round_number_break, gap) on
every RTH bar close from that point on — including the anchor bar
itself, since gap() only ever fires there.

Detections that land on the same (symbol, bar) are grouped into a cluster
and scored with score_cluster(). One row per cluster is written to
out/replay_detections.csv AND to the SQLite journal (data/journal.db),
including sub-threshold ('log' tier) clusters — see CLAUDE.md. Forward
price marks (+15/+30/+60min) are backfilled into the journal for every
replayed session.

avg_cum_volume_by_bar for a session is built only from sessions cached
strictly before it, so the earliest cached session has no rvol_spike
baseline yet (by construction, not a bug).

"trend" in the output is a simple close-vs-prior_close label — up if the
latest close is at or above the frozen prior_close, down otherwise. No
other trend definition was specified, so treat this as a placeholder to
revisit if it needs to mean something more specific.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.config import WATCHLIST
from tradebot.detectors import DETECTORS, Bar, atr, bar_close_ts, build_anchors, score_cluster
from tradebot.journal import backfill_marks, code_version, connect, write_cluster
from tradebot.marketdata import ReplayMarketData

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
OUT_PATH = Path(__file__).resolve().parent.parent / "out" / "replay_detections.csv"
ET = ZoneInfo("America/New_York")


def cached_session_dates(cache_dir: Path, symbols: list[str]) -> list[date]:
    """Session dates with an intraday cache file for every symbol."""
    common: set[date] | None = None
    for symbol in symbols:
        dates = set()
        for path in (cache_dir / symbol).glob("intraday_*.csv"):
            dates.add(date.fromisoformat(path.stem.removeprefix("intraday_")))
        common = dates if common is None else (common & dates)
    return sorted(common or set())


def full_session_rth_bars(symbol: str, session_date: date) -> list[Bar]:
    """A session's RTH bars, fully revealed. Used only to build history
    for later sessions' avg_cum_volume_by_bar — never for evaluation."""
    md = ReplayMarketData(CACHE_DIR, symbol, session_date)
    while md.advance():
        pass
    return list(md.session_bars(symbol, session_date))


def replay_symbol_session(
    symbol: str, session_date: date, historical_session_bars: list[list[Bar]]
) -> list[dict]:
    md = ReplayMarketData(CACHE_DIR, symbol, session_date)

    # Advance until the first RTH bar has closed (09:35 ET).
    rth_bars: list[Bar] = []
    while not rth_bars:
        if not md.advance():
            return []  # no RTH bars this session at all
        rth_bars = list(md.session_bars(symbol, session_date))

    daily = md.daily_bars(symbol, 20)
    if not daily:
        return []  # no prior daily bar cached yet for this date

    anchors = build_anchors(
        symbol=symbol,
        session_date=session_date,
        prior_daily_bars=daily,
        opening_range_bars=rth_bars[:1],
        historical_session_bars=historical_session_bars,
    )

    rows: list[dict] = []
    prev_len = 0
    while True:
        bars = list(md.session_bars(symbol, session_date))
        if len(bars) > prev_len and bars:
            prev_len = len(bars)
            detections = [
                d for d in (detector(bars, anchors) for detector in DETECTORS) if d is not None
            ]
            if detections:
                last = bars[-1]
                expected_close = bar_close_ts(last)
                for d in detections:
                    assert d.ts >= expected_close, (
                        f"lookahead violation: {d.kind} detection ts={d.ts} precedes "
                        f"bar close={expected_close} for {symbol} {session_date}"
                    )
                window = atr(bars)
                rows.append(
                    {
                        "session": session_date.isoformat(),
                        "ts_et": expected_close.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                        "ts_utc": expected_close.isoformat(),
                        "symbol": symbol,
                        "kinds": ",".join(d.kind for d in detections),
                        "headlines": "; ".join(d.headline for d in detections),
                        "score": round(score_cluster(detections), 4),
                        "close": last.close,
                        "atr14": round(window, 4) if window is not None else "",
                        "trend": "up" if last.close >= anchors.prior_close else "down",
                        "detections": detections,
                    }
                )
        if not md.advance():
            break

    return rows


def print_histogram(scores: list[float], bucket_width: float = 0.25) -> None:
    if not scores:
        print("(no clusters)")
        return
    buckets: Counter[float] = Counter()
    for s in scores:
        bucket = (int(s / bucket_width)) * bucket_width
        buckets[round(bucket, 2)] += 1
    max_count = max(buckets.values())
    scale = 50 / max_count if max_count > 50 else 1
    for bucket in sorted(buckets):
        count = buckets[bucket]
        bar = "#" * max(1, round(count * scale))
        print(f"  {bucket:>5.2f}-{bucket + bucket_width:<5.2f} | {bar} ({count})")


def main() -> None:
    sessions = cached_session_dates(CACHE_DIR, WATCHLIST)
    if not sessions:
        raise SystemExit("no cached sessions found for the full watchlist — run fetch_cache.py first")

    print(f"replaying {len(sessions)} sessions x {len(WATCHLIST)} symbols: {sessions[0]} .. {sessions[-1]}")

    all_rows: list[dict] = []
    history_by_symbol: dict[str, list[list[Bar]]] = {s: [] for s in WATCHLIST}

    for session_date in sessions:
        for symbol in WATCHLIST:
            rows = replay_symbol_session(symbol, session_date, history_by_symbol[symbol])
            all_rows.extend(rows)
            # this session's RTH bars become available history for later sessions
            history_by_symbol[symbol].append(full_session_rth_bars(symbol, session_date))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    csv_fields = ["session", "ts_et", "symbol", "kinds", "headlines", "score", "close", "atr14", "trend"]
    with OUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nwrote {len(all_rows)} cluster rows to {OUT_PATH}")

    conn = connect()
    version = code_version()
    for r in all_rows:
        write_cluster(
            conn,
            session=r["session"],
            symbol=r["symbol"],
            ts_utc=r["ts_utc"],
            kinds=r["kinds"],
            headlines=r["headlines"],
            score=r["score"],
            close=r["close"],
            atr14=r["atr14"] if r["atr14"] != "" else None,
            trend=r["trend"],
            detections=r["detections"],
            code_version_str=version,
        )
    conn.commit()
    print(f"wrote {len(all_rows)} cluster rows to the journal ({version})")

    marks_written = 0
    for session_date in sessions:
        marks_written += backfill_marks(conn, session_date)
    print(f"backfilled {marks_written} forward-price marks")

    print("\n=== journal count by tier ===")
    for tier, count in conn.execute(
        "SELECT tier, COUNT(*) AS n FROM detections GROUP BY tier ORDER BY n DESC"
    ).fetchall():
        print(f"  {tier:<8} {count}")
    conn.close()

    scores = [r["score"] for r in all_rows]
    print("\n=== score histogram (bucket width 0.25) ===")
    print_histogram(scores)

    print("\n=== counts by symbol ===")
    by_symbol = Counter(r["symbol"] for r in all_rows)
    for symbol in WATCHLIST:
        print(f"  {symbol:<6} {by_symbol.get(symbol, 0)}")

    print("\n=== counts by detector kind ===")
    by_kind: Counter[str] = Counter()
    for r in all_rows:
        for kind in r["kinds"].split(","):
            by_kind[kind] += 1
    for kind, count in by_kind.most_common():
        print(f"  {kind:<16} {count}")

    print("\n=== clusters per day (summed across the watchlist) ===")
    per_day: dict[str, int] = defaultdict(int)
    for r in all_rows:
        per_day[r["session"]] += 1
    daily_counts = [per_day.get(s.isoformat(), 0) for s in sessions]
    mean_per_day = sum(daily_counts) / len(daily_counts)
    print(f"  mean: {mean_per_day:.2f}")
    print(f"  max:  {max(daily_counts)} (on {sessions[daily_counts.index(max(daily_counts))]})")


if __name__ == "__main__":
    main()
