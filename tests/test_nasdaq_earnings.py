"""Contract tests for the fragile, undocumented NASDAQ calendar shape."""
from __future__ import annotations

from datetime import date

import pytest
import requests

from tradebot.vendors import nasdaq_earnings


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_calendar_distinguishes_a_real_empty_day(monkeypatch):
    monkeypatch.setattr(
        nasdaq_earnings.requests, "get", lambda *args, **kwargs: _Response({"data": {"rows": None}}),
    )
    assert nasdaq_earnings.fetch_earnings_calendar(date(2026, 8, 8)) == []


def test_fetch_calendar_normalizes_symbols_and_timing(monkeypatch):
    payload = {
        "data": {
            "rows": [
                {"symbol": " crwd ", "time": "time-after-hours"},
                {"symbol": "CRM", "time": "time-pre-market"},
                {"symbol": "OKTA", "time": "unknown"},
            ]
        }
    }
    monkeypatch.setattr(nasdaq_earnings.requests, "get", lambda *args, **kwargs: _Response(payload))
    events = nasdaq_earnings.fetch_earnings_calendar(date(2026, 8, 26))
    assert [(event.symbol, event.timing) for event in events] == [
        ("CRWD", "after-hours"), ("CRM", "pre-market"), ("OKTA", "unspecified"),
    ]


def test_fetch_calendar_raises_a_typed_error_on_transport_failure(monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(nasdaq_earnings.requests, "get", timeout)
    with pytest.raises(nasdaq_earnings.EarningsCalendarFetchError, match="fetch failed"):
        nasdaq_earnings.fetch_earnings_calendar(date(2026, 8, 26))


@pytest.mark.parametrize(
    "payload",
    [
        [], {"data": None}, {"data": []},
        {"data": {"rows": {"symbol": "CRWD"}}}, {"data": {"rows": ["CRWD"]}},
    ],
)
def test_fetch_calendar_raises_on_an_unexpected_success_shape(monkeypatch, payload):
    monkeypatch.setattr(nasdaq_earnings.requests, "get", lambda *args, **kwargs: _Response(payload))
    with pytest.raises(nasdaq_earnings.EarningsCalendarFetchError):
        nasdaq_earnings.fetch_earnings_calendar(date(2026, 8, 26))
