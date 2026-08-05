#!/usr/bin/env python3
"""Print the hour-of-day performance breakdown from the journal.

Purely informational — this never gates or suppresses alerts (see
journal.hour_performance()'s docstring for why: a train/test split
showed hour-of-day patterns inverting between halves of the data,
i.e. noise, not a stable effect, at the sample sizes available when
that check was run). Re-run this periodically as more sessions
accumulate to see whether a real pattern eventually stabilizes.

Usage:
    python3 scripts/hour_report.py                # HIGH tier, +30min
    python3 scripts/hour_report.py --tier all
    python3 scripts/hour_report.py --offset 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.journal import connect, hour_performance


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="high", help="'high', 'medium', 'log', or 'all' for every non-log tier")
    parser.add_argument("--offset", type=int, default=30, help="forward-price offset in minutes")
    args = parser.parse_args()

    tier = None if args.tier == "all" else args.tier
    conn = connect()
    result = hour_performance(conn, tier=tier, offset_min=args.offset)

    label = "all non-log tiers" if tier is None else tier.upper()
    print(f"=== hour-of-day performance: {label}, +{args.offset}min ===\n")
    if not result:
        print("no hour has enough samples yet")
        return

    total = sum(hp.sample_size for hp in result.values())
    for hour in sorted(result):
        hp = result[hour]
        print(
            f"  {hour:02d}:00-{hour:02d}:59 ET   n={hp.sample_size:<5} "
            f"continued={hp.continuation_rate * 100:5.1f}%  avg={hp.avg_return_pct:+.4f}%"
        )
    print(f"\ntotal sampled: {total}")


if __name__ == "__main__":
    main()
