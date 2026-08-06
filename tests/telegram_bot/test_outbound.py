"""Tests for tradebot.telegram_bot.outbound.send_once — classifies a
single Telegram API response into exactly one SendOutcome. Mocks
requests.post directly (there's no higher-level fake to inject here;
this module IS the HTTP boundary) — the chaos/load tests instead point a
real subprocess at a local fake server via TELEGRAM_API_ROOT."""
from __future__ import annotations

import requests

from tradebot.telegram_bot.outbound import SendOutcome, send_once


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_delivered_on_a_real_2xx_ok_response(monkeypatch):
    monkeypatch.setattr(
        "tradebot.telegram_bot.outbound.requests.post",
        lambda url, json, timeout: _FakeResponse(200, {"ok": True, "result": {"message_id": 42}}),
    )
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.DELIVERED
    assert result.message_id == 42


def test_rate_limited_carries_the_exact_retry_after(monkeypatch):
    monkeypatch.setattr(
        "tradebot.telegram_bot.outbound.requests.post",
        lambda url, json, timeout: _FakeResponse(
            429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 17}},
        ),
    )
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.RATE_LIMITED
    assert result.retry_after == 17.0


def test_forbidden_blocked_by_user_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        "tradebot.telegram_bot.outbound.requests.post",
        lambda url, json, timeout: _FakeResponse(
            403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
        ),
    )
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.UNREACHABLE


def test_chat_not_found_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        "tradebot.telegram_bot.outbound.requests.post",
        lambda url, json, timeout: _FakeResponse(
            400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
        ),
    )
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.UNREACHABLE


def test_server_error_is_retryable(monkeypatch):
    monkeypatch.setattr(
        "tradebot.telegram_bot.outbound.requests.post",
        lambda url, json, timeout: _FakeResponse(502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}),
    )
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.RETRYABLE_ERROR


def test_other_bad_request_is_permanent_not_retried_forever(monkeypatch):
    monkeypatch.setattr(
        "tradebot.telegram_bot.outbound.requests.post",
        lambda url, json, timeout: _FakeResponse(
            400, {"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"},
        ),
    )
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.PERMANENT_ERROR


def test_network_exception_is_retryable(monkeypatch):
    def _raise(url, json, timeout):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr("tradebot.telegram_bot.outbound.requests.post", _raise)
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.RETRYABLE_ERROR
    assert "boom" in result.error


def test_non_json_response_on_5xx_is_retryable(monkeypatch):
    class _HtmlResponse:
        status_code = 502
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr("tradebot.telegram_bot.outbound.requests.post", lambda url, json, timeout: _HtmlResponse())
    result = send_once("token", 1, "hi")
    assert result.outcome == SendOutcome.RETRYABLE_ERROR


def test_api_root_is_overridable_via_env_var(monkeypatch):
    captured = {}

    def _capture(url, json, timeout):
        captured["url"] = url
        return _FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    monkeypatch.setenv("TELEGRAM_API_ROOT", "http://localhost:9999")
    monkeypatch.setattr("tradebot.telegram_bot.outbound.requests.post", _capture)
    send_once("token", 1, "hi")
    assert captured["url"].startswith("http://localhost:9999")
