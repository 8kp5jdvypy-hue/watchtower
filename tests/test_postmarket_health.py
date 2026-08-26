from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradebot.postmarket_health import evaluate_postmarket_health, main


ACTIVE = datetime(2026, 8, 27, 20, 30, tzinfo=timezone.utc)


def _write(path, when, status="ok"):
    path.write_text(json.dumps({"ts_utc": when.isoformat(), "status": status}))


def test_disabled_service_is_healthy_without_heartbeat(tmp_path):
    result = evaluate_postmarket_health(tmp_path / "missing.json", enabled=False, now=ACTIVE)
    assert result.healthy is True
    assert result.enabled is False
    assert result.detail == "shadow observer disabled by kill switch"


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),  # RTH
        datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc),  # Saturday
        datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc),  # before early close
        datetime(2026, 11, 28, 1, 6, tzinfo=timezone.utc),  # after final early-close bar grace
    ],
)
def test_enabled_service_needs_no_freshness_outside_window(tmp_path, now):
    result = evaluate_postmarket_health(tmp_path / "missing.json", enabled=True, now=now)
    assert result.healthy is True
    assert result.window_active is False


def test_missing_heartbeat_is_unhealthy_in_window(tmp_path):
    result = evaluate_postmarket_health(tmp_path / "missing.json", enabled=True, now=ACTIVE)
    assert result.healthy is False
    assert result.window_active is True


@pytest.mark.parametrize("status", ["running", "ok"])
def test_fresh_working_heartbeat_is_healthy(tmp_path, status):
    heartbeat = tmp_path / "heartbeat.json"
    _write(heartbeat, ACTIVE - timedelta(seconds=30), status)
    result = evaluate_postmarket_health(heartbeat, enabled=True, now=ACTIVE)
    assert result.healthy is True
    assert result.heartbeat_age_seconds == 30


def test_reported_error_is_unhealthy_even_when_fresh(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    _write(heartbeat, ACTIVE - timedelta(seconds=10), "error")
    result = evaluate_postmarket_health(heartbeat, enabled=True, now=ACTIVE)
    assert result.healthy is False
    assert result.detail == "postmarket observer reported an error"


def test_stale_and_future_heartbeats_are_unhealthy(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    _write(heartbeat, ACTIVE - timedelta(seconds=600))
    assert evaluate_postmarket_health(heartbeat, enabled=True, now=ACTIVE).healthy is False
    _write(heartbeat, ACTIVE + timedelta(seconds=1))
    assert evaluate_postmarket_health(heartbeat, enabled=True, now=ACTIVE).healthy is False


@pytest.mark.parametrize("payload", ["not-json", "{}", '{"ts_utc":3,"status":"ok"}'])
def test_malformed_heartbeat_is_unhealthy(tmp_path, payload):
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(payload)
    result = evaluate_postmarket_health(heartbeat, enabled=True, now=ACTIVE)
    assert result.healthy is False
    assert "unreadable" in result.detail


def test_cli_honors_disabled_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("POSTMARKET_SHADOW_ENABLED", raising=False)
    exit_code = main(["--heartbeat", str(tmp_path / "missing.json")])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "shadow observer disabled by kill switch"
