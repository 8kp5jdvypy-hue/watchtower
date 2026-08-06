"""Contract selection: turns a detection cluster into either one specific
option contract (or a debit vertical) with a real breakeven cost, or an
explicit, reasoned NO TRADE. Never fabricates a delta, a day volume, or
an IV rank — every one of those either comes from a real vendor call (or
a local history cache built from real data over time) or the selection
stops there and says why (see CLAUDE.md's rule against guessing greeks,
extended here to every other input this module needs).

Bullish clusters (trend="up") get calls; bearish (trend="down") get
puts. Strike is chosen by target delta, not nearest-to-spot — a 0.5
delta contract isn't always the closest strike to spot once skew is
accounted for, and delta is what actually determines the breakeven-move
math below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tradebot.config import liquidity_class
from tradebot.marketdata import OptionChain, OptionContract

DEFAULT_COMMISSION_PER_SIDE = 0.65

# Liquidity gates are per symbol class (see config.liquidity_class) — one
# global floor makes the gate look broken on thin, high-beta names that
# never had a chance of clearing a mega-cap's open interest.
MIN_OPEN_INTEREST = {"deep": 500, "thin": 250}
MIN_DAY_VOLUME = 100
MAX_SPREAD_PCT_OF_MID = 0.10
MAX_SPREAD_ABSOLUTE_UNDER_3 = 0.15  # additional cap for sub-$3 contracts, where 10% of mid is too loose
MIN_BID = 0.05

TARGET_DELTA_LOW = 0.40
TARGET_DELTA_HIGH = 0.55
VERTICAL_LONG_DELTA = 0.45
VERTICAL_SHORT_DELTA = 0.25

DEFAULT_MIN_DTE = 7
DEFAULT_MAX_DTE = 14

# The round-trip cost model assumes you hold for this long. AlertBudget's
# cooldown is 45 minutes for a given (symbol, kind) pair — the ONLY
# window this hold-time assumption is actually enforced over. A second
# alert on the SAME symbol but a DIFFERENT kind can still fire inside
# that window (cooldown is keyed on (symbol, kind), not symbol alone) —
# that's an accepted, explicit tradeoff, not an oversight: a different
# kind means a different rationale and a different contract decision,
# not a duplicate of the same trade idea. Matching this to the cooldown,
# rather than an arbitrary 60 minutes, means the cost model describes
# the one holding period this system actually guarantees isn't
# immediately re-triggered.
DEFAULT_HOLD_MINUTES = 45
DEFAULT_HOURS_HELD = DEFAULT_HOLD_MINUTES / 60

MIN_SIMILAR_SETUPS_SAMPLE = 50  # go/no-go comparison — stricter than journal.MIN_HISTORY_SAMPLE (5)
IV_RANK_MIN_SAMPLE = 20  # sessions of local IV history before a rank is ever reported
IV_RANK_THRESHOLD = 70


@dataclass(frozen=True)
class Leg:
    contract: OptionContract
    side: str  # "long" | "short"


@dataclass(frozen=True)
class Breakeven:
    pct: float  # fraction of spot the underlying must move to break even (0.029, not 2.9)
    atr_units: float  # the same move, expressed in ATR — directly comparable to Score
    legs: tuple  # tuple[Leg, ...] — one leg for a single contract, two (long, short) for a vertical
    is_vertical: bool

    @property
    def contract(self) -> OptionContract:
        """The primary (long) leg — for callers that only care about a
        single-contract summary and don't need to handle verticals."""
        return self.legs[0].contract


@dataclass(frozen=True)
class NoTrade:
    reason: str  # "no_liquid_strike" | "breakeven_exceeds_typical_move" | "earnings_blackout"
    detail: str


@dataclass(frozen=True)
class ContractSelection:
    breakeven: Breakeven | None
    no_trade: NoTrade | None
    expiry: date | None
    dte: int | None
    similar_setups_sample: int | None
    insufficient_sample: bool  # True: a contract WAS selected but n < MIN_SIMILAR_SETUPS_SAMPLE, so go/no-go couldn't run

    @property
    def is_tradable(self) -> bool:
        return self.breakeven is not None


def _mid(contract: OptionContract) -> float:
    return (contract.bid + contract.ask) / 2


def _passes_liquidity(contract: OptionContract, symbol: str) -> bool:
    if contract.bid <= MIN_BID or contract.ask <= 0 or contract.ask < contract.bid:
        return False
    mid = _mid(contract)
    if mid <= 0:
        return False
    spread = contract.ask - contract.bid
    if spread / mid > MAX_SPREAD_PCT_OF_MID:
        return False
    if mid < 3 and spread > MAX_SPREAD_ABSOLUTE_UNDER_3:
        return False
    if contract.open_interest < MIN_OPEN_INTEREST[liquidity_class(symbol)]:
        return False
    if contract.day_volume is not None and contract.day_volume < MIN_DAY_VOLUME:
        return False
    return True


def pick_expiry(chain_fn, today: date, min_dte: int = DEFAULT_MIN_DTE, max_dte: int = DEFAULT_MAX_DTE):
    """Tries each Friday inside [today+min_dte, today+max_dte], closest
    to the midpoint first, against the real chain (a candidate date with
    no listed contracts returns an empty chain, not an error — thin
    names don't have every weekly listed). Returns (expiry, chain) for
    the first one with any contracts, or (None, None) if nothing in the
    window is listed at all. chain_fn: Callable[[date], OptionChain | None]."""
    candidates = [
        today + timedelta(days=d) for d in range(min_dte, max_dte + 1)
        if (today + timedelta(days=d)).weekday() == 4
    ]
    if not candidates:
        return None, None
    midpoint_days = (min_dte + max_dte) / 2
    candidates.sort(key=lambda d: abs((d - today).days - midpoint_days))
    for candidate in candidates:
        chain = chain_fn(candidate)
        if chain is not None and chain.contracts:
            return candidate, chain
    return None, None


def _pick_by_delta(contracts: list, target_low: float, target_high: float):
    """Nearest to the midpoint of [target_low, target_high] by absolute
    delta; contracts with no delta can't be scored and are excluded, not
    guessed at."""
    target_mid = (target_low + target_high) / 2
    scored = [c for c in contracts if c.delta is not None]
    if not scored:
        return None
    return min(scored, key=lambda c: abs(abs(c.delta) - target_mid))


def _single_leg_breakeven(contract, spot: float, atr14, hours_held: float, commissions_per_side: float):
    if contract.delta is None or contract.theta is None or contract.delta == 0:
        return None
    spread_cost = (contract.ask - contract.bid) * 100
    commissions = commissions_per_side * 2  # round trip: open + close
    theta_per_hour = abs(contract.theta) / 24
    total_cost = spread_cost + commissions + theta_per_hour * hours_held
    pct = total_cost / (abs(contract.delta) * 100 * spot)
    atr_units = (pct * spot) / atr14 if atr14 else float("inf")
    return Breakeven(pct=pct, atr_units=atr_units, legs=(Leg(contract, "long"),), is_vertical=False)


def _vertical_breakeven(long_c, short_c, spot: float, atr14, hours_held: float, commissions_per_side: float):
    """Approximates a debit vertical's breakeven the same way as the
    single-leg case, generalized to two legs: round-trip spread cost
    summed across both legs (you cross both markets on entry and exit),
    commissions doubled (2 legs), and the net theta/delta of the combined
    position — a vertical's theta-offsetting is a real, modeled benefit
    here, not an approximation error."""
    if long_c.delta is None or short_c.delta is None or long_c.theta is None or short_c.theta is None:
        return None
    net_delta = long_c.delta - short_c.delta
    if net_delta == 0:
        return None
    spread_cost = ((long_c.ask - long_c.bid) + (short_c.ask - short_c.bid)) * 100
    commissions = commissions_per_side * 2 * 2  # 2 legs, open + close
    theta_per_hour = abs(long_c.theta - short_c.theta) / 24
    total_cost = spread_cost + commissions + theta_per_hour * hours_held
    pct = total_cost / (abs(net_delta) * 100 * spot)
    atr_units = (pct * spot) / atr14 if atr14 else float("inf")
    return Breakeven(
        pct=pct, atr_units=atr_units,
        legs=(Leg(long_c, "long"), Leg(short_c, "short")), is_vertical=True,
    )


def select_contract(
    chain_fn,
    symbol: str,
    spot: float,
    direction: str,
    atr14,
    similar_setups,
    today: date,
    *,
    iv_rank_fn=None,
    earnings_check_fn=None,
    min_dte: int = DEFAULT_MIN_DTE,
    max_dte: int = DEFAULT_MAX_DTE,
    hours_held: float = DEFAULT_HOURS_HELD,
    commissions_per_side: float = DEFAULT_COMMISSION_PER_SIDE,
) -> ContractSelection:
    """The single entry point: cluster in, one tradable contract (or
    vertical) with a real breakeven — or an explicit, reasoned NO TRADE.

    chain_fn: Callable[[date], OptionChain | None] — already bound to
        `symbol`; called once per expiry candidate tried.
    direction: "up" (calls) or "down" (puts), matching Cluster.trend.
    similar_setups: a journal.HistoricalPerformance for this exact
        (kind, direction), fetched with a lookback large enough to reach
        MIN_SIMILAR_SETUPS_SAMPLE — the caller's job, not this
        function's (costs.py has no DB access).
    iv_rank_fn: Callable[[float], tuple[float | None, int]] — given the
        candidate contract's real, just-fetched IV, returns (rank,
        sample_size) from a local history cache. Taking a callable
        instead of a precomputed rank avoids fetching the chain twice
        (once to know the IV, once to pick a contract) — this function
        already has the candidate in hand when it needs the rank. Below
        IV_RANK_MIN_SAMPLE this never prefers a vertical, since a rank
        computed on too little history isn't a rank, it's noise.
    earnings_check_fn: Callable[[date], bool | None] — given the expiry
        this function decided on (not knowable to the caller in
        advance), returns True (blackout), False (cleared), or None if
        there's no earnings data to check against at all — e.g. before
        the day's nasdaq_earnings ingest has run (see tradebot.events'
        has_earnings_before). None does NOT block: treating "unknown" as
        "blackout" would make this feature return NO TRADE any time the
        day's ingest simply hasn't happened yet. That's an explicit,
        accepted risk — the same "missing data is informational, not a
        blocker" stance /events already takes — not a silent gap.
    """
    expiry, chain = pick_expiry(chain_fn, today, min_dte, max_dte)
    if expiry is None or chain is None:
        return ContractSelection(
            breakeven=None, no_trade=NoTrade("no_liquid_strike", f"no listed expiry {min_dte}-{max_dte} DTE"),
            expiry=None, dte=None, similar_setups_sample=None, insufficient_sample=False,
        )
    dte = (expiry - today).days

    if earnings_check_fn is not None and earnings_check_fn(expiry) is True:
        return ContractSelection(
            breakeven=None, no_trade=NoTrade("earnings_blackout", f"earnings fall before {expiry.isoformat()}"),
            expiry=expiry, dte=dte, similar_setups_sample=None, insufficient_sample=False,
        )

    right = "call" if direction == "up" else "put"
    candidates = [c for c in chain.contracts if c.right == right and _passes_liquidity(c, symbol)]
    if not candidates:
        return ContractSelection(
            breakeven=None,
            no_trade=NoTrade("no_liquid_strike", f"no {right} cleared the liquidity gate at {expiry.isoformat()}"),
            expiry=expiry, dte=dte, similar_setups_sample=None, insufficient_sample=False,
        )

    use_vertical = False
    if iv_rank_fn is not None:
        atm_ish = _pick_by_delta(candidates, TARGET_DELTA_LOW, TARGET_DELTA_HIGH)
        if atm_ish is not None and atm_ish.implied_volatility is not None:
            rank, sample = iv_rank_fn(atm_ish.implied_volatility)
            use_vertical = rank is not None and sample >= IV_RANK_MIN_SAMPLE and rank > IV_RANK_THRESHOLD
    breakeven = None
    if use_vertical:
        long_c = _pick_by_delta(candidates, VERTICAL_LONG_DELTA, VERTICAL_LONG_DELTA)
        short_c = _pick_by_delta(candidates, VERTICAL_SHORT_DELTA, VERTICAL_SHORT_DELTA)
        if long_c is not None and short_c is not None and long_c.strike != short_c.strike:
            breakeven = _vertical_breakeven(long_c, short_c, spot, atr14, hours_held, commissions_per_side)
    if breakeven is None:
        single = _pick_by_delta(candidates, TARGET_DELTA_LOW, TARGET_DELTA_HIGH)
        if single is None:
            return ContractSelection(
                breakeven=None,
                no_trade=NoTrade("no_liquid_strike", f"no liquid strike near {TARGET_DELTA_LOW:.2f}-{TARGET_DELTA_HIGH:.2f} delta"),
                expiry=expiry, dte=dte, similar_setups_sample=None, insufficient_sample=False,
            )
        breakeven = _single_leg_breakeven(single, spot, atr14, hours_held, commissions_per_side)
    if breakeven is None:
        return ContractSelection(
            breakeven=None, no_trade=NoTrade("no_liquid_strike", "selected contract is missing greeks"),
            expiry=expiry, dte=dte, similar_setups_sample=None, insufficient_sample=False,
        )

    sample = similar_setups.sample_size if similar_setups is not None else 0
    if sample < MIN_SIMILAR_SETUPS_SAMPLE:
        return ContractSelection(
            breakeven=breakeven, no_trade=None, expiry=expiry, dte=dte,
            similar_setups_sample=sample, insufficient_sample=True,
        )

    typical_move_atr = similar_setups.avg_return_atr if similar_setups is not None else None
    if typical_move_atr is not None and breakeven.atr_units > typical_move_atr:
        return ContractSelection(
            breakeven=None,
            no_trade=NoTrade(
                "breakeven_exceeds_typical_move",
                f"breakeven {breakeven.atr_units:.2f} ATR > typical move {typical_move_atr:.2f} ATR (n={sample})",
            ),
            expiry=expiry, dte=dte, similar_setups_sample=sample, insufficient_sample=False,
        )

    return ContractSelection(
        breakeven=breakeven, no_trade=None, expiry=expiry, dte=dte,
        similar_setups_sample=sample, insufficient_sample=False,
    )
