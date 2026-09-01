"""Market-wide discovery operational-control evidence."""
from __future__ import annotations

import ast
import hashlib
import json
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_discovery_controls as controls_module
from tradebot.postmarket_discovery_controls import (
    CONTROL_KINDS,
    DiscoveryControlCheck,
    _artifact,
    main,
    run_discovery_control_suite,
    run_discovery_delivery_isolation,
    run_discovery_failure_injection,
    run_discovery_kill_switch,
    write_artifact,
)


REVISION = "abcdef1"
COMPLETED = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)


def _attest_clean_head(monkeypatch, *, head: str = "a" * 40) -> None:
    def commit(repo, revision):
        controls_module._revision(revision, "Git revision")
        return head

    monkeypatch.setattr(controls_module, "_git_commit", commit)
    monkeypatch.setattr(controls_module, "_git_head", lambda repo: head)
    monkeypatch.setattr(controls_module, "_git_is_clean", lambda repo: True)


def test_failure_injection_proves_conservation_and_zero_candidate_leakage():
    artifact = run_discovery_failure_injection(REVISION, completed_at=COMPLETED)

    assert artifact.kind == "discovery_failure_injection"
    assert artifact.status == "passed"
    assert artifact.revision == REVISION
    assert artifact.completed_at_utc == COMPLETED.isoformat()
    assert len(artifact.checks) == 9
    assert all(check.passed for check in artifact.checks)
    assert {check.name for check in artifact.checks} >= {
        "missing_bulk_bar_is_conserved_as_fetch_error",
        "missing_bulk_bar_cannot_fabricate_candidate",
        "stale_screen_fails_before_bar_fetch_and_persistence",
        "screener_outage_fails_before_persistence",
        "full_universe_sweep_outage_is_explicit_and_conserved",
        "full_universe_sweep_outage_cannot_fabricate_or_suppress_candidate",
    }


def test_kill_switch_proves_default_off_and_fail_closed_behavior():
    artifact = run_discovery_kill_switch(REVISION, completed_at=COMPLETED)

    assert artifact.kind == "discovery_kill_switch"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 7
    assert all(check.passed for check in artifact.checks)


def test_delivery_isolation_proves_no_alert_or_order_dependency():
    artifact = run_discovery_delivery_isolation(REVISION, completed_at=COMPLETED)

    assert artifact.kind == "discovery_delivery_isolation"
    assert artifact.status == "passed"
    assert len(artifact.checks) == 5
    assert all(check.passed for check in artifact.checks)


def test_kill_switch_artifact_fails_when_compose_default_is_not_off(tmp_path):
    compose = Path(controls_module.COMPOSE_PATH).read_text(encoding="utf-8")
    bad = tmp_path / "docker-compose.yml"
    bad.write_text(
        compose.replace(
            "POSTMARKET_DISCOVERY_ENABLED: ${POSTMARKET_DISCOVERY_ENABLED:-0}",
            "POSTMARKET_DISCOVERY_ENABLED: ${POSTMARKET_DISCOVERY_ENABLED:-1}",
        ),
        encoding="utf-8",
    )

    artifact = run_discovery_kill_switch(
        REVISION,
        completed_at=COMPLETED,
        compose_path=bad,
    )

    assert artifact.status == "failed"
    checks = {check.name: check for check in artifact.checks}
    assert checks["compose_service_is_independently_default_off"].passed is False


def test_delivery_artifact_fails_on_forbidden_import(tmp_path):
    source = Path(controls_module.DISCOVERY_SOURCE_PATH).read_text(encoding="utf-8")
    bad = tmp_path / "postmarket_discovery_shadow.py"
    bad.write_text(source + "\nimport tradebot.telegram_bot\n", encoding="utf-8")

    artifact = run_discovery_delivery_isolation(
        REVISION,
        completed_at=COMPLETED,
        discovery_source_path=bad,
    )

    assert artifact.status == "failed"
    checks = {check.name: check for check in artifact.checks}
    assert checks["discovery_modules_have_no_delivery_or_order_import"].passed is False
    assert "tradebot.telegram_bot" in checks[
        "discovery_modules_have_no_delivery_or_order_import"
    ].evidence


def test_delivery_artifact_covers_rth_handoff_module(tmp_path):
    source = Path(controls_module.RTH_MOMENTUM_SOURCE_PATH).read_text(encoding="utf-8")
    bad = tmp_path / "rth_momentum.py"
    bad.write_text(source + "\nimport tradebot.telegram_bot\n", encoding="utf-8")

    artifact = run_discovery_delivery_isolation(
        REVISION,
        completed_at=COMPLETED,
        rth_source_path=bad,
    )

    assert artifact.status == "failed"
    checks = {check.name: check for check in artifact.checks}
    assert checks["discovery_modules_have_no_delivery_or_order_import"].passed is False
    assert "tradebot.telegram_bot" in checks[
        "discovery_modules_have_no_delivery_or_order_import"
    ].evidence


def test_delivery_artifact_covers_rth_audit_module(tmp_path):
    source = Path(controls_module.RTH_AUDIT_SOURCE_PATH).read_text(encoding="utf-8")
    bad = tmp_path / "rth_momentum_audit.py"
    bad.write_text(source + "\nimport tradebot.telegram_bot\n", encoding="utf-8")

    artifact = run_discovery_delivery_isolation(
        REVISION,
        completed_at=COMPLETED,
        rth_audit_source_path=bad,
    )

    assert artifact.status == "failed"
    checks = {check.name: check for check in artifact.checks}
    assert checks["discovery_modules_have_no_delivery_or_order_import"].passed is False
    assert "tradebot.telegram_bot" in checks[
        "discovery_modules_have_no_delivery_or_order_import"
    ].evidence


def test_artifact_contract_and_schema_inventory_are_exact():
    artifact = _artifact(
        "discovery_kill_switch",
        REVISION,
        COMPLETED,
        (DiscoveryControlCheck("example", True, "deterministic evidence"),),
    )
    payload = asdict(artifact)
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "truth"
            / "postmarket_discovery_control_evidence_v1.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert set(payload) == {
        "schema_version",
        "kind",
        "status",
        "revision",
        "completed_at_utc",
        "checks",
    }
    assert set(schema["properties"]["kind"]["enum"]) == set(CONTROL_KINDS)
    assert schema["properties"]["revision"]["pattern"] == "^[0-9a-f]{7,40}$"


def test_writer_is_exclusive_read_only_and_digest_bound(tmp_path):
    artifact = _artifact(
        "discovery_kill_switch",
        REVISION,
        COMPLETED,
        (DiscoveryControlCheck("example", True, "deterministic evidence"),),
    )
    path = tmp_path / "kill-switch.json"

    written = write_artifact(path, artifact)

    raw = path.read_bytes()
    assert written.kind == artifact.kind
    assert written.path == str(path)
    assert written.sha256 == hashlib.sha256(raw).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert json.loads(raw) == json.loads(json.dumps(asdict(artifact)))
    with pytest.raises(FileExistsError, match="refusing to replace"):
        write_artifact(path, artifact)


def test_suite_writes_exact_atomic_inventory_for_checked_out_revision(
    tmp_path, monkeypatch,
):
    _attest_clean_head(monkeypatch)
    output = tmp_path / "evidence"

    written = run_discovery_control_suite(
        REVISION,
        output,
        completed_at=COMPLETED,
    )

    assert {item.kind for item in written} == set(CONTROL_KINDS)
    assert {path.name for path in output.iterdir()} == {
        f"{kind}.json" for kind in CONTROL_KINDS
    }
    assert all(item.revision == REVISION for item in written)
    assert all(stat.S_IMODE(Path(item.path).stat().st_mode) == 0o444 for item in written)


def test_suite_refuses_existing_evidence_set(tmp_path, monkeypatch):
    _attest_clean_head(monkeypatch)
    output = tmp_path / "evidence"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="existing discovery control evidence set"):
        run_discovery_control_suite(
            REVISION,
            output,
            completed_at=COMPLETED,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_suite_refuses_revision_not_checked_out_at_head(tmp_path, monkeypatch):
    monkeypatch.setattr(controls_module, "_git_commit", lambda repo, revision: "a" * 40)
    monkeypatch.setattr(controls_module, "_git_head", lambda repo: "b" * 40)

    with pytest.raises(ValueError, match="does not match checked-out HEAD"):
        run_discovery_control_suite(
            REVISION,
            tmp_path / "evidence",
            completed_at=COMPLETED,
        )

    assert not (tmp_path / "evidence").exists()


def test_suite_refuses_dirty_worktree(tmp_path, monkeypatch):
    _attest_clean_head(monkeypatch)
    monkeypatch.setattr(controls_module, "_git_is_clean", lambda repo: False)

    with pytest.raises(ValueError, match="worktree is dirty"):
        run_discovery_control_suite(
            REVISION,
            tmp_path / "evidence",
            completed_at=COMPLETED,
        )

    assert not (tmp_path / "evidence").exists()


def test_suite_publication_is_atomic_when_write_fails(tmp_path, monkeypatch):
    _attest_clean_head(monkeypatch)
    output = tmp_path / "evidence"
    real_writer = controls_module.write_artifact
    calls = 0

    def fail_second_write(path, artifact):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected disk failure")
        return real_writer(path, artifact)

    monkeypatch.setattr(controls_module, "write_artifact", fail_second_write)

    with pytest.raises(OSError, match="injected disk failure"):
        run_discovery_control_suite(
            REVISION,
            output,
            completed_at=COMPLETED,
        )

    assert not output.exists()
    assert list(tmp_path.glob(".evidence.*.tmp")) == []


def test_cli_writes_inventory_and_reports_invalid_revision(
    tmp_path, capsys, monkeypatch,
):
    _attest_clean_head(monkeypatch)
    output = tmp_path / "evidence"

    assert main(
        [
            "--revision",
            REVISION,
            "--output-dir",
            str(output),
        ]
    ) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert {item["kind"] for item in inventory} == set(CONTROL_KINDS)

    assert main(
        [
            "--revision",
            "unknown",
            "--output-dir",
            str(tmp_path / "bad"),
        ]
    ) == 2
    assert "Git SHA" in json.loads(capsys.readouterr().out)["error"]


def test_control_tool_has_no_direct_live_vendor_or_delivery_import():
    source_path = (
        Path(__file__).parents[1]
        / "tradebot"
        / "postmarket_discovery_controls.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = (
        "requests",
        "tradebot.alerts",
        "tradebot.broker",
        "tradebot.order",
        "tradebot.telegram_bot",
        "tradebot.vendors",
    )
    assert not any(module.startswith(forbidden) for module in imports)
