"""Tests for tradebot.telegram_bot.jsonlog — one JSON object per log
line, carrying alert_id/chat_id/etc. through `extra=`."""
from __future__ import annotations

import json
import logging

from tradebot.telegram_bot.jsonlog import JsonFormatter


def _format(record_kwargs, extra=None):
    logger = logging.getLogger("test_jsonlog")
    record = logger.makeRecord(
        "test_jsonlog", logging.INFO, "test", 1, record_kwargs.pop("msg", "delivered"), (), None, extra=extra,
    )
    return json.loads(JsonFormatter().format(record))


def test_emits_valid_json_with_core_fields():
    payload = _format({"msg": "delivered"})
    assert payload["message"] == "delivered"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test_jsonlog"
    assert "ts" in payload


def test_carries_extra_fields_like_alert_id_and_chat_id():
    payload = _format({"msg": "delivered"}, extra={"alert_id": "abc123", "chat_id": 42, "event": "delivered"})
    assert payload["alert_id"] == "abc123"
    assert payload["chat_id"] == 42
    assert payload["event"] == "delivered"


def test_never_leaks_internal_logrecord_attributes():
    payload = _format({"msg": "delivered"}, extra={"alert_id": "abc123"})
    # only real logging internals should be absent; keys we didn't add stay out
    assert "args" not in payload
    assert "levelno" not in payload
    assert "pathname" not in payload


def test_includes_exception_info_when_present():
    logger = logging.getLogger("test_jsonlog_exc")
    try:
        raise ValueError("boom")
    except ValueError:
        record = logger.makeRecord(
            "test_jsonlog_exc", logging.ERROR, "test", 1, "send failed", (), __import__("sys").exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exc_info"]
    assert "boom" in payload["exc_info"]
