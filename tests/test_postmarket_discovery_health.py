"""Discovery health covers its active loop and off-window maintenance."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradebot.postmarket_discovery_health import evaluate_discovery_health


ACTIVE = datetime(2026, 8, 27, 20, 30, tzinfo=timezone.utc)
INACTIVE = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
REVISION = "abc1234"


def _write(path, when, *, status, **extra):
    path.write_text(json.dumps({
        "ts_utc": when.isoformat(),
        "status": status,
        "enabled": True,
        "observer": "postmarket-marketwide-shadow",
        "code_version": REVISION,
        **extra,
    }))


def test_disabled_discovery_is_healthy_without_heartbeat(tmp_path):
    result = evaluate_discovery_health(
        tmp_path / "missing.json", enabled=False,
        expected_revision=REVISION, now=ACTIVE,
    )
    assert result.healthy is True
    assert result.enabled is False


@pytest.mark.parametrize("now,status", [(ACTIVE, "running"), (ACTIVE, "ok"), (INACTIVE, "idle")])
def test_fresh_matching_heartbeat_is_healthy(tmp_path, now, status):
    path = tmp_path / "heartbeat.json"
    _write(path, now - timedelta(seconds=30), status=status)
    result = evaluate_discovery_health(
        path, enabled=True, expected_revision=REVISION, now=now,
    )
    assert result.healthy is True
    assert result.heartbeat_age_seconds == 30


def test_off_window_heartbeat_must_still_be_fresh(tmp_path):
    path = tmp_path / "heartbeat.json"
    _write(path, INACTIVE - timedelta(seconds=600), status="idle")
    result = evaluate_discovery_health(
        path, enabled=True, expected_revision=REVISION, now=INACTIVE,
    )
    assert result.healthy is False
    assert result.window_active is False
    assert "stale" in result.detail


@pytest.mark.parametrize(
    "override,detail",
    [
        ({"enabled": False}, "says disabled"),
        ({"observer": "wrong-observer"}, "unexpected supervised observer"),
        ({"code_version": "oldrev"}, "revision mismatch"),
        ({"status": "error"}, "unexpected supervised heartbeat status"),
    ],
)
def test_identity_and_top_level_failures_are_unhealthy(tmp_path, override, detail):
    path = tmp_path / "heartbeat.json"
    payload = {
        "ts_utc": (ACTIVE - timedelta(seconds=10)).isoformat(),
        "status": "ok",
        "enabled": True,
        "observer": "postmarket-marketwide-shadow",
        "code_version": REVISION,
        **override,
    }
    path.write_text(json.dumps(payload))
    result = evaluate_discovery_health(
        path, enabled=True, expected_revision=REVISION, now=ACTIVE,
    )
    assert result.healthy is False
    assert detail in result.detail


@pytest.mark.parametrize(
    "field",
    [
        "audit_status",
        "quality_backfill_status",
        "recall_census_status",
        "provider_proof_status",
        "context_backfill_status",
        "lifecycle_status",
        "rank_status",
        "rth_handoff_status",
        "rth_audit_status",
    ],
)
def test_explicit_subsystem_errors_are_unhealthy(tmp_path, field):
    path = tmp_path / "heartbeat.json"
    _write(path, INACTIVE - timedelta(seconds=10), status="idle", **{field: "error"})
    result = evaluate_discovery_health(
        path, enabled=True, expected_revision=REVISION, now=INACTIVE,
    )
    assert result.healthy is False
    assert field in result.detail


def test_degraded_evidence_does_not_trigger_restart_loop(tmp_path):
    path = tmp_path / "heartbeat.json"
    _write(
        path, INACTIVE - timedelta(seconds=10), status="idle",
        quality_backfill_status="degraded",
        recall_census_status="degraded",
        lifecycle_status="degraded",
    )
    result = evaluate_discovery_health(
        path, enabled=True, expected_revision=REVISION, now=INACTIVE,
    )
    assert result.healthy is True
