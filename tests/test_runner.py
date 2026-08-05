"""Tests for the pure/testable pieces of tradebot.runner.

The full run_replay()/run_live() loops are exercised via an actual
--replay-date run (see the session transcript), not unit tests — they're
integration-shaped (calendars, journaling, alerting all wired together).
These tests cover the pieces that are meaningfully testable in isolation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.alerts import Decision
from tradebot.journal import TierPerformance
from tradebot.marketdata import Bar
from tradebot.runner import HeartbeatStats, is_halted_bar, is_stale, session_bounds


def test_is_stale_true_past_the_threshold():
    latest_close = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    now = latest_close + timedelta(seconds=91)
    assert is_stale(latest_close, now, max_seconds=90) is True


def test_is_stale_false_within_the_threshold():
    latest_close = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    now = latest_close + timedelta(seconds=89)
    assert is_stale(latest_close, now, max_seconds=90) is False


def test_is_halted_bar_detects_zero_volume():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    assert is_halted_bar(Bar("BE", ts, 10, 10, 10, 10, volume=0)) is True
    assert is_halted_bar(Bar("BE", ts, 10, 10.5, 9.5, 10.2, volume=100)) is False


def test_session_bounds_regular_day():
    open_ts, close_ts = session_bounds(date(2026, 7, 23))
    assert open_ts.hour == 13 and open_ts.minute == 30  # 09:30 ET in UTC (EDT)
    assert close_ts.hour == 20 and close_ts.minute == 0  # 16:00 ET in UTC (EDT)


def test_session_bounds_honors_early_close():
    # day after Thanksgiving 2026 — a known 13:00 ET early close
    open_ts, close_ts = session_bounds(date(2026, 11, 27))
    assert close_ts.hour == 18  # 13:00 ET in UTC (EST, UTC-5)


def test_session_bounds_rejects_non_trading_day():
    with pytest.raises(ValueError):
        session_bounds(date(2026, 7, 25))  # a Saturday


def test_heartbeat_stats_summary_includes_tier_and_suppression_counts():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    stats = HeartbeatStats(start_time=start, session_date=date(2026, 7, 23))
    stats.record_cluster("high", Decision.SEND)
    stats.record_cluster("high", Decision.SUPPRESS_COOLDOWN)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)
    stats.data_gaps.append("BE: no prior daily bar cached")

    text = stats.summary_text(start + timedelta(hours=6, minutes=30))
    assert "6:30:00" in text
    assert "'high': 2" in text
    assert "'log': 1" in text
    assert "cooldown_active" in text
    assert "Data gaps: 1" in text
    assert "BE: no prior daily bar cached" in text


def test_heartbeat_stats_summary_includes_tier_performance_when_provided():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    stats = HeartbeatStats(start_time=start, session_date=date(2026, 7, 23))
    tier_perf = {
        "high": TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30),
        "medium": TierPerformance(tier="medium", sample_size=315, continuation_rate=0.492, avg_return_pct=-0.014, offset_min=30),
    }
    text = stats.summary_text(start + timedelta(hours=1), tier_perf=tier_perf)
    assert "Tier track record (+30m" in text
    assert "HIGH: 59.5% continued (n=42), avg +0.36%" in text
    assert "MEDIUM: 49.2% continued (n=315), avg -0.01%" in text


def test_heartbeat_stats_summary_omits_tier_performance_when_empty():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    stats = HeartbeatStats(start_time=start, session_date=date(2026, 7, 23))
    text = stats.summary_text(start + timedelta(hours=1), tier_perf={})
    assert "Tier track record" not in text
