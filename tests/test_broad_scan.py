"""Tests for tradebot.broad_scan — the Stage 1 cheap screen that cuts a
large universe down to a short list before the real (Stage 2, existing
detectors.py) evaluation runs."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from tradebot import metrics
from tradebot.broad_scan import (
    GAP_PCT_THRESHOLD,
    MOVE_PCT_THRESHOLD,
    RANGE_PCT_THRESHOLD,
    RVOL_THRESHOLD,
    CandidateScore,
    Snapshot,
    promote_candidates,
    run_stage1_screen,
    screen_snapshot,
)
from tradebot.detectors import Bar


def _snapshot(symbol="TEST", open_=100.0, high=100.5, low=99.5, close=100.0, prior_close=100.0, volume=1000, avg_volume=1000) -> Snapshot:
    return Snapshot(
        symbol=symbol, open=open_, high=high, low=low, close=close,
        prior_close=prior_close, volume=volume, avg_volume=avg_volume,
    )


def test_quiet_symbol_screens_to_none():
    assert screen_snapshot(_snapshot()) is None


def test_flags_unusual_volume():
    result = screen_snapshot(_snapshot(volume=RVOL_THRESHOLD * 1000, avg_volume=1000))
    assert result is not None
    assert "unusual_volume" in result.reasons


def test_flags_price_acceleration():
    result = screen_snapshot(_snapshot(close=104.0, prior_close=100.0))  # +4% >= 3% threshold
    assert result is not None
    assert "price_acceleration" in result.reasons


def test_flags_range_expansion():
    result = screen_snapshot(_snapshot(high=106.0, low=100.0, prior_close=100.0))  # 6% range
    assert result is not None
    assert "range_expansion" in result.reasons


def test_flags_gap():
    result = screen_snapshot(_snapshot(open_=104.0, close=104.2, prior_close=100.0))
    assert result is not None
    assert "gap" in result.reasons


def test_never_fabricates_a_score_without_a_real_baseline():
    assert screen_snapshot(_snapshot(avg_volume=0)) is None
    assert screen_snapshot(_snapshot(prior_close=0)) is None


def test_multiple_confirming_checks_score_higher_than_one_alone():
    one_reason = screen_snapshot(_snapshot(volume=RVOL_THRESHOLD * 1000))
    two_reasons = screen_snapshot(_snapshot(volume=RVOL_THRESHOLD * 1000, close=104.0, prior_close=100.0))
    assert two_reasons.score > one_reason.score
    assert set(two_reasons.reasons) == {"unusual_volume", "price_acceleration"}


def test_reasons_are_ordered_strongest_first():
    result = screen_snapshot(_snapshot(volume=RVOL_THRESHOLD * 5000, close=103.1, prior_close=100.0))
    assert result.reasons[0] == "unusual_volume"


def test_live_snapshot_requires_a_daily_bar_from_the_requested_session():
    from tradebot.broad_scan import build_snapshots_from_daily_bars

    def bars(symbol, last_day):
        return [
            Bar(symbol, datetime(2026, 8, day, 13, 30, tzinfo=timezone.utc), 100, 101, 99, 100, 1000)
            for day in range(last_day - 5, last_day + 1)
        ]

    snapshots = build_snapshots_from_daily_bars(
        {"CURRENT": bars("CURRENT", 25), "STALE": bars("STALE", 22)},
        session_date=date(2026, 8, 25),
    )

    assert [snapshot.symbol for snapshot in snapshots] == ["CURRENT"]


# ---------------------------------------------------------------------- #
# promote_candidates
# ---------------------------------------------------------------------- #


def test_promote_candidates_filters_by_threshold_and_sorts_descending():
    scores = [
        CandidateScore("A", score=0.5, reasons=()),
        CandidateScore("B", score=2.0, reasons=()),
        CandidateScore("C", score=1.0, reasons=()),
    ]
    promoted = promote_candidates(scores, threshold=1.0)
    assert [c.symbol for c in promoted] == ["B", "C"]


def test_promote_candidates_default_threshold_excludes_nothing_below_one():
    scores = [CandidateScore("A", score=0.99, reasons=())]
    assert promote_candidates(scores) == []


# ---------------------------------------------------------------------- #
# run_stage1_screen — real funnel metrics
# ---------------------------------------------------------------------- #


def test_run_stage1_screen_records_the_full_funnel_in_metrics(tmp_path):
    metrics_path = tmp_path / "metrics.json"
    snapshots = [
        _snapshot(symbol="QUIET1"),
        _snapshot(symbol="QUIET2"),
        _snapshot(symbol="LOUD", volume=RVOL_THRESHOLD * 1000),
    ]

    promoted = run_stage1_screen(snapshots, threshold=1.0, metrics_path=metrics_path)

    assert [c.symbol for c in promoted] == ["LOUD"]
    data = json.loads(metrics_path.read_text())
    assert data["universe_symbols_monitored"] == 3
    assert data["universe_candidates_created"] == 1
    assert data["universe_candidates_promoted"] == 1
    assert data["universe_stage1_runs"] == 1
    assert "universe_stage1_latency_ms_total" in data


def test_run_stage1_screen_accumulates_across_multiple_runs(tmp_path):
    metrics_path = tmp_path / "metrics.json"
    run_stage1_screen([_snapshot(symbol="A")], metrics_path=metrics_path)
    run_stage1_screen([_snapshot(symbol="B"), _snapshot(symbol="C")], metrics_path=metrics_path)

    data = json.loads(metrics_path.read_text())
    assert data["universe_symbols_monitored"] == 3  # 1 + 2, not overwritten
    assert data["universe_stage1_runs"] == 2


def test_metrics_increment_amount_defaults_to_one(tmp_path):
    """Regression check for metrics.increment's new `amount` parameter —
    every pre-existing call site (no amount passed) must still add
    exactly 1, unchanged."""
    path = tmp_path / "metrics.json"
    metrics.increment("some_counter", path=path)
    metrics.increment("some_counter", path=path)
    assert json.loads(path.read_text())["some_counter"] == 2
