from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_operator_controls as controls_module
from tradebot.postmarket_operator_controls import (
    CONTROL_KINDS,
    OperatorControlCheck,
    _artifact,
    run_operator_control_suite,
    run_operator_failure_injection,
    run_operator_kill_switch,
    run_operator_owner_isolation,
    write_artifact,
)


REVISION = "abcdef1"
COMPLETED = datetime(2026, 8, 31, 23, 30, tzinfo=timezone.utc)


def _attest_clean_head(monkeypatch, *, head: str = "a" * 40) -> None:
    monkeypatch.setattr(controls_module, "_git_commit", lambda repo, revision: head)
    monkeypatch.setattr(controls_module, "_git_head", lambda repo: head)
    monkeypatch.setattr(controls_module, "_git_is_clean", lambda repo: True)


def test_failure_injection_proves_owner_only_fresh_idempotent_delivery():
    artifact = run_operator_failure_injection(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "operator_failure_injection"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 6
    assert all(check.passed for check in artifact.checks)


def test_kill_switch_proves_default_off_and_pre_database_branch():
    artifact = run_operator_kill_switch(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "operator_kill_switch"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 5
    assert all(check.passed for check in artifact.checks)


def test_owner_isolation_proves_single_admin_route_and_no_execution_path():
    artifact = run_operator_owner_isolation(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "operator_owner_isolation"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 6
    assert all(check.passed for check in artifact.checks)


def test_kill_switch_fails_if_compose_default_is_enabled(tmp_path):
    compose = Path(controls_module.COMPOSE_PATH).read_text(encoding="utf-8")
    bad = tmp_path / "docker-compose.yml"
    bad.write_text(
        compose.replace(
            "POSTMARKET_OPERATOR_ALERTS_ENABLED: "
            "${POSTMARKET_OPERATOR_ALERTS_ENABLED:-0}",
            "POSTMARKET_OPERATOR_ALERTS_ENABLED: "
            "${POSTMARKET_OPERATOR_ALERTS_ENABLED:-1}",
        ),
        encoding="utf-8",
    )
    artifact = run_operator_kill_switch(
        REVISION, completed_at=COMPLETED, compose_path=bad
    )
    assert artifact.status == "failed"
    checks = {check.name: check for check in artifact.checks}
    assert checks["compose_service_is_independently_default_off"].passed is False


def test_owner_isolation_fails_if_admin_predicate_is_removed(tmp_path):
    source = Path(controls_module.OPERATOR_PATH).read_text(encoding="utf-8")
    bad = tmp_path / "postmarket_operator.py"
    bad.write_text(source.replace(" AND is_admin=1", ""), encoding="utf-8")
    artifact = run_operator_owner_isolation(
        REVISION, completed_at=COMPLETED, operator_path=bad
    )
    assert artifact.status == "failed"
    checks = {check.name: check for check in artifact.checks}
    assert checks["destination_requires_exactly_one_admin_row"].passed is False


def test_artifact_contract_matches_schema_inventory():
    artifact = _artifact(
        "operator_kill_switch",
        REVISION,
        COMPLETED,
        (OperatorControlCheck("example", True, "deterministic evidence"),),
    )
    schema = json.loads(
        (Path("truth") / "postmarket_operator_control_evidence_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert set(asdict(artifact)) == {
        "schema_version", "kind", "status", "revision", "completed_at_utc", "checks"
    }
    assert set(schema["properties"]["kind"]["enum"]) == set(CONTROL_KINDS)


def test_writer_is_exclusive_read_only_and_digest_bound(tmp_path):
    artifact = _artifact(
        "operator_kill_switch",
        REVISION,
        COMPLETED,
        (OperatorControlCheck("example", True, "deterministic evidence"),),
    )
    path = tmp_path / "control.json"
    written = write_artifact(path, artifact)
    raw = path.read_bytes()
    assert written.sha256 == hashlib.sha256(raw).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError, match="refusing to replace"):
        write_artifact(path, artifact)


def test_suite_writes_exact_inventory_for_clean_checked_out_revision(
    tmp_path, monkeypatch
):
    _attest_clean_head(monkeypatch)
    output = tmp_path / "operator-controls"
    written = run_operator_control_suite(
        REVISION, output, completed_at=COMPLETED
    )
    assert {item.kind for item in written} == set(CONTROL_KINDS)
    assert {path.name for path in output.iterdir()} == {
        f"{kind}.json" for kind in CONTROL_KINDS
    }
    assert all(stat.S_IMODE(Path(item.path).stat().st_mode) == 0o444 for item in written)


def test_suite_refuses_dirty_or_mismatched_revision(tmp_path, monkeypatch):
    _attest_clean_head(monkeypatch)
    monkeypatch.setattr(controls_module, "_git_is_clean", lambda repo: False)
    with pytest.raises(ValueError, match="dirty"):
        run_operator_control_suite(REVISION, tmp_path / "dirty")

    monkeypatch.setattr(controls_module, "_git_is_clean", lambda repo: True)
    monkeypatch.setattr(controls_module, "_git_head", lambda repo: "b" * 40)
    with pytest.raises(ValueError, match="does not match"):
        run_operator_control_suite(REVISION, tmp_path / "mismatch")
