"""Tests for tradebot.detectors — pure detector functions."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.detectors import (
    CONTEXT_DETECTORS,
    DETECTORS,
    Tier,
    build_anchors,
    gap,
    level_break,
    range_expansion,
    relative_strength_break,
    round_number_break,
    rvol_spike,
    score_cluster,
    tier_for_score,
    vwap,
    vwap_break,
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
    anchors = _flat_anchors(prior_close=95.0)  # prior_high=96, prior_low=94, range=2
    first = _bar(0, 105.0)  # gapped well above prior_close (95) relative to the prior day's range
    assert gap([first], anchors) is not None
    assert gap([first, _bar(1, 105.0)], anchors) is None


def test_gap_uses_prior_session_range_not_first_bars_own_thin_range():
    """Regression: gap()'s proxy is the prior session's range, not the
    first bar's own range. Real example that motivated this: USO
    premarket prints with a near-zero own-range (thin volume) used to
    either blow up to an astronomical score or get floored to zero. A
    thin opening print with a genuine gap must still fire correctly,
    scored against the prior day's range — not its own tiny range."""
    anchors = _flat_anchors(prior_close=95.0)  # prior_high=96, prior_low=94, range=2
    thin_first = _bar(0, 105.0, spread=0.001)  # tiny own-range, real gap up from 95
    d = gap([thin_first], anchors)
    assert d is not None
    assert d.score == pytest.approx((105 - 95) / 2)  # gap_size / prior range, not / thin own-range


def test_gap_returns_none_when_prior_session_range_is_degenerate():
    """If the prior day itself was flat (high == low), there's no
    meaningful proxy at all — must return None, never fabricate a score."""
    flat_prior_daily = [Bar(SYMBOL, OPEN0 - timedelta(days=1), 95.0, 95.0, 95.0, 95.0, 500_000)]
    anchors = build_anchors(SYMBOL, SESSION, flat_prior_daily, [_bar(0, 100.0)], historical_session_bars=[])
    first = _bar(0, 105.0)  # a perfectly normal bar — the degeneracy is in the prior day, not this bar
    assert gap([first], anchors) is None


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


def test_level_break_includes_swing_high_from_days_before_yesterday():
    prior_daily = [
        Bar(SYMBOL, OPEN0 - timedelta(days=3), 105, 110.0, 104, 108, 500_000),  # oldest day: high=110
        Bar(SYMBOL, OPEN0 - timedelta(days=2), 95, 97.0, 94, 96, 500_000),
        Bar(SYMBOL, OPEN0 - timedelta(days=1), 95, 96.0, 94, 95, 500_000),  # "yesterday": prior_high=96
    ]
    opening_range = [_bar(0, 90.0)]  # opening_range_high ~91, well below everything else
    anchors = build_anchors(SYMBOL, SESSION, prior_daily, opening_range, historical_session_bars=[])
    assert anchors.prior_high == 96.0
    assert anchors.swing_high == 110.0

    # calm bars already sit above prior_high/opening_range_high — those are
    # "already broken" from the start and won't re-fire
    calm = [_bar(i, 100.0) for i in range(15)]
    for i in range(1, 15):
        assert level_break(calm[: i + 1], anchors) is None

    # jump past swing_high specifically — the only fresh crossing this bar
    breakout = calm + [_bar(15, 115.0)]
    d = level_break(breakout, anchors)
    assert d is not None
    assert d.context["level_name"] == "swing_high"


def test_vwap_is_volume_weighted_average_of_typical_price():
    # with _bar()'s default spread, high/low are symmetric around close, so
    # typical price ((high+low+close)/3) reduces exactly to close
    bars = [_bar(0, 100.0, volume=100), _bar(1, 200.0, volume=300)]
    expected = (100.0 * 100 + 200.0 * 300) / (100 + 300)
    assert vwap(bars) == pytest.approx(expected)


def test_vwap_none_with_no_bars():
    assert vwap([]) is None


def test_vwap_break_fires_once_at_the_crossing_not_on_every_later_bar():
    anchors = _flat_anchors()
    calm = [_bar(i, 100.0) for i in range(15)]  # VWAP ~100, small ATR baseline
    for i in range(1, 15):
        assert vwap_break(calm[: i + 1], anchors) is None

    jump = calm + [_bar(15, 150.0)]
    d = vwap_break(jump, anchors)
    assert d is not None
    assert d.kind == "vwap_break"

    still_up = jump + [_bar(16, 152.0)]
    assert vwap_break(still_up, anchors) is None


def test_round_number_break_fires_on_a_fresh_crossing():
    anchors = _flat_anchors()
    # under $100 the round-number spacing is $5; calm bars sit at 97, well
    # inside a $5 band, so no crossing happens yet
    calm = [_bar(i, 97.0, spread=0.2) for i in range(15)]
    for i in range(1, 15):
        assert round_number_break(calm[: i + 1], anchors) is None

    crossing = calm + [_bar(15, 101.0, spread=0.2)]
    d = round_number_break(crossing, anchors)
    assert d is not None
    assert d.kind == "round_number_break"

    still_above = crossing + [_bar(16, 102.0, spread=0.2)]
    assert round_number_break(still_above, anchors) is None


def test_all_detectors_are_registered():
    kinds = {d.__name__ for d in DETECTORS}
    assert kinds == {
        "level_break", "rvol_spike", "range_expansion", "vwap_break", "round_number_break", "gap",
    }


def test_all_context_detectors_are_registered():
    kinds = {d.__name__ for d in CONTEXT_DETECTORS}
    assert kinds == {"relative_strength_break"}


def _spy_bar(i: int, close: float) -> Bar:
    ts = OPEN0 + timedelta(minutes=5 * i)
    return Bar("SPY", ts, close, close + 1.0, close - 1.0, close, 10_000)


def test_relative_strength_break_fires_when_symbol_diverges_from_a_flat_market():
    anchors = _flat_anchors(prior_close=95.0)
    bars = [_bar(i, 100.0 + (i % 2) * 0.05) for i in range(16)]  # stable ATR baseline near 100
    spy_bars = [_spy_bar(i, 400.0) for i in range(16)]  # SPY dead flat throughout

    for i in range(1, 16):
        assert relative_strength_break(bars[: i + 1], anchors, {"SPY": spy_bars[: i + 1]}) is None

    breakout = bars + [_bar(16, 130.0)]  # TEST up ~30%, SPY unchanged
    spy_flat = spy_bars + [_spy_bar(16, 400.0)]
    d = relative_strength_break(breakout, anchors, {"SPY": spy_flat})
    assert d is not None
    assert d.kind == "relative_strength_break"
    assert d.context["market_proxy"] == "SPY"

    # stays diverged on the same side — must not re-fire
    still_diverged = breakout + [_bar(17, 131.0)]
    spy_flat_2 = spy_flat + [_spy_bar(17, 400.0)]
    assert relative_strength_break(still_diverged, anchors, {"SPY": spy_flat_2}) is None


def test_relative_strength_break_does_not_fire_when_symbol_moves_in_lockstep_with_the_market():
    anchors = _flat_anchors(prior_close=95.0)
    bars = [_bar(i, 100.0 + (i % 2) * 0.05) for i in range(16)] + [_bar(16, 130.0)]  # +30%
    spy_bars = [_spy_bar(i, 400.0) for i in range(16)] + [_spy_bar(16, 520.0)]  # SPY also +30%
    assert relative_strength_break(bars, anchors, {"SPY": spy_bars}) is None


def test_relative_strength_break_never_fires_on_the_proxy_symbol_itself():
    spy_anchors = build_anchors(
        "SPY", SESSION,
        [Bar("SPY", OPEN0 - timedelta(days=1), 95.0, 96.0, 94.0, 95.0, 500_000)],
        [_spy_bar(0, 100.0)], historical_session_bars=[],
    )
    bars = [_spy_bar(i, 100.0 + (i % 2) * 0.05) for i in range(16)] + [_spy_bar(16, 130.0)]
    assert relative_strength_break(bars, spy_anchors, {"SPY": bars}) is None


@pytest.mark.parametrize("market_bars", [None, {}, {"SPY": []}])
def test_relative_strength_break_returns_none_never_raises_on_missing_market_data(market_bars):
    anchors = _flat_anchors(prior_close=95.0)
    bars = [_bar(i, 100.0 + (i % 2) * 0.05) for i in range(16)] + [_bar(16, 130.0)]
    assert relative_strength_break(bars, anchors, market_bars) is None


def test_relative_strength_break_returns_none_when_the_proxy_has_not_caught_up_yet():
    anchors = _flat_anchors(prior_close=95.0)
    bars = [_bar(i, 100.0 + (i % 2) * 0.05) for i in range(16)] + [_bar(16, 130.0)]
    spy_bars = [_spy_bar(i, 400.0) for i in range(10)]  # shorter than bars — not aligned yet
    assert relative_strength_break(bars, anchors, {"SPY": spy_bars}) is None


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
