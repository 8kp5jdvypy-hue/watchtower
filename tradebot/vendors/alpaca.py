"""Alpaca vendor adapter.

This is the only file in the project allowed to import the Alpaca SDK —
see CLAUDE.md ("no vendor SDK imports outside its own adapter module").
Everything here returns plain Bar objects; callers never see Alpaca's
own types.
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from requests.adapters import HTTPAdapter

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, MarketType, MostActivesBy, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (
    MarketMoversRequest,
    MostActivesRequest,
    OptionBarsRequest,
    OptionChainRequest,
    NewsRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest, GetOptionContractsRequest

from tradebot.detectors import Bar
from tradebot.marketdata import AssetInfo as MDAssetInfo
from tradebot.marketdata import MarketScreenEntry as MDMarketScreenEntry
from tradebot.marketdata import MarketWideScreen as MDMarketWideScreen
from tradebot.marketdata import NewsItem as MDNewsItem
from tradebot.marketdata import OptionChain as MDOptionChain
from tradebot.marketdata import OptionContract as MDOptionContract
from tradebot.marketdata import Quote as MDQuote

# 2026-08-21 vendor-call observability: a child of "watchtower" (see
# runner.py's configure_logging, which attaches the actual handler on
# that parent) -- so these events inherit visibility for free when
# running through python -m tradebot.runner, with no wiring needed here.
logger = logging.getLogger("watchtower.vendors.alpaca")

# The feed every detector-facing call (fetch_daily_bars,
# fetch_intraday_bars, fetch_daily_bars_bulk) uses -- read once at
# import time, defaulting to "iex" so this is a no-op for current live
# behavior until DETECTOR_DATA_FEED is actually set. See
# docs/sip-migration-proposal.md for why this exists and what it does
# and doesn't cover: the quote-display calls (fetch_latest_quote,
# fetch_latest_quotes) stay on DataFeed.SIP unconditionally, and
# fetch_option_chain's OptionsFeed.INDICATIVE is a different feed enum
# entirely -- neither references this constant.
def _resolve_detector_data_feed() -> DataFeed:
    raw = os.environ.get("DETECTOR_DATA_FEED", "iex").strip().lower()
    if raw not in ("iex", "sip"):
        raise ValueError(f"DETECTOR_DATA_FEED must be 'iex' or 'sip', got {raw!r}")
    return DataFeed.SIP if raw == "sip" else DataFeed.IEX


DETECTOR_DATA_FEED = _resolve_detector_data_feed()
_EASTERN = ZoneInfo("America/New_York")


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


# 2026-08-21 production incident: the single-threaded live runner's
# heartbeat stopped advancing for ~16 minutes; the kernel stack showed
# the process blocked in tcp_recvmsg -> sk_wait_data on an established
# TCP/443 socket -- an Alpaca HTTP call with no bound on how long it can
# sit waiting for the connection or a response. alpaca-py 0.43.5's
# RESTClient.__init__ exposes no timeout parameter, and _request/
# _one_request never put a `timeout` key in the requests.Session.request()
# call (alpaca/common/rest.py), so this was always true, not a regression
# -- it just hadn't stalled in production before.
#
# This bounds CONNECT/READ INACTIVITY, not total wall-clock request
# duration: `read` is the max gap between consecutive socket reads, not
# a cap on how long a response can take to fully arrive. A response can
# run for an arbitrarily long total duration if it keeps making socket
# progress and no individual gap between reads exceeds
# READ_TIMEOUT_SECONDS -- that slow-drip case, a slow DNS resolution
# (not reliably covered by the connect phase at all -- socket.
# create_connection() resolves the hostname before the timeout ever
# applies), and a request that internally paginates into several HTTP
# calls are all real, unclosed residual risks this does not bound. What
# it does bound, directly and exactly: the failure class actually
# observed -- an idle, established socket making zero progress.
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15


class _TimeoutHTTPAdapter(HTTPAdapter):
    """Injects a default (connect, read) inactivity timeout only when the
    caller didn't already specify one -- alpaca-py's RESTClient never
    does (see the module comment above), so this is the one place a
    bound actually gets applied. Any timeout the caller DOES supply
    (tuple or scalar) is preserved exactly, unchanged.

    No __init__ override, no max_retries change -- requests.HTTPAdapter's
    own default retry behavior is untouched; this class adds nothing but
    a timeout default to send()."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if timeout is None:
            timeout = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
        return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)


def _bound_timeout(client):
    """Mounts _TimeoutHTTPAdapter on the client's own requests.Session,
    via that Session's public .mount() method -- not a monkeypatch of
    any alpaca-py method. Touches client._session, a private-by-
    convention attribute, only because RESTClient.__init__ (alpaca-py
    0.43.5) exposes no public constructor hook to inject a session,
    adapter, or timeout; every alpaca-py client class Perch uses
    (StockHistoricalDataClient, OptionHistoricalDataClient,
    TradingClient) subclasses the same RESTClient, so this one helper
    covers all three identically."""
    adapter = _TimeoutHTTPAdapter()
    client._session.mount("https://", adapter)
    client._session.mount("http://", adapter)
    return client


def _client() -> StockHistoricalDataClient:
    key_id, secret_key = _credentials()
    return _bound_timeout(StockHistoricalDataClient(key_id, secret_key))


def _option_client() -> OptionHistoricalDataClient:
    key_id, secret_key = _credentials()
    return _bound_timeout(OptionHistoricalDataClient(key_id, secret_key))


def _trading_client() -> TradingClient:
    key_id, secret_key = _credentials()
    return _bound_timeout(TradingClient(key_id, secret_key, paper=True))


def _screener_client() -> ScreenerClient:
    key_id, secret_key = _credentials()
    return _bound_timeout(ScreenerClient(key_id, secret_key))


def _news_client() -> NewsClient:
    key_id, secret_key = _credentials()
    return _bound_timeout(NewsClient(key_id, secret_key))


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


def _observed_call(operation: str, fn, *, client: str, **context):
    """Wraps one logical Alpaca call (an existing _with_backoff(...)
    expression, from the outside -- _with_backoff itself, retries, and
    _TimeoutHTTPAdapter are all untouched) with start/finish/failure
    logging, so if the runner becomes slow or is inspected/killed while
    blocked, the last vendor_call_start line names the exact logical
    operation and, where Perch itself exposes the boundary, the
    Perch-level bulk chunk (chunk_index) or explicit option-contract
    page (page_index) responsible. SDK-internal market-data pagination
    and individual HTTP-attempt identity are NOT visible at this
    boundary -- a stall could be on any attempt within the logical
    call, including a retried one, not only the first; see elapsed_ms
    below for what that means for interpreting a failure's duration.

    elapsed_ms is the wall-clock duration of the whole logical call, via
    time.monotonic() -- NOT a single HTTP attempt's duration. It can
    include one or more SDK-internal market-data pages, earlier
    retryable APIError responses and _with_backoff's sleep between them,
    and the final successful or failed attempt, all folded together. A
    ReadTimeout failure's elapsed_ms is not necessarily ~15,000ms (the
    plain single-attempt case): _with_backoff doesn't retry a Timeout,
    but the SAME logical call may have already absorbed earlier
    retryable APIError attempts/backoff before the one that timed out.
    Distinguishing those requires HTTP-attempt-level instrumentation,
    which this deliberately does not add -- see the module's own
    observability-boundary discussion; nothing here changes if that
    becomes necessary later.

    context: only ever scalars (symbol, chunk_index, chunk_count,
    chunk_size, lookback_days, phase, page_index, ...) -- never a
    request object, URL, header, or the symbol list itself, so these
    logs stay safe to ship as plain text.

    Re-raises whatever fn() raises, completely unchanged (same
    instance, same type, same traceback) -- this is pure observation,
    never a retry/suppression/transformation boundary."""
    context_str = " ".join(f"{k}={v}" for k, v in context.items())
    label = f"operation={operation} client={client}" + (f" {context_str}" if context_str else "")

    start = time.monotonic()
    logger.info("vendor_call_start %s", label)
    try:
        result = fn()
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        status_code = getattr(e, "status_code", None) if isinstance(e, APIError) else None
        status_part = f" status_code={status_code}" if status_code is not None else ""
        logger.warning(
            "vendor_call_failure %s elapsed_ms=%.0f exception=%s%s",
            label, elapsed_ms, type(e).__name__, status_part,
        )
        raise
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("vendor_call_finish %s elapsed_ms=%.0f outcome=success", label, elapsed_ms)
    return result


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

    Feed controlled by DETECTOR_DATA_FEED (see the module-level constant
    above), defaulting to IEX -- even though the account is now
    SIP-entitled (Algo Trader Plus, 2026-08). This feeds anchors/history
    for the detectors, and rvol_spike's avg_cum_volume_by_bar baseline
    is built from cached replay history (see scripts/fetch_cache.py) —
    live-measured, SIP volume runs ~20-40x IEX's on this watchlist (real
    numbers, not an estimate: SPY 26x, NVDA 20x, TSLA 42x, same session,
    same RTH window). Setting DETECTOR_DATA_FEED=sip before that cached
    baseline is rebuilt against SIP-scale history would make rvol_spike
    fire on almost everything -- see docs/sip-migration-proposal.md's
    Phase 1/3 for the required cache-rebuild sequencing. Flip together
    with fetch_intraday_bars below, never one call at a time.
    """
    client = _client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(n * 1.6) + 10)  # padding for weekends/holidays
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DETECTOR_DATA_FEED,
    )
    response = _observed_call(
        "fetch_daily_bars",
        lambda: _with_backoff(lambda: client.get_stock_bars(request)),
        client="stock",
        symbol=symbol,
    )
    raw = response.data.get(symbol, [])
    return _to_bars(symbol, raw)[-n:]


def fetch_intraday_bars(symbol: str, session_date: date) -> list[Bar]:
    """5-minute bars spanning the full New York calendar day for one session —
    covers premarket, RTH, and anything else the feed reports in that
    window. Callers slice into premarket vs. RTH by clock time.

    Feed controlled by DETECTOR_DATA_FEED, defaulting to IEX -- this is
    what rvol_spike and every other detector actually evaluates. See
    fetch_daily_bars' docstring for why this can't move to SIP on its
    own without a cache rebuild first.
    """
    client = _client()
    start, end = _intraday_request_window(session_date)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DETECTOR_DATA_FEED,
    )
    response = _observed_call(
        "fetch_intraday_bars",
        lambda: _with_backoff(lambda: client.get_stock_bars(request)),
        client="stock",
        symbol=symbol,
    )
    raw = response.data.get(symbol, [])
    return _to_bars(symbol, raw)


def fetch_intraday_bars_bulk(
    symbols: list[str], session_date: date,
) -> dict[str, list[Bar]]:
    """One-session five-minute bars for a bounded multi-symbol candidate set.

    This is deliberately not used against the complete asset universe. The
    market-wide screener narrows that universe first; this call then retrieves
    the real completed-bar inputs used by the strict postmarket evaluator.
    Missing symbols remain absent so the caller can record them explicitly.
    """
    if not symbols:
        return {}
    client = _client()
    start, end = _intraday_request_window(session_date)
    out: dict[str, list[Bar]] = {}
    chunk_count = math.ceil(len(symbols) / BULK_FETCH_CHUNK_SIZE)
    for i in range(0, len(symbols), BULK_FETCH_CHUNK_SIZE):
        chunk = symbols[i : i + BULK_FETCH_CHUNK_SIZE]
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=DETECTOR_DATA_FEED,
        )
        response = _observed_call(
            "fetch_intraday_bars_bulk",
            lambda r=request: _with_backoff(lambda: client.get_stock_bars(r)),
            client="stock",
            chunk_index=i // BULK_FETCH_CHUNK_SIZE + 1,
            chunk_count=chunk_count,
            chunk_size=len(chunk),
            session=session_date.isoformat(),
        )
        for symbol, raw_bars in response.data.items():
            out[symbol] = _to_bars(symbol, raw_bars)
    return out


def fetch_intraday_bars_window_bulk(
    symbols: list[str], *, start: datetime, end: datetime,
) -> dict[str, list[Bar]]:
    """Five-minute bars for an explicit bounded UTC window and symbol set.

    The live universe sweep and recall census use this to request only the final
    RTH bar plus the postmarket window, rather than materializing a full calendar
    day for a large explicit symbol set. Missing symbols remain absent.
    """
    if not symbols:
        return {}
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise ValueError("end must follow start")
    if end_utc - start_utc > timedelta(days=1):
        raise ValueError("intraday window must not exceed one day")
    client = _client()
    out: dict[str, list[Bar]] = {}
    chunk_count = math.ceil(len(symbols) / BULK_FETCH_CHUNK_SIZE)
    for i in range(0, len(symbols), BULK_FETCH_CHUNK_SIZE):
        chunk = symbols[i : i + BULK_FETCH_CHUNK_SIZE]
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_utc,
            end=end_utc,
            feed=DETECTOR_DATA_FEED,
        )
        response = _observed_call(
            "fetch_intraday_bars_window_bulk",
            lambda r=request: _with_backoff(lambda: client.get_stock_bars(r)),
            client="stock",
            chunk_index=i // BULK_FETCH_CHUNK_SIZE + 1,
            chunk_count=chunk_count,
            chunk_size=len(chunk),
            start=start_utc.isoformat(),
            end=end_utc.isoformat(),
        )
        for symbol, raw_bars in response.data.items():
            out[symbol] = _to_bars(symbol, raw_bars)
    return out


def _intraday_request_window(session_date: date) -> tuple[datetime, datetime]:
    """UTC bounds for one complete New York trading date.

    A fixed UTC day truncates 19:00-20:00 ET after daylight saving time
    ends. Local-midnight bounds retain the complete extended-hours session
    on both EST and EDT dates.
    """
    local_start = datetime.combine(session_date, datetime.min.time(), tzinfo=_EASTERN)
    local_end = datetime.combine(
        session_date + timedelta(days=1), datetime.min.time(), tzinfo=_EASTERN
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def fetch_marketwide_postmarket_screen(top: int = 50) -> MDMarketWideScreen:
    """Top SIP movers plus volume/trade-count leaders across US stocks.

    Alpaca's screener performs the market-wide ranking. Perch records the
    returned rank, metric, source timestamp, endpoint, and requested bound;
    it never treats a provider top-N response as a complete per-symbol census.
    """
    if not 1 <= top <= 50:
        raise ValueError("market-wide screener top must be between 1 and 50")
    client = _screener_client()
    movers = _observed_call(
        "fetch_market_movers",
        lambda: _with_backoff(
            lambda: client.get_market_movers(
                MarketMoversRequest(top=top, market_type=MarketType.STOCKS)
            )
        ),
        client="screener",
        top=top,
    )
    volume = _observed_call(
        "fetch_most_actives",
        lambda: _with_backoff(
            lambda: client.get_most_actives(
                MostActivesRequest(top=top, by=MostActivesBy.VOLUME)
            )
        ),
        client="screener",
        top=top,
        by="volume",
    )
    trades = _observed_call(
        "fetch_most_actives",
        lambda: _with_backoff(
            lambda: client.get_most_actives(
                MostActivesRequest(top=top, by=MostActivesBy.TRADES)
            )
        ),
        client="screener",
        top=top,
        by="trades",
    )
    entries: list[MDMarketScreenEntry] = []
    for source, rows in (("market_gainer", movers.gainers), ("market_loser", movers.losers)):
        entries.extend(
            MDMarketScreenEntry(
                symbol=row.symbol,
                source=source,
                rank=rank,
                source_updated_at=movers.last_updated.astimezone(timezone.utc),
                move_pct=float(row.percent_change),
                price=float(row.price),
            )
            for rank, row in enumerate(rows, 1)
        )
    for source, response in (("most_active_volume", volume), ("most_active_trades", trades)):
        entries.extend(
            MDMarketScreenEntry(
                symbol=row.symbol,
                source=source,
                rank=rank,
                source_updated_at=response.last_updated.astimezone(timezone.utc),
                volume=float(row.volume),
                trade_count=float(row.trade_count),
            )
            for rank, row in enumerate(response.most_actives, 1)
        )
    return MDMarketWideScreen(
        entries=tuple(entries),
        requested_top_n=top,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", movers.last_updated.astimezone(timezone.utc)),
            ("most_actives_volume", volume.last_updated.astimezone(timezone.utc)),
            ("most_actives_trades", trades.last_updated.astimezone(timezone.utc)),
        ),
    )


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
    response = _observed_call(
        "fetch_latest_quote",
        lambda: _with_backoff(lambda: client.get_stock_latest_quote(request)),
        client="stock",
        symbol=symbol,
    )
    q = response[symbol]
    return MDQuote(
        symbol=symbol,
        ts=q.timestamp.astimezone(timezone.utc),
        bid=float(q.bid_price),
        ask=float(q.ask_price),
        last=float((q.bid_price + q.ask_price) / 2),
        bid_size=(
            float(q.bid_size) if getattr(q, "bid_size", None) is not None else None
        ),
        ask_size=(
            float(q.ask_size) if getattr(q, "ask_size", None) is not None else None
        ),
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
    chunk_count = math.ceil(len(symbols) / BULK_FETCH_CHUNK_SIZE)
    for i in range(0, len(symbols), BULK_FETCH_CHUNK_SIZE):
        chunk = symbols[i : i + BULK_FETCH_CHUNK_SIZE]
        request = StockLatestQuoteRequest(symbol_or_symbols=chunk, feed=DataFeed.SIP)
        response = _observed_call(
            "fetch_latest_quotes",
            lambda r=request: _with_backoff(lambda: client.get_stock_latest_quote(r)),
            client="stock",
            chunk_index=i // BULK_FETCH_CHUNK_SIZE + 1,
            chunk_count=chunk_count,
            chunk_size=len(chunk),
        )
        for symbol, q in response.items():
            out[symbol] = MDQuote(
                symbol=symbol,
                ts=q.timestamp.astimezone(timezone.utc),
                bid=float(q.bid_price),
                ask=float(q.ask_price),
                last=float((q.bid_price + q.ask_price) / 2),
                bid_size=(
                    float(q.bid_size)
                    if getattr(q, "bid_size", None) is not None else None
                ),
                ask_size=(
                    float(q.ask_size)
                    if getattr(q, "ask_size", None) is not None else None
                ),
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
    page_index = 0
    while True:
        page_index += 1
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol], expiration_date=expiry, page_token=page_token
        )
        contracts_response = _observed_call(
            "fetch_option_chain",
            lambda: _with_backoff(lambda: trading_client.get_option_contracts(request)),
            client="trading",
            symbol=symbol,
            phase="contracts",
            page_index=page_index,
        )
        contract_meta.update({c.symbol: c for c in contracts_response.option_contracts})
        page_token = contracts_response.next_page_token
        if not page_token:
            break
    if not contract_meta:
        return MDOptionChain(symbol=symbol, expiry=expiry, contracts=[])

    option_client = _option_client()
    snapshots = _observed_call(
        "fetch_option_chain",
        lambda: _with_backoff(
            lambda: option_client.get_option_chain(
                OptionChainRequest(underlying_symbol=symbol, expiration_date=expiry, feed=OptionsFeed.INDICATIVE)
            )
        ),
        client="option",
        symbol=symbol,
        phase="chain_snapshot",
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
                quote_ts=(
                    quote.timestamp.astimezone(timezone.utc)
                    if getattr(quote, "timestamp", None) is not None else None
                ),
                quote_feed="indicative",
            )
        )
    return MDOptionChain(symbol=symbol, expiry=expiry, contracts=contracts)


def fetch_nearest_option_chain(symbol: str, session: date, spot: float) -> MDOptionChain:
    """Fetch the first non-empty weekly expiry after the observed session.

    `spot` is accepted to keep the external-context fetch interface explicit;
    strike selection remains provider-free in postmarket_external_context.
    """
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("spot must be finite and positive")
    first = session + timedelta(days=1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    last = MDOptionChain(symbol=symbol, expiry=first_friday, contracts=[])
    for week in range(4):
        expiry = first_friday + timedelta(days=7 * week)
        last = fetch_option_chain(symbol, expiry)
        if last.contracts:
            return last
    return last


def fetch_news(symbol: str, start: datetime, end: datetime) -> list[MDNewsItem]:
    """Return attributable news metadata for one bounded symbol window."""
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    if end < start:
        raise ValueError("end must not precede start")
    client = _news_client()
    response = _observed_call(
        "fetch_news",
        lambda: _with_backoff(
            lambda: client.get_news(
                NewsRequest(
                    symbols=symbol,
                    start=start.astimezone(timezone.utc),
                    end=end.astimezone(timezone.utc),
                    sort="asc",
                    limit=50,
                    include_content=False,
                    exclude_contentless=False,
                )
            )
        ),
        client="news",
        symbol=symbol,
    )
    items = []
    for rows in response.data.values():
        for row in rows:
            items.append(
                MDNewsItem(
                    provider_id=str(row.id),
                    headline=row.headline,
                    source=row.source,
                    url=row.url,
                    created_at=row.created_at.astimezone(timezone.utc),
                    updated_at=row.updated_at.astimezone(timezone.utc),
                    symbols=tuple(sorted({value.upper() for value in row.symbols})),
                )
            )
    return sorted(items, key=lambda row: (row.created_at, row.provider_id))


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

    Feed controlled by DETECTOR_DATA_FEED, defaulting to IEX --
    broad_scan.py computes its own rvol = snapshot.volume /
    snapshot.avg_volume against these same cached bars (see
    screen_snapshot()'s RVOL_THRESHOLD check), the identical
    live-vs-historical-baseline mismatch fetch_daily_bars' docstring
    describes. Same blocker, same fix: flip together with the other
    two detector-facing call sites in this file, only after a real
    recalibration pass and cache rebuild.
    """
    client = _client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    out: dict[str, list[Bar]] = {}
    chunk_count = math.ceil(len(symbols) / BULK_FETCH_CHUNK_SIZE)
    for i in range(0, len(symbols), BULK_FETCH_CHUNK_SIZE):
        chunk = symbols[i : i + BULK_FETCH_CHUNK_SIZE]
        request = StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end, feed=DETECTOR_DATA_FEED,
        )
        response = _observed_call(
            "fetch_daily_bars_bulk",
            lambda r=request: _with_backoff(lambda: client.get_stock_bars(r)),
            client="stock",
            chunk_index=i // BULK_FETCH_CHUNK_SIZE + 1,
            chunk_count=chunk_count,
            chunk_size=len(chunk),
            lookback_days=lookback_days,
        )
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
    assets = _observed_call(
        "fetch_us_equity_assets",
        lambda: _with_backoff(lambda: client.get_all_assets(request)),
        client="trading",
    )

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
        bar_set = _observed_call(
            "fetch_option_day_volume",
            lambda: _with_backoff(lambda: option_client.get_option_bars(
                OptionBarsRequest(symbol_or_symbols=occ_symbol, timeframe=TimeFrame.Day, start=start, end=end)
            )),
            client="option",
            symbol=occ_symbol,
        )
    except APIError:
        return None
    bars = bar_set.data.get(occ_symbol) if hasattr(bar_set, "data") else None
    if not bars:
        return None
    return int(bars[-1].volume)


def fetch_option_day_range(occ_symbol: str, session_date: date) -> tuple[float, float] | None:
    """The contract's real low/high across every 5-minute trade bar that
    session — trade-based (not a bid/ask midpoint), so it reflects actual
    fills, not a quote nobody traded at. Returns None only when a successful
    response contains no intraday bars (the contract did not trade). Provider,
    auth, timeout, and response failures propagate to the caller's per-contract
    handler, where they are logged and remain eligible for a later retry."""
    option_client = _option_client()
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    bar_set = _observed_call(
        "fetch_option_day_range",
        lambda: _with_backoff(lambda: option_client.get_option_bars(
            OptionBarsRequest(
                symbol_or_symbols=occ_symbol, timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=start, end=end
            )
        )),
        client="option",
        symbol=occ_symbol,
    )
    bars = bar_set.data.get(occ_symbol) if hasattr(bar_set, "data") else None
    if not bars:
        return None
    return (min(b.low for b in bars), max(b.high for b in bars))
