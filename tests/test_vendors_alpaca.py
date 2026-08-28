"""Tests for tradebot.vendors.alpaca -- limited to the parts that don't
need a real Alpaca client (DETECTOR_DATA_FEED resolution), plus the
timeout-adapter/backoff logic added for the 2026-08-21 reliability fix
and the vendor-call observability logging added the same day, all fully
testable without any real network I/O: constructing a client and
mounting an adapter is local object setup, _with_backoff/_observed_call
are exercised with fake callables, and the public fetch_* functions'
observability context (chunk/page context, no symbol-list/URL/secret
leakage) is exercised with fake client objects standing in for the real
Alpaca SDK clients. Everything else in this module talks to the real
Alpaca SDK by design (see its own module docstring) and isn't unit
tested here.
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from requests.adapters import HTTPAdapter

import tradebot.runner as runner_mod
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


def test_all_client_factories_mount_the_timeout_adapter(monkeypatch):
    """The explicit compatibility test for the one private-SDK coupling
    this fix relies on: client._session. If alpaca-py 0.43.5 ever stops
    exposing a plain requests.Session there, this fails loudly and
    specifically rather than silently losing timeout coverage."""
    monkeypatch.setenv("ALPACA_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-key")

    factories = (
        alpaca_module._client,
        alpaca_module._option_client,
        alpaca_module._trading_client,
        alpaca_module._screener_client,
    )
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


# --------------------------------------------------------------------------
# 2026-08-21 vendor-call observability: _observed_call, and the 10
# existing _with_backoff(...) call sites it now wraps from the outside.
# --------------------------------------------------------------------------


def test_observed_call_uses_the_watchtower_vendors_alpaca_logger():
    assert alpaca_module.logger.name == "watchtower.vendors.alpaca"


def test_observed_call_emits_start_before_invoking_the_callable(caplog):
    """The whole point of a start event: proving it exists BEFORE fn()
    runs, not just that it exists somewhere in the log. fn() checks the
    already-captured records for its own start line as its first action,
    which only passes if the start log truly precedes the call."""
    saw_start_before_running = []

    def fn():
        saw_start_before_running.append(
            any("vendor_call_start" in r.message for r in caplog.records)
        )
        return "result"

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        result = alpaca_module._observed_call("fetch_daily_bars", fn, client="stock", symbol="TSLA")

    assert saw_start_before_running == [True]
    assert result == "result"


def test_observed_call_success_returns_result_unchanged_and_emits_finish(caplog):
    def fn():
        return {"ok": True}

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        result = alpaca_module._observed_call("fetch_daily_bars", fn, client="stock", symbol="TSLA")

    assert result == {"ok": True}
    finish_records = [r for r in caplog.records if "vendor_call_finish" in r.message]
    assert len(finish_records) == 1
    msg = finish_records[0].message
    assert "operation=fetch_daily_bars" in msg
    assert "client=stock" in msg
    assert "symbol=TSLA" in msg
    assert "outcome=success" in msg
    elapsed = float(msg.split("elapsed_ms=")[1].split()[0])
    assert elapsed >= 0


def test_observed_call_failure_read_timeout(caplog):
    def fn():
        raise requests.exceptions.ReadTimeout("read timed out, host=data.alpaca.markets")

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        with pytest.raises(requests.exceptions.ReadTimeout):
            alpaca_module._observed_call(
                "fetch_daily_bars_bulk", fn, client="stock", chunk_index=4, chunk_count=9, chunk_size=1500,
            )

    failure_records = [r for r in caplog.records if "vendor_call_failure" in r.message]
    assert len(failure_records) == 1
    assert failure_records[0].levelname == "WARNING"
    msg = failure_records[0].message
    assert "exception=ReadTimeout" in msg
    assert "status_code=" not in msg  # not an APIError -- no status_code available
    assert "read timed out" not in msg  # str(exception) must never be logged
    assert "data.alpaca.markets" not in msg


def test_observed_call_failure_connect_timeout(caplog):
    def fn():
        raise requests.exceptions.ConnectTimeout("connect timed out")

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        with pytest.raises(requests.exceptions.ConnectTimeout):
            alpaca_module._observed_call("fetch_intraday_bars", fn, client="stock", symbol="TSLA")

    failure_records = [r for r in caplog.records if "vendor_call_failure" in r.message]
    assert len(failure_records) == 1
    assert "exception=ConnectTimeout" in failure_records[0].message
    assert "connect timed out" not in failure_records[0].message


def test_observed_call_failure_apierror_includes_status_code_not_body(caplog):
    def fn():
        raise _fake_api_error(500)

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        with pytest.raises(APIError):
            alpaca_module._observed_call("fetch_us_equity_assets", fn, client="trading")

    failure_records = [r for r in caplog.records if "vendor_call_failure" in r.message]
    assert len(failure_records) == 1
    msg = failure_records[0].message
    assert "exception=APIError" in msg
    assert "status_code=500" in msg
    # only type name + status code are logged, never the APIError's own
    # message body ("error", from _fake_api_error's constructor arg) --
    # checked as the exact standalone body text, since "APIError" itself
    # lowercases to a substring containing "error"
    assert " error " not in f" {msg} "


def test_observed_call_reraises_the_identical_exception_instance():
    original = requests.exceptions.ReadTimeout("boom")

    def fn():
        raise original

    with pytest.raises(requests.exceptions.ReadTimeout) as exc_info:
        alpaca_module._observed_call("fetch_daily_bars", fn, client="stock", symbol="TSLA")

    assert exc_info.value is original


def test_observed_call_output_never_contains_secrets_or_urls(caplog):
    def fn():
        return "result"

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        alpaca_module._observed_call(
            "fetch_daily_bars_bulk", fn, client="stock",
            chunk_index=4, chunk_count=9, chunk_size=1500, lookback_days=30,
        )

    all_messages = "\n".join(r.message for r in caplog.records)
    for forbidden in ("http://", "https://", "Authorization", "APCA-API-KEY-ID", "APCA-API-SECRET-KEY"):
        assert forbidden not in all_messages


def test_observed_call_wrapping_with_backoff_preserves_the_429_retry_shape(monkeypatch, caplog):
    """_observed_call wraps the LOGICAL call, not each retry attempt --
    one start/failure event pair for the whole 5-attempt retried
    sequence, and _with_backoff's own attempt count/sleep pattern
    (already proven unchanged by the dedicated tests above) is
    unaffected by being wrapped."""
    sleep_calls = []
    monkeypatch.setattr(alpaca_module.time, "sleep", lambda s: sleep_calls.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _fake_api_error(429)

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        with pytest.raises(APIError):
            alpaca_module._observed_call(
                "fetch_daily_bars_bulk",
                lambda: alpaca_module._with_backoff(fn),
                client="stock", chunk_index=1, chunk_count=1, chunk_size=10,
            )

    assert calls["n"] == 5
    assert sleep_calls == [2, 4, 8, 16]
    assert len([r for r in caplog.records if "vendor_call_start" in r.message]) == 1
    assert len([r for r in caplog.records if "vendor_call_failure" in r.message]) == 1


# --------------------------------------------------------------------------
# The same context/safety guarantees, exercised through the real public
# fetch_* functions (fake client objects standing in for the Alpaca SDK)
# rather than _observed_call directly -- proves the actual call-site
# wiring (chunk math, page counters, phase labels), not just the helper.
# --------------------------------------------------------------------------


class _FakeQuote:
    def __init__(self, ts: datetime = datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)):
        self.timestamp = ts
        self.bid_price = 100.0
        self.ask_price = 100.2


def test_fetch_latest_quotes_logs_correct_chunk_context_and_no_symbol_list(monkeypatch, caplog):
    class _FakeClient:
        def get_stock_latest_quote(self, request):
            return {s: _FakeQuote() for s in request.symbol_or_symbols}

    monkeypatch.setattr(alpaca_module, "_client", lambda: _FakeClient())
    monkeypatch.setattr(alpaca_module, "BULK_FETCH_CHUNK_SIZE", 2)
    symbols = ["AAA", "BBB", "CCC"]  # chunk 1: [AAA, BBB] (size 2), chunk 2: [CCC] (size 1)

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        result = alpaca_module.fetch_latest_quotes(symbols)

    assert set(result.keys()) == {"AAA", "BBB", "CCC"}

    starts = [r.message for r in caplog.records if "vendor_call_start" in r.message]
    assert len(starts) == 2
    assert "operation=fetch_latest_quotes" in starts[0] and "client=stock" in starts[0]
    assert "chunk_index=1" in starts[0] and "chunk_count=2" in starts[0] and "chunk_size=2" in starts[0]
    assert "chunk_index=2" in starts[1] and "chunk_count=2" in starts[1] and "chunk_size=1" in starts[1]

    all_messages = "\n".join(r.message for r in caplog.records)
    for symbol in symbols:
        assert symbol not in all_messages
    assert "symbols=" not in all_messages


def test_fetch_daily_bars_bulk_logs_correct_chunk_context_and_no_symbol_list(monkeypatch, caplog):
    class _FakeBar:
        timestamp = datetime(2026, 8, 5, tzinfo=timezone.utc)
        open = high = low = close = 100.0
        volume = 1000

    class _FakeResponse:
        def __init__(self, symbols):
            self.data = {s: [_FakeBar()] for s in symbols}

    class _FakeClient:
        def get_stock_bars(self, request):
            return _FakeResponse(request.symbol_or_symbols)

    monkeypatch.setattr(alpaca_module, "_client", lambda: _FakeClient())
    monkeypatch.setattr(alpaca_module, "BULK_FETCH_CHUNK_SIZE", 2)
    symbols = ["AAA", "BBB", "CCC"]

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        result = alpaca_module.fetch_daily_bars_bulk(symbols, lookback_days=30)

    assert set(result.keys()) == {"AAA", "BBB", "CCC"}

    starts = [r.message for r in caplog.records if "vendor_call_start" in r.message]
    assert len(starts) == 2
    assert "operation=fetch_daily_bars_bulk" in starts[0]
    assert "chunk_index=1" in starts[0] and "chunk_count=2" in starts[0] and "chunk_size=2" in starts[0]
    assert "lookback_days=30" in starts[0]
    assert "chunk_index=2" in starts[1] and "chunk_size=1" in starts[1]

    all_messages = "\n".join(r.message for r in caplog.records)
    for symbol in symbols:
        assert symbol not in all_messages


def test_fetch_intraday_bars_bulk_conserves_missing_symbols_and_chunk_context(
    monkeypatch, caplog,
):
    class _FakeBar:
        timestamp = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
        open = high = low = close = 100.0
        volume = 1000

    class _FakeResponse:
        def __init__(self, symbols):
            self.data = {symbol: [_FakeBar()] for symbol in symbols if symbol != "MISSING"}

    class _FakeClient:
        def get_stock_bars(self, request):
            return _FakeResponse(request.symbol_or_symbols)

    monkeypatch.setattr(alpaca_module, "_client", lambda: _FakeClient())
    monkeypatch.setattr(alpaca_module, "BULK_FETCH_CHUNK_SIZE", 2)
    symbols = ["AAA", "MISSING", "CCC"]

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        result = alpaca_module.fetch_intraday_bars_bulk(symbols, date(2026, 8, 27))

    assert set(result) == {"AAA", "CCC"}
    starts = [record.message for record in caplog.records if "vendor_call_start" in record.message]
    assert len(starts) == 2
    assert "operation=fetch_intraday_bars_bulk" in starts[0]
    assert "chunk_index=1" in starts[0] and "chunk_count=2" in starts[0]
    assert "session=2026-08-27" in starts[0]
    all_messages = "\n".join(record.message for record in caplog.records)
    for symbol in symbols:
        assert symbol not in all_messages


def test_fetch_intraday_window_bulk_uses_exact_bounds_and_conserves_missing(monkeypatch):
    start = datetime(2026, 8, 27, 19, 55, tzinfo=timezone.utc)
    end = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    requests_seen = []

    class _FakeBar:
        timestamp = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
        open = high = low = close = 100.0
        volume = 1000

    class _FakeResponse:
        def __init__(self, symbols):
            self.data = {symbol: [_FakeBar()] for symbol in symbols if symbol != "MISSING"}

    class _FakeClient:
        def get_stock_bars(self, request):
            requests_seen.append(request)
            return _FakeResponse(request.symbol_or_symbols)

    monkeypatch.setattr(alpaca_module, "_client", lambda: _FakeClient())
    monkeypatch.setattr(alpaca_module, "BULK_FETCH_CHUNK_SIZE", 2)
    result = alpaca_module.fetch_intraday_bars_window_bulk(
        ["AAA", "MISSING", "CCC"], start=start, end=end
    )

    assert set(result) == {"AAA", "CCC"}
    assert len(requests_seen) == 2
    assert all(
        request.start == start.replace(tzinfo=None)
        and request.end == end.replace(tzinfo=None)
        for request in requests_seen
    )
    with pytest.raises(ValueError, match="end must follow start"):
        alpaca_module.fetch_intraday_bars_window_bulk(["AAA"], start=end, end=start)


def test_intraday_request_window_covers_complete_est_postmarket_hour():
    start, end = alpaca_module._intraday_request_window(date(2026, 12, 15))

    assert start == datetime(2026, 12, 15, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 12, 16, 5, 0, tzinfo=timezone.utc)
    assert datetime(2026, 12, 16, 1, 0, tzinfo=timezone.utc) < end


def test_marketwide_screen_preserves_source_rank_metric_and_timestamp(monkeypatch):
    updated = datetime(2026, 8, 27, 20, 9, tzinfo=timezone.utc)

    class _FakeScreener:
        def get_market_movers(self, request):
            assert request.top == 50
            return SimpleNamespace(
                gainers=[SimpleNamespace(symbol="GAIN", percent_change=12.5, price=22.0)],
                losers=[SimpleNamespace(symbol="LOSS", percent_change=-9.0, price=8.0)],
                last_updated=updated,
            )

        def get_most_actives(self, request):
            symbol = "VOL" if str(request.by.value) == "volume" else "TRADES"
            return SimpleNamespace(
                most_actives=[SimpleNamespace(symbol=symbol, volume=1000, trade_count=200)],
                last_updated=updated,
            )

    monkeypatch.setattr(alpaca_module, "_screener_client", lambda: _FakeScreener())

    screen = alpaca_module.fetch_marketwide_postmarket_screen(50)

    assert screen.provider == "alpaca"
    assert screen.feed == "sip"
    assert screen.requested_top_n == 50
    assert [entry.source for entry in screen.entries] == [
        "market_gainer",
        "market_loser",
        "most_active_volume",
        "most_active_trades",
    ]
    assert screen.entries[0].move_pct == 12.5
    assert screen.entries[0].rank == 1
    assert all(entry.source_updated_at == updated for entry in screen.entries)
    assert dict(screen.source_updates) == {
        "market_movers": updated,
        "most_actives_volume": updated,
        "most_actives_trades": updated,
    }


@pytest.mark.parametrize("top", [0, 51])
def test_marketwide_screen_rejects_out_of_contract_top_bound(top):
    with pytest.raises(ValueError, match="between 1 and 50"):
        alpaca_module.fetch_marketwide_postmarket_screen(top)


def test_fetch_option_chain_logs_contracts_phase_then_chain_snapshot_phase(monkeypatch, caplog):
    class _FakeContract:
        symbol = "TSLA260130C00500000"

    class _FakeContractsResponse:
        option_contracts = [_FakeContract()]
        next_page_token = None

    class _FakeTradingClient:
        def get_option_contracts(self, request):
            return _FakeContractsResponse()

    class _FakeOptionClient:
        def get_option_chain(self, request):
            return {}  # no snapshot for the one contract -- it's just dropped; only logging matters here

    monkeypatch.setattr(alpaca_module, "_trading_client", lambda: _FakeTradingClient())
    monkeypatch.setattr(alpaca_module, "_option_client", lambda: _FakeOptionClient())

    with caplog.at_level("INFO", logger="watchtower.vendors.alpaca"):
        alpaca_module.fetch_option_chain("TSLA", date(2026, 1, 30))

    starts = [r.message for r in caplog.records if "vendor_call_start" in r.message]
    assert len(starts) == 2

    assert "operation=fetch_option_chain" in starts[0] and "client=trading" in starts[0]
    assert "phase=contracts" in starts[0] and "page_index=1" in starts[0] and "symbol=TSLA" in starts[0]

    assert "operation=fetch_option_chain" in starts[1] and "client=option" in starts[1]
    assert "phase=chain_snapshot" in starts[1] and "symbol=TSLA" in starts[1]


def test_configure_logging_integration_with_a_real_observed_call():
    """PR #67's own tests already prove any watchtower.* child inherits
    visibility generically (using a bare logging.getLogger(...) call) --
    this is the one integration point that couldn't exist until now:
    proving the REAL _observed_call-driven log line from this module
    flows through runner.configure_logging()'s handler end to end."""
    wt_logger = logging.getLogger("watchtower")
    saved_handlers = list(wt_logger.handlers)
    saved_level = wt_logger.level
    saved_propagate = wt_logger.propagate
    try:
        stream = io.StringIO()
        runner_mod.configure_logging(level="INFO", stream=stream)

        alpaca_module._observed_call("fetch_daily_bars", lambda: "ok", client="stock", symbol="TSLA")

        output = stream.getvalue()
        assert "vendor_call_start operation=fetch_daily_bars" in output
        assert "vendor_call_finish operation=fetch_daily_bars" in output
        assert "watchtower.vendors.alpaca" in output
    finally:
        wt_logger.handlers = saved_handlers
        wt_logger.setLevel(saved_level)
        wt_logger.propagate = saved_propagate
