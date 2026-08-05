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
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

from tradebot.detectors import Bar
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
    """The n most recent daily bars, oldest first."""
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
    window. Callers slice into premarket vs. RTH by clock time."""
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
    """The current NBBO quote. Live mode only — not used by replay."""
    client = _client()
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    response = _with_backoff(lambda: client.get_stock_latest_quote(request))
    q = response[symbol]
    return MDQuote(
        symbol=symbol,
        ts=q.timestamp.astimezone(timezone.utc),
        bid=float(q.bid_price),
        ask=float(q.ask_price),
        last=float((q.bid_price + q.ask_price) / 2),
    )


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
            )
        )
    return MDOptionChain(symbol=symbol, expiry=expiry, contracts=contracts)
