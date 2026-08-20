"""Tests for scripts/replay.py — loaded by file path, same pattern as
tests/test_compare_replay.py (scripts/ isn't a package).

A1 (docs/open-awareness-proposals-2026-08.md): this harness does NOT go
through tradebot.runner.evaluate_bar, so it needs its own call into the
shared tradebot.features.pct_from_prior_close primitive -- these tests
prove that call actually happened and landed the right value/status on
each row, the second of A1's two required call sites."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from tradebot.marketdata import Bar, write_bars_csv

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "replay.py"
spec = importlib.util.spec_from_file_location("replay_under_test", SCRIPT_PATH)
replay = importlib.util.module_from_spec(spec)
sys.modules["replay_under_test"] = replay
spec.loader.exec_module(replay)

SYMBOL = "TSLA"
SESSION = date(2026, 8, 5)


def _write_fixture_cache(cache_dir: Path) -> None:
    daily_bars = [
        Bar(SYMBOL, datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc), 99.0, 101.0, 98.0, 100.0, volume=50_000_000),
        Bar(SYMBOL, datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 100.0, 102.0, 99.0, 100.0, volume=50_000_000),
    ]
    write_bars_csv(cache_dir / SYMBOL / "daily.csv", daily_bars)

    intraday_bars = [
        Bar(SYMBOL, datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc), 110.0, 112.0, 109.0, 110.0, volume=1_000_000),
        Bar(SYMBOL, datetime(2026, 8, 5, 13, 35, tzinfo=timezone.utc), 110.0, 111.0, 109.5, 110.5, volume=800_000),
    ]
    write_bars_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", intraday_bars)


def test_replay_symbol_session_reports_pct_from_prior_close(tmp_path):
    _write_fixture_cache(tmp_path)

    rows = replay.replay_symbol_session(SYMBOL, SESSION, historical_session_bars=[], cache_dir=tmp_path)

    assert len(rows) >= 1  # gap fires on bar 0: |110-100|/prior day's range
    first = rows[0]
    # prior_close (last daily bar's close) = 100.0, first bar close = 110.0
    assert first["pct_from_prior_close"] == 10.0
    assert first["pct_from_prior_close_status"] == "AVAILABLE"


def test_replay_symbol_session_pct_from_prior_close_matches_the_shared_primitive(tmp_path):
    """Not just 'some number' -- the EXACT value/status
    tradebot.features.pct_from_prior_close would independently compute
    for the same (close, prior_close) pair, since both callers must use
    one implementation."""
    from tradebot.features import pct_from_prior_close

    _write_fixture_cache(tmp_path)
    rows = replay.replay_symbol_session(SYMBOL, SESSION, historical_session_bars=[], cache_dir=tmp_path)

    for row in rows:
        expected = pct_from_prior_close(row["close"], 100.0)  # prior_close frozen from the fixture's daily bars
        assert row["pct_from_prior_close"] == expected.value
        assert row["pct_from_prior_close_status"] == expected.status


def test_replay_csv_export_includes_the_a1_columns(tmp_path, monkeypatch):
    """main()'s CSV export must carry the new columns -- the analysis
    harness's whole reason for existing is out/replay_detections.csv."""
    cache_dir = tmp_path / "cache"
    _write_fixture_cache(cache_dir)
    out_path = tmp_path / "out.csv"
    db_path = tmp_path / "journal.db"

    monkeypatch.setattr(replay, "WATCHLIST", [SYMBOL])
    monkeypatch.setattr(sys, "argv", [
        "replay.py", "--cache-dir", str(cache_dir), "--out", str(out_path), "--db-path", str(db_path),
    ])
    replay.main()

    import csv as csv_mod
    with out_path.open(newline="", encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        assert "pct_from_prior_close" in reader.fieldnames
        assert "pct_from_prior_close_status" in reader.fieldnames
        rows = list(reader)
    assert len(rows) >= 1
    assert rows[0]["pct_from_prior_close_status"] == "AVAILABLE"

    import sqlite3
    conn = sqlite3.connect(db_path)
    db_row = conn.execute(
        "SELECT pct_from_prior_close, pct_from_prior_close_status FROM detections WHERE symbol = ?", (SYMBOL,)
    ).fetchone()
    assert db_row == (10.0, "AVAILABLE")
