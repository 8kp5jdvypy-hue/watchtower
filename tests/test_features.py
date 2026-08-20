"""Tests for tradebot.features — the A1 shared feature primitive.

See docs/open-awareness-proposals-2026-08.md: experiment A1 records
prior-close displacement as an audit feature only, never a detector or
scoring input."""
from __future__ import annotations

from tradebot.features import FeatureAvailability, PctFromPriorClose, pct_from_prior_close


def test_pct_from_prior_close_up_move_is_signed_percentage_points():
    result = pct_from_prior_close(current_close=110.0, prior_close=100.0)
    assert result.availability is FeatureAvailability.AVAILABLE
    assert result.value == 10.0  # +10.0, not 0.10 -- percentage POINTS
    assert result.status == "AVAILABLE"
    assert result.reason is None


def test_pct_from_prior_close_down_move_is_negative():
    result = pct_from_prior_close(current_close=90.0, prior_close=100.0)
    assert result.value == -10.0
    assert result.status == "AVAILABLE"


def test_pct_from_prior_close_large_move_matches_hand_computed_value():
    # prior 62.96 -> current 174.38 is approximately +176.97 percentage points.
    result = pct_from_prior_close(current_close=174.38, prior_close=62.96)
    assert result.availability is FeatureAvailability.AVAILABLE
    assert round(result.value, 2) == 176.97


def test_pct_from_prior_close_unavailable_when_prior_close_is_none():
    result = pct_from_prior_close(current_close=100.0, prior_close=None)
    assert result.availability is FeatureAvailability.UNAVAILABLE
    assert result.value is None
    assert result.reason == "no_prior_close"
    assert result.status == "UNAVAILABLE:no_prior_close"


def test_pct_from_prior_close_unavailable_when_prior_close_is_zero():
    result = pct_from_prior_close(current_close=100.0, prior_close=0.0)
    assert result.availability is FeatureAvailability.UNAVAILABLE
    assert result.value is None
    assert result.reason == "invalid_prior_close"
    assert result.status == "UNAVAILABLE:invalid_prior_close"


def test_pct_from_prior_close_unavailable_when_prior_close_is_negative():
    """A negative prior_close is a degenerate/bad daily bar -- never
    fabricates a value from it, same discipline as the zero case."""
    result = pct_from_prior_close(current_close=100.0, prior_close=-5.0)
    assert result.availability is FeatureAvailability.UNAVAILABLE
    assert result.reason == "invalid_prior_close"


def test_pct_from_prior_close_no_move_is_zero_not_unavailable():
    """Exactly flat vs. prior close is a real, available zero -- not to
    be confused with the unavailable case."""
    result = pct_from_prior_close(current_close=100.0, prior_close=100.0)
    assert result.availability is FeatureAvailability.AVAILABLE
    assert result.value == 0.0


def test_pct_from_prior_close_is_deterministic_and_pure():
    """Same inputs, same output, every time -- no I/O, no clock, no
    global state (see CLAUDE.md)."""
    a = pct_from_prior_close(123.45, 100.0)
    b = pct_from_prior_close(123.45, 100.0)
    assert a == b


def test_pct_from_prior_close_dataclass_is_frozen():
    result = pct_from_prior_close(110.0, 100.0)
    assert isinstance(result, PctFromPriorClose)
    try:
        result.value = 999.0  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except AttributeError:
        pass
