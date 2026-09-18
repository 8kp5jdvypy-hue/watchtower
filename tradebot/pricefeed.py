"""Price feed: last prices for display/alert context, from free official
sources with ordered fallback and a TTL cache.

Decision 2026-09-17 (docs/DECISIONS.md, cost arithmetic in
docs/price-feed-cost-note-2026-09.md): prices do NOT come from
SocialCrawl. At 5 credits per /v1/finance/quote call, ten instruments
polled every five minutes is ~$1,080/month for data that Coinbase,
Kraken and Alpaca hand out for $0. SocialCrawl is not a PriceSource
here and must not become one; its single justified use (Polymarket
research) is a different job and lives outside this module.

Shape:

- `Instrument` names one thing Perch wants a price for and how each
  vendor spells it. `PriceSource` is the one protocol every vendor is
  wrapped in: batch in, {symbol: PricePoint} out, a raised exception
  means "this source failed", an absent symbol means "this source
  doesn't have it". Both are fallback triggers; they're logged
  differently.
- `PriceFeed.get(symbols)` serves from cache while an entry is younger
  than `ttl`; otherwise walks that asset class's sources in order,
  moving only still-missing symbols to the next source. If every
  source fails and a cached value younger than `max_stale` exists, it
  is served with `stale=True` -- visibly, never silently. Past
  `max_stale` the symbol is absent; nothing is ever fabricated.
- A source that raised is skipped for `cooldown` seconds so a dead
  vendor costs one timeout, not one timeout per poll.
- The clock is injected (`now_fn`) so all of the above is testable
  without sleeping; nothing here reads time on its own. Vendor
  modules are imported inside the source classes' fetch methods, the
  same deferred-import pattern marketdata.LiveMarketData uses, so
  importing this module never pulls in the Alpaca SDK.

Not a MarketData implementation: detectors never see these prices
(CLAUDE.md -- detectors get bars through the MarketData protocol and
nothing else). This feeds the Telegram bot / dashboard "current
price" line and alert context only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Protocol, Sequence

from tradebot.marketdata import PricePoint

logger = logging.getLogger("watchtower.pricefeed")

CRYPTO = "crypto"
EQUITY = "equity"

DEFAULT_TTL = timedelta(minutes=5)
DEFAULT_MAX_STALE = timedelta(minutes=30)
DEFAULT_COOLDOWN = timedelta(minutes=2)


@dataclass(frozen=True)
class Instrument:
    """One Perch symbol and each vendor's spelling of it. A vendor field
    left None means "this vendor can't serve this instrument" -- the
    source skips it rather than guessing a mapping."""

    symbol: str
    asset_class: str
    coinbase_product: str | None = None
    kraken_pair: str | None = None
    alpaca_symbol: str | None = None
    finnhub_symbol: str | None = None


def crypto(base: str, *, kraken_pair: str | None = None) -> Instrument:
    """USD-quoted crypto. Kraken spells bitcoin XBT; everything else
    we've needed so far matches Coinbase's base ticker."""
    return Instrument(
        symbol=base,
        asset_class=CRYPTO,
        coinbase_product=f"{base}-USD",
        kraken_pair=kraken_pair or f"{'XBT' if base == 'BTC' else base}USD",
    )


def equity(symbol: str) -> Instrument:
    return Instrument(symbol=symbol, asset_class=EQUITY, alpaca_symbol=symbol, finnhub_symbol=symbol)


# The instruments the bot actually shows. Kept explicit and short on
# purpose: every entry here is polled every `ttl`, so this list IS the
# request budget (see the cost note). Equities mirror the scanner's
# six named symbols in CLAUDE.md, not the whole config.WATCHLIST.
DEFAULT_INSTRUMENTS: tuple[Instrument, ...] = (
    crypto("BTC"),
    crypto("ETH"),
    crypto("SOL"),
    equity("SPY"),
    equity("QQQ"),
    equity("GOOGL"),
    equity("TSLA"),
    equity("BE"),
    equity("IONQ"),
)


class PriceSource(Protocol):
    name: str
    asset_class: str

    def fetch(self, instruments: Sequence[Instrument]) -> Mapping[str, PricePoint]:
        """Prices for as many of `instruments` as this source can serve.
        Raise on transport/provider failure; omit what it doesn't have."""
        ...


class CoinbaseSource:
    name = "coinbase"
    asset_class = CRYPTO

    def fetch(self, instruments: Sequence[Instrument]) -> Mapping[str, PricePoint]:
        from tradebot.vendors import coinbase

        out: dict[str, PricePoint] = {}
        for inst in instruments:
            if inst.coinbase_product is None:
                continue
            out[inst.symbol] = coinbase.fetch_ticker(inst.coinbase_product, symbol=inst.symbol)
        return out


class KrakenSource:
    name = "kraken"
    asset_class = CRYPTO

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn

    def fetch(self, instruments: Sequence[Instrument]) -> Mapping[str, PricePoint]:
        from tradebot.vendors import kraken

        pairs = [(i.symbol, i.kraken_pair) for i in instruments if i.kraken_pair is not None]
        kwargs = {"now_fn": self._now_fn} if self._now_fn is not None else {}
        return kraken.fetch_tickers(pairs, **kwargs)


class AlpacaIexSource:
    name = "alpaca_iex"
    asset_class = EQUITY

    def fetch(self, instruments: Sequence[Instrument]) -> Mapping[str, PricePoint]:
        from tradebot.vendors import alpaca

        symbols = [i.alpaca_symbol for i in instruments if i.alpaca_symbol is not None]
        if not symbols:
            return {}
        by_vendor = alpaca.fetch_latest_trade_prices(symbols)
        return {
            i.symbol: by_vendor[i.alpaca_symbol]
            for i in instruments
            if i.alpaca_symbol in by_vendor
        }


class FinnhubSource:
    name = "finnhub"
    asset_class = EQUITY

    def fetch(self, instruments: Sequence[Instrument]) -> Mapping[str, PricePoint]:
        from tradebot.vendors import finnhub

        out: dict[str, PricePoint] = {}
        for inst in instruments:
            if inst.finnhub_symbol is None:
                continue
            try:
                out[inst.symbol] = finnhub.fetch_quote(inst.finnhub_symbol)
            except finnhub.FinnhubError as exc:
                # One unknown symbol is a miss, not a source failure --
                # the remaining symbols still get their request.
                logger.info("pricefeed_source_miss source=%s symbol=%s reason=%s", self.name, inst.symbol, exc)
        return out


def default_sources(now_fn: Callable[[], datetime] | None = None) -> list[PriceSource]:
    """Primary-then-fallback order per asset class. Finnhub joins only
    when its key is present -- see vendors.finnhub."""
    from tradebot.vendors import finnhub

    sources: list[PriceSource] = [CoinbaseSource(), KrakenSource(now_fn), AlpacaIexSource()]
    if finnhub.configured():
        sources.append(FinnhubSource())
    return sources


@dataclass
class SourceStats:
    calls: int = 0
    failures: int = 0
    served: int = 0


@dataclass
class _CacheEntry:
    point: PricePoint
    fetched_at: datetime


@dataclass
class PriceFeed:
    sources: Sequence[PriceSource]
    instruments: Sequence[Instrument] = DEFAULT_INSTRUMENTS
    ttl: timedelta = DEFAULT_TTL
    max_stale: timedelta = DEFAULT_MAX_STALE
    cooldown: timedelta = DEFAULT_COOLDOWN
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    stats: dict[str, SourceStats] = field(default_factory=dict)
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)
    _down_until: dict[str, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_symbol = {i.symbol: i for i in self.instruments}
        for source in self.sources:
            self.stats.setdefault(source.name, SourceStats())

    def get(self, symbols: Sequence[str] | None = None) -> dict[str, PricePoint]:
        """Prices for `symbols` (default: every configured instrument).
        A symbol with no fresh, fallback, or acceptably-stale value is
        absent from the result."""
        now = self.now_fn()
        wanted = list(symbols) if symbols is not None else [i.symbol for i in self.instruments]
        unknown = [s for s in wanted if s not in self._by_symbol]
        if unknown:
            raise KeyError(f"not configured instruments: {unknown}")

        out: dict[str, PricePoint] = {}
        missing: list[Instrument] = []
        for symbol in wanted:
            entry = self._cache.get(symbol)
            if entry is not None and now - entry.fetched_at < self.ttl:
                out[symbol] = entry.point
            else:
                missing.append(self._by_symbol[symbol])
        if missing:
            out.update(self._refresh(missing, now))
        return out

    def _refresh(self, instruments: Sequence[Instrument], now: datetime) -> dict[str, PricePoint]:
        fresh: dict[str, PricePoint] = {}
        for asset_class in sorted({i.asset_class for i in instruments}):
            pending = [i for i in instruments if i.asset_class == asset_class]
            for source in self.sources:
                if not pending:
                    break
                if source.asset_class != asset_class:
                    continue
                down_until = self._down_until.get(source.name)
                if down_until is not None and now < down_until:
                    logger.info("pricefeed_source_skipped source=%s cooldown_until=%s", source.name, down_until.isoformat())
                    continue
                stats = self.stats[source.name]
                stats.calls += 1
                try:
                    got = source.fetch(pending)
                except Exception as exc:  # any vendor failure is a fallback trigger
                    stats.failures += 1
                    self._down_until[source.name] = now + self.cooldown
                    logger.warning(
                        "pricefeed_source_failed source=%s asset_class=%s pending=%d error=%s",
                        source.name, asset_class, len(pending), exc,
                    )
                    continue
                for inst in list(pending):
                    point = got.get(inst.symbol)
                    if point is None:
                        continue
                    fresh[inst.symbol] = point
                    self._cache[inst.symbol] = _CacheEntry(point=point, fetched_at=now)
                    stats.served += 1
                    pending.remove(inst)
                if pending:
                    logger.info(
                        "pricefeed_source_partial source=%s unserved=%d", source.name, len(pending)
                    )
            for inst in pending:
                entry = self._cache.get(inst.symbol)
                if entry is not None and now - entry.fetched_at < self.max_stale:
                    fresh[inst.symbol] = replace(entry.point, stale=True)
                    logger.warning(
                        "pricefeed_stale_served symbol=%s age_s=%d source=%s",
                        inst.symbol, int((now - entry.fetched_at).total_seconds()), entry.point.source,
                    )
                else:
                    logger.warning("pricefeed_unavailable symbol=%s asset_class=%s", inst.symbol, asset_class)
        return fresh


SEPARATOR = " \u00b7 "  # middle dot, matching the alert cards' field separator
MISSING = "\u2014"      # em dash, the rendering.fields.dash convention


def render_status_lines(points: Mapping[str, PricePoint], instruments: Sequence[Instrument]) -> list[str]:
    """Pure: one HTML-safe line per asset class present, in `instruments`
    order, e.g. "Crypto: BTC $80,967.53 · ETH $2,593.30 · SOL $111.64".
    A symbol the feed couldn't serve prints as "SYM —" (never omitted --
    the rendering.fields.dash rule); a stale one gets a "(stale)" mark.
    Empty `points` renders nothing rather than a row of dashes, so a
    feed that is down entirely costs /status one honest line, not
    three -- the caller adds that line."""
    from tradebot.rendering.fields import money

    by_class: dict[str, list[str]] = {}
    for inst in instruments:
        point = points.get(inst.symbol)
        if point is None:
            cell = f"{inst.symbol} {MISSING}"
        else:
            cell = f"{inst.symbol} {money(point.price)}" + (" (stale)" if point.stale else "")
        by_class.setdefault(inst.asset_class, []).append(cell)
    if not points:
        return []
    return [f"{asset_class.capitalize()}: {SEPARATOR.join(cells)}" for asset_class, cells in by_class.items()]


def build_default_feed(**overrides) -> PriceFeed:
    """The production wiring: DEFAULT_INSTRUMENTS over default_sources()."""
    now_fn = overrides.get("now_fn")
    return PriceFeed(sources=default_sources(now_fn), **overrides)
