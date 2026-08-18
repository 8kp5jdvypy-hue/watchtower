"""Tests for scripts/verify_extreme_mover_evidence.py (Proposal 3
VPS validation) -- the discovery+replay half only. Calls the real
shipped tradebot.guard.extreme_mover_evidence(), so these tests double
as an end-to-end regression check on realistic multi-bar sessions, not
just guard.py's own unit-level fixtures.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_extreme_mover_evidence as script
from tradebot.detectors import Bar
from tradebot.marketdata import write_bars_csv


def _session_bars(symbol: str, prior_close: float, closes: list[float], volumes: list[int]) -> list[Bar]:
    base = datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc)
    bars = [Bar(symbol, base, prior_close, prior_close + 0.01, prior_close - 0.01, prior_close, volume=1000)]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        ts = base + timedelta(minutes=5 * (i + 1))
        bars.append(Bar(symbol, ts, c, c + 0.02, c - 0.02, c, volume=v))
    return bars


def test_finds_and_verifies_a_pltr_shaped_mover(tmp_path):
    """The doc's own worked example: opened 0.87, held near 1.295
    (+48.8%) across two consecutive bars on real volume."""
    bars = _session_bars("PLTR", 0.87, [1.20, 1.295, 1.30], [400_000, 412_000, 380_000])
    write_bars_csv(tmp_path / "PLTR" / "intraday_2026-08-04.csv", bars)

    sessions = script.find_mover_sessions(tmp_path)

    assert len(sessions) == 1
    s = sessions[0]
    assert s.symbol == "PLTR"
    assert s.verified is not None
    bar_index, evidence = s.verified
    # verifies as soon as bars[1] (1.20) and bars[2] (1.295) both clear
    # the line and sit within 10% of each other -- bar3 isn't needed
    assert bar_index == 2
    assert evidence.gap_pct > 0.25
    assert evidence.verified_volume == 400_000 + 412_000


def test_a_single_print_spike_never_verifies(tmp_path):
    bars = _session_bars("BADPRINT", 10.00, [15.0, 10.05, 10.02], [50_000, 40_000, 41_000])
    write_bars_csv(tmp_path / "BADPRINT" / "intraday_2026-08-04.csv", bars)

    sessions = script.find_mover_sessions(tmp_path)

    assert len(sessions) == 1
    assert sessions[0].verified is None


def test_no_sessions_when_nothing_crosses_the_line(tmp_path):
    bars = _session_bars("QUIET", 100.0, [101.0, 102.0, 101.5], [10_000, 10_000, 10_000])
    write_bars_csv(tmp_path / "QUIET" / "intraday_2026-08-04.csv", bars)

    assert script.find_mover_sessions(tmp_path) == []


def test_empty_cache_dir_returns_no_sessions(tmp_path):
    assert script.find_mover_sessions(tmp_path) == []
