"""Tests for scripts/purge_and_backfill_runts.py (Proposal 5b) --
the runt-identification half only. Reuses the shipped plausibility-floor
functions (Proposal 5c) as the source of truth for "is this a runt", so
these tests double as a regression check that the two proposals agree.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import purge_and_backfill_runts as script
from tradebot.detectors import Bar
from tradebot.marketdata import write_bars_csv


def _bars(session_date: date, n: int, volume: int) -> list[Bar]:
    rth_open = datetime(session_date.year, session_date.month, session_date.day, 13, 30, tzinfo=timezone.utc)
    return [
        Bar(symbol="SPY", ts=rth_open + timedelta(minutes=5 * i), open=1, high=1, low=1, close=1, volume=volume)
        for i in range(n)
    ]


def test_find_runts_flags_a_low_volume_pair_against_healthy_history(tmp_path):
    """The exact incident shape: ~1M vs ~40M normal SPY RTH volume on
    2026-08-11/12 (docs/open-awareness-proposals-2026-08.md's HANDOFF
    section)."""
    for i in range(7):
        d = date(2026, 8, 4) + timedelta(days=i)
        write_bars_csv(tmp_path / "SPY" / f"intraday_{d.isoformat()}.csv", _bars(d, 78, 40_000_000))
    write_bars_csv(tmp_path / "SPY" / "intraday_2026-08-11.csv", _bars(date(2026, 8, 11), 78, 1_000_000))
    write_bars_csv(tmp_path / "SPY" / "intraday_2026-08-12.csv", _bars(date(2026, 8, 12), 78, 1_000_000))

    runts = script.find_runts(tmp_path, ["SPY"], [date(2026, 8, 11), date(2026, 8, 12)])

    assert {(sym, d) for sym, d, _ in runts} == {("SPY", date(2026, 8, 11)), ("SPY", date(2026, 8, 12))}
    assert all(reason.startswith("implausible_volume:") for _, _, reason in runts)


def test_find_runts_is_quiet_on_a_healthy_pair(tmp_path):
    for i in range(9):
        d = date(2026, 8, 4) + timedelta(days=i)
        write_bars_csv(tmp_path / "SPY" / f"intraday_{d.isoformat()}.csv", _bars(d, 78, 40_000_000))

    runts = script.find_runts(tmp_path, ["SPY"], [date(2026, 8, 11), date(2026, 8, 12)])

    assert runts == []


def test_find_runts_skips_a_symbol_date_with_no_cached_file(tmp_path):
    """A symbol that simply has no file for a candidate date (never
    fetched) is not this script's concern -- fetch_cache.py's normal
    backfill covers it, not a purge."""
    runts = script.find_runts(tmp_path, ["SPY"], [date(2026, 8, 11)])
    assert runts == []


def test_main_apply_deletes_only_the_runt_files(tmp_path, capsys, monkeypatch):
    for i in range(7):
        d = date(2026, 8, 4) + timedelta(days=i)
        write_bars_csv(tmp_path / "SPY" / f"intraday_{d.isoformat()}.csv", _bars(d, 78, 40_000_000))
    runt_path = tmp_path / "SPY" / "intraday_2026-08-11.csv"
    healthy_path = tmp_path / "SPY" / "intraday_2026-08-12.csv"
    write_bars_csv(runt_path, _bars(date(2026, 8, 11), 78, 1_000_000))
    write_bars_csv(healthy_path, _bars(date(2026, 8, 12), 78, 40_000_000))

    monkeypatch.setattr(
        sys, "argv",
        ["purge_and_backfill_runts.py", "--symbols", "SPY", "--dates", "2026-08-11,2026-08-12", "--cache-dir", str(tmp_path), "--apply"],
    )
    script.main()

    assert not runt_path.exists()
    assert healthy_path.exists()  # the genuinely healthy file must survive


def test_main_report_only_by_default_deletes_nothing(tmp_path, monkeypatch):
    for i in range(7):
        d = date(2026, 8, 4) + timedelta(days=i)
        write_bars_csv(tmp_path / "SPY" / f"intraday_{d.isoformat()}.csv", _bars(d, 78, 40_000_000))
    runt_path = tmp_path / "SPY" / "intraday_2026-08-11.csv"
    write_bars_csv(runt_path, _bars(date(2026, 8, 11), 78, 1_000_000))

    monkeypatch.setattr(
        sys, "argv",
        ["purge_and_backfill_runts.py", "--symbols", "SPY", "--dates", "2026-08-11", "--cache-dir", str(tmp_path)],
    )
    script.main()

    assert runt_path.exists()
