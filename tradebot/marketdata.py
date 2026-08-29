"""Market data access layer.

All market data reads go through the MarketData protocol — see CLAUDE.md.
No vendor SDK imports belong here; vendor-specific code lives in its own
adapter module under tradebot/vendors/.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.detectors import Bar

ET = ZoneInfo("America/New_York")
XNYS = ecals.get_calendar("XNYS")

# Proposal 5c (docs/open-awareness-proposals-2026-08.md): a cached
# session is implausible -- and must never join a baseline (rvol,
# P1's future TR profile) or be written to disk at close -- when its
# RTH volume or bar count is a fraction of what a healthy session
# looks like. Re-verified 2026-08-17 by running this shipped code (not
# the doc's own scratch script) against all 2,448 local cache files:
# the volume floor trips 3 files (0.12%; the doc's own scratch
# calibration found 4 -- the gap is this implementation computing each
# session's reference from the trailing window of already-ACCEPTED
# sessions rather than raw surrounding history, a deliberate choice --
# see filter_plausible_sessions), all genuinely degenerate IEX-thin USO
# days. The bar-count floor at 50% (not a tighter 75%) trips zero of
# those same files: the thinnest local session is exactly 39 of 78
# expected bars (50% on the nose), and real IEX-thin USO sessions have
# been observed as low as 38 bars without being early closes -- 75%
# would have false-tripped on the whole cluster.
VOLUME_FLOOR_PCT = 0.20
BAR_COUNT_FLOOR_PCT = 0.50
PLAUSIBILITY_WINDOW_SESSIONS = 20  # trailing sessions the volume median is computed over


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime
    bid: float
    ask: float
    last: float
    bid_size: float | None = None
    ask_size: float | None = None


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    expiry: date
    strike: float
    right: str  # "call" | "put"
    bid: float
    ask: float
    last: float
    delta: float | None
    theta: float | None
    open_interest: int
    implied_volatility: float | None = None
    # Real cumulative day volume for this one contract — NOT populated by
    # a chain fetch (the snapshot endpoint used for the rest of the chain
    # doesn't carry it; see vendors.alpaca.fetch_option_day_volume). None
    # means "not looked up," not "zero" — costs.py treats them differently.
    day_volume: int | None = None


@dataclass(frozen=True)
class OptionChain:
    symbol: str
    expiry: date
    contracts: Sequence[OptionContract]


@dataclass(frozen=True)
class AssetInfo:
    """One tradable security as Alpaca's asset catalog describes it — the
    unit tradebot.universe discovers, diffs, and stores. `attributes` is
    kept verbatim (not just the two booleans derived from it) so a future
    need (e.g. 'ipo', fractional-trading eligibility) doesn't require a
    second live-verified adapter change to find out what Alpaca actually
    calls it — see vendors.alpaca.fetch_us_equity_assets's docstring for
    the real, observed attribute vocabulary this was built against.
    overnight_eligible is None (not False) when neither 'overnight_tradable'
    nor 'overnight_halted' is present — Alpaca doesn't tag every asset,
    and "not tagged" is not the same claim as "confirmed not eligible"."""

    symbol: str
    exchange: str
    name: str
    tradable: bool
    options_enabled: bool
    overnight_eligible: bool | None
    attributes: tuple[str, ...]


@dataclass(frozen=True)
class IntradaySessionBars:
    """One provider snapshot partitioned by actual exchange session."""

    premarket: tuple[Bar, ...]
    rth: tuple[Bar, ...]
    postmarket: tuple[Bar, ...]


@dataclass(frozen=True)
class MarketScreenEntry:
    """One attributable row returned by a provider market-wide screener."""

    symbol: str
    source: str
    rank: int
    source_updated_at: datetime
    move_pct: float | None = None
    price: float | None = None
    volume: float | None = None
    trade_count: float | None = None


@dataclass(frozen=True)
class MarketWideScreen:
    """Bounded provider results whose upstream scope is the stock market."""

    entries: tuple[MarketScreenEntry, ...]
    requested_top_n: int
    provider: str
    feed: str
    endpoints: tuple[str, ...]
    source_updates: tuple[tuple[str, datetime], ...]


class MarketData(Protocol):
    def daily_bars(self, symbol: str, n: int) -> Sequence[Bar]:
        """The n most recent daily bars, oldest first."""
        ...

    def session_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        """5-minute RTH (09:30-16:00 ET) bars for one session, oldest first."""
        ...

    def premarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        """5-minute premarket (04:00-09:30 ET) bars for one session, oldest first."""
        ...

    def postmarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        """5-minute bars from the real XNYS close through 20:00 ET."""
        ...

    def intraday_snapshot(self, symbol: str, session_date: date) -> IntradaySessionBars:
        """All session partitions derived from one provider snapshot."""
        ...

    def quote(self, symbol: str) -> Quote:
        ...

    def chain(self, symbol: str, expiry: date) -> OptionChain:
        ...


def partition_intraday_bars(bars: Sequence[Bar]) -> IntradaySessionBars:
    """Partition one already-fetched snapshot without reordering its data."""
    return IntradaySessionBars(
        premarket=tuple(bar for bar in bars if _is_premarket(bar)),
        rth=tuple(bar for bar in bars if _is_rth(bar)),
        postmarket=tuple(bar for bar in bars if _is_postmarket(bar)),
    )


def _read_bars(path: Path, symbol: str) -> list[Bar]:
    if not path.exists():
        return []
    bars = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def write_bars_csv(path: Path, bars: list[Bar]) -> None:
    """The write-side counterpart to _read_bars -- same CSV shape, so a
    file this writes is a file _read_bars (and therefore ReplayMarketData/
    backfill_marks) can read straight back. Moved here from
    scripts/fetch_cache.py (2026-08-12) so tradebot.runner can call it
    too, without needing scripts/ importable inside the container -- see
    docs/DEPLOYMENT.md's fetch_cache note about why scripts/ isn't part
    of the image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])


@lru_cache(maxsize=512)
def _session_bounds_utc(session_date: date) -> tuple[datetime, datetime] | None:
    """Real XNYS bounds for a date, cached for bar-level predicates."""
    if not XNYS.is_session(session_date):
        return None
    return (
        XNYS.session_open(session_date).to_pydatetime().astimezone(timezone.utc),
        XNYS.session_close(session_date).to_pydatetime().astimezone(timezone.utc),
    )


def _is_rth(bar: Bar) -> bool:
    """Whether a bar opens inside the actual XNYS session.

    The old fixed ``09:30 <= t < 16:00`` rule mislabeled 13:00-16:00 ET
    prints as RTH on early-close sessions. That corrupts both the session
    close baseline and any postmarket reaction measured from it.
    """
    local_date = bar.ts.astimezone(ET).date()
    bounds = _session_bounds_utc(local_date)
    return bounds is not None and bounds[0] <= bar.ts < bounds[1]


def _is_premarket(bar: Bar) -> bool:
    local = bar.ts.astimezone(ET)
    bounds = _session_bounds_utc(local.date())
    if bounds is None:
        return False
    premarket_open = local.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return premarket_open <= bar.ts < bounds[0]


def _is_postmarket(bar: Bar) -> bool:
    """Whether a bar opens after the real close and before 20:00 ET."""
    local = bar.ts.astimezone(ET)
    bounds = _session_bounds_utc(local.date())
    if bounds is None:
        return False
    postmarket_end = local.replace(hour=20, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return bounds[1] <= bar.ts < postmarket_end


def median_session_volume(session_totals: Sequence[float]) -> float | None:
    """Median of a symbol's per-session RTH volume totals -- the
    plausibility floor's volume reference. None (not 0) if there is
    nothing to compare against yet, since "no history" is not the same
    claim as "history says zero volume is normal"."""
    if not session_totals:
        return None
    ordered = sorted(session_totals)
    n = len(ordered)
    mid = n // 2
    return float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def implausible_session_reason(
    bars: Sequence[Bar], *, median_volume: float | None, expected_bar_count: int
) -> str | None:
    """Proposal 5c's plausibility floor. `bars` must already be RTH-only
    (see _is_rth) -- this makes no attempt to filter premarket bars out
    itself, so a caller handing it a mixed session would silently pollute
    both checks.

    median_volume: the symbol's trailing-window median RTH session
    volume (see median_session_volume) -- None skips the volume check
    entirely (nothing plausible/implausible to compare against for a
    symbol's first cached sessions).

    expected_bar_count: the calendar-expected RTH bar count for this
    exact session date (see runner.expected_rth_bar_count) -- computed
    per-session so an early close is never mistaken for a runt.

    Returns a rejection reason (stable, short rule-name prefix, mirroring
    tradebot.guard's convention) or None if the session passes."""
    total_volume = sum(b.volume for b in bars)
    if median_volume is not None and median_volume > 0:
        floor = VOLUME_FLOOR_PCT * median_volume
        if total_volume < floor:
            return (
                f"implausible_volume: {total_volume:,} RTH volume is below "
                f"{VOLUME_FLOOR_PCT * 100:.0f}% of the {median_volume:,.0f} trailing "
                f"{PLAUSIBILITY_WINDOW_SESSIONS}-session median ({floor:,.0f})"
            )

    bar_floor = BAR_COUNT_FLOOR_PCT * expected_bar_count
    if len(bars) < bar_floor:
        return (
            f"implausible_bar_count: {len(bars)} RTH bars is below "
            f"{BAR_COUNT_FLOOR_PCT * 100:.0f}% of the {expected_bar_count} calendar-expected "
            f"({bar_floor:.0f})"
        )
    return None


def filter_plausible_sessions(
    sessions: Sequence[tuple[date, Sequence[Bar]]],
    expected_bar_count_fn: Callable[[date], int],
    *,
    window: int = PLAUSIBILITY_WINDOW_SESSIONS,
) -> tuple[list[Sequence[Bar]], list[tuple[date, str]]]:
    """Applies the plausibility floor to an ordered (oldest first)
    sequence of one symbol's cached sessions, e.g. before they can join
    the rvol baseline (avg_cum_volume_by_bar) or a future TR profile.

    Each session's volume is judged against the median of the `window`
    sessions immediately preceding it THAT ALREADY PASSED the floor --
    not the raw surrounding history -- so a run of runt files can't drag
    the reference down for the sessions after them. A session with fewer
    than one prior accepted session only faces the bar-count check (see
    implausible_session_reason).

    Returns (accepted RTH bar sequences, [(date, reason), ...]
    rejections), both oldest-first."""
    accepted: list[Sequence[Bar]] = []
    accepted_totals: list[float] = []
    rejections: list[tuple[date, str]] = []
    for session_date, bars in sessions:
        median_volume = median_session_volume(accepted_totals[-window:])
        reason = implausible_session_reason(
            bars, median_volume=median_volume, expected_bar_count=expected_bar_count_fn(session_date)
        )
        if reason is not None:
            rejections.append((session_date, reason))
            continue
        accepted.append(bars)
        accepted_totals.append(sum(b.volume for b in bars))
    return accepted, rejections


class ReplayMarketData:
    """Reads cached CSVs from data/cache/{symbol}/ and replays one session
    bar by bar behind an internal cursor.

    This is the project's lookahead guard: session_bars() and
    premarket_bars()/postmarket_bars() only ever return bars revealed so far by advance().
    No caller — detector, replay harness, or anything else — can see a bar
    at or beyond the cursor, no matter what else is sitting in the cache
    file on disk.
    """

    def __init__(
        self,
        cache_dir: Path | str,
        symbol: str,
        session_date: date,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.symbol = symbol
        self.session_date = session_date
        symbol_dir = self.cache_dir / symbol
        self._daily = _read_bars(symbol_dir / "daily.csv", symbol)
        self._all_session_bars = _read_bars(
            symbol_dir / f"intraday_{session_date.isoformat()}.csv", symbol
        )
        self._cursor = 0  # number of bars revealed so far

    def _check(self, symbol: str, session_date: date) -> None:
        if symbol != self.symbol:
            raise ValueError(
                f"this ReplayMarketData is scoped to {self.symbol}, not {symbol}"
            )
        if session_date != self.session_date:
            raise ValueError(
                f"this ReplayMarketData is scoped to {self.session_date}, not {session_date}"
            )

    @property
    def _visible(self) -> list[Bar]:
        return self._all_session_bars[: self._cursor]

    def daily_bars(self, symbol: str, n: int) -> Sequence[Bar]:
        if symbol != self.symbol:
            raise ValueError(
                f"this ReplayMarketData is scoped to {self.symbol}, not {symbol}"
            )
        # Never hand back a daily bar dated on or after the session being
        # replayed — the cache holds the 60 most recent bars as of fetch
        # time, which for an old replay session includes bars from its
        # future.
        eligible = [b for b in self._daily if b.ts.astimezone(ET).date() < self.session_date]
        return tuple(eligible[-n:])

    def session_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        self._check(symbol, session_date)
        return tuple(b for b in self._visible if _is_rth(b))

    def premarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        self._check(symbol, session_date)
        return tuple(b for b in self._visible if _is_premarket(b))

    def postmarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        self._check(symbol, session_date)
        return tuple(b for b in self._visible if _is_postmarket(b))

    def intraday_snapshot(self, symbol: str, session_date: date) -> IntradaySessionBars:
        self._check(symbol, session_date)
        return partition_intraday_bars(self._visible)

    def quote(self, symbol: str) -> Quote:
        raise NotImplementedError("ReplayMarketData has no live quotes; use session_bars()")

    def chain(self, symbol: str, expiry: date) -> OptionChain:
        raise NotImplementedError(
            "ReplayMarketData has no historical options chain cache — costs.select_contract() "
            "will report 'no tradable contract' rather than mix live greeks into a "
            "historical replay"
        )

    def advance(self) -> bool:
        """Reveal the next bar. Returns False once nothing is left to
        reveal (the cursor does not move past the end)."""
        if self._cursor >= len(self._all_session_bars):
            return False
        self._cursor += 1
        return True


class LiveMarketData:
    """Polls the Alpaca adapter for the current session's bars and quote.

    Live mode only. Unlike ReplayMarketData, this has not been exercised
    against real live market conditions in this build — only verified to
    construct correctly and delegate to the (separately tested) Alpaca
    adapter functions. Treat --live as unverified until it's actually run
    during market hours.

    Imports from tradebot.vendors.alpaca are deferred to method bodies:
    that module imports Quote from this one, so a top-level import here
    would be circular.
    """

    def __init__(self, symbol: str, session_date: date) -> None:
        self.symbol = symbol
        self.session_date = session_date

    def _check(self, symbol: str, session_date: date | None = None) -> None:
        if symbol != self.symbol:
            raise ValueError(f"this LiveMarketData is scoped to {self.symbol}, not {symbol}")
        if session_date is not None and session_date != self.session_date:
            raise ValueError(
                f"this LiveMarketData is scoped to {self.session_date}, not {session_date}"
            )

    def daily_bars(self, symbol: str, n: int) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_daily_bars

        self._check(symbol)
        bars = fetch_daily_bars(symbol, n + 5)
        eligible = [b for b in bars if b.ts.astimezone(ET).date() < self.session_date]
        return tuple(eligible[-n:])

    def session_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_intraday_bars

        self._check(symbol, session_date)
        return tuple(b for b in fetch_intraday_bars(symbol, session_date) if _is_rth(b))

    def premarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_intraday_bars

        self._check(symbol, session_date)
        return tuple(b for b in fetch_intraday_bars(symbol, session_date) if _is_premarket(b))

    def postmarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_intraday_bars

        self._check(symbol, session_date)
        return tuple(b for b in fetch_intraday_bars(symbol, session_date) if _is_postmarket(b))

    def intraday_snapshot(self, symbol: str, session_date: date) -> IntradaySessionBars:
        """Fetch one session snapshot once per scoped MarketData object.

        The postmarket observer needs both the official RTH close and the
        extended-hours reaction. They must come from one provider snapshot:
        two independent calls waste half the request budget and can observe
        different revisions of the same five-minute bar.
        """
        from tradebot.vendors.alpaca import fetch_intraday_bars

        self._check(symbol, session_date)
        bars = tuple(fetch_intraday_bars(symbol, session_date))
        return partition_intraday_bars(bars)

    def quote(self, symbol: str) -> Quote:
        from tradebot.vendors.alpaca import fetch_latest_quote

        self._check(symbol)
        return fetch_latest_quote(symbol)

    def chain(self, symbol: str, expiry: date) -> OptionChain:
        from tradebot.vendors.alpaca import fetch_option_chain

        self._check(symbol)
        return fetch_option_chain(symbol, expiry)


def fetch_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Live mode only -- the only function in this module not scoped to a
    single MarketData instance, since batching many symbols into one
    vendor call is a different shape of operation than everything else
    here (all per-symbol). Deferred import for the same reason
    LiveMarketData's methods defer theirs: tradebot.vendors.alpaca
    imports Quote from this module, so a top-level import here would be
    circular."""
    from tradebot.vendors.alpaca import fetch_latest_quotes

    return fetch_latest_quotes(symbols)
