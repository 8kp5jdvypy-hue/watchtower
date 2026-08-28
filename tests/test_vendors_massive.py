"""Massive adapter tests use fake HTTP sessions and never touch the network."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
import requests

from tradebot.vendors import massive


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


def _configured(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "secret-key")
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)


def test_configuration_accepts_new_and_legacy_key_names(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    assert massive.configured() is False
    monkeypatch.setenv("POLYGON_API_KEY", " legacy ")
    assert massive.api_key() == "legacy"
    monkeypatch.setenv("MASSIVE_API_KEY", " current ")
    assert massive.api_key() == "current"


def test_fetch_intraday_bars_is_bounded_unadjusted_and_ordered(monkeypatch):
    _configured(monkeypatch)
    start = datetime(2026, 8, 27, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 27, 20, 5, tzinfo=timezone.utc)
    ts1 = int(start.timestamp() * 1000)
    ts2 = int(end.timestamp() * 1000)
    client = FakeSession({
        "status": "OK", "ticker": "ABC", "results": [
            {"t": ts1, "o": 100, "h": 102, "l": 99, "c": 101, "v": 10},
            {"t": ts2, "o": 109, "h": 111, "l": 108, "c": 110, "v": 20},
        ],
    })
    bars = massive.fetch_intraday_bars("ABC", start, end, session=client)
    assert [bar.ts for bar in bars] == [start, end]
    assert bars[-1].close == 110
    url, kwargs = client.calls[0]
    assert url.startswith("https://api.massive.com/v2/aggs/ticker/ABC/range/5/minute/")
    assert kwargs["params"] == {
        "adjusted": "false", "sort": "asc", "limit": 50_000,
        "apiKey": "secret-key",
    }
    assert kwargs["timeout"] == (5, 15)
    assert "secret-key" not in url


def test_fetch_intraday_bars_rejects_duplicates_instead_of_deduplicating(monkeypatch):
    _configured(monkeypatch)
    start = datetime(2026, 8, 27, 20, tzinfo=timezone.utc)
    end = datetime(2026, 8, 27, 20, 5, tzinfo=timezone.utc)
    ts = int(start.timestamp() * 1000)
    row = {"t": ts, "o": 100, "h": 102, "l": 99, "c": 101, "v": 10}
    client = FakeSession({"status": "OK", "ticker": "ABC", "results": [row, row]})
    with pytest.raises(massive.MassiveError, match="duplicated"):
        massive.fetch_intraday_bars("ABC", start, end, session=client)


def test_fetch_ticker_reference_passes_as_of_date_and_returns_only_bounded_fields(monkeypatch):
    _configured(monkeypatch)
    client = FakeSession({
        "status": "OK",
        "results": {
            "ticker": "ABC", "active": True, "market": "stocks",
            "primary_exchange": "XNAS", "type": "CS", "currency_name": "usd",
            "market_cap": 1_000_000, "share_class_shares_outstanding": 100_000,
            "weighted_shares_outstanding": 90_000, "sic_code": "3571",
            "sic_description": "ELECTRONIC COMPUTERS",
            "last_updated_utc": "2026-08-27T19:00:00Z",
            "description": "must not be copied",
        },
    })
    result = massive.fetch_ticker_reference("ABC", date(2026, 8, 27), session=client)
    assert result.market_cap == 1_000_000
    assert result.sic_code == "3571"
    assert not hasattr(result, "description")
    _, kwargs = client.calls[0]
    assert kwargs["params"]["date"] == "2026-08-27"


def test_provider_http_error_is_secret_safe(monkeypatch):
    _configured(monkeypatch)
    client = FakeSession({}, status_code=403)
    with pytest.raises(massive.MassiveError) as raised:
        massive.fetch_ticker_reference("ABC", date(2026, 8, 27), session=client)
    assert "secret-key" not in str(raised.value)
    assert "403" in str(raised.value)
