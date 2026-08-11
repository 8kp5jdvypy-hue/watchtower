"""Alpaca vendor adapter.

This is the only file in the project allowed to import the Alpaca SDK —
see CLAUDE.md ("no vendor SDK imports outside its own adapter module").
Everything here returns plain Bar objects; callers never see Alpaca's
own types.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionBarsRequest,
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest, GetOptionContractsRequest

from tradebot.detectors import Bar
from tradebot.marketdata import AssetInfo as MDAssetInfo
from tradebot.marketdata import OptionChain as MDOptionChain
from tradebot.marketdata import OptionContract as MDOptionContract
from tradebot.marketdata import Quote as MDQuote


class AlpacaCredentialsError(RuntimeError):
    pass


def _credentials() -> tuple[str, str]:
    key_id = os.environ.get("ALPACA_KEY_ID")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not key_id or not secret_key:
        raise AlpacaCredentialsError(
            "ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set in the environment. "
            "Set them before running fetch_cache.py."
        )
    return key_id, secret_key


def _client() -> StockHistoricalDataClient:
    key_id, secret_key = _credentials()
    return StockHistoricalDataClient(key_id, secret_key)


def _option_client() -> OptionHistoricalDataClient:
    key_id, secret_key = _credentials()
    return OptionHistoricalDataClient(key_id, secret_key)


def _trading_client() -> TradingClient:
    key_id, secret_key = _credentials()
    return TradingClient(key_id, secret_key, paper=True)


def _with_backoff(fn, max_retries: int = 5, base_delay: float = 2.0):
    for attempt in range(max_retries):
        try:
            return fn()
        except APIError as e:
            if attempt == max_retries - 1:
                raise
            status = getattr(e, "status_code", None)
            delay = base_delay * (2**attempt) if status == 429 else base_delay
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def _to_bars(symbol: str, raw_bars) -> list[Bar]:
    bars = [
        Bar(
            symbol=symbol,
            ts=b.timestamp.astimezone(timezone.utc),
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=int(b.volume),
        )
        for b in raw_bars
    ]
    bars.sort(key=lambda bar: bar.ts)
    return bars


def fetch_daily_bars(symbol: str, n: int) -> list[Bar]:
    """The n most recent daily bars, oldest first.

    Still IEX, deliberately, even though the account is now SIP-entitled
    (Algo Trader Plus, 2026-08). This feeds anchors/history for the
    detectors, and rvol_spike's avg_cum_volume_by_bar baseline is built
    from IEX-cached replay history (see scripts/fetch_cache.py) — live-
    measured, SIP volume runs ~20-40x IEX's on this watchlist (real
    numbers, not an estimate: SPY 26x, NVDA 20x, TSLA 42x, same session,
    same RTH window). Flipping this to SIP before that baseline is
    rebuilt against SIP-scale history would make rvol_spike fire on
    almost everything. Flip together with fetch_intraday_bars below,
    only after a real recalibration pass — not one call at a time.
    """
    client = _client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(n * 1.6) + 10)  # padding for weekends/holidays
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    response = _with_backoff(lambda: client.get_stock_bars(request))
    raw = response.data.get(symbol, [])
    return _to_bars(symbol, raw)[-n:]


def fetch_intraday_bars(symbol: str, session_date: date) -> list[Bar]:
    """5-minute bars spanning the full calendar day (UTC) for one session —
    covers premarket, RTH, and anything else the feed reports in that
    window. Callers slice into premarket vs. RTH by clock time.

    Still IEX -- this is what rvol_spike and every other detector
    actually evaluates. See fetch_daily_bars' docstring for why this
    can't move to SIP on its own.
    """
    client = _client()
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    response = _with_backoff(lambda: client.get_stock_bars(request))
    raw = response.data.get(symbol, [])
    return _to_bars(symbol, raw)


def fetch_latest_quote(symbol: str) -> MDQuote:
    """The current NBBO quote. Live mode only — not used by replay.

    SIP, not IEX: this feeds the dashboard's /quotes endpoint (price
    display only, no detector/baseline dependency), so it carries none
    of fetch_daily_bars/fetch_intraday_bars' recalibration blocker —
    pure upside from the tighter, more complete consolidated-tape quote.
    Live-verified 2026-08-11: same moment, same symbol, IEX bid/ask spread
    was $46 wide (749.35/795.77) against SIP's $0.11 (772.35/772.46).
    """
    client = _client()
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.SIP)
    response = _with_backoff(lambda: client.get_stock_latest_quote(request))
    q = response[symbol]
    return MDQuote(
        symbol=symbol,
        ts=q.timestamp.astimezone(timezone.utc),
        bid=float(q.bid_price),
        ask=float(q.ask_price),
        last=float((q.bid_price + q.ask_price) / 2),
    )


def fetch_latest_quotes(symbols: list[str]) -> dict[str, MDQuote]:
    """Current NBBO quotes for many symbols in one request instead of N --
    SIP, same reasoning as fetch_latest_quote above. Chunked the same way
    fetch_daily_bars_bulk is (see BULK_FETCH_CHUNK_SIZE), though a
    dashboard watchlist is never remotely close to that size in practice.
    A symbol Alpaca has no quote for is simply absent from the result,
    never padded with a fabricated entry -- same discipline as
    fetch_daily_bars_bulk."""
    client = _client()
    out: dict[str, MDQuote] = {}
    for i in range(0, len(symbols), BULK_FETCH_CHUNK_SIZE):
        chunk = symbols[i : i + BULK_FETCH_CHUNK_SIZE]
        request = StockLatestQuoteRequest(symbol_or_symbols=chunk, feed=DataFeed.SIP)
        response = _with_backoff(lambda r=request: client.get_stock_latest_quote(r))
        for symbol, q in response.items():
            out[symbol] = MDQuote(
                symbol=symbol,
                ts=q.timestamp.astimezone(timezone.utc),
                bid=float(q.bid_price),
                ask=float(q.ask_price),
                last=float((q.bid_price + q.ask_price) / 2),
            )
    return out


def fetch_option_chain(symbol: str, expiry: date) -> MDOptionChain:
    """The option chain for one underlying + expiry. Live mode only —
    there's no cached historical options data, so ReplayMarketData never
    calls this.

    Contract metadata (strike, type, open interest) comes from the
    trading API; live bid/ask/greeks come from the market data snapshot
    endpoint. The two are joined by contract symbol — a contract with no
    matching snapshot (e.g. no quote yet today) is dropped rather than
    included with fabricated pricing.
    """
    trading_client = _trading_client()
    contract_meta: dict = {}
    page_token = None
    while True:
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol], expiration_date=expiry, page_token=page_token
        )
        contracts_response = _with_backoff(lambda: trading_client.get_option_contracts(request))
        contract_meta.update({c.symbol: c for c in contracts_response.option_contracts})
        page_token = contracts_response.next_page_token
        if not page_token:
            break
    if not contract_meta:
        return MDOptionChain(symbol=symbol, expiry=expiry, contracts=[])

    option_client = _option_client()
    snapshots = _with_backoff(
        lambda: option_client.get_option_chain(
            OptionChainRequest(underlying_symbol=symbol, expiration_date=expiry, feed=OptionsFeed.INDICATIVE)
        )
    )

    contracts = []
    for occ_symbol, meta in contract_meta.items():
        snap = snapshots.get(occ_symbol)
        if snap is None or snap.latest_quote is None:
            continue
        quote = snap.latest_quote
        greeks = snap.greeks
        last = float(snap.latest_trade.price) if snap.latest_trade else (float(quote.bid_price) + float(quote.ask_price)) / 2
        contracts.append(
            MDOptionContract(
                symbol=occ_symbol,
                expiry=expiry,
                strike=float(meta.strike_price),
                right=meta.type.value,
                bid=float(quote.bid_price),
                ask=float(quote.ask_price),
                last=last,
                delta=float(greeks.delta) if greeks and greeks.delta is not None else None,
                theta=float(greeks.theta) if greeks and greeks.theta is not None else None,
                open_interest=int(meta.open_interest) if meta.open_interest is not None else 0,
                implied_volatility=float(snap.implied_volatility) if snap.implied_volatility is not None else None,
            )
        )
    return MDOptionChain(symbol=symbol, expiry=expiry, contracts=contracts)


# Alpaca's bars endpoint takes the symbol list as a URL query parameter —
# live-verified 2026-08-08: a single request for all ~13,000 active
# universe symbols at once fails with "414 Request-URI Too Large" (nginx,
# not an Alpaca-specific limit). 1,000-3,000 symbols per request measured
# working fine (1.8-4.5s); 1,500 is used as a conservative, tested-safe
# chunk size, not the actual observed ceiling.
BULK_FETCH_CHUNK_SIZE = 1500


def fetch_daily_bars_bulk(symbols: list[str], lookback_days: int = 30) -> dict[str, list[Bar]]:
    """Daily bars for many symbols in a handful of requests instead of
    one per symbol — this is what makes Stage 1 broad scanning
    (tradebot.broad_scan) affordable across the full active universe
    (tradebot.universe) instead of the fixed watchlist. Chunked
    automatically (see BULK_FETCH_CHUNK_SIZE); a symbol Alpaca has no
    bars for (new listing, halted, etc.) is simply absent from the
    result, never padded with an empty/fabricated entry.

    Still IEX -- broad_scan.py computes its own rvol = snapshot.volume /
    snapshot.avg_volume against these same cached bars (see
    screen_snapshot()'s RVOL_THRESHOLD check), the identical
    live-vs-historical-baseline mismatch fetch_daily_bars' docstring
    describes. Same blocker, same fix: flip together with the other
    three bar/volume call sites in this file, only after a real
    recalibration pass.
    """
    client = _client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    out: dict[str, list[Bar]] = {}
    for i in range(0, len(symbols), BULK_FETCH_CHUNK_SIZE):
        chunk = symbols[i : i + BULK_FETCH_CHUNK_SIZE]
        request = StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX,
        )
        response = _with_backoff(lambda r=request: client.get_stock_bars(r))
        for symbol, raw_bars in response.data.items():
            out[symbol] = _to_bars(symbol, raw_bars)
    return out


def fetch_us_equity_assets() -> list[MDAssetInfo]:
    """Every currently-ACTIVE US equity/ETF asset Alpaca's Trading API
    knows about — one call, ~14,000 rows as of this writing (live-checked
    2026-08-08 against the paper endpoint: 14,195 active us_equity
    assets). This is the Trading API (the same one used for options
    contract metadata), not the Market Data API — asset listing isn't
    gated by a market-data plan tier the way bar/quote history is.

    options_enabled/overnight_eligible are derived from the real,
    live-observed `attributes` vocabulary ('has_options',
    'overnight_tradable', 'overnight_halted', plus 'ipo',
    'fractional_eh_enabled', 'ptp_no_exception'/'ptp_with_exception',
    'options_late_close' seen on the same sample) — not guessed from
    Alpaca's docs. See MDAssetInfo's docstring for why the raw list is
    kept too, and why an untagged asset's overnight eligibility is None,
    not False.
    """
    client = _trading_client()
    request = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = _with_backoff(lambda: client.get_all_assets(request))

    out = []
    for a in assets:
        attrs = tuple(str(x) for x in (a.attributes or ()))
        overnight = True if "overnight_tradable" in attrs else (False if "overnight_halted" in attrs else None)
        out.append(
            MDAssetInfo(
                symbol=a.symbol,
                exchange=str(a.exchange).removeprefix("AssetExchange."),
                name=a.name or a.symbol,
                tradable=bool(a.tradable),
                options_enabled="has_options" in attrs,
                overnight_eligible=overnight,
                attributes=attrs,
            )
        )
    return out


def fetch_option_day_volume(occ_symbol: str, session_date: date) -> int | None:
    """Real cumulative contract volume for session_date, via a dedicated
    1-day bar request — the chain snapshot endpoint only carries the size
    of the single latest trade, not a day total, so this is a second call
    made only for the one contract actually being considered, not the
    whole chain. Returns None (never 0) on any failure or missing bar —
    None means "couldn't check," 0 means "checked, no volume traded,"
    and costs.py treats those differently."""
    option_client = _option_client()
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    try:
        bar_set = _with_backoff(lambda: option_client.get_option_bars(
            OptionBarsRequest(symbol_or_symbols=occ_symbol, timeframe=TimeFrame.Day, start=start, end=end)
        ))
    except APIError:
        return None
    bars = bar_set.data.get(occ_symbol) if hasattr(bar_set, "data") else None
    if not bars:
        return None
    return int(bars[-1].volume)


def fetch_option_day_range(occ_symbol: str, session_date: date) -> tuple[float, float] | None:
    """The contract's real low/high across every 5-minute trade bar that
    session — trade-based (not a bid/ask midpoint), so it reflects actual
    fills, not a quote nobody traded at. Returns None (never a fabricated
    range) on any failure, or when the contract had no intraday bars at
    all that day (illiquid enough that nothing traded) — the same "None
    means couldn't check" discipline as fetch_option_day_volume."""
    option_client = _option_client()
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    try:
        bar_set = _with_backoff(lambda: option_client.get_option_bars(
            OptionBarsRequest(
                symbol_or_symbols=occ_symbol, timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=start, end=end
            )
        ))
    except APIError:
        return None
    bars = bar_set.data.get(occ_symbol) if hasattr(bar_set, "data") else None
    if not bars:
        return None
    return (min(b.low for b in bars), max(b.high for b in bars))
