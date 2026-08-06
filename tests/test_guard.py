"""Tests for tradebot.guard.validate_alert_data() — the data
integrity check every alert must pass before publish. One test per
rejection rule (see guard.py's module docstring for why each rule
exists); the goal is that a corrupted, stale, or self-contradictory
alert can never reach a subscriber unexplained.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from tradebot.alerts import Cluster
from tradebot.detectors import DailyAnchors, Detection
from tradebot.guard import validate_alert_data
from tradebot.marketdata import Bar, Quote


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


def _bars(last_low=365.90, last_high=366.50):
    # A GOOGL breakdown from ~380 down through $370 to ~366 — the session's
    # own bars are the ground truth for [session_low, session_high], so
    # they must actually reach down to where quote/cluster.close sit for
    # the "consistent data" baseline to pass.
    base = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    return [
        Bar("GOOGL", base, 380.20, 380.50, 379.80, 380.00, volume=50_000),
        Bar("GOOGL", base + timedelta(minutes=5), 380.00, 380.10, 372.00, 372.50, volume=80_000),
        Bar("GOOGL", base + timedelta(minutes=10), 372.50, last_high, last_low, 366.00, volume=120_000),
    ]


def _primary_detection(**context_overrides) -> Detection:
    context = {"atr14": 1.77}
    context.update(context_overrides)
    return Detection(symbol="GOOGL", kind="level_break", ts=datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc),
                      score=15.77, headline="h", context=context)


def test_passes_on_internally_consistent_data():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(), bars=_bars(), now=None, primary_detection=_primary_detection())
    assert reason is None


# ---------------------------------------------------------------------- #
# Rule: last price outside [session_low, session_high]
# ---------------------------------------------------------------------- #


def test_rejects_last_price_outside_the_actual_session_range():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(last=900.0), bars=_bars())
    assert reason is not None and reason.startswith("last_outside_session_range") and "quote.last" in reason


def test_rejects_cluster_close_outside_the_actual_session_range():
    reason = validate_alert_data(_cluster(close=1.0), _anchors(), _quote(), bars=_bars())
    assert reason is not None and reason.startswith("last_outside_session_range") and "cluster.close" in reason


def test_allows_a_price_right_at_the_session_extreme():
    # boundary case: exactly at session_low should pass (inclusive), not
    # get rejected as "outside" by an off-by-one
    bars = _bars(last_low=366.00)
    reason = validate_alert_data(_cluster(close=366.00), _anchors(), _quote(last=366.00, bid=365.98, ask=366.02), bars=bars)
    assert reason is None


# ---------------------------------------------------------------------- #
# Rule: bid > ask
# ---------------------------------------------------------------------- #


def test_rejects_crossed_quote_bid_greater_than_ask():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=367.00, ask=366.00), bars=_bars())
    assert reason is not None and reason.startswith("crossed_quote")


# ---------------------------------------------------------------------- #
# Rule: spread > 5% of mid
# ---------------------------------------------------------------------- #


def test_rejects_a_spread_wider_than_5pct_of_mid():
    # mid ~366, spread must be <= ~18.3 to pass; use a spread of $30
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=351.00, ask=381.00), bars=_bars(last_low=350, last_high=382))
    assert reason is not None and reason.startswith("spread_too_wide")


def test_allows_a_tight_spread():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=365.99, ask=366.01), bars=_bars())
    assert reason is None


# ---------------------------------------------------------------------- #
# Rule: required numeric field is None / NaN / inf
# ---------------------------------------------------------------------- #


def test_rejects_null_cluster_close():
    reason = validate_alert_data(_cluster(close=None), _anchors(), _quote(), bars=_bars())
    assert reason is not None and reason.startswith("bad_numeric_field") and "cluster.close" in reason


def test_rejects_nan_quote_last():
    reason = validate_alert_data(_cluster(), _anchors(), _quote(last=float("nan")), bars=_bars())
    assert reason is not None and reason.startswith("bad_numeric_field") and "quote.last" in reason


def test_rejects_infinite_cluster_score():
    reason = validate_alert_data(_cluster(score=float("inf")), _anchors(), _quote(), bars=_bars())
    assert reason is not None and reason.startswith("bad_numeric_field") and "cluster.score" in reason


def test_rejects_null_anchor_opening_range():
    reason = validate_alert_data(_cluster(), _anchors(opening_range_high=None), _quote(), bars=_bars())
    assert reason is not None and reason.startswith("bad_numeric_field") and "anchors.opening_range_high" in reason


def test_rejects_non_positive_prior_close():
    reason = validate_alert_data(_cluster(), _anchors(prior_close=0.0), _quote(), bars=_bars())
    assert reason is not None and reason.startswith("bad_numeric_field") and "prior_close" in reason


def test_never_raises_on_nan_inputs_it_rejects():
    # a NaN comparison (nan > x) is always False in Python — the null/NaN/inf
    # guard must run before any arithmetic that could misbehave on it
    reason = validate_alert_data(_cluster(), _anchors(), _quote(bid=float("nan")), bars=_bars())
    assert reason is not None


# ---------------------------------------------------------------------- #
# Rule: same field appearing twice with different values — the exact bug
# class from the "$366.00 / ATR shown as both 0.88 and 1.77" report.
# ---------------------------------------------------------------------- #


def test_rejects_atr14_disagreeing_with_the_primary_detectors_own_headline_value():
    detection = _primary_detection(atr14=0.88)  # headline says "ATR(14)=0.88"...
    reason = validate_alert_data(_cluster(atr14=1.77), _anchors(), _quote(), bars=_bars(), primary_detection=detection)
    assert reason is not None
    assert reason.startswith("inconsistent_duplicate_field")
    assert "0.88" in reason and "1.77" in reason


def test_rejects_bar_range_disagreeing_with_the_actual_last_bar():
    bars = _bars()  # actual last bar high-low
    actual_range = bars[-1].high - bars[-1].low
    detection = _primary_detection(bar_range=actual_range + 50)  # headline claims a different range
    reason = validate_alert_data(_cluster(), _anchors(), _quote(), bars=bars, primary_detection=detection)
    assert reason is not None and reason.startswith("inconsistent_duplicate_field") and "bar_range" in reason


def test_allows_matching_atr14_between_headline_and_cluster():
    detection = _primary_detection(atr14=1.77)  # matches cluster.atr14
    reason = validate_alert_data(_cluster(atr14=1.77), _anchors(), _quote(), bars=_bars(), primary_detection=detection)
    assert reason is None


def test_skips_duplicate_field_check_when_no_primary_detection_given():
    # callers that don't have a Detection handy (or older tests) shouldn't
    # be forced to pass one — the check simply doesn't run
    reason = validate_alert_data(_cluster(atr14=1.77), _anchors(), _quote(), bars=_bars(), primary_detection=None)
    assert reason is None


# ---------------------------------------------------------------------- #
# Rule: quote timestamp >60s old at send
# ---------------------------------------------------------------------- #


def test_rejects_a_stale_quote():
    quote = _quote(ts=datetime(2026, 7, 23, 16, 0, 0, tzinfo=timezone.utc))
    now = datetime(2026, 7, 23, 16, 1, 30, tzinfo=timezone.utc)  # 90s later
    reason = validate_alert_data(_cluster(), _anchors(), quote, bars=_bars(), now=now)
    assert reason is not None and reason.startswith("stale_quote")


def test_allows_a_fresh_quote():
    quote = _quote(ts=datetime(2026, 7, 23, 16, 0, 0, tzinfo=timezone.utc))
    now = datetime(2026, 7, 23, 16, 0, 30, tzinfo=timezone.utc)  # 30s later
    reason = validate_alert_data(_cluster(), _anchors(), quote, bars=_bars(), now=now)
    assert reason is None


def test_skips_staleness_check_when_now_is_not_given():
    # replay mode passes now=None — a historical quote.ts is definitionally
    # "stale" relative to real wall-clock time, so the check must not run
    quote = _quote(ts=datetime(2020, 1, 1, tzinfo=timezone.utc))
    reason = validate_alert_data(_cluster(), _anchors(), quote, bars=_bars(), now=None)
    assert reason is None


# ---------------------------------------------------------------------- #
# Rule: bar range != (high - low) — basic bar self-consistency
# ---------------------------------------------------------------------- #


def test_rejects_a_bar_with_high_below_low():
    bars = _bars()
    corrupted_last = Bar("GOOGL", bars[-1].ts, bars[-1].open, high=360.0, low=370.0, close=366.0, volume=1000)
    bars = bars[:-1] + [corrupted_last]
    reason = validate_alert_data(_cluster(), _anchors(), _quote(), bars=bars)
    assert reason is not None and reason.startswith("invalid_bar")


# ---------------------------------------------------------------------- #
# Rule: prior close >25% from last
# ---------------------------------------------------------------------- #


def test_rejects_an_implausible_gap_from_prior_close():
    # prior_close 377.68, last 200.00 is a ~47% move in one session
    reason = validate_alert_data(_cluster(close=200.0), _anchors(), _quote(last=200.0, bid=199.98, ask=200.02),
                                  bars=_bars(last_low=199, last_high=380.5))
    assert reason is not None and reason.startswith("extreme_prior_close_gap")


def test_allows_a_large_but_plausible_gap():
    # a real ~15% move is well within the sanity ceiling and should pass
    reason = validate_alert_data(_cluster(close=320.0), _anchors(), _quote(last=320.0, bid=319.98, ask=320.02),
                                  bars=_bars(last_low=319, last_high=380.5))
    assert reason is None
