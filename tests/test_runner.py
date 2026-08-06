"""Tests for the pure/testable pieces of tradebot.runner.

The full run_replay()/run_live() loops are exercised via an actual
--replay-date run (see the session transcript), not unit tests — they're
integration-shaped (calendars, journaling, alerting all wired together).
These tests cover the pieces that are meaningfully testable in isolation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.alerts import AlertBudget, ConsoleAlerter, Decision
from tradebot.detectors import DailyAnchors, Detection
from tradebot.marketdata import Bar, Quote
from tradebot.journal import connect as journal_connect
import tradebot.runner as runner_mod
from tradebot.detectors import atr as compute_atr
from tradebot.runner import HeartbeatStats, evaluate_bar, is_halted_bar, is_stale, process_new_bar, session_bounds


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


def test_heartbeat_stats_record_cluster_tracks_tier_and_suppression_counts():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    stats = HeartbeatStats(start_time=start, session_date=date(2026, 7, 23))
    stats.record_cluster("high", Decision.SEND)
    stats.record_cluster("high", Decision.SUPPRESS_COOLDOWN)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)

    assert stats.tier_counts["high"] == 2
    assert stats.tier_counts["log"] == 1
    assert stats.suppression_counts["cooldown_active"] == 1
    # a plain SEND is not a suppression
    assert "send" not in stats.suppression_counts


def _high_tier_fixture():
    """A synthetic evaluate_bar() result scored well above TIER_HIGH, for
    exercising process_new_bar's SEND branch without needing to hand-craft
    real detector-triggering bars."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), 100.0, 100.5, 99.8, 100.2, volume=10_000)
    primary_detection = Detection("TSLA", "gap", bar.ts, 10.0, "a gap", {})
    result = {
        "ts": datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), "close": 100.2, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary_detection,
        "score": 10.0, "trend": "up", "detections": [primary_detection],
    }
    return anchors, bar, result


def test_process_new_bar_without_a_subscriber_hook_behaves_exactly_as_before(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol):
        raise NotImplementedError

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)
    # no subscriber_hook passed — must not raise, and behaves like the pre-hook implementation
    assert stats.tier_counts["high"] == 1


def test_process_new_bar_calls_subscriber_hook_with_the_cluster_and_rendered_text_on_a_high_send(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol):
        raise NotImplementedError

    calls = []
    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=lambda cluster, text: calls.append((cluster, text)),
    )

    assert len(calls) == 1
    cluster, text = calls[0]
    assert cluster.symbol == "TSLA" and cluster.tier == "high"
    assert "TSLA" in text


def test_process_new_bar_swallows_a_subscriber_hook_exception_without_dropping_the_alert(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol):
        raise NotImplementedError

    def broken_hook(cluster, text):
        raise RuntimeError("simulated fan-out failure")

    process_new_bar(  # must not raise
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=broken_hook,
    )
    assert any("fan-out failed" in e for e in stats.errors)
    row = conn.execute("SELECT alerted FROM detections").fetchone()
    assert row[0] == 1  # the alert itself still went out despite the hook blowing up


def test_evaluate_bar_cluster_atr14_matches_the_primary_detectors_own_atr():
    """Regression test for a real production bug: an alert showed ATR(14)
    as two different numbers in one message — the headline (built from a
    detector's own window) and the stats block (built from
    evaluate_bar's independently-recomputed atr(bars)) could disagree,
    because range_expansion scores against atr(bars[:-1]) (deliberately
    excluding the current, possibly-anomalous bar) while evaluate_bar used
    to compute atr(bars) (including it) for cluster.atr14. On a genuinely
    wide current bar those two windows can diverge sharply. The fix reuses
    whatever ATR the primary/headline detector actually used instead of
    recomputing a second, independent number."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=115.0, prior_low=85.0,
        opening_range_high=120.0, opening_range_low=80.0, opening_range_volume=10_000,
        swing_high=130.0, swing_low=70.0, avg_cum_volume_by_bar={},
    )
    base = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = []
    price = 100.0
    for i in range(16):
        bars.append(Bar("TSLA", base + timedelta(minutes=5 * i), price, price + 0.1, price - 0.1, price + 0.05, volume=1_000))
        price += 0.05
    # The current bar has a huge range relative to the last 16 quiet bars —
    # this is exactly what triggers range_expansion, and exactly the
    # scenario where atr(bars) vs atr(bars[:-1]) diverge sharply.
    bars.append(Bar("TSLA", base + timedelta(minutes=80), 100.8, 115.0, 95.0, 101.0, volume=5_000))

    result = evaluate_bar("TSLA", bars, anchors)

    assert result["primary_kind"] == "range_expansion"
    assert "ATR(14)=0.20" in result["primary_headline"]

    primary_detection = result["detections"][0]
    assert result["atr14"] == primary_detection.context["atr14"]

    # Prove this is a real behavior change, not a vacuous assertion: the
    # old buggy computation (atr on the full window, including the huge
    # current bar) is a materially different number.
    old_buggy_value = compute_atr(bars)
    assert result["atr14"] != old_buggy_value
    assert abs(old_buggy_value - result["atr14"]) > 1.0  # ~1.6 vs ~0.2 in this fixture


def test_process_new_bar_guard_rejection_logs_error_and_emits_a_metric(monkeypatch, caplog, tmp_path):
    from tradebot import metrics as metrics_mod

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_path)

    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def bad_quote_fn(symbol):
        # crossed quote — bid > ask — must trip the guard
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)

    def chain_fn(symbol):
        raise NotImplementedError

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, bad_quote_fn, chain_fn, stats)

    assert any("alert suppressed by data guard" in r.message and "rule=crossed_quote" in r.message for r in caplog.records)

    row = conn.execute("SELECT alerted, suppress_reason FROM detections").fetchone()
    assert row[0] == 0
    assert row[1].startswith("data_integrity_failed: crossed_quote")

    assert metrics_mod.read_all(metrics_path) == {"validator_rejection{rule=crossed_quote}": 1}
