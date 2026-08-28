"""External-context health protects pre-close and off-window supervision."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tradebot.postmarket_external_context_health import evaluate_external_context_health


PRE_CLOSE = datetime(2026, 8, 27, 19, 55, tzinfo=timezone.utc)
OFF_WINDOW = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
REVISION = "abc1234"


def _write(path, when, status="idle", **extra):
    path.write_text(json.dumps({
        "ts_utc": when.isoformat(),
        "status": status,
        "enabled": True,
        "observer": "postmarket-external-context-shadow",
        "code_version": REVISION,
        **extra,
    }))


def test_pre_close_capture_heartbeat_is_supervised(tmp_path):
    path = tmp_path / "heartbeat.json"
    _write(path, PRE_CLOSE - timedelta(seconds=30), status="pre_event")
    result = evaluate_external_context_health(
        path, enabled=True, expected_revision=REVISION, now=PRE_CLOSE,
    )
    assert result.healthy is True
    assert result.window_active is False


def test_off_window_external_context_heartbeat_must_be_fresh(tmp_path):
    path = tmp_path / "heartbeat.json"
    _write(path, OFF_WINDOW - timedelta(seconds=600))
    result = evaluate_external_context_health(
        path, enabled=True, expected_revision=REVISION, now=OFF_WINDOW,
    )
    assert result.healthy is False
    assert "stale" in result.detail


def test_external_context_revision_mismatch_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat.json"
    _write(path, OFF_WINDOW - timedelta(seconds=10), code_version="oldrev")
    result = evaluate_external_context_health(
        path, enabled=True, expected_revision=REVISION, now=OFF_WINDOW,
    )
    assert result.healthy is False
    assert "revision mismatch" in result.detail


def test_disabled_external_context_needs_no_heartbeat(tmp_path):
    result = evaluate_external_context_health(
        tmp_path / "missing.json", enabled=False,
        expected_revision=REVISION, now=OFF_WINDOW,
    )
    assert result.healthy is True
    assert result.enabled is False
