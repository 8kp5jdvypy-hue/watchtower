"""Tests for tradebot.detectors — pure detector functions."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradebot.detectors import (
    DETECTORS,
    Tier,
    build_anchors,
    gap,
    level_break,
    range_expansion,
    rvol_spike,
    score_cluster,
    tier_for_score,
)
from tradebot.marketdata import Bar

SYMBOL = "TEST"
SESSION = date(2026, 6, 15)
OPEN0 = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)


def _bar(i: int, close: float, volume: int = 10_000, spread: float = 1.0) -> Bar:
    ts = OPEN0 + timedelta(minutes=5 * i)
    return Bar(SYMBOL, ts, close, close + spread, close - spread, close, volume)


def _flat_anchors(prior_close: float = 95.0) -> "object":
    prior_daily = [Bar(SYMBOL, OPEN0 - timedelta(days=1), prior_close, prior_close + 1, prior_close - 1, prior_close, 500_000)]
    opening_range = [_bar(0, 100.0)]
    return build_anchors(SYMBOL, SESSION, prior_daily, opening_range, historical_session_bars=[])


def test_level_break_fires_once_at_the_crossing_not_on_every_later_bar():
    anchors = _flat_anchors(prior_close=95.0)  # prior_high = 96
    # 15 bars near 100 to give ATR a stable, small baseline before the break
    bars = [_bar(i, 100.0 + (i % 2) * 0.05) for i in range(16)]
    for i in range(1, 16):
        assert level_break(bars[: i + 1], anchors) is None

    # bar 16: price jumps well past prior_high (96) — should fire now
    breakout = bars + [_bar(16, 130.0)]
    d = level_break(breakout, anchors)
    assert d is not None
    assert d.kind == "level_break"

    # bar 17: price stays up at the same broken level — must NOT re-fire
    still_broken = breakout + [_bar(17, 130.5)]
    assert level_break(still_broken, anchors) is None

    # bar 18: still broken — still must not re-fire
    still_broken_2 = still_broken + [_bar(18, 131.0)]
    assert level_break(still_broken_2, anchors) is None


def test_level_break_refires_on_a_fresh_crossing_after_retreating():
    anchors = _flat_anchors(prior_close=95.0)
    bars = [_bar(i, 100.0) for i in range(16)]
    breakout = bars + [_bar(16, 130.0)]
    assert level_break(breakout, anchors) is not None

    # retreat back under the level for a stretch
    retreat = breakout + [_bar(i, 100.0) for i in range(17, 30)]
    assert level_break(retreat, anchors) is None

    # cross again — this is a fresh event, should fire again
    second_break = retreat + [_bar(30, 130.0)]
    d = level_break(second_break, anchors)
    assert d is not None


def test_level_break_needs_at_least_two_bars():
    anchors = _flat_anchors()
    assert level_break([_bar(0, 200.0)], anchors) is None


def test_gap_fires_only_on_the_first_bar():
    anchors = _flat_anchors(prior_close=95.0)
    first = _bar(0, 105.0)  # gapped well above prior_close (95) relative to its own range
    assert gap([first], anchors) is not None
    assert gap([first, _bar(1, 105.0)], anchors) is None


def _anchors_with_volume_history() -> "object":
    prior_daily = [Bar(SYMBOL, OPEN0 - timedelta(days=1), 95, 96, 94, 95, 500_000)]
    history = [[_bar(i, 100.0, volume=10_000) for i in range(3)]]
    return build_anchors(SYMBOL, SESSION, prior_daily, [_bar(0, 100.0)], history)


def test_rvol_spike_needs_a_baseline_from_history():
    anchors_no_history = _flat_anchors()
    bars = [_bar(0, 100.0, volume=1_000_000), _bar(1, 100.0, volume=1_000_000)]
    assert rvol_spike(bars, anchors_no_history) is None  # no avg_cum_volume_by_bar yet

    anchors_with_history = _anchors_with_volume_history()
    calm_then_spike = [_bar(0, 100.0, volume=10_000), _bar(1, 100.0, volume=1_000_000)]
    spike = rvol_spike(calm_then_spike, anchors_with_history)
    assert spike is not None
    assert spike.score > 3.0


def test_rvol_spike_fires_once_not_on_every_bar_while_elevated():
    anchors = _anchors_with_volume_history()
    bars = [_bar(0, 100.0, volume=10_000), _bar(1, 100.0, volume=1_000_000)]
    assert rvol_spike(bars, anchors) is not None

    # cumulative volume only ever grows — the next bar is still "spiking"
    # relative to baseline, but it's not a fresh crossing, so must not re-fire
    still_elevated = bars + [_bar(2, 100.0, volume=10_000)]
    assert rvol_spike(still_elevated, anchors) is None


def test_range_expansion_fires_on_an_outsized_bar():
    anchors = _flat_anchors()
    calm = [_bar(i, 100.0, spread=0.1) for i in range(15)]
    wide = calm + [Bar(SYMBOL, OPEN0 + timedelta(minutes=5 * 15), 100, 110, 90, 100, 10_000)]
    d = range_expansion(wide, anchors)
    assert d is not None
    assert d.kind == "range_expansion"


def test_score_cluster_rewards_corroboration_but_caps_it_below_double_counting():
    single = score_cluster([_fake_detection(4.0)])
    corroborated = score_cluster([_fake_detection(4.0), _fake_detection(2.0)])
    assert single == 4.0
    assert corroborated > 4.0
    assert corroborated < 4.0 + 2.0  # partial credit, not full double-counting


def test_tier_for_score_matches_the_configured_thresholds():
    assert tier_for_score(100.0) == Tier.HIGH
    assert tier_for_score(0.0) == Tier.LOW


def _fake_detection(score: float):
    from tradebot.detectors import Detection

    return Detection(SYMBOL, "fake", OPEN0, score, "fake", {})
