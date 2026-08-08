"""Market data access layer.

All market data reads go through the MarketData protocol — see CLAUDE.md.
No vendor SDK imports belong here; vendor-specific code lives in its own
adapter module under tradebot/vendors/.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from tradebot.detectors import Bar

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime
    bid: float
    ask: float
    last: float


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

    def quote(self, symbol: str) -> Quote:
        ...

    def chain(self, symbol: str, expiry: date) -> OptionChain:
        ...


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


def _is_rth(bar: Bar) -> bool:
    local = bar.ts.astimezone(ET)
    return (9, 30) <= (local.hour, local.minute) < (16, 0)


def _is_premarket(bar: Bar) -> bool:
    local = bar.ts.astimezone(ET)
    return (4, 0) <= (local.hour, local.minute) < (9, 30)


class ReplayMarketData:
    """Reads cached CSVs from data/cache/{symbol}/ and replays one session
    bar by bar behind an internal cursor.

    This is the project's lookahead guard: session_bars() and
    premarket_bars() only ever return bars revealed so far by advance().
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

    def _check(self, symbol: str) -> None:
        if symbol != self.symbol:
            raise ValueError(f"this LiveMarketData is scoped to {self.symbol}, not {symbol}")

    def daily_bars(self, symbol: str, n: int) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_daily_bars

        self._check(symbol)
        bars = fetch_daily_bars(symbol, n + 5)
        eligible = [b for b in bars if b.ts.astimezone(ET).date() < self.session_date]
        return tuple(eligible[-n:])

    def session_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_intraday_bars

        self._check(symbol)
        return tuple(b for b in fetch_intraday_bars(symbol, session_date) if _is_rth(b))

    def premarket_bars(self, symbol: str, session_date: date) -> Sequence[Bar]:
        from tradebot.vendors.alpaca import fetch_intraday_bars

        self._check(symbol)
        return tuple(b for b in fetch_intraday_bars(symbol, session_date) if _is_premarket(b))

    def quote(self, symbol: str) -> Quote:
        from tradebot.vendors.alpaca import fetch_latest_quote

        self._check(symbol)
        return fetch_latest_quote(symbol)

    def chain(self, symbol: str, expiry: date) -> OptionChain:
        from tradebot.vendors.alpaca import fetch_option_chain

        self._check(symbol)
        return fetch_option_chain(symbol, expiry)
