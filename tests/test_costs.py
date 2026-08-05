"""Tests for tradebot.costs.breakeven_move()."""
from __future__ import annotations

from datetime import date

import pytest

from tradebot.costs import breakeven_move, format_breakeven
from tradebot.marketdata import OptionChain, OptionContract

SYMBOL = "TSLA"
EXPIRY = date(2026, 7, 31)


def _contract(strike=430.0, bid=5.0, ask=5.2, delta=0.5, theta=-0.3, oi=1200) -> OptionContract:
    return OptionContract(
        symbol=f"{SYMBOL}C{strike}", expiry=EXPIRY, strike=strike, right="call",
        bid=bid, ask=ask, last=(bid + ask) / 2, delta=delta, theta=theta, open_interest=oi,
    )


def test_breakeven_move_computes_from_the_nearest_atm_contract():
    chain = OptionChain(symbol=SYMBOL, expiry=EXPIRY, contracts=[
        _contract(strike=400.0), _contract(strike=430.0), _contract(strike=460.0),
    ])
    result = breakeven_move(chain, spot=431.0, atr14=4.0, hours_held=1.0)
    assert result is not None
    # picks strike 430 (nearest to spot 431), not 400 or 460
    assert result.contract.strike == 430.0

    spread_cost = (5.2 - 5.0) * 100
    commissions = 0.65 * 2
    theta_per_hour = abs(-0.3) / 24
    expected_pct = (spread_cost + commissions + theta_per_hour * 1.0) / (0.5 * 100 * 431.0)
    assert result.pct == pytest.approx(expected_pct)
    assert result.atr_units == pytest.approx((expected_pct * 431.0) / 4.0)


def test_breakeven_move_returns_none_without_a_chain():
    assert breakeven_move(None, spot=431.0, atr14=4.0) is None


def test_breakeven_move_returns_none_when_spread_too_wide():
    wide = _contract(bid=4.0, ask=5.0)  # spread/mid = 1.0/4.5 = 22% > 12%
    chain = OptionChain(symbol=SYMBOL, expiry=EXPIRY, contracts=[wide])
    assert breakeven_move(chain, spot=430.0, atr14=4.0) is None


def test_breakeven_move_returns_none_when_open_interest_too_low():
    illiquid = _contract(oi=100)
    chain = OptionChain(symbol=SYMBOL, expiry=EXPIRY, contracts=[illiquid])
    assert breakeven_move(chain, spot=430.0, atr14=4.0) is None


def test_breakeven_move_returns_none_rather_than_fabricating_a_missing_delta():
    no_greeks = _contract()
    no_greeks = OptionContract(**{**no_greeks.__dict__, "delta": None})
    chain = OptionChain(symbol=SYMBOL, expiry=EXPIRY, contracts=[no_greeks])
    assert breakeven_move(chain, spot=430.0, atr14=4.0) is None


def test_format_breakeven():
    chain = OptionChain(symbol=SYMBOL, expiry=EXPIRY, contracts=[_contract()])
    result = breakeven_move(chain, spot=431.0, atr14=4.0)
    text = format_breakeven(result)
    assert "%" in text and "ATR" in text
    assert format_breakeven(None) == "no tradable contract"
