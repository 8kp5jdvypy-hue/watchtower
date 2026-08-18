#!/usr/bin/env python3
"""Generates a weekly recap (Part B, docs/phase4-proof-engine-proposal.md)
for one [week-start, week-start + 7 days) window, in markdown and/or an
HTML fragment. Thin CLI shell over tradebot.rendering.recap's pure
build/render functions — see that module for the actual logic (and for
what makes this deterministic: same week, same databases, same output).

Reads two databases: journal.db (detections/marks) and users.db (the
outbox, for the real send timestamp) — same two-connection shape
Part A's /public/track-record route uses, for the same reason.

Usage (see docs/DEPLOYMENT.md's "Running scripts/ tools in-container"
for the docker compose invocation on the VPS):
    python3 scripts/generate_weekly_recap.py --week-start 2026-07-27
    python3 scripts/generate_weekly_recap.py --week-start 2026-07-27 --format html
    python3 scripts/generate_weekly_recap.py --week-start 2026-07-27 --out-dir out/
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.journal import DEFAULT_DB_PATH as DEFAULT_JOURNAL_DB_PATH
from tradebot.journal import connect as journal_connect
from tradebot.rendering.recap import build_recap_data, render_recap_html, render_recap_markdown
from tradebot.telegram_bot.db import DEFAULT_DB_PATH as DEFAULT_USERS_DB_PATH
from tradebot.telegram_bot.db import connect as users_connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week-start", required=True, help="ISO date the recap week starts on")
    parser.add_argument("--format", choices=["markdown", "html", "both"], default="both")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_JOURNAL_DB_PATH)
    parser.add_argument("--users-db-path", type=Path, default=DEFAULT_USERS_DB_PATH)
    parser.add_argument("--out-dir", type=Path, default=None, help="write files here instead of printing to stdout")
    args = parser.parse_args()

    week_start = date.fromisoformat(args.week_start).isoformat()
    week_end = (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()

    journal_conn = journal_connect(args.db_path)
    users_conn = users_connect(args.users_db_path)
    data = build_recap_data(journal_conn, users_conn, week_start, week_end)

    outputs: dict[str, str] = {}
    if args.format in ("markdown", "both"):
        outputs["md"] = render_recap_markdown(data)
    if args.format in ("html", "both"):
        outputs["html"] = render_recap_html(data)

    if args.out_dir is None:
        for fmt, text in outputs.items():
            print(f"=== {fmt} ===")
            print(text)
            print()
    else:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for fmt, text in outputs.items():
            path = args.out_dir / f"recap_{week_start}.{fmt}"
            path.write_text(text)
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
