"""Tests for scripts/compare_replay.py — no existing precedent in this
repo for testing a script directly, so it's loaded by file path rather
than as a package import (scripts/ isn't a package)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from tradebot.detectors import Detection
from tradebot.journal import connect as journal_connect
from tradebot.journal import write_cluster

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "compare_replay.py"
spec = importlib.util.spec_from_file_location("compare_replay", SCRIPT_PATH)
compare_replay = importlib.util.module_from_spec(spec)
sys.modules["compare_replay"] = compare_replay
spec.loader.exec_module(compare_replay)

SESSION = "2026-08-05"


def _detection(kind="gap", score=4.0) -> Detection:
    return Detection("TSLA", kind, datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc), score, "h", {})


def _write(db_path, symbol, ts_utc, kinds, score, suppress_category=None, lifecycle_state=None, alerted=0):
    conn = journal_connect(db_path)
    detection_id = write_cluster(
        conn, session=SESSION, symbol=symbol, ts_utc=ts_utc, kinds=kinds, headlines="h", score=score,
        close=100.0, atr14=1.0, trend="up", detections=[_detection(kind=kinds.split(",")[0], score=score)],
        code_version_str="abc123",
    )
    conn.execute(
        "UPDATE detections SET suppress_category=?, lifecycle_state=?, alerted=? WHERE id=?",
        (suppress_category, lifecycle_state, alerted, detection_id),
    )
    conn.commit()
    conn.close()
    return detection_id


def test_load_rows_and_aggregate_counts(tmp_path):
    db = tmp_path / "a.db"
    _write(db, "TSLA", "2026-08-05T14:00:00+00:00", "gap", 5.0, alerted=1)
    _write(db, "TSLA", "2026-08-05T14:10:00+00:00", "vwap_break", 4.5, suppress_category="duplicate")
    _write(db, "AAPL", "2026-08-05T14:00:00+00:00", "level_break", 1.0)  # log tier, not high

    rows = compare_replay._load_rows(str(db), SESSION)
    assert len(rows) == 3
    agg = compare_replay._aggregate_counts(rows)
    assert agg["alerts_sent"] == 1
    assert agg["duplicates_prevented"] == 1
    assert agg["by_suppress_category"]["duplicate"] == 1


def test_compare_identifies_only_in_a_only_in_b_and_differing_moments(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    # Same moment, same result in both — should not appear as a diff.
    _write(db_a, "TSLA", "2026-08-05T14:00:00+00:00", "gap", 5.0, alerted=1)
    _write(db_b, "TSLA", "2026-08-05T14:00:00+00:00", "gap", 5.0, alerted=1)

    # Only in A — version B no longer produces this candidate.
    _write(db_a, "TSLA", "2026-08-05T14:05:00+00:00", "vwap_break", 2.0)

    # Only in B — a new detector (e.g. relative_strength_break) produces
    # a candidate that didn't exist under A.
    _write(db_b, "AAPL", "2026-08-05T14:10:00+00:00", "relative_strength_break", 1.5)

    # Same moment, different outcome — B's stricter dedup suppresses what
    # A sent.
    _write(db_a, "MSFT", "2026-08-05T14:15:00+00:00", "gap", 5.0, alerted=1)
    _write(db_b, "MSFT", "2026-08-05T14:15:00+00:00", "gap", 5.0, suppress_category="duplicate")

    rows_a = compare_replay._load_rows(str(db_a), SESSION)
    rows_b = compare_replay._load_rows(str(db_b), SESSION)
    diff = compare_replay.compare(rows_a, rows_b)

    assert diff["only_in_a"] == [("TSLA", "2026-08-05T14:05:00+00:00")]
    assert diff["only_in_b"] == [("AAPL", "2026-08-05T14:10:00+00:00")]
    assert len(diff["differs"]) == 1
    (symbol, ts_utc), row_a, row_b = diff["differs"][0]
    assert symbol == "MSFT" and ts_utc == "2026-08-05T14:15:00+00:00"
    assert row_a["suppress_category"] is None
    assert row_b["suppress_category"] == "duplicate"
