"""Tests for tradebot.formatting.guard.validate_alert_data() — the data
integrity check run before any alert is sent. Each rejection case gets
its own test; the goal is that a corrupted quote or a decimal-error
anchor can never reach a subscriber unexplained.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

from tradebot.alerts import Cluster
from tradebot.detectors import DailyAnchors
from tradebot.formatting.guard import validate_alert_data
from tradebot.marketdata import Quote


def _anchors(**overrides) -> DailyAnchors:
    fields = dict(
        symbol="GOOGL", session_date=date(2026, 7, 23), prior_close=377.68,
        prior_high=384.44, prior_low=379.50, opening_range_high=380.20,
        opening_range_low=378.90, opening_range_volume=250_000,
        swing_high=386.10, swing_low=365.00, avg_cum_volume_by_bar={},
    )
    fields.update(overrides)
    return DailyAnchors(**fields)


def _quote(**overrides) -> Quote:
    fields = dict(symbol="GOOGL", ts=datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc), bid=365.98, ask=366.02, last=366.00)
    fields.update(overrides)
    return Quote(**fields)


def _cluster(**overrides) -> Cluster:
    fields = dict(
        id="abc123", ts_utc="2026-07-23T16:05:00+00:00", session="2026-07-23", symbol="GOOGL",
        kinds="level_break", headlines="h", primary_headline="h", score=15.77, tier="high",
        close=366.00, atr14=1.77, trend="down", code_version="f665fba",
    )
    fields.update(overrides)
    return Cluster(**fields)


def test_passes_on_internally_consistent_data():
    assert validate_alert_data(_cluster(), _anchors(), _quote()) is None


def test_rejects_null_cluster_close():
    reason = validate_alert_data(_cluster(close=None), _anchors(), _quote())
    assert reason is not None and "cluster.close" in reason


def test_rejects_nan_cluster_score():
    reason = validate_alert_data(_cluster(score=float("nan")), _anchors(), _quote())
    assert reason is not None and "cluster.score" in reason


def test_rejects_null_quote_bid():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=None))
    assert reason is not None and "quote.bid" in reason


def test_rejects_nan_quote_last():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(last=float("nan")))
    assert reason is not None and "quote.last" in reason


def test_rejects_null_anchor_prior_close():
    reason = validate_alert_data(_cluster(), _anchors(prior_close=None), _quote())
    assert reason is not None and "anchors.prior_close" in reason


def test_rejects_null_anchor_opening_range():
    reason = validate_alert_data(_cluster(), _anchors(opening_range_high=None), _quote())
    assert reason is not None and "anchors.opening_range_high" in reason


def test_rejects_crossed_quote_bid_greater_than_ask():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=367.00, ask=366.00))
    assert reason is not None and "crossed quote" in reason


def test_rejects_last_price_far_outside_the_session_range():
    # every anchor band tops out around 386; a "last" of 900 is corrupted data, not a real move
    reason = validate_alert_data(_cluster(), _anchors(), _quote(last=900.0))
    assert reason is not None and "quote.last" in reason and "outside plausible session range" in reason


def test_rejects_cluster_close_with_no_overlap_to_the_known_range():
    reason = validate_alert_data(_cluster(close=1.0), _anchors(), _quote())
    assert reason is not None and "cluster.close" in reason and "outside plausible session range" in reason


def test_allows_a_real_breakout_that_legitimately_clears_the_opening_range():
    # a genuine breakout can print outside the opening range/prior range —
    # the guard is a sanity check against corruption, not a tight band
    reason = validate_alert_data(_cluster(close=395.0), _anchors(), _quote(bid=394.98, ask=395.02, last=395.0))
    assert reason is None


def test_never_raises_on_nan_inputs_it_rejects():
    # a NaN comparison (nan > x) is always False in Python — make sure the
    # null/NaN guard runs before any arithmetic that could misbehave on it
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=float("nan")))
    assert reason is not None
