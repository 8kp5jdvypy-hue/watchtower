#!/usr/bin/env python3
"""Ship #1 (P5b): purge the runt watchlist cache files and backfill 20
SIP sessions -- docs/open-awareness-proposals-2026-08.md, Proposal 5b.

Identifies which watchlist symbols' 2026-08-11/08-12 intraday cache files
actually fail the plausibility floor (tradebot.marketdata.
implausible_session_reason -- the same shipped function Proposal 5c wires
into the live pipeline), using each symbol's own prior cached sessions as
the volume reference. Report-only by default; --apply deletes exactly the
files that failed, nothing else -- a healthy file for a symbol that
wasn't actually affected is never touched.

Deletion only clears space for a refetch; it does not itself refetch.
Run scripts/fetch_cache.py afterward (see usage below) to pull the SIP
history back in -- that script is already idempotent/resumable, so this
script doesn't duplicate its fetch logic.

Usage (on the VPS, in-container -- this only touches the cache dir it's
pointed at, never the live process):
    # 1. See what would be purged, without touching anything:
    python3 scripts/purge_and_backfill_runts.py

    # 2. Delete exactly the files that fail the floor:
    python3 scripts/purge_and_backfill_runts.py --apply

    # 3. Refetch under SIP -- also backfills up to 20 SIP sessions total
    #    per symbol (fetch_cache.py's default --sessions-n), which is
    #    what Proposal 1/2's baselines need:
    DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py --sessions-n 20
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.config import WATCHLIST
from tradebot.marketdata import implausible_session_reason, median_session_volume
from tradebot.runner import cached_session_dates, expected_rth_bar_count, full_session_rth_bars

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
DEFAULT_CANDIDATE_DATES = (date(2026, 8, 11), date(2026, 8, 12))


def find_runts(cache_dir: Path, symbols: list[str], candidate_dates: list[date]) -> list[tuple[str, date, str]]:
    """Returns [(symbol, date, reason), ...] for every candidate date that
    fails the plausibility floor, judged against that symbol's own cached
    sessions strictly before the candidate."""
    runts: list[tuple[str, date, str]] = []
    for symbol in symbols:
        path_dates = sorted(
            date.fromisoformat(p.stem.removeprefix("intraday_"))
            for p in (cache_dir / symbol).glob("intraday_*.csv")
        )
        for candidate in candidate_dates:
            if candidate not in path_dates:
                continue  # nothing cached for this symbol/date -- not this script's concern
            reference_dates = [d for d in path_dates if d < candidate][-20:]
            median_volume = median_session_volume(
                [sum(b.volume for b in full_session_rth_bars(symbol, d, cache_dir)) for d in reference_dates]
            )
            bars = full_session_rth_bars(symbol, candidate, cache_dir)
            reason = implausible_session_reason(
                bars, median_volume=median_volume, expected_bar_count=expected_rth_bar_count(candidate)
            )
            if reason is not None:
                runts.append((symbol, candidate, reason))
    return runts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default=",".join(WATCHLIST), help="comma-separated symbols")
    parser.add_argument("--dates", default=",".join(d.isoformat() for d in DEFAULT_CANDIDATE_DATES), help="comma-separated ISO dates to check")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--apply", action="store_true", help="actually delete the files that fail the floor (default: report only)")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    candidate_dates = [date.fromisoformat(d.strip()) for d in args.dates.split(",") if d.strip()]

    runts = find_runts(args.cache_dir, symbols, candidate_dates)

    if not runts:
        print(f"no runts found among {symbols} x {candidate_dates} under {args.cache_dir}")
        return

    print(f"{len(runts)} runt file(s) found:")
    for symbol, d, reason in runts:
        path = args.cache_dir / symbol / f"intraday_{d.isoformat()}.csv"
        print(f"  {path}: {reason}")

    if not args.apply:
        print("\nreport-only (pass --apply to delete these files), then run:")
        print("  DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py --sessions-n 20")
        return

    for symbol, d, _reason in runts:
        path = args.cache_dir / symbol / f"intraday_{d.isoformat()}.csv"
        path.unlink()
        print(f"deleted {path}")

    print("\nnow run:")
    print("  DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py --sessions-n 20")


if __name__ == "__main__":
    main()
