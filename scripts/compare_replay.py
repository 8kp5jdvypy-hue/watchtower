#!/usr/bin/env python3
"""Diff two versions of the detection logic against the same historical
session — the tool used to calibrate new detectors/thresholds (e.g.
relative_strength_break, tradebot.dedup's window/escalation constants)
against real data before trusting a default.

This is a manual, two-checkout workflow, not an automated subsystem:

    git checkout <before-commit-or-branch>
    python -m tradebot.runner --replay-date 2026-08-05 --db-path data/journal_a.db

    git checkout <after-commit-or-branch>
    python -m tradebot.runner --replay-date 2026-08-05 --db-path data/journal_b.db

    python scripts/compare_replay.py --db-path-a data/journal_a.db \
        --db-path-b data/journal_b.db --session-date 2026-08-05

Rows are joined on (symbol, ts_utc), not `id` — a detection's id is a
hash of (symbol, session, ts_utc, kinds) (see journal.cluster_id()), and
`kinds` is exactly the kind of thing a detection-logic change can
legitimately alter for the same real bar (e.g. a new detector
contributing to what fired), so joining on id would make every changed
row look like two unrelated ones instead of a diff.

See journal.write_cluster()'s upsert-by-identity behavior for why A and
B must go to SEPARATE db files rather than one shared db: re-running the
same session under a second code version would silently overwrite the
first version's rows wherever kinds happens to match, and produce two
un-diffable rows wherever it doesn't. Two files, diffed externally here,
avoids that entirely — see the implementation plan's "A/B replay
infrastructure" section for the fuller rationale (cluster_id() is relied
on as a stable FK by marks/contract_selections/the outbox/Telegram
callbacks, so changing its formula to include code_version was ruled out
as disproportionate to this one need).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_rows(db_path: str, session_date: str) -> dict[tuple[str, str], sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, ts_utc, kinds, score, tier, suppress_category, lifecycle_state, alerted "
        "FROM detections WHERE session = ?",
        (session_date,),
    ).fetchall()
    return {(r["symbol"], r["ts_utc"]): r for r in rows}


def _aggregate_counts(rows: dict[tuple[str, str], sqlite3.Row]) -> dict[str, Counter]:
    by_tier: Counter = Counter()
    by_suppress_category: Counter = Counter()
    duplicates_prevented = 0
    alerts_sent = 0
    for row in rows.values():
        by_tier[row["tier"]] += 1
        if row["suppress_category"]:
            by_suppress_category[row["suppress_category"]] += 1
            if row["suppress_category"] == "duplicate":
                duplicates_prevented += 1
        if row["alerted"]:
            alerts_sent += 1
    return {
        "by_tier": by_tier,
        "by_suppress_category": by_suppress_category,
        "duplicates_prevented": duplicates_prevented,
        "alerts_sent": alerts_sent,
    }


def compare(rows_a: dict, rows_b: dict) -> dict:
    """Returns {"only_in_a": [...], "only_in_b": [...], "differs": [...]}
    — differs holds (key, row_a, row_b) tuples where the same (symbol,
    ts_utc) moment produced a different kinds/score/tier/category/state
    under the two versions."""
    keys_a, keys_b = set(rows_a), set(rows_b)
    only_in_a = sorted(keys_a - keys_b)
    only_in_b = sorted(keys_b - keys_a)
    differs = []
    fields = ("kinds", "score", "tier", "suppress_category", "lifecycle_state")
    for key in sorted(keys_a & keys_b):
        row_a, row_b = rows_a[key], rows_b[key]
        if any(row_a[f] != row_b[f] for f in fields):
            differs.append((key, row_a, row_b))
    return {"only_in_a": only_in_a, "only_in_b": only_in_b, "differs": differs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path-a", required=True, help="journal DB from the 'before' checkout")
    parser.add_argument("--db-path-b", required=True, help="journal DB from the 'after' checkout")
    parser.add_argument("--session-date", required=True, help="YYYY-MM-DD, must match what both replays used")
    args = parser.parse_args()

    rows_a = _load_rows(args.db_path_a, args.session_date)
    rows_b = _load_rows(args.db_path_b, args.session_date)

    print(f"=== {args.session_date}: A={args.db_path_a} ({len(rows_a)} rows) "
          f"vs B={args.db_path_b} ({len(rows_b)} rows) ===\n")

    for label, agg in (("A", _aggregate_counts(rows_a)), ("B", _aggregate_counts(rows_b))):
        print(f"[{label}] by tier: {dict(agg['by_tier'])}")
        print(f"[{label}] suppressed by category: {dict(agg['by_suppress_category'])}")
        print(f"[{label}] duplicates prevented: {agg['duplicates_prevented']}")
        print(f"[{label}] alerts sent (ops channel): {agg['alerts_sent']}")
        print()

    diff = compare(rows_a, rows_b)
    print(f"candidates only in A (B no longer produces): {len(diff['only_in_a'])}")
    for symbol, ts_utc in diff["only_in_a"][:20]:
        print(f"  {symbol} {ts_utc}")
    print(f"\ncandidates only in B (new in this version): {len(diff['only_in_b'])}")
    for symbol, ts_utc in diff["only_in_b"][:20]:
        print(f"  {symbol} {ts_utc}")
    print(f"\nmoments present in both but differing (kinds/score/tier/category/state): {len(diff['differs'])}")
    for (symbol, ts_utc), row_a, row_b in diff["differs"][:20]:
        print(f"  {symbol} {ts_utc}")
        print(f"    A: kinds={row_a['kinds']!r} score={row_a['score']:.2f} tier={row_a['tier']} "
              f"category={row_a['suppress_category']} state={row_a['lifecycle_state']}")
        print(f"    B: kinds={row_b['kinds']!r} score={row_b['score']:.2f} tier={row_b['tier']} "
              f"category={row_b['suppress_category']} state={row_b['lifecycle_state']}")


if __name__ == "__main__":
    main()
