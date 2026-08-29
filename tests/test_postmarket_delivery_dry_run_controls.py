import hashlib
import json
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_delivery_dry_run_controls as controls
from tradebot.postmarket_delivery_dry_run_controls import (
    CONTROL_KINDS,
    DryRunControlCheck,
    _artifact,
    main,
    run_control_suite,
    run_delivery_isolation,
    run_failure_injection,
    run_kill_switch,
    run_rollback_runbook,
    write_artifact,
)


REVISION = "abcdef1"
COMPLETED = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)


def _attest_clean_head(monkeypatch, *, head="a" * 40):
    monkeypatch.setattr(controls, "_git_commit", lambda repo, revision: head)
    monkeypatch.setattr(controls, "_git_head", lambda repo: head)
    monkeypatch.setattr(controls, "_git_is_clean", lambda repo: True)


def test_failure_injection_proves_all_fail_closed_and_dedup_cases():
    artifact = run_failure_injection(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "customer_dry_run_failure_injection"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 7
    assert all(check.passed for check in artifact.checks)
    assert {check.name for check in artifact.checks} >= {
        "missing_owner_authorization_is_suppressed",
        "stale_completed_bar_is_explicitly_suppressed",
        "degraded_discovery_is_suppressed",
        "runtime_revision_mismatch_is_suppressed",
        "eligible_identity_is_transactionally_deduplicated",
    }


def test_kill_switch_proves_independent_default_off_behavior():
    artifact = run_kill_switch(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "customer_dry_run_kill_switch"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 5
    assert all(check.passed for check in artifact.checks)


def test_delivery_isolation_proves_no_live_customer_or_trading_path():
    artifact = run_delivery_isolation(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "customer_dry_run_delivery_isolation"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 5
    assert all(check.passed for check in artifact.checks)


def test_rollback_runbook_is_complete_and_evidence_preserving():
    artifact = run_rollback_runbook(REVISION, completed_at=COMPLETED)
    assert artifact.kind == "customer_dry_run_rollback_runbook"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 4
    assert all(check.passed for check in artifact.checks)


def test_kill_switch_artifact_fails_if_compose_default_changes(tmp_path):
    compose = controls.COMPOSE_PATH.read_text()
    bad = tmp_path / "docker-compose.yml"
    bad.write_text(compose.replace(
        "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED: "
        "${POSTMARKET_CUSTOMER_DRY_RUN_ENABLED:-0}",
        "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED: "
        "${POSTMARKET_CUSTOMER_DRY_RUN_ENABLED:-1}",
    ))
    artifact = run_kill_switch(
        REVISION, completed_at=COMPLETED, compose_path=bad
    )
    checks = {check.name: check for check in artifact.checks}
    assert artifact.status == "failed"
    assert checks["compose_service_is_independently_default_off"].passed is False


def test_delivery_isolation_fails_on_forbidden_import(tmp_path):
    copied = []
    for source in controls.MODULE_PATHS:
        target = tmp_path / source.name
        target.write_bytes(source.read_bytes())
        copied.append(target)
    copied[0].write_text(copied[0].read_text() + "\nimport tradebot.telegram_bot\n")
    artifact = run_delivery_isolation(
        REVISION,
        completed_at=COMPLETED,
        module_paths=tuple(copied),
    )
    checks = {check.name: check for check in artifact.checks}
    assert artifact.status == "failed"
    assert not checks[
        "dry_run_modules_have_no_delivery_provider_or_order_import"
    ].passed


def test_artifact_contract_matches_truth_schema():
    artifact = _artifact(
        "customer_dry_run_kill_switch",
        REVISION,
        COMPLETED,
        (DryRunControlCheck("example", True, "deterministic evidence"),),
    )
    schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_control_evidence_v1.schema.json"
    ).read_text())
    assert set(asdict(artifact)) == set(schema["required"])
    assert set(schema["properties"]["kind"]["enum"]) == set(CONTROL_KINDS)


def test_writer_is_exclusive_read_only_and_digest_bound(tmp_path):
    artifact = _artifact(
        "customer_dry_run_kill_switch",
        REVISION,
        COMPLETED,
        (DryRunControlCheck("example", True, "deterministic evidence"),),
    )
    path = tmp_path / "artifact.json"
    written = write_artifact(path, artifact)
    assert written.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError, match="refusing to replace"):
        write_artifact(path, artifact)


def test_suite_publishes_exact_atomic_inventory_for_clean_head(tmp_path, monkeypatch):
    _attest_clean_head(monkeypatch)
    output = tmp_path / "controls"
    written = run_control_suite(REVISION, output, completed_at=COMPLETED)
    assert {item.kind for item in written} == set(CONTROL_KINDS)
    assert {path.name for path in output.iterdir()} == {
        f"{kind}.json" for kind in CONTROL_KINDS
    }
    assert all(stat.S_IMODE(Path(item.path).stat().st_mode) == 0o444 for item in written)


def test_suite_refuses_dirty_mismatched_existing_or_failed_evidence(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(controls, "_git_commit", lambda repo, revision: "a" * 40)
    monkeypatch.setattr(controls, "_git_head", lambda repo: "b" * 40)
    monkeypatch.setattr(controls, "_git_is_clean", lambda repo: True)
    with pytest.raises(ValueError, match="does not match"):
        run_control_suite(REVISION, tmp_path / "mismatch")

    _attest_clean_head(monkeypatch)
    monkeypatch.setattr(controls, "_git_is_clean", lambda repo: False)
    with pytest.raises(ValueError, match="dirty"):
        run_control_suite(REVISION, tmp_path / "dirty")

    _attest_clean_head(monkeypatch)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="replace"):
        run_control_suite(REVISION, existing)

    failed = _artifact(
        "customer_dry_run_failure_injection",
        REVISION,
        COMPLETED,
        (DryRunControlCheck("injected", False, "failed"),),
    )
    monkeypatch.setattr(controls, "run_failure_injection", lambda *a, **k: failed)
    with pytest.raises(RuntimeError, match="no artifacts written"):
        run_control_suite(REVISION, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()


def test_cli_returns_structured_failure_without_partial_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        controls,
        "run_control_suite",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad revision")),
    )
    output = tmp_path / "controls"
    assert main(["--revision", REVISION, "--output-dir", str(output)]) == 2
    assert "bad revision" in json.loads(capsys.readouterr().out)["error"]
    assert not output.exists()


def test_control_module_has_no_live_provider_delivery_or_trading_dependency():
    source = Path("tradebot/postmarket_delivery_dry_run_controls.py").read_text().lower()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    for forbidden in ("telegram", "outbox", "requests", "alpaca", "broker", "order"):
        assert not any(forbidden in line for line in imports)
