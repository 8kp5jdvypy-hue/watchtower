"""Tests for tradebot.guard.validate_alert_data() — the data
integrity check every alert must pass before publish. One test per
rejection rule (see guard.py's module docstring for why each rule
exists); the goal is that a corrupted, stale, or self-contradictory
alert can never reach a subscriber unexplained.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.alerts import Cluster
from tradebot.detectors import DailyAnchors, Detection
from tradebot.guard import extreme_mover_evidence, spread_pct_of_mid, validate_alert_data
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


# ---------------------------------------------------------------------- #
# Proposal 3: extreme-mover persistence check
# (docs/open-awareness-proposals-2026-08.md)
# ---------------------------------------------------------------------- #


def _extreme_mover_bars(prior_close=100.0, close1=140.0, close2=138.0, volume=50_000, symbol="GOOGL"):
    """Prior bar at prior_close, then two consecutive bars closing beyond
    it -- the shape extreme_mover_evidence looks for. close1/close2
    default within 10% of each other so the happy path is the default."""
    base = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    lo = min(prior_close, close1, close2) - 1
    hi = max(prior_close, close1, close2) + 1
    return [
        Bar(symbol, base, prior_close, prior_close + 0.5, prior_close - 0.5, prior_close, volume=10_000),
        Bar(symbol, base + timedelta(minutes=5), prior_close, hi, lo, close1, volume=volume),
        Bar(symbol, base + timedelta(minutes=10), close1, hi, lo, close2, volume=volume),
    ]


def _quote_for_spread(last: float, spread_pct: float, ts=datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc)) -> Quote:
    half = last * spread_pct / 2
    return _quote(last=last, bid=last - half, ask=last + half, ts=ts)


def test_extreme_mover_evidence_none_within_the_25pct_ceiling():
    quote = _quote(last=110.0)  # 10% from prior_close=100 -- not extreme at all
    reason = extreme_mover_evidence(_extreme_mover_bars(close1=110.0, close2=109.0), _anchors(prior_close=100.0), quote)
    assert reason is None


def test_extreme_mover_evidence_none_without_two_bars():
    quote = _quote(last=140.0)
    assert extreme_mover_evidence(None, _anchors(prior_close=100.0), quote) is None
    assert extreme_mover_evidence([], _anchors(prior_close=100.0), quote) is None
    one_bar = _extreme_mover_bars()[-1:]
    assert extreme_mover_evidence(one_bar, _anchors(prior_close=100.0), quote) is None


def test_extreme_mover_evidence_none_when_a_persistence_bar_has_zero_volume():
    bars = _extreme_mover_bars(volume=0)
    quote = _quote(last=138.0)
    assert extreme_mover_evidence(bars, _anchors(prior_close=100.0), quote) is None


def test_extreme_mover_evidence_none_when_only_one_bar_crosses_the_line():
    # bars[-2] close (110 -- 10% from prior_close) never crosses 25%, even
    # though bars[-1] (140 -- 40%) does
    bars = _extreme_mover_bars(close1=110.0, close2=140.0)
    quote = _quote(last=140.0)
    assert extreme_mover_evidence(bars, _anchors(prior_close=100.0), quote) is None


def test_extreme_mover_evidence_none_when_the_two_closes_diverge_too_much():
    # both bars are well past 25%, but 60 apart on a ~170 base (~35%) --
    # a real level shouldn't be moving that fast bar-to-bar
    bars = _extreme_mover_bars(close1=140.0, close2=200.0)
    quote = _quote(last=200.0)
    assert extreme_mover_evidence(bars, _anchors(prior_close=100.0), quote) is None


def test_extreme_mover_evidence_none_when_bars_disagree_on_direction_with_the_quote():
    # quote says +40% (140), but the persistence bars actually show a
    # -40% move (60) -- not evidence FOR the quote's own claimed move
    bars = _extreme_mover_bars(close1=60.0, close2=62.0)
    quote = _quote(last=140.0)
    assert extreme_mover_evidence(bars, _anchors(prior_close=100.0), quote) is None


def test_extreme_mover_evidence_verified_carries_gap_pct_and_summed_volume():
    bars = _extreme_mover_bars(close1=140.0, close2=138.0, volume=50_000)
    quote = _quote(last=138.0)
    evidence = extreme_mover_evidence(bars, _anchors(prior_close=100.0), quote)
    assert evidence is not None
    assert evidence.gap_pct == pytest.approx(0.38)
    assert evidence.verified_volume == 100_000


def test_extreme_mover_evidence_none_below_the_notional_floor():
    # Ship #2 VPS acceptance run, 2026-08-17: the real shape that
    # slipped through pre-fix -- a sub-penny name, 200+300 shares,
    # $8.02 combined notional. Non-zero volume, real persistence, real
    # tolerance -- everything but real money.
    base = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("AACBR", base, 0.03, 0.031, 0.029, 0.03, volume=1000),
        Bar("AACBR", base + timedelta(minutes=5), 0.03, 0.018, 0.016, 0.017, volume=200),
        Bar("AACBR", base + timedelta(minutes=10), 0.017, 0.016, 0.015, 0.0154, volume=300),
    ]
    quote = _quote(last=0.0154)
    assert extreme_mover_evidence(bars, _anchors(prior_close=0.03), quote) is None


def test_extreme_mover_evidence_verified_at_a_real_notional():
    # Same shape, real dollar notional -- must still pass.
    bars = _extreme_mover_bars(close1=140.0, close2=138.0, volume=10)  # (140+138)*10 = $2,780
    quote = _quote(last=138.0)
    assert extreme_mover_evidence(bars, _anchors(prior_close=100.0), quote) is not None


def test_spread_pct_of_mid_matches_the_guards_own_formula():
    quote = _quote(bid=95.0, ask=105.0)
    assert spread_pct_of_mid(quote) == pytest.approx(0.10, abs=1e-9)


def test_spread_pct_of_mid_none_on_non_positive_mid():
    quote = _quote(bid=-5.0, ask=5.0)  # mid == 0
    assert spread_pct_of_mid(quote) is None


def test_verified_extreme_mover_bypasses_the_gap_suppression():
    bars = _extreme_mover_bars(close1=140.0, close2=138.0)
    reason = validate_alert_data(
        _cluster(close=138.0), _anchors(prior_close=100.0), _quote_for_spread(138.0, 0.01), bars=bars,
    )
    assert reason is None


def test_verified_extreme_mover_gets_the_widened_15pct_spread_ceiling():
    # 11% spread -- would fail the normal 5% ceiling, passes the 15% one
    bars = _extreme_mover_bars(close1=140.0, close2=138.0)
    reason = validate_alert_data(
        _cluster(close=138.0), _anchors(prior_close=100.0), _quote_for_spread(138.0, 0.11), bars=bars,
    )
    assert reason is None


def test_verified_extreme_mover_still_suppressed_above_the_15pct_spread_ceiling():
    # Option B is explicit: silent above 15%, even with verified persistence
    bars = _extreme_mover_bars(close1=140.0, close2=138.0)
    reason = validate_alert_data(
        _cluster(close=138.0), _anchors(prior_close=100.0), _quote_for_spread(138.0, 0.20), bars=bars,
    )
    assert reason is not None and reason.startswith("spread_too_wide") and "15%" in reason


def test_unverified_extreme_mover_keeps_only_the_normal_5pct_spread_ceiling():
    # gap is real (40%) but persistence fails (only one bar crosses) --
    # must NOT get the widened ceiling just because the quote is extreme
    bars = _extreme_mover_bars(close1=110.0, close2=140.0)
    reason = validate_alert_data(
        _cluster(close=140.0), _anchors(prior_close=100.0), _quote_for_spread(140.0, 0.11), bars=bars,
    )
    assert reason is not None and reason.startswith("spread_too_wide") and "5%" in reason


def test_sub_25pct_move_is_unaffected_by_the_extreme_mover_carve_out():
    # a normal ~10% move must still use the normal 5% spread ceiling --
    # regression guard: no change to sub-25% behavior
    bars = _bars()
    reason = validate_alert_data(_cluster(), _anchors(), _quote_for_spread(366.0, 0.11), bars=bars)
    assert reason is not None and reason.startswith("spread_too_wide") and "5%" in reason
