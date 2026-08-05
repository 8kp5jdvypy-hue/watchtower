#!/usr/bin/env python3
"""Fetch and cache market data for the watchlist, via the Alpaca adapter.

Per symbol, caches:
  - 60 daily bars      -> data/cache/{symbol}/daily.csv
  - 20 sessions of 5-minute bars (premarket + RTH combined)
                        -> data/cache/{symbol}/intraday_{YYYY-MM-DD}.csv

Idempotent: any cache file that already exists is left alone and counted
as satisfied. Resumable: re-running only fetches what's still missing.
Session dates are found by walking backward from yesterday and skipping
non-trading days (Alpaca returns no bars for holidays/weekends, so those
don't count toward the 20).

Usage:
    python3 scripts/fetch_cache.py
    python3 scripts/fetch_cache.py --symbols SPY
    python3 scripts/fetch_cache.py --symbols SPY,QQQ --daily-n 60 --sessions-n 20
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.config import WATCHLIST
from tradebot.detectors import Bar
from tradebot.vendors.alpaca import AlpacaCredentialsError, fetch_daily_bars, fetch_intraday_bars

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
MAX_LOOKBACK_DAYS = 60  # safety cap so a long holiday streak can't loop forever


def _write_bars_csv(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])


def ensure_daily(symbol: str, cache_dir: Path, n: int) -> str:
    path = cache_dir / symbol / "daily.csv"
    if path.exists():
        return "skipped (exists)"
    bars = fetch_daily_bars(symbol, n)
    if not bars:
        return "no data returned"
    _write_bars_csv(path, bars)
    return f"fetched {len(bars)} bars"


def ensure_sessions(symbol: str, cache_dir: Path, n: int) -> list[tuple[date, str]]:
    results: list[tuple[date, str]] = []
    satisfied = 0
    candidate = date.today() - timedelta(days=1)
    checked = 0

    while satisfied < n and checked < MAX_LOOKBACK_DAYS:
        checked += 1
        if candidate.weekday() >= 5:  # weekend
            candidate -= timedelta(days=1)
            continue

        path = cache_dir / symbol / f"intraday_{candidate.isoformat()}.csv"
        if path.exists():
            satisfied += 1
            results.append((candidate, "skipped (exists)"))
        else:
            bars = fetch_intraday_bars(symbol, candidate)
            if bars:
                _write_bars_csv(path, bars)
                satisfied += 1
                results.append((candidate, f"fetched {len(bars)} bars"))
            else:
                results.append((candidate, "no data (holiday?)"))

        candidate -= timedelta(days=1)

    if satisfied < n:
        results.append(
            (candidate, f"gave up after {checked} candidate days, only {satisfied}/{n} sessions satisfied")
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(WATCHLIST), help="comma-separated symbols")
    parser.add_argument("--daily-n", type=int, default=60)
    parser.add_argument("--sessions-n", type=int, default=20)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    summary_rows = []
    for symbol in symbols:
        print(f"\n=== {symbol} ===")

        daily_status = ensure_daily(symbol, args.cache_dir, args.daily_n)
        print(f"daily.csv: {daily_status}")

        session_results = ensure_sessions(symbol, args.cache_dir, args.sessions_n)
        fetched = sum(1 for _, s in session_results if s.startswith("fetched"))
        skipped = sum(1 for _, s in session_results if s.startswith("skipped"))
        no_data = sum(1 for _, s in session_results if s.startswith("no data"))
        for d, status in session_results:
            print(f"  {d}: {status}")

        summary_rows.append(
            {
                "symbol": symbol,
                "daily": daily_status,
                "sessions_fetched": fetched,
                "sessions_skipped": skipped,
                "sessions_no_data": no_data,
            }
        )

    print("\n=== summary ===")
    header = f"{'symbol':<8} {'daily':<20} {'fetched':>8} {'skipped':>8} {'no_data':>8}"
    print(header)
    print("-" * len(header))
    for row in summary_rows:
        print(
            f"{row['symbol']:<8} {row['daily']:<20} "
            f"{row['sessions_fetched']:>8} {row['sessions_skipped']:>8} {row['sessions_no_data']:>8}"
        )


if __name__ == "__main__":
    try:
        main()
    except AlpacaCredentialsError as e:
        raise SystemExit(f"error: {e}")
