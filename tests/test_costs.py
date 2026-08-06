"""Tests for tradebot.costs.select_contract() — contract selection,
liquidity gates, expiry choice, go/no-go, and NO TRADE reasons."""
from __future__ import annotations

from datetime import date

import pytest

from tradebot.costs import (
    IV_RANK_MIN_SAMPLE,
    IV_RANK_THRESHOLD,
    MIN_SIMILAR_SETUPS_SAMPLE,
    pick_expiry,
    position_size,
    select_contract,
)
from tradebot.journal import HistoricalPerformance
from tradebot.marketdata import OptionChain, OptionContract

SYMBOL = "TSLA"  # deep-liquidity class
THIN_SYMBOL = "BE"  # thin class
TODAY = date(2026, 7, 20)  # a Monday
EXPIRY = date(2026, 7, 31)  # a Friday, 11 DTE from TODAY


def _contract(strike=430.0, right="call", bid=5.0, ask=5.2, delta=0.5, theta=-0.3, oi=1200, iv=0.30, day_volume=200, expiry=EXPIRY):
    return OptionContract(
        symbol=f"{SYMBOL}{strike}{right}", expiry=expiry, strike=strike, right=right,
        bid=bid, ask=ask, last=(bid + ask) / 2, delta=delta, theta=theta, open_interest=oi,
        implied_volatility=iv, day_volume=day_volume,
    )


def _chain_fn(contracts, valid_expiry=EXPIRY):
    def fn(expiry):
        if expiry != valid_expiry:
            return OptionChain(symbol=SYMBOL, expiry=expiry, contracts=[])
        return OptionChain(symbol=SYMBOL, expiry=expiry, contracts=contracts)
    return fn


def _similar(sample_size=60, avg_return_atr=1.0, continuation_rate=0.4, avg_return_pct=-0.5):
    return HistoricalPerformance(
        sample_size=sample_size, continuation_rate=continuation_rate, avg_return_pct=avg_return_pct,
        offset_min=30, avg_return_atr=avg_return_atr,
    )


# ---------------------------------------------------------------------- #
# Direction -> right
# ---------------------------------------------------------------------- #


def test_bullish_direction_selects_a_call():
    contracts = [_contract(strike=430.0, right="call", delta=0.47), _contract(strike=430.0, right="put", delta=-0.47)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert result.is_tradable
    assert result.breakeven.contract.right == "call"


def test_bearish_direction_selects_a_put():
    contracts = [_contract(strike=430.0, right="call", delta=0.47), _contract(strike=430.0, right="put", delta=-0.47)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="down", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert result.is_tradable
    assert result.breakeven.contract.right == "put"


# ---------------------------------------------------------------------- #
# Expiry selection: 7-14 DTE, tries real Fridays, thin names may not have every weekly
# ---------------------------------------------------------------------- #


def test_pick_expiry_prefers_the_midpoint_of_the_dte_window():
    seen = []
    def chain_fn(expiry):
        seen.append(expiry)
        return OptionChain(symbol=SYMBOL, expiry=expiry, contracts=[_contract(expiry=expiry)])
    expiry, chain = pick_expiry(chain_fn, TODAY, min_dte=7, max_dte=14)
    assert expiry is not None
    dte = (expiry - TODAY).days
    assert 7 <= dte <= 14
    assert expiry.weekday() == 4  # Friday
    # first candidate tried should be the one closest to the midpoint (10.5 DTE)
    assert abs((seen[0] - TODAY).days - 10.5) == min(abs((d - TODAY).days - 10.5) for d in seen)


def test_pick_expiry_falls_through_to_the_next_friday_when_a_thin_name_has_no_listed_contracts():
    tried = []
    def chain_fn(expiry):
        tried.append(expiry)
        if len(tried) < 2:
            return OptionChain(symbol=THIN_SYMBOL, expiry=expiry, contracts=[])  # not listed
        return OptionChain(symbol=THIN_SYMBOL, expiry=expiry, contracts=[_contract(expiry=expiry)])
    # widen the window so there's more than one candidate Friday to fall through to
    expiry, chain = pick_expiry(chain_fn, TODAY, min_dte=7, max_dte=21)
    assert len(tried) >= 2
    assert expiry == tried[-1]
    assert chain.contracts


def test_pick_expiry_returns_none_when_nothing_in_the_window_is_listed():
    expiry, chain = pick_expiry(lambda e: OptionChain(symbol=SYMBOL, expiry=e, contracts=[]), TODAY)
    assert expiry is None and chain is None


def test_select_contract_reports_no_liquid_strike_when_no_expiry_is_listed():
    result = select_contract(
        lambda e: OptionChain(symbol=SYMBOL, expiry=e, contracts=[]), SYMBOL, spot=431.0, direction="up",
        atr14=4.0, similar_setups=_similar(), today=TODAY,
    )
    assert not result.is_tradable
    assert result.no_trade.reason == "no_liquid_strike"


# ---------------------------------------------------------------------- #
# Per-symbol-class liquidity gates
# ---------------------------------------------------------------------- #


def test_deep_class_requires_500_open_interest():
    contracts = [_contract(oi=499, delta=0.47)]
    result = select_contract(_chain_fn(contracts), "TSLA", spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert not result.is_tradable and result.no_trade.reason == "no_liquid_strike"

    contracts_ok = [_contract(oi=500, delta=0.47)]
    result_ok = select_contract(_chain_fn(contracts_ok), "TSLA", spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert result_ok.is_tradable


def test_thin_class_only_requires_250_open_interest():
    # 300 OI would fail the deep-class floor (500) but passes the thin-class floor (250)
    contracts = [_contract(oi=300, delta=0.47, bid=1.0, ask=1.05)]
    result = select_contract(_chain_fn(contracts), THIN_SYMBOL, spot=25.0, direction="up", atr14=1.0, similar_setups=_similar(), today=TODAY)
    assert result.is_tradable


def test_rejects_spread_wider_than_10pct_of_mid():
    contracts = [_contract(bid=4.0, ask=5.0, delta=0.47)]  # spread/mid = 1.0/4.5 = 22%
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert not result.is_tradable and result.no_trade.reason == "no_liquid_strike"


def test_rejects_absolute_spread_over_15c_under_3_dollars_even_if_pct_passes():
    # bid=2.00 ask=2.19: spread 0.19 / mid 2.095 = 9% (passes the 10% rule) but > $0.15 absolute, mid < $3
    contracts = [_contract(bid=2.00, ask=2.19, delta=0.47)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert not result.is_tradable


def test_allows_a_wider_absolute_spread_once_mid_is_at_or_above_3_dollars():
    contracts = [_contract(bid=3.00, ask=3.19, delta=0.47)]  # same $0.19 spread, mid >= 3 now
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert result.is_tradable


def test_rejects_bid_at_or_below_a_nickel():
    contracts = [_contract(bid=0.05, ask=0.10, delta=0.47)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert not result.is_tradable


def test_rejects_day_volume_under_100_when_known():
    contracts = [_contract(delta=0.47, day_volume=50)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert not result.is_tradable


def test_unknown_day_volume_does_not_block():
    contracts = [_contract(delta=0.47, day_volume=None)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert result.is_tradable


# ---------------------------------------------------------------------- #
# Strike selection by target delta (0.40-0.55), not nearest-to-spot
# ---------------------------------------------------------------------- #


def test_picks_by_target_delta_not_nearest_strike_to_spot():
    contracts = [
        _contract(strike=431.0, delta=0.10),  # closest to spot, but way off target delta
        _contract(strike=400.0, delta=0.47),  # far from spot, but right in the target delta band
    ]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert result.breakeven.contract.strike == 400.0


def test_required_move_is_reported_in_atr_directly_comparable_to_score():
    contracts = [_contract(strike=430.0, delta=0.5)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    spread_cost = (5.2 - 5.0) * 100
    commissions = 0.65 * 2
    theta_per_hour = abs(-0.3) / 24
    from tradebot.costs import DEFAULT_HOURS_HELD
    expected_pct = (spread_cost + commissions + theta_per_hour * DEFAULT_HOURS_HELD) / (0.5 * 100 * 431.0)
    assert result.breakeven.pct == pytest.approx(expected_pct)
    assert result.breakeven.atr_units == pytest.approx((expected_pct * 431.0) / 4.0)


def test_returns_no_liquid_strike_rather_than_fabricating_a_missing_delta():
    no_greeks = _contract()
    no_greeks = OptionContract(**{**no_greeks.__dict__, "delta": None})
    result = select_contract(_chain_fn([no_greeks]), SYMBOL, spot=430.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY)
    assert not result.is_tradable and result.no_trade.reason == "no_liquid_strike"


# ---------------------------------------------------------------------- #
# Go/no-go vs Similar Setups' historical avg move, and the n<50 marker
# ---------------------------------------------------------------------- #


def test_no_trade_when_breakeven_exceeds_the_typical_move():
    contracts = [_contract(strike=430.0, delta=0.5)]  # a normal, liquidity-passing contract
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0,
        # an implausibly tiny typical move so any real breakeven exceeds it
        similar_setups=_similar(sample_size=60, avg_return_atr=0.001), today=TODAY,
    )
    assert not result.is_tradable
    assert result.no_trade.reason == "breakeven_exceeds_typical_move"
    assert "ATR" in result.no_trade.detail


def test_trades_when_breakeven_is_within_the_typical_move():
    contracts = [_contract(strike=430.0, delta=0.5)]
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0,
        similar_setups=_similar(sample_size=60, avg_return_atr=5.0), today=TODAY,
    )
    assert result.is_tradable


def test_insufficient_sample_still_prints_the_contract_but_skips_go_no_go():
    contracts = [_contract(strike=430.0, delta=0.5)]  # would fail go/no-go (see the tiny avg_return_atr) if it ran
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0,
        similar_setups=_similar(sample_size=MIN_SIMILAR_SETUPS_SAMPLE - 1, avg_return_atr=0.001), today=TODAY,
    )
    assert result.is_tradable
    assert result.insufficient_sample is True


def test_no_similar_setups_at_all_is_treated_as_insufficient_sample():
    contracts = [_contract(strike=430.0, delta=0.5)]
    result = select_contract(_chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=None, today=TODAY)
    assert result.is_tradable and result.insufficient_sample


# ---------------------------------------------------------------------- #
# Earnings blackout
# ---------------------------------------------------------------------- #


def test_earnings_check_true_blocks_with_a_distinct_reason():
    contracts = [_contract(strike=430.0, delta=0.5)]
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY,
        earnings_check_fn=lambda expiry: True,
    )
    assert not result.is_tradable and result.no_trade.reason == "earnings_blackout"


def test_earnings_check_unknown_does_not_block():
    contracts = [_contract(strike=430.0, delta=0.5)]
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0, similar_setups=_similar(), today=TODAY,
        earnings_check_fn=lambda expiry: None,
    )
    assert result.is_tradable


# ---------------------------------------------------------------------- #
# IV rank -> debit vertical
# ---------------------------------------------------------------------- #


def test_high_iv_rank_with_enough_sample_prefers_a_vertical_with_both_legs_shown():
    contracts = [
        _contract(strike=420.0, delta=0.45, bid=8.0, ask=8.2),
        _contract(strike=440.0, delta=0.25, bid=3.0, ask=3.2),
    ]
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0,
        similar_setups=_similar(avg_return_atr=5.0), today=TODAY,
        iv_rank_fn=lambda iv: (IV_RANK_THRESHOLD + 1, IV_RANK_MIN_SAMPLE),
    )
    assert result.is_tradable
    assert result.breakeven.is_vertical
    assert len(result.breakeven.legs) == 2
    assert result.breakeven.legs[0].side == "long" and result.breakeven.legs[0].contract.strike == 420.0
    assert result.breakeven.legs[1].side == "short" and result.breakeven.legs[1].contract.strike == 440.0


def test_iv_rank_below_min_sample_never_prefers_a_vertical():
    contracts = [
        _contract(strike=420.0, delta=0.45, bid=8.0, ask=8.2),
        _contract(strike=440.0, delta=0.25, bid=3.0, ask=3.2),
    ]
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0,
        similar_setups=_similar(avg_return_atr=5.0), today=TODAY,
        iv_rank_fn=lambda iv: (95.0, IV_RANK_MIN_SAMPLE - 1),  # high rank, but not enough history
    )
    assert result.is_tradable
    assert not result.breakeven.is_vertical


def test_iv_rank_at_or_below_threshold_does_not_prefer_a_vertical():
    contracts = [
        _contract(strike=420.0, delta=0.45, bid=8.0, ask=8.2),
        _contract(strike=440.0, delta=0.25, bid=3.0, ask=3.2),
    ]
    result = select_contract(
        _chain_fn(contracts), SYMBOL, spot=431.0, direction="up", atr14=4.0,
        similar_setups=_similar(avg_return_atr=5.0), today=TODAY,
        iv_rank_fn=lambda iv: (IV_RANK_THRESHOLD, IV_RANK_MIN_SAMPLE),
    )
    assert not result.breakeven.is_vertical


# ---------------------------------------------------------------------- #
# 45-minute hold assumption, explicit and enforced
# ---------------------------------------------------------------------- #


def test_default_hold_matches_the_alert_budgets_cooldown_not_an_arbitrary_hour():
    from tradebot.costs import DEFAULT_HOLD_MINUTES, DEFAULT_HOURS_HELD
    from tradebot.alerts import AlertBudget
    assert DEFAULT_HOLD_MINUTES == AlertBudget.cooldown_minutes
    assert DEFAULT_HOURS_HELD == pytest.approx(DEFAULT_HOLD_MINUTES / 60)


# ---------------------------------------------------------------------- #
# position_size() — tied to the SAME entry_mid select_contract() already
# computes, not a separate estimate. Options settle in 100-share
# (per-contract) multiples, so the dollar math always uses entry_mid*100.
# ---------------------------------------------------------------------- #


def test_position_size_suggests_the_real_contract_count_within_budget():
    # $50,000 * 2% = $1,000 budget; $2.00 entry mid -> $200/contract -> 5 contracts
    size = position_size(entry_mid=2.00, account_size=50_000, risk_per_trade_pct=2.0)
    assert size.max_contracts == 5
    assert size.dollars_at_risk == pytest.approx(1_000.0)
    assert size.risk_budget == pytest.approx(1_000.0)
    assert size.exceeds_limit is False


def test_position_size_rounds_down_never_up():
    # $1,000 budget / $300 per contract = 3.33 -> must floor to 3, never round to 3.33 or 4
    size = position_size(entry_mid=3.00, account_size=50_000, risk_per_trade_pct=2.0)
    assert size.max_contracts == 3
    assert size.dollars_at_risk == pytest.approx(900.0)


def test_position_size_exceeds_limit_when_even_one_contract_breaches_the_budget():
    # $100 budget, $200/contract — not even one contract fits
    size = position_size(entry_mid=2.00, account_size=10_000, risk_per_trade_pct=1.0)
    assert size.max_contracts == 0
    assert size.exceeds_limit is True
    assert size.dollars_at_risk == 0.0


def test_position_size_never_suggests_a_size_that_breaches_the_budget():
    """Property check across a range of premiums/budgets: dollars_at_risk
    must never exceed risk_budget, and exceeds_limit must be set exactly
    when max_contracts is 0."""
    for entry_mid in (0.10, 0.55, 1.00, 2.37, 5.00, 12.34):
        for account_size in (1_000, 5_000, 25_000, 100_000):
            for risk_pct in (0.25, 1.0, 2.0, 5.0):
                size = position_size(entry_mid, account_size, risk_pct)
                assert size.dollars_at_risk <= size.risk_budget + 1e-9
                assert size.exceeds_limit == (size.max_contracts == 0)


def test_position_size_zero_or_negative_premium_exceeds_limit_never_divides_by_zero():
    size = position_size(entry_mid=0.0, account_size=50_000, risk_per_trade_pct=2.0)
    assert size.exceeds_limit is True
    assert size.max_contracts == 0
