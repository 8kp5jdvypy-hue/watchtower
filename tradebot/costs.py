"""breakeven_move() — the round-trip cost hurdle for a fixed-duration
option hold, so it's visible next to the observed move at decision time.

Never fabricates a delta: if the chain is unavailable or the nearest-ATM
contract fails the liquidity filter, breakeven_move() returns None rather
than guessing greeks — see CLAUDE.md.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradebot.marketdata import OptionChain, OptionContract

DEFAULT_COMMISSION_PER_SIDE = 0.65
MAX_SPREAD_PCT = 0.12
MIN_OPEN_INTEREST = 500


@dataclass(frozen=True)
class Breakeven:
    pct: float  # fraction of spot the underlying must move to break even
    atr_units: float
    contract: OptionContract


def _nearest_atm_contract(chain: OptionChain, spot: float) -> OptionContract | None:
    if not chain.contracts:
        return None
    return min(chain.contracts, key=lambda c: abs(c.strike - spot))


def _passes_liquidity_filter(contract: OptionContract) -> bool:
    if contract.bid <= 0 or contract.ask <= 0 or contract.ask < contract.bid:
        return False
    mid = (contract.bid + contract.ask) / 2
    if (contract.ask - contract.bid) / mid > MAX_SPREAD_PCT:
        return False
    if contract.open_interest < MIN_OPEN_INTEREST:
        return False
    return True


def breakeven_move(
    chain: OptionChain | None,
    spot: float,
    atr14: float | None,
    hours_held: float = 1.0,
    commissions_per_side: float = DEFAULT_COMMISSION_PER_SIDE,
) -> Breakeven | None:
    """The underlying move (in % and ATR units) needed for a hold of the
    nearest-ATM contract to break even on spread + commissions + theta
    decay:

        breakeven_move_pct = (spread_cost + commissions
                              + abs(theta_per_hour) * hours_held)
                             / (delta * 100 * spot)

    spread_cost is the full round-trip bid-ask spread (100 shares/contract).
    commissions is commissions_per_side charged on both the open and the
    close. theta_per_hour is the contract's theta (quoted per day, the
    standard convention) prorated over 24 hours — a modeling choice, not
    a market fact; revisit if trading-hours-only decay proves more
    accurate in practice.

    Returns None — never a fabricated number — if there's no chain, no
    contract, the ATM contract fails the liquidity filter (spread > 12%
    of mid, or open interest < 500), or its delta/theta aren't available.
    """
    if chain is None:
        return None
    contract = _nearest_atm_contract(chain, spot)
    if contract is None or not _passes_liquidity_filter(contract):
        return None
    if contract.delta is None or contract.theta is None or contract.delta == 0:
        return None

    spread_cost = (contract.ask - contract.bid) * 100
    commissions = commissions_per_side * 2  # round trip: open + close
    theta_per_hour = abs(contract.theta) / 24
    total_cost = spread_cost + commissions + theta_per_hour * hours_held

    pct = total_cost / (abs(contract.delta) * 100 * spot)
    atr_units = (pct * spot) / atr14 if atr14 else float("inf")
    return Breakeven(pct=pct, atr_units=atr_units, contract=contract)


def format_breakeven(breakeven: Breakeven | None) -> str:
    if breakeven is None:
        return "no tradable contract"
    return f"{breakeven.pct:.2%} ({breakeven.atr_units:.2f} ATR)"
