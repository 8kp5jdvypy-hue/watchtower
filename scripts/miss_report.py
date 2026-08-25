#!/usr/bin/env python3
"""Missed Mover Investigation Report — "why did Perch miss this?"

Read-only diagnosis. Walks the pipeline funnel for one (symbol, session)
across all four stores and reports the FIRST stage that explains the
absence of an alert, with an evidence class on every conclusion.

    python scripts/miss_report.py --symbol AAPL --session 2026-08-24 --move-pct 9.2

--move-pct is supplied by the operator on purpose: Perch retains no price
history for most symbols (data/cache is WATCHLIST-scoped, Stage 1's bulk
daily bars are never persisted, and marks are keyed on detection_id, so a
missed mover has none). The tool explains the miss; it cannot discover it.

Every database is opened with SQLite's read-only URI, which also refuses
to create a missing file -- so running this can neither modify state nor
leave new files behind. A store that does not exist is reported as absent
rather than crashing the run: evaluations.db will not exist until the
first live session after PR #80, and users.db does not exist on a
scanner-only box.

Exit code is 0 for any successfully produced report, including one whose
verdict is NOT_IN_UNIVERSE or INCONCLUSIVE -- a verdict is an answer, not
a tool failure. Non-zero is reserved for the tool itself failing: bad
arguments, or every store unreadable.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot import miss_report
from tradebot.evaluations import DEFAULT_DB_PATH as EVALUATIONS_DB
from tradebot.journal import DEFAULT_DB_PATH as JOURNAL_DB
from tradebot.telegram_bot.db import DEFAULT_DB_PATH as USERS_DB
from tradebot.universe import DEFAULT_DB_PATH as UNIVERSE_DB


def _parse_event_time(raw: str, session: str):
    """HH:MM on --session, or a full ISO timestamp.

    A bare HH:MM is treated as UTC, matching every timestamp in every
    store (bar_ts_utc, ts_utc, tick_utc, delivered_at are all UTC). A
    full ISO value carrying its own offset is honoured as given; one
    without an offset is assumed UTC for the same reason."""
    raw = raw.strip()
    try:
        parsed = time.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        return datetime.combine(date.fromisoformat(session), parsed, tzinfo=timezone.utc)
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", required=True, help="the symbol that moved, e.g. AAPL")
    parser.add_argument("--session", required=True, help="session date, YYYY-MM-DD")
    parser.add_argument(
        "--move-pct", type=float, default=None,
        help="the move you observed, e.g. 9.2 -- operator-supplied and labelled as such, "
             "because Perch does not retain price history for most symbols",
    )
    parser.add_argument(
        "--event-time", default=None,
        help="when the move mattered: HH:MM (interpreted in UTC on --session) or a full ISO "
             "timestamp. WITHOUT this the tool prints the session timeline and refuses to name a "
             "failure point -- a session-wide verdict can report ALERTED for a move that was "
             "actually missed hours earlier",
    )
    parser.add_argument(
        "--window-minutes", type=int, default=60,
        help="how long Perch had to react, forward from --event-time (default 60). Forward, not "
             "symmetric: Perch can only act on bars that closed at or after the move became visible",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="restrict Stage 2 to one run. By default every run_id for the session is shown "
             "separately -- a mid-session restart produces a second run over the same bars, and "
             "merging them would present two independent evaluations as one",
    )
    parser.add_argument("--journal-db", default=None, help=f"override (default {JOURNAL_DB})")
    parser.add_argument("--universe-db", default=None, help=f"override (default {UNIVERSE_DB})")
    parser.add_argument("--evaluations-db", default=None, help=f"override (default {EVALUATIONS_DB})")
    parser.add_argument("--users-db", default=None, help=f"override (default {USERS_DB})")
    args = parser.parse_args(argv)

    try:
        date.fromisoformat(args.session)
    except ValueError:
        parser.error(f"--session must be YYYY-MM-DD, got {args.session!r}")

    symbol = args.symbol.strip().upper()
    if not symbol:
        parser.error("--symbol must not be empty")

    window = None
    if args.event_time:
        event_time = _parse_event_time(args.event_time, args.session)
        if event_time is None:
            parser.error(f"--event-time must be HH:MM or a full ISO timestamp, "
                         f"got {args.event_time!r}")
        if args.window_minutes <= 0:
            parser.error("--window-minutes must be positive")
        window = miss_report.EventWindow(event_time=event_time, minutes=args.window_minutes)

    stores = {
        "universe": miss_report.open_readonly(args.universe_db or UNIVERSE_DB),
        "evaluations": miss_report.open_readonly(args.evaluations_db or EVALUATIONS_DB),
        "journal": miss_report.open_readonly(args.journal_db or JOURNAL_DB),
        "users": miss_report.open_readonly(args.users_db or USERS_DB),
    }
    if not any(stores.values()):
        print("no database could be opened -- nothing to diagnose against", file=sys.stderr)
        return 2

    report = miss_report.build_report(
        symbol=symbol, session=args.session, move_pct=args.move_pct,
        run_id=args.run_id, window=window, **stores,
    )
    print(miss_report.render(report))
    for conn in stores.values():
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
