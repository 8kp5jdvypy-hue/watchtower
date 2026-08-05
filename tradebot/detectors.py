"""Pure detector functions and session anchors for the watchtower scanner.

Detectors take market data in and return a Detection | None. No I/O, no
network, no clock reads, no globals — see CLAUDE.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Mapping, Sequence

# Calibrated 2026-08-05 from a replay of 20 cached sessions x the full
# watchlist (577 clusters): mean 2.40 HIGH/day, 10.60 MEDIUM/day, no
# symbol over 27% of HIGH. Re-derive from out/replay_detections.csv if the
# watchlist or detectors change materially.
TIER_HIGH = 3.0
TIER_MEDIUM = 1.5


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
    # bar index (0 = first RTH bar of the day) -> historical average
    # cumulative volume through that bar, used by rvol_spike.
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


def build_anchors(
    symbol: str,
    session_date: date,
    prior_daily_bars: Sequence[Bar],
    opening_range_bars: Sequence[Bar],
    historical_session_bars: Sequence[Sequence[Bar]],
) -> DailyAnchors:
    """Build and freeze the day's anchor levels.

    prior_daily_bars: daily bars up to and including the prior session
        (used for prior_close/prior_high/prior_low).
    opening_range_bars: this session's RTH bars from open through 09:35 ET.
    historical_session_bars: RTH bars from prior sessions, one sequence per
        session, used to build avg_cum_volume_by_bar.
    """
    if not prior_daily_bars:
        raise ValueError("need at least one prior daily bar to build anchors")
    if not opening_range_bars:
        raise ValueError("need opening range bars to build anchors")

    prior = prior_daily_bars[-1]

    cum_by_bar_index: dict[int, list[int]] = {}
    for session_bars in historical_session_bars:
        cum = 0
        for i, b in enumerate(session_bars):
            cum += b.volume
            cum_by_bar_index.setdefault(i, []).append(cum)
    avg_cum_volume_by_bar = {
        i: sum(values) / len(values) for i, values in cum_by_bar_index.items()
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
    last = bars[-1]
    bar_index = len(bars) - 1
    baseline = anchors.avg_cum_volume_by_bar.get(bar_index)
    prev_baseline = anchors.avg_cum_volume_by_bar.get(bar_index - 1)
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
            f"the {bar_index + 1}-bar average ({baseline:,.0f})"
        ),
        context={"cum_volume": cum_volume, "baseline": baseline, "bar_index": bar_index},
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


def gap(
    bars: Sequence[Bar], anchors: DailyAnchors, atr_units: float = 0.75
) -> Detection | None:
    """Fires only on the session's first bar, when the open gaps from
    prior_close by more than atr_units times that first bar's own range
    (a same-session ATR isn't available yet at the first bar)."""
    _check_symbol(bars, anchors)
    if len(bars) != 1:
        return None
    first = bars[0]
    gap_size = first.open - anchors.prior_close
    proxy_range = max(first.high - first.low, 1e-9)
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


DETECTORS = (level_break, rvol_spike, range_expansion, gap)


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
