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
import logging
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as ecals

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.config import WATCHLIST
from tradebot.marketdata import write_bars_csv as _write_bars_csv
from tradebot.vendors.alpaca import AlpacaCredentialsError, fetch_daily_bars, fetch_intraday_bars

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
MAX_LOOKBACK_DAYS = 60  # safety cap so a long holiday streak can't loop forever

logger = logging.getLogger("watchtower.fetch_cache")
_CALENDAR = ecals.get_calendar("XNYS")  # same instance pattern as runner.py's CALENDAR


def _atomic_write_bars_csv(path: Path, bars: list) -> None:
    """Same CSV shape as marketdata.write_bars_csv (reused verbatim, not
    reimplemented -- two independent CSV writers would be exactly the
    kind of drift this project avoids elsewhere), but via a temp file in
    the same directory + os.replace() rather than a direct write to
    `path`.

    Without this, a kill mid-write leaves a truncated file sitting at
    the final path -- and ensure_daily/ensure_sessions key their whole
    idempotency check on path.exists(), so a truncated file is locked
    in as "done" forever, never re-fetched or re-validated (full-code-
    review finding #11). os.replace() is atomic on the same filesystem,
    which the same-directory temp file guarantees. This wraps the
    shared tradebot.marketdata.write_bars_csv from the outside rather
    than changing it -- that function is also called directly by
    tradebot.runner's live close-time cacher, which this fix does not
    touch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _write_bars_csv(tmp_path, bars)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def ensure_daily(symbol: str, cache_dir: Path, n: int) -> str:
    path = cache_dir / symbol / "daily.csv"
    if path.exists():
        return "skipped (exists)"
    bars = fetch_daily_bars(symbol, n)
    if not bars:
        # Unlike an intraday session, an active symbol legitimately
        # returning zero daily bars is never expected -- always loud.
        logger.error("fetch_daily_bars returned no data for %s (requested n=%d)", symbol, n)
        return "no data returned"
    _atomic_write_bars_csv(path, bars)
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
                _atomic_write_bars_csv(path, bars)
                satisfied += 1
                results.append((candidate, f"fetched {len(bars)} bars"))
            elif _CALENDAR.is_session(candidate):
                # A real NYSE trading day with zero bars back is never
                # expected -- the pre-fix code labeled this identically to
                # an actual holiday ("no data (holiday?)"), which is
                # exactly the kind of silence the 2026-08-12 incident
                # review flagged: a real failure reads the same as
                # nothing-to-do.
                logger.error("fetch_intraday_bars returned no data for %s on a real trading day %s", symbol, candidate)
                results.append((candidate, "ERROR: no data on a real trading day"))
            else:
                results.append((candidate, "no data (holiday)"))

        candidate -= timedelta(days=1)

    if satisfied < n:
        results.append(
            (candidate, f"gave up after {checked} candidate days, only {satisfied}/{n} sessions satisfied")
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(WATCHLIST), help="comma-separated symbols")
    parser.add_argument("--daily-n", type=int, default=60)
    parser.add_argument("--sessions-n", type=int, default=20)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    summary_rows = []
    failed_symbols = []
    for symbol in symbols:
        print(f"\n=== {symbol} ===")

        daily_status = ensure_daily(symbol, args.cache_dir, args.daily_n)
        print(f"daily.csv: {daily_status}")

        session_results = ensure_sessions(symbol, args.cache_dir, args.sessions_n)
        fetched = sum(1 for _, s in session_results if s.startswith("fetched"))
        skipped = sum(1 for _, s in session_results if s.startswith("skipped"))
        no_data = sum(1 for _, s in session_results if s.startswith("no data"))
        errors = sum(1 for _, s in session_results if s.startswith("ERROR"))
        gave_up = sum(1 for _, s in session_results if s.startswith("gave up"))
        for d, status in session_results:
            print(f"  {d}: {status}")

        summary_rows.append(
            {
                "symbol": symbol,
                "daily": daily_status,
                "sessions_fetched": fetched,
                "sessions_skipped": skipped,
                "sessions_no_data": no_data,
                "sessions_errors": errors,
                "sessions_gave_up": gave_up,
            }
        )

        # A genuine post-retry failure, not a legitimate no-op: "skipped
        # (exists)"/"no data (holiday)" leave a symbol fully satisfied
        # and must not trip this. "no data returned" (ensure_daily) and
        # any "ERROR"/"gave up" session row (ensure_sessions) are all
        # cases where fetch_daily_bars/fetch_intraday_bars already
        # exhausted their own internal retries and came back empty on a
        # day that should have had data -- see full-code-review.md
        # finding #3/C3.
        if daily_status == "no data returned" or errors > 0 or gave_up > 0:
            failed_symbols.append(symbol)

    print("\n=== summary ===")
    header = (
        f"{'symbol':<8} {'daily':<20} {'fetched':>8} {'skipped':>8} {'no_data':>8} {'errors':>8} {'gave_up':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in summary_rows:
        print(
            f"{row['symbol']:<8} {row['daily']:<20} "
            f"{row['sessions_fetched']:>8} {row['sessions_skipped']:>8} {row['sessions_no_data']:>8} "
            f"{row['sessions_errors']:>8} {row['sessions_gave_up']:>8}"
        )

    if failed_symbols:
        print(
            f"\n{len(failed_symbols)}/{len(symbols)} symbols failed: {', '.join(failed_symbols)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlpacaCredentialsError as e:
        raise SystemExit(f"error: {e}")
