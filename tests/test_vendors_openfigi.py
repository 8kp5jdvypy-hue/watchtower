"""OpenFIGI identity mapping is strict, lossless, and ambiguity-preserving."""
from __future__ import annotations

import pytest
import requests

from tradebot.vendors import openfigi


class _Response:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("provider body must not reach logs")
            error.response = self
            raise error

    def json(self):
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _match(figi, *, composite="BBG000ABC123", share_class="BBG001ABC123"):
    return {
        "figi": figi,
        "name": "ABC CORP",
        "ticker": "ABC",
        "exchCode": "US",
        "compositeFIGI": composite,
        "shareClassFIGI": share_class,
        "marketSector": "Equity",
        "securityType": "Common Stock",
        "securityType2": "Common Stock",
        "securityDescription": "ABC",
    }


def test_fetch_preserves_every_match_and_uses_optional_key(monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "secret-key")
    client = _Session(_Response([{"data": [
        _match("BBG000000002"), _match("BBG000000001"),
    ]}]))

    result = openfigi.fetch_ticker_identity("ABC", session=client)

    assert [row.figi for row in result.matches] == ["BBG000000001", "BBG000000002"]
    _, request = client.calls[0]
    assert request["headers"]["X-OPENFIGI-APIKEY"] == "secret-key"
    assert request["json"] == [{
        "idType": "TICKER", "idValue": "ABC", "marketSecDes": "Equity",
        "exchCode": "US",
    }]


def test_no_identifier_is_a_valid_attributable_zero_match():
    result = openfigi.parse_mapping_response(
        "ABC", [{"warning": "No identifier found."}],
    )
    assert result.matches == ()
    assert result.provider_warning == "No identifier found."


def test_invalid_or_duplicate_provider_rows_fail_closed():
    with pytest.raises(openfigi.OpenFigiError, match="omitted figi"):
        openfigi.parse_mapping_response("ABC", [{"data": [{}]}])
    with pytest.raises(openfigi.OpenFigiError, match="duplicate FIGIs"):
        openfigi.parse_mapping_response("ABC", [{"data": [
            _match("BBG000000001"), _match("BBG000000001"),
        ]}])


def test_http_failure_is_sanitized():
    client = _Session(_Response({"sensitive": "provider body"}, status_code=429))
    with pytest.raises(openfigi.OpenFigiError, match=r"request failed status=429") as error:
        openfigi.fetch_ticker_identity("ABC", api_key="secret-key", session=client)
    assert "sensitive" not in str(error.value)
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize("symbol", ["abc", " ABC", "A B", ""])
def test_symbols_must_be_canonical(symbol):
    with pytest.raises(ValueError, match="symbol"):
        openfigi.parse_mapping_response(symbol, [{"data": []}])
