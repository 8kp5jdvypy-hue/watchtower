from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradebot.runner_health import evaluate_runner_health, main, market_is_open


RTH = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)  # 10:00 ET


def _write_heartbeat(path, when: datetime) -> None:
    path.write_text(json.dumps({"ts_utc": when.isoformat()}))


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),  # pre-open
        datetime(2026, 8, 27, 21, 0, tzinfo=timezone.utc),  # after close
        datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc),  # Saturday
        datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc),   # Labor Day
        datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc),  # after Black Friday early close
    ],
)
def test_off_session_does_not_require_a_heartbeat(tmp_path, now):
    result = evaluate_runner_health(tmp_path / "missing.json", now=now)

    assert result.healthy is True
    assert result.market_open is False
    assert result.heartbeat_age_seconds is None
    assert result.detail == "market closed — heartbeat freshness not required"


def test_market_is_open_only_during_a_real_xnys_session():
    assert market_is_open(RTH) is True
    assert market_is_open(datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)) is False
    assert market_is_open(datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)) is False


def test_fresh_heartbeat_is_healthy_during_rth(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, RTH - timedelta(minutes=5))

    result = evaluate_runner_health(heartbeat, now=RTH)

    assert result.healthy is True
    assert result.market_open is True
    assert result.heartbeat_age_seconds == 300
    assert result.detail == "heartbeat is fresh during RTH (300s old)"


def test_stale_heartbeat_is_unhealthy_during_rth(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, RTH - timedelta(minutes=15))

    result = evaluate_runner_health(heartbeat, now=RTH)

    assert result.healthy is False
    assert result.market_open is True
    assert result.heartbeat_age_seconds == 900
    assert result.detail == "heartbeat is stale during RTH (900s old; limit 900s)"


def test_missing_heartbeat_is_unhealthy_during_rth(tmp_path):
    result = evaluate_runner_health(tmp_path / "missing.json", now=RTH)

    assert result.healthy is False
    assert result.market_open is True
    assert result.detail == "no heartbeat recorded during RTH"


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("not json", "unreadable heartbeat during RTH"),
        (json.dumps({}), "unreadable heartbeat during RTH"),
        (json.dumps({"ts_utc": "2026-08-27T13:55:00"}), "ts_utc must be timezone-aware"),
    ],
)
def test_malformed_heartbeat_is_unhealthy_during_rth(tmp_path, payload, expected):
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(payload)

    result = evaluate_runner_health(heartbeat, now=RTH)

    assert result.healthy is False
    assert result.market_open is True
    assert expected in result.detail


def test_non_utf8_heartbeat_is_unhealthy_during_rth(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_bytes(b"\xff\xfe")

    result = evaluate_runner_health(heartbeat, now=RTH)

    assert result.healthy is False
    assert result.market_open is True
    assert "unreadable heartbeat during RTH" in result.detail


def test_future_heartbeat_is_unhealthy_during_rth(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, RTH + timedelta(seconds=1))

    result = evaluate_runner_health(heartbeat, now=RTH)

    assert result.healthy is False
    assert result.heartbeat_age_seconds == -1
    assert result.detail == "heartbeat is 1s in the future during RTH"


def test_cli_returns_success_off_hours_without_a_heartbeat(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "tradebot.runner_health._utc_now",
        lambda: datetime(2026, 8, 27, 21, 0, tzinfo=timezone.utc),
    )

    exit_code = main(["--heartbeat", str(tmp_path / "missing.json")])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "market closed — heartbeat freshness not required"


def test_cli_returns_failure_for_stale_rth_heartbeat(tmp_path, monkeypatch, capsys):
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, RTH - timedelta(minutes=16))
    monkeypatch.setattr(
        "tradebot.runner_health._utc_now",
        lambda: RTH,
    )

    exit_code = main(["--heartbeat", str(heartbeat)])

    assert exit_code == 1
    assert "heartbeat is stale during RTH" in capsys.readouterr().out
