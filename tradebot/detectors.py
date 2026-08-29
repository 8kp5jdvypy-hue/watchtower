"""Pure detector functions and session anchors for the watchtower scanner.

Detectors take market data in and return a Detection | None. No I/O, no
network, no clock reads, no globals — see CLAUDE.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from tradebot.config import DEFAULT_MARKET_PROXY, MARKET_PROXY_SYMBOLS


EASTERN = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
BAR_MINUTES = 5

# Calibrated 2026-08-05 (6-symbol watchlist, 577 clusters): mean 2.40
# HIGH/day. Re-checked 2026-08-05 after adding vwap_break and
# round_number_break (1106 clusters): still mean 2.95 HIGH/day, no
# change needed. Re-calibrated 2026-08-05 after expanding the watchlist
# to 11 symbols (added NVDA, AAPL, AMD, META, AMZN; 2064 clusters): mean
# 3.15 HIGH/day, max 8/day, no symbol over 14% of HIGH. MEDIUM is now
# ~23/day (vs. the original "about 15" target) — expected since it scales
# with watchlist size and is delivered as a single hourly digest, not
# individual pings; tighten TIER_MEDIUM if that digest gets too long.
# Re-verified 2026-08-05 after expanding history from 20 to 143 cached
# sessions (Jan-Aug 2026, 14626 clusters): still mean 3.79 HIGH/day, no
# symbol over 16% of HIGH — no change needed at the larger sample.
# Re-calibrated 2026-08-05 after expanding to 17 symbols (added MSFT,
# COIN, PLTR, SMCI, IWM, USO) AND fixing a real bug in gap() (it used to
# score against the triggering bar's own high-low range, which blew up
# to absurd values on thin/near-zero-range bars — real example: USO
# premarket prints on ~100 shares volume; fixed to score against the
# prior session's range instead, which cut gap detections ~4x, from
# ~1420 to 362, and changed the overall score distribution materially).
# 21552 clusters: mean 3.62 HIGH/day, max 20/day (one real volatile day,
# 2026-01-21), no symbol over 17% of HIGH.
# Re-derive from out/replay_detections.csv if the watchlist or detectors
# change materially.
TIER_HIGH = 3.8
TIER_MEDIUM = 1.9


class Tier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "log"  # sub-threshold; still journaled, never alerted


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ts is the UTC timestamp of the bar's OPEN."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class DailyAnchors:
    """Session reference levels for one symbol, computed once at the
    09:35 ET bar and frozen for the rest of the session."""

    symbol: str
    session_date: date
    prior_close: float
    prior_high: float
    prior_low: float
    opening_range_high: float
    opening_range_low: float
    opening_range_volume: int
    # highest high / lowest low over however many prior daily bars were
    # passed to build_anchors() (up to 20 in practice — see callers).
    swing_high: float
    swing_low: float
    # Expected RTH time slot (0 = 09:30 ET, 1 = 09:35 ET, ...) ->
    # historical average cumulative volume through that wall-clock slot,
    # used by rvol_spike. This is deliberately a time slot rather than a
    # list position: a missing vendor bar must not shift every later
    # baseline comparison earlier by five minutes.
    avg_cum_volume_by_bar: Mapping[int, float]


@dataclass(frozen=True)
class Detection:
    symbol: str
    kind: str
    ts: datetime  # timestamp of the bar CLOSE that produced this detection
    score: float  # in ATR units (or a ratio, for rvol_spike)
    headline: str
    context: dict


def true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Average true range over the trailing `period` bars. Needs at least
    period + 1 bars (each true-range value needs a previous close).
    Returns None if there isn't enough history yet."""
    if len(bars) < period + 1:
        return None
    trs = [
        true_range(bars[i - 1].close, bars[i].high, bars[i].low)
        for i in range(1, len(bars))
    ]
    return sum(trs[-period:]) / period


def bar_close_ts(bar: Bar, bar_minutes: int = 5) -> datetime:
    """The timestamp at which this bar's data became knowable."""
    return bar.ts + timedelta(minutes=bar_minutes)


def _rth_bar_slot(bar: Bar, bar_minutes: int = BAR_MINUTES) -> int | None:
    """Return the bar's expected zero-based RTH wall-clock slot.

    Bar timestamps are UTC instants, while the US cash-session open follows
    America/New_York daylight-saving time. Converting to ET before deriving
    the slot keeps winter and summer history aligned. Off-grid, premarket,
    and naive timestamps fail closed instead of being coerced into a slot.
    """
    if bar.ts.tzinfo is None or bar.ts.utcoffset() is None:
        return None
    local = bar.ts.astimezone(EASTERN)
    if local.second or local.microsecond:
        return None
    minutes = local.hour * 60 + local.minute
    open_minutes = RTH_OPEN.hour * 60 + RTH_OPEN.minute
    elapsed = minutes - open_minutes
    if elapsed < 0 or elapsed % bar_minutes:
        return None
    return elapsed // bar_minutes


def build_anchors(
    symbol: str,
    session_date: date,
    prior_daily_bars: Sequence[Bar],
    opening_range_bars: Sequence[Bar],
    historical_session_bars: Sequence[Sequence[Bar]],
) -> DailyAnchors:
    """Build and freeze the day's anchor levels.

    prior_daily_bars: daily bars up to and including the prior session
        (used for prior_close/prior_high/prior_low, and swing_high/low
        over however many days are passed in).
    opening_range_bars: this session's RTH bars from open through 09:35 ET.
    historical_session_bars: RTH bars from prior sessions, one sequence per
        session, used to build avg_cum_volume_by_bar.
    """
    if not prior_daily_bars:
        raise ValueError("need at least one prior daily bar to build anchors")
    if not opening_range_bars:
        raise ValueError("need opening range bars to build anchors")

    prior = prior_daily_bars[-1]
    swing_high = max(b.high for b in prior_daily_bars)
    swing_low = min(b.low for b in prior_daily_bars)

    cum_by_bar_slot: dict[int, list[int]] = {}
    for session_bars in historical_session_bars:
        cum = 0
        seen_slots: set[int] = set()
        for b in session_bars:
            slot = _rth_bar_slot(b)
            if slot is None or slot in seen_slots:
                continue
            seen_slots.add(slot)
            cum += b.volume
            cum_by_bar_slot.setdefault(slot, []).append(cum)
    avg_cum_volume_by_bar = {
        slot: sum(values) / len(values) for slot, values in cum_by_bar_slot.items()
    }

    return DailyAnchors(
        symbol=symbol,
        session_date=session_date,
        prior_close=prior.close,
        prior_high=prior.high,
        prior_low=prior.low,
        opening_range_high=max(b.high for b in opening_range_bars),
        opening_range_low=min(b.low for b in opening_range_bars),
        opening_range_volume=sum(b.volume for b in opening_range_bars),
        swing_high=swing_high,
        swing_low=swing_low,
        avg_cum_volume_by_bar=avg_cum_volume_by_bar,
    )


def _check_symbol(bars: Sequence[Bar], anchors: DailyAnchors) -> None:
    if bars and bars[-1].symbol != anchors.symbol:
        raise ValueError(
            f"bars are for {bars[-1].symbol} but anchors are for {anchors.symbol}"
        )


def level_break(
    bars: Sequence[Bar], anchors: DailyAnchors, atr_units: float = 0.5
) -> Detection | None:
    """Fires once, on the bar where the close first crosses beyond
    prior_high/prior_low or the opening range by more than atr_units *
    ATR — not on every later bar that remains beyond it. Needs a previous
    bar to tell a fresh crossing from an already-broken level."""
    _check_symbol(bars, anchors)
    if len(bars) < 2:
        return None
    window = atr(bars)
    if window is None or window <= 0:
        return None

    last, prev = bars[-1], bars[-2]
    levels = {
        "prior_high": (anchors.prior_high, "up"),
        "prior_low": (anchors.prior_low, "down"),
        "opening_range_high": (anchors.opening_range_high, "up"),
        "opening_range_low": (anchors.opening_range_low, "down"),
        "swing_high": (anchors.swing_high, "up"),
        "swing_low": (anchors.swing_low, "down"),
    }
    threshold = atr_units * window

    best = None
    for name, (level, direction) in levels.items():
        move = (last.close - level) if direction == "up" else (level - last.close)
        if move <= threshold:
            continue
        prev_move = (prev.close - level) if direction == "up" else (level - prev.close)
        if prev_move > threshold:
            continue  # already broken as of the previous bar — not a fresh crossing
        score = move / window
        if best is None or score > best[0]:
            best = (score, name, level, direction, move)

    if best is None:
        return None
    score, name, level, direction, move = best
    return Detection(
        symbol=last.symbol,
        kind="level_break",
        ts=bar_close_ts(last),
        score=score,
        headline=f"{last.symbol} broke {name} ({level:.2f}) {direction}, {score:.2f} ATR",
        context={
            "level_name": name,
            "level": level,
            "close": last.close,
            "atr14": window,
            "direction": direction,
        },
    )


def rvol_spike(
    bars: Sequence[Bar], anchors: DailyAnchors, spike_ratio: float = 3.0
) -> Detection | None:
    """Fires once, on the bar where cumulative session volume first crosses
    spike_ratio times the historical average cumulative volume for this
    time-of-day (anchors.avg_cum_volume_by_bar) — not on every later bar
    while volume remains elevated, since cumulative volume never falls
    back down within a session."""
    _check_symbol(bars, anchors)
    if len(bars) < 2:
        return None
    last, prev = bars[-1], bars[-2]
    bar_slot = _rth_bar_slot(last)
    prev_bar_slot = _rth_bar_slot(prev)
    if bar_slot is None or prev_bar_slot is None or prev_bar_slot >= bar_slot:
        return None
    baseline = anchors.avg_cum_volume_by_bar.get(bar_slot)
    prev_baseline = anchors.avg_cum_volume_by_bar.get(prev_bar_slot)
    if not baseline or not prev_baseline:
        return None
    cum_volume = sum(b.volume for b in bars)
    prev_cum_volume = cum_volume - last.volume
    ratio = cum_volume / baseline
    if ratio < spike_ratio:
        return None
    prev_ratio = prev_cum_volume / prev_baseline
    if prev_ratio >= spike_ratio:
        return None  # already spiking as of the previous bar — not a fresh crossing
    return Detection(
        symbol=last.symbol,
        kind="rvol_spike",
        ts=bar_close_ts(last),
        score=ratio,
        headline=(
            f"{last.symbol} cumulative volume {cum_volume:,} is {ratio:.1f}x "
            f"the {bar_slot + 1}-bar average ({baseline:,.0f})"
        ),
        context={"cum_volume": cum_volume, "baseline": baseline, "bar_index": bar_slot},
    )


def range_expansion(
    bars: Sequence[Bar],
    anchors: DailyAnchors,
    period: int = 14,
    atr_multiple: float = 2.0,
) -> Detection | None:
    """Fires when the latest bar's high-low range exceeds atr_multiple
    times the trailing ATR computed on the bars before it."""
    _check_symbol(bars, anchors)
    if len(bars) < period + 2:
        return None
    window = atr(bars[:-1], period=period)
    if window is None or window <= 0:
        return None
    last = bars[-1]
    bar_range = last.high - last.low
    ratio = bar_range / window
    if ratio < atr_multiple:
        return None
    return Detection(
        symbol=last.symbol,
        kind="range_expansion",
        ts=bar_close_ts(last),
        score=ratio,
        headline=f"{last.symbol} bar range {bar_range:.2f} is {ratio:.1f}x ATR({period})={window:.2f}",
        context={"bar_range": bar_range, "atr14": window},
    )


def vwap(bars: Sequence[Bar]) -> float | None:
    """Session-to-date volume-weighted average price, using typical price
    (high+low+close)/3 per bar. None if there's no volume yet."""
    if not bars:
        return None
    cum_pv = 0.0
    cum_volume = 0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3
        cum_pv += typical * b.volume
        cum_volume += b.volume
    if cum_volume == 0:
        return None
    return cum_pv / cum_volume


def vwap_break(
    bars: Sequence[Bar], anchors: DailyAnchors, atr_units: float = 0.5
) -> Detection | None:
    """Fires once, on the bar where the close first moves more than
    atr_units * ATR away from session VWAP on the side it wasn't on before
    — not on every later bar that stays on that side. Uses the current
    bar's VWAP as the reference for both bars, since VWAP itself moves
    every bar and comparing against two different VWAP values would make
    'already broken' ambiguous."""
    _check_symbol(bars, anchors)
    if len(bars) < 2:
        return None
    window = atr(bars)
    if window is None or window <= 0:
        return None
    current_vwap = vwap(bars)
    if current_vwap is None:
        return None

    last, prev = bars[-1], bars[-2]
    threshold = atr_units * window
    move = last.close - current_vwap
    prev_move = prev.close - current_vwap

    if abs(move) <= threshold:
        return None
    if abs(prev_move) > threshold and (move > 0) == (prev_move > 0):
        return None  # already on this side of VWAP as of the previous bar

    direction = "above" if move > 0 else "below"
    score = abs(move) / window
    return Detection(
        symbol=last.symbol,
        kind="vwap_break",
        ts=bar_close_ts(last),
        score=score,
        headline=f"{last.symbol} broke {direction} VWAP ({current_vwap:.2f}), {score:.2f} ATR",
        context={"vwap": current_vwap, "close": last.close, "atr14": window, "direction": direction},
    )


def _round_increment(price: float) -> float:
    """Spacing between 'round number' levels, scaled to price magnitude —
    $1 under $20, $5 under $100, $10 under $500, $25 above."""
    if price < 20:
        return 1.0
    if price < 100:
        return 5.0
    if price < 500:
        return 10.0
    return 25.0


def round_number_break(
    bars: Sequence[Bar], anchors: DailyAnchors, atr_units: float = 0.5
) -> Detection | None:
    """Fires once, on the bar where price crosses the nearest round-number
    level (spacing scaled to price — see _round_increment) and closes at
    least atr_units * ATR past it."""
    _check_symbol(bars, anchors)
    if len(bars) < 2:
        return None
    window = atr(bars)
    if window is None or window <= 0:
        return None

    last, prev = bars[-1], bars[-2]
    increment = _round_increment(last.close)
    level = round(last.close / increment) * increment

    crossed_up = prev.close < level <= last.close
    crossed_down = prev.close > level >= last.close
    if not (crossed_up or crossed_down):
        return None

    move = abs(last.close - level)
    threshold = atr_units * window
    if move < threshold:
        return None

    direction = "above" if crossed_up else "below"
    score = move / window
    return Detection(
        symbol=last.symbol,
        kind="round_number_break",
        ts=bar_close_ts(last),
        score=score,
        headline=f"{last.symbol} crossed {direction} round number {level:.2f}, {score:.2f} ATR past it",
        context={"level": level, "close": last.close, "atr14": window, "direction": direction},
    )


def gap(
    bars: Sequence[Bar], anchors: DailyAnchors, atr_units: float = 0.75
) -> Detection | None:
    """Fires only on the session's first bar, when the open gaps from
    prior_close by more than atr_units times the prior session's true
    range (prior_high - prior_low).

    Earlier versions used the first bar's own high-low range as the proxy
    (a same-session ATR isn't available yet at the first bar). That's
    fragile: a single 5-minute bar, especially in thin opening liquidity,
    can have a near-zero range independent of how large the real gap is,
    which either divides out to an astronomically large, meaningless score
    (real example: USO premarket prints with high==low on ~100 shares
    volume) or gets suppressed by an arbitrary epsilon floor. A full prior
    session's range is a stable, always-meaningful baseline instead."""
    _check_symbol(bars, anchors)
    if len(bars) != 1:
        return None
    first = bars[0]
    proxy_range = anchors.prior_high - anchors.prior_low
    if proxy_range <= 0:
        return None
    gap_size = first.open - anchors.prior_close
    score = abs(gap_size) / proxy_range
    if score < atr_units:
        return None
    direction = "up" if gap_size > 0 else "down"
    return Detection(
        symbol=first.symbol,
        kind="gap",
        ts=bar_close_ts(first),
        score=score,
        headline=f"{first.symbol} gapped {direction} {abs(gap_size):.2f} from prior close {anchors.prior_close:.2f}",
        context={"gap_size": gap_size, "prior_close": anchors.prior_close, "open": first.open},
    )


DETECTORS = (level_break, rvol_spike, range_expansion, vwap_break, round_number_break, gap)


def relative_strength_break(
    bars: Sequence[Bar],
    anchors: DailyAnchors,
    market_bars: Mapping[str, Sequence[Bar]] | None = None,
    atr_units: float = 1.0,  # PLACEHOLDER — needs a replay calibration pass, see detectors.py:13-35's discipline
    market_proxy: str = DEFAULT_MARKET_PROXY,
) -> Detection | None:
    """Fires when `symbol`'s own return since this session's open diverges
    from the broad market's (market_proxy, default SPY) return by more
    than atr_units * ATR, expressed as a $ divergence normalized by the
    symbol's own ATR — never a bare percentage (see CLAUDE.md: all
    thresholds in ATR units). Distinguishes "everything is moving" from
    "this symbol is behaving unusually" — nothing else in DETECTORS
    checks a symbol against anything but its own history.

    market_bars is a {symbol: bars} map the caller (runner.py) builds
    once per while-loop iteration -- one shared fetch per proxy per
    tick, reused across every symbol evaluated that iteration, not
    refetched per symbol. Symbol and proxy bars are aligned by exact bar
    timestamp, so a missing bar in either stream cannot shift all later
    comparisons. This has no dependency on WATCHLIST iteration order.

    Returns None — never raises — on any missing/short/misaligned
    market_bars, same fail-conservative discipline as every other
    None-guard in this module: a signal that can't be corroborated
    against the market isn't fabricated, it's just not fired."""
    _check_symbol(bars, anchors)
    if len(bars) < 2 or bars[-1].symbol in MARKET_PROXY_SYMBOLS:
        return None
    window = atr(bars)
    if window is None or window <= 0:
        return None
    if not market_bars:
        return None
    proxy_bars = market_bars.get(market_proxy)
    if not proxy_bars:
        return None

    last, prev = bars[-1], bars[-2]
    if prev.ts >= last.ts or _rth_bar_slot(bars[0]) != 0:
        return None
    proxy_by_ts = {bar.ts: bar for bar in proxy_bars}
    if len(proxy_by_ts) != len(proxy_bars):
        return None
    symbol_open = bars[0]
    proxy_open = proxy_by_ts.get(symbol_open.ts)
    proxy_prev = proxy_by_ts.get(prev.ts)
    proxy_last = proxy_by_ts.get(last.ts)
    if proxy_open is None or proxy_prev is None or proxy_last is None:
        return None  # required proxy timestamps are absent — never fabricate alignment
    if symbol_open.close <= 0 or proxy_open.close <= 0:
        return None

    def divergence_dollars(sym_bar: Bar, proxy_bar: Bar) -> float:
        symbol_return = (sym_bar.close - symbol_open.close) / symbol_open.close
        market_return = (proxy_bar.close - proxy_open.close) / proxy_open.close
        return (symbol_return - market_return) * sym_bar.close

    move = divergence_dollars(last, proxy_last)
    prev_move = divergence_dollars(prev, proxy_prev)
    threshold = atr_units * window
    if abs(move) <= threshold:
        return None
    if abs(prev_move) > threshold and (move > 0) == (prev_move > 0):
        return None  # already diverging as of the previous bar — not a fresh crossing

    direction = "outperforming" if move > 0 else "underperforming"
    score = abs(move) / window
    return Detection(
        symbol=last.symbol,
        kind="relative_strength_break",
        ts=bar_close_ts(last),
        score=score,
        headline=f"{last.symbol} {direction} {market_proxy} by {score:.2f} ATR since the open",
        context={"market_proxy": market_proxy, "divergence": move, "atr14": window},
    )


CONTEXT_DETECTORS = (relative_strength_break,)


def score_cluster(detections: Sequence[Detection]) -> float:
    """Combine same-bar detections into one cluster score: the strongest
    individual score, plus a partial bonus per corroborating detector, so
    several weaker signals firing together can outscore one strong signal
    without letting duplicate near-identical detectors dominate."""
    if not detections:
        raise ValueError("cannot score an empty cluster")
    scores = sorted((d.score for d in detections), reverse=True)
    return scores[0] + 0.25 * sum(scores[1:])


def tier_for_score(score: float) -> Tier:
    if score >= TIER_HIGH:
        return Tier.HIGH
    if score >= TIER_MEDIUM:
        return Tier.MEDIUM
    return Tier.LOW
