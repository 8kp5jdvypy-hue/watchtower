"""Coinbase / Kraken / Finnhub adapters and the Alpaca IEX last-trade
call, all against fake HTTP sessions or fake SDK clients -- never the
network."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

import tradebot.vendors.alpaca as alpaca_module
from tradebot.vendors import coinbase, finnhub, kraken


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload, status_code=self.status_code)


# ---- Coinbase ------------------------------------------------------------

def test_coinbase_parses_last_trade_and_z_timestamp():
    session = FakeSession({"trade_id": 1, "price": "64123.45", "size": "0.01",
                           "bid": "64123.00", "ask": "64124.00", "volume": "1000",
                           "time": "2026-09-18T14:03:11.123456Z"})
    point = coinbase.fetch_ticker("BTC-USD", session=session)
    assert point.symbol == "BTC" and point.price == 64123.45
    assert point.ts == datetime(2026, 9, 18, 14, 3, 11, 123456, tzinfo=timezone.utc)
    assert point.source == "coinbase" and point.asset_class == "crypto"
    assert point.stale is False and point.ts_is_fetch_time is False
    url, kwargs = session.calls[0]
    assert url == "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    assert kwargs["timeout"] == (5, 10)
    assert "headers" not in kwargs  # keyless


def test_coinbase_parses_nanosecond_timestamp_seen_live_2026_09_18():
    session = FakeSession({"price": "64000", "time": "2026-09-18T16:16:38.309013277Z"})
    point = coinbase.fetch_ticker("BTC-USD", session=session)
    assert point.ts == datetime(2026, 9, 18, 16, 16, 38, 309013, tzinfo=timezone.utc)


def test_coinbase_parses_whole_second_timestamp():
    session = FakeSession({"price": "64000", "time": "2026-09-18T16:16:38Z"})
    assert coinbase.fetch_ticker("BTC-USD", session=session).ts == datetime(2026, 9, 18, 16, 16, 38, tzinfo=timezone.utc)


def test_coinbase_symbol_override():
    session = FakeSession({"price": "1.0", "time": "2026-09-18T00:00:00Z"})
    assert coinbase.fetch_ticker("SOL-USD", symbol="SOLANA", session=session).symbol == "SOLANA"


@pytest.mark.parametrize("payload", [
    {"time": "2026-09-18T00:00:00Z"},           # no price
    {"price": "0", "time": "2026-09-18T00:00:00Z"},   # non-positive
    {"price": "nan", "time": "2026-09-18T00:00:00Z"},
    {"price": "1", "time": "yesterday"},
    ["not", "an", "object"],
])
def test_coinbase_rejects_unusable_payloads(payload):
    with pytest.raises(coinbase.CoinbaseError):
        coinbase.fetch_ticker("BTC-USD", session=FakeSession(payload))


def test_coinbase_http_error_is_wrapped_with_status():
    with pytest.raises(coinbase.CoinbaseError, match="status=503"):
        coinbase.fetch_ticker("BTC-USD", session=FakeSession({}, status_code=503))


# ---- Kraken --------------------------------------------------------------

_KRAKEN_OK = {"error": [], "result": {
    "XXBTZUSD": {"a": ["64200.0", "1", "1.000"], "b": ["64199.0", "1", "1.000"], "c": ["64150.5", "0.02"]},
    "XETHZUSD": {"c": ["2500.25", "1.0"]},
    "SOLUSD": {"c": ["150.75", "3.0"]},
}}


def test_kraken_batches_pairs_and_maps_internal_names_back():
    session = FakeSession(_KRAKEN_OK)
    fixed = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    out = kraken.fetch_tickers(
        [("BTC", "XBTUSD"), ("ETH", "ETHUSD"), ("SOL", "SOLUSD")],
        session=session, now_fn=lambda: fixed,
    )
    assert len(session.calls) == 1
    assert session.calls[0][1]["params"] == {"pair": "XBTUSD,ETHUSD,SOLUSD"}
    assert out["BTC"].price == 64150.5 and out["ETH"].price == 2500.25 and out["SOL"].price == 150.75
    assert all(p.source == "kraken" and p.asset_class == "crypto" for p in out.values())
    # No trade timestamp on Kraken's ticker: fetch time, and it says so.
    assert all(p.ts == fixed and p.ts_is_fetch_time for p in out.values())


def test_kraken_omits_unattributable_pair_without_fabricating():
    out = kraken.fetch_tickers([("BTC", "XBTUSD"), ("DOGE", "XDGUSD")],
                               session=FakeSession(_KRAKEN_OK), now_fn=lambda: datetime.now(timezone.utc))
    assert set(out) == {"BTC"}


def test_kraken_error_list_raises():
    with pytest.raises(kraken.KrakenError, match="EQuery:Unknown asset pair"):
        kraken.fetch_tickers([("BTC", "XBTUSD")],
                             session=FakeSession({"error": ["EQuery:Unknown asset pair"], "result": {}}))


def test_kraken_empty_request_makes_no_call():
    session = FakeSession(_KRAKEN_OK)
    assert kraken.fetch_tickers([], session=session) == {}
    assert session.calls == []


# ---- Finnhub -------------------------------------------------------------

def test_finnhub_is_inert_without_a_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert finnhub.configured() is False
    with pytest.raises(finnhub.FinnhubError):
        finnhub.fetch_quote("SPY", session=FakeSession({"c": 1}))


def test_finnhub_sends_key_as_header_never_in_url_or_error(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", " secret-token ")
    session = FakeSession({"c": 512.34, "h": 1, "l": 1, "o": 1, "pc": 1, "t": 1789999000})
    point = finnhub.fetch_quote("SPY", session=session)
    assert point.price == 512.34 and point.source == "finnhub" and point.asset_class == "equity"
    assert point.ts == datetime.fromtimestamp(1789999000, tz=timezone.utc)
    url, kwargs = session.calls[0]
    assert kwargs["headers"] == {"X-Finnhub-Token": "secret-token"}
    assert "secret-token" not in url and "token" not in kwargs["params"]

    with pytest.raises(finnhub.FinnhubError) as excinfo:
        finnhub.fetch_quote("SPY", session=FakeSession({}, status_code=429))
    assert "secret-token" not in str(excinfo.value)


def test_finnhub_unknown_symbol_zero_payload_is_a_miss(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    with pytest.raises(finnhub.FinnhubError, match="no usable quote"):
        finnhub.fetch_quote("NOPE", session=FakeSession({"c": 0, "t": 0}))


# ---- Alpaca IEX last trade -----------------------------------------------

def test_alpaca_latest_trade_prices_use_iex_and_last_trade(monkeypatch, caplog):
    seen = []

    class _FakeClient:
        def get_stock_latest_trade(self, request):
            seen.append(request)
            ts = datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc)
            return {s: SimpleNamespace(price=101.5, timestamp=ts) for s in request.symbol_or_symbols}

    monkeypatch.setattr(alpaca_module, "_client", lambda: _FakeClient())
    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        out = alpaca_module.fetch_latest_trade_prices(["SPY", "QQQ"])

    assert seen[0].feed == alpaca_module.DataFeed.IEX
    assert set(out) == {"SPY", "QQQ"}
    assert out["SPY"].price == 101.5 and out["SPY"].source == "alpaca_iex" and out["SPY"].asset_class == "equity"
    messages = "\n".join(r.message for r in caplog.records)
    assert "operation=fetch_latest_trade_prices" in messages
    assert "SPY" not in messages and "symbols=" not in messages


def test_alpaca_latest_trade_prices_drop_nonpositive_and_missing(monkeypatch):
    class _FakeClient:
        def get_stock_latest_trade(self, request):
            ts = datetime(2026, 9, 18, tzinfo=timezone.utc)
            return {"SPY": SimpleNamespace(price=0.0, timestamp=ts)}  # QQQ absent

    monkeypatch.setattr(alpaca_module, "_client", lambda: _FakeClient())
    assert alpaca_module.fetch_latest_trade_prices(["SPY", "QQQ"]) == {}
