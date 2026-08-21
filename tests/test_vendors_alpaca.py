"""Tests for tradebot.vendors.alpaca -- limited to the parts that don't
need a real Alpaca client (DETECTOR_DATA_FEED resolution), plus the
timeout-adapter/backoff logic added for the 2026-08-21 reliability fix,
which is fully testable without any real network I/O: constructing a
client and mounting an adapter is local object setup, and _with_backoff
is exercised with fake callables. Everything else in this module talks
to the real Alpaca SDK by design (see its own module docstring) and
isn't unit tested here.
"""
from __future__ import annotations

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from requests.adapters import HTTPAdapter

import tradebot.vendors.alpaca as alpaca_module
from tradebot.vendors.alpaca import _resolve_detector_data_feed


def test_detector_data_feed_defaults_to_iex(monkeypatch):
    monkeypatch.delenv("DETECTOR_DATA_FEED", raising=False)
    assert _resolve_detector_data_feed() == DataFeed.IEX


def test_detector_data_feed_reads_sip(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "sip")
    assert _resolve_detector_data_feed() == DataFeed.SIP


def test_detector_data_feed_reads_iex_explicitly(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "iex")
    assert _resolve_detector_data_feed() == DataFeed.IEX


def test_detector_data_feed_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "  SIP  ")
    assert _resolve_detector_data_feed() == DataFeed.SIP


def test_detector_data_feed_rejects_an_unknown_value(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "nasdaq")
    with pytest.raises(ValueError, match="DETECTOR_DATA_FEED"):
        _resolve_detector_data_feed()


def test_module_constant_matches_the_environment_at_import_time():
    """DETECTOR_DATA_FEED itself (the module constant every detector-
    facing call site actually uses) is resolved once at import -- this
    process has no DETECTOR_DATA_FEED set, so it must be the default."""
    import tradebot.vendors.alpaca as alpaca_module

    assert alpaca_module.DETECTOR_DATA_FEED == DataFeed.IEX


# --------------------------------------------------------------------------
# 2026-08-21 reliability fix: _TimeoutHTTPAdapter / _bound_timeout /
# _with_backoff's existing (unchanged) retry behavior.
# --------------------------------------------------------------------------


def _patch_base_send(monkeypatch, capture: dict):
    """Replaces requests.adapters.HTTPAdapter.send (the base class
    _TimeoutHTTPAdapter delegates to via super().send(...)) with a fake
    that records exactly what timeout it was called with and returns
    without touching the network. Patching the base class method, not
    the subclass, is what proves _TimeoutHTTPAdapter's own send() is
    the thing doing the timeout injection -- super().send() resolves
    this patched attribute via the normal MRO lookup at call time."""

    def fake_send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        capture["timeout"] = timeout
        return "fake-response"

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)


def test_timeout_adapter_injects_the_default_when_caller_gives_none(monkeypatch):
    capture = {}
    _patch_base_send(monkeypatch, capture)
    adapter = alpaca_module._TimeoutHTTPAdapter()

    result = adapter.send(request=object(), timeout=None)

    assert capture["timeout"] == (alpaca_module.CONNECT_TIMEOUT_SECONDS, alpaca_module.READ_TIMEOUT_SECONDS)
    assert result == "fake-response"


def test_timeout_adapter_preserves_an_explicit_tuple_timeout(monkeypatch):
    capture = {}
    _patch_base_send(monkeypatch, capture)
    adapter = alpaca_module._TimeoutHTTPAdapter()

    adapter.send(request=object(), timeout=(1, 2))

    assert capture["timeout"] == (1, 2)


def test_timeout_adapter_preserves_an_explicit_scalar_timeout(monkeypatch):
    """The caller doesn't always supply a (connect, read) tuple -- a bare
    number is also valid requests/HTTPAdapter usage and must pass through
    unchanged too, not be coerced or replaced."""
    capture = {}
    _patch_base_send(monkeypatch, capture)
    adapter = alpaca_module._TimeoutHTTPAdapter()

    adapter.send(request=object(), timeout=7)

    assert capture["timeout"] == 7


def test_all_three_client_factories_mount_the_timeout_adapter(monkeypatch):
    """The explicit compatibility test for the one private-SDK coupling
    this fix relies on: client._session. If alpaca-py 0.43.5 ever stops
    exposing a plain requests.Session there, this fails loudly and
    specifically rather than silently losing timeout coverage."""
    monkeypatch.setenv("ALPACA_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-key")

    factories = (alpaca_module._client, alpaca_module._option_client, alpaca_module._trading_client)
    for factory in factories:
        client = factory()
        https_adapter = client._session.get_adapter("https://data.alpaca.markets/v2/stocks/bars")
        http_adapter = client._session.get_adapter("http://data.alpaca.markets/v2/stocks/bars")
        assert isinstance(https_adapter, alpaca_module._TimeoutHTTPAdapter), factory.__name__
        assert isinstance(http_adapter, alpaca_module._TimeoutHTTPAdapter), factory.__name__


def test_with_backoff_propagates_read_timeout_with_no_retry(monkeypatch):
    """Transport errors get zero Perch-level retries -- this is the
    current behavior (uncaught propagation, since _with_backoff only
    ever catches APIError) being verified, not new behavior being added.
    No RequestException branch exists and none is added here."""
    sleep_calls = []
    monkeypatch.setattr(alpaca_module.time, "sleep", lambda s: sleep_calls.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("read timed out")

    with pytest.raises(requests.exceptions.ReadTimeout):
        alpaca_module._with_backoff(fn)

    assert calls["n"] == 1
    assert sleep_calls == []


def test_with_backoff_propagates_connect_timeout_with_no_retry(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(alpaca_module.time, "sleep", lambda s: sleep_calls.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise requests.exceptions.ConnectTimeout("connect timed out")

    with pytest.raises(requests.exceptions.ConnectTimeout):
        alpaca_module._with_backoff(fn)

    assert calls["n"] == 1
    assert sleep_calls == []


def _fake_api_error(status_code: int) -> APIError:
    """APIError.status_code is a property backed by its constructor's
    http_error.response.status_code (see alpaca/common/exceptions.py) --
    not a directly-assignable attribute. This reproduces the real
    construction shape RESTClient._one_request uses: a requests.Response
    with the desired status, wrapped in a real requests.exceptions.HTTPError,
    wrapped in APIError."""
    response = requests.Response()
    response.status_code = status_code
    http_error = requests.exceptions.HTTPError(response=response)
    return APIError("error", http_error)


def test_with_backoff_retries_429_with_the_existing_exponential_backoff(monkeypatch):
    """Regression baseline for Perch's OWN outer _with_backoff only --
    not a reproduction of alpaca-py's separate internal RESTClient retry
    loop, which is a different mechanism entirely. Unchanged by this PR;
    this test exists because no baseline existed before it."""
    sleep_calls = []
    monkeypatch.setattr(alpaca_module.time, "sleep", lambda s: sleep_calls.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _fake_api_error(429)

    with pytest.raises(APIError):
        alpaca_module._with_backoff(fn)

    assert calls["n"] == 5
    assert sleep_calls == [2, 4, 8, 16]


def test_with_backoff_retries_a_non_429_apierror_with_flat_backoff(monkeypatch):
    """Only status_code == 429 gets exponential backoff -- any other
    APIError status (500 here, but this also covers 504, which alpaca-py's
    own internal layer already retries separately) gets a flat base_delay
    every time. Confirming this precisely since an earlier round of this
    review incorrectly assumed 429 and 504 shared the same outer backoff
    shape."""
    sleep_calls = []
    monkeypatch.setattr(alpaca_module.time, "sleep", lambda s: sleep_calls.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _fake_api_error(500)

    with pytest.raises(APIError):
        alpaca_module._with_backoff(fn)

    assert calls["n"] == 5
    assert sleep_calls == [2, 2, 2, 2]
