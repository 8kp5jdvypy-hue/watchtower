"""Operational-control evidence exercises and fail-closed artifact handling."""
from __future__ import annotations

import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_controls as controls_module
from tradebot.postmarket_controls import (
    CONTROL_KINDS,
    ControlCheck,
    _artifact,
    main,
    run_control_suite,
    run_failure_injection,
    run_kill_switch,
    run_rollback_rehearsal,
    write_artifact,
)
from tradebot.postmarket_evidence_gate import _control_passed


REVISION = "abcdef1"
COMPLETED = datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "controls@example.invalid")
    _git(repo, "config", "user.name", "Control Test")
    (repo / "state.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "--short=7", "HEAD")

    (repo / "state.txt").write_text("current\n", encoding="utf-8")
    _git(repo, "commit", "-am", "current")
    current = _git(repo, "rev-parse", "--short=7", "HEAD")

    _git(repo, "switch", "--detach", base)
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "side")
    side = _git(repo, "rev-parse", "--short=7", "HEAD")
    return repo, base, current, side


def test_failure_injection_is_loud_conserved_and_candidate_free():
    artifact = run_failure_injection(REVISION, completed_at=COMPLETED)

    assert artifact.kind == "failure_injection"
    assert artifact.status == "passed"
    assert artifact.revision == REVISION
    assert artifact.completed_at_utc == COMPLETED.isoformat()
    assert all(check.passed for check in artifact.checks)
    assert {check.name for check in artifact.checks} >= {
        "provider_failure_is_loud",
        "missing_bar_is_rejected",
        "malformed_bar_is_rejected",
        "persistence_failure_is_rejected",
        "tick_conservation_holds",
        "no_candidate_is_fabricated",
    }


def test_kill_switch_exercises_default_off_health_and_delivery_isolation():
    artifact = run_kill_switch(REVISION, completed_at=COMPLETED)

    assert artifact.kind == "kill_switch"
    assert artifact.status == "passed"
    assert all(check.passed for check in artifact.checks)


def test_changed_compose_default_makes_kill_switch_artifact_fail(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("POSTMARKET_SHADOW_ENABLED: ${POSTMARKET_SHADOW_ENABLED:-1}\n")

    artifact = run_kill_switch(
        REVISION,
        completed_at=COMPLETED,
        compose_path=compose,
    )

    assert artifact.status == "failed"
    assert not next(
        check for check in artifact.checks if check.name == "compose_default_is_off"
    ).passed


def test_delivery_import_makes_kill_switch_artifact_fail(tmp_path):
    source = tmp_path / "shadow.py"
    source.write_text("import tradebot.alerts\n", encoding="utf-8")

    artifact = run_kill_switch(
        REVISION,
        completed_at=COMPLETED,
        shadow_source_path=source,
    )

    assert artifact.status == "failed"
    assert not next(
        check for check in artifact.checks if check.name == "observer_has_no_delivery_import"
    ).passed


def test_rollback_rehearsal_restores_bytes_and_accepts_real_ancestor(tmp_path):
    repo, base, current, _ = _git_fixture(tmp_path)

    artifact = run_rollback_rehearsal(
        current,
        base,
        completed_at=COMPLETED,
        repo_path=repo,
    )

    assert artifact.status == "passed"
    assert all(check.passed for check in artifact.checks)
    assert "backup_sha256=" in next(
        check
        for check in artifact.checks
        if check.name == "restored_database_matches_backup_bytes"
    ).evidence


def test_nonancestor_rollback_target_is_recorded_as_failed_control(tmp_path):
    repo, _, current, side = _git_fixture(tmp_path)

    artifact = run_rollback_rehearsal(
        current,
        side,
        completed_at=COMPLETED,
        repo_path=repo,
    )

    assert artifact.status == "failed"
    assert not next(
        check
        for check in artifact.checks
        if check.name == "rollback_revision_is_ancestor"
    ).passed


@pytest.mark.parametrize("revision", ["unknown", "abc", "ABCDEF1", "abcdefg", "a" * 41])
def test_unattributable_revision_is_rejected(revision):
    with pytest.raises(ValueError, match="Git SHA"):
        run_failure_injection(revision, completed_at=COMPLETED)


def test_control_summary_cannot_claim_pass_over_a_failed_check():
    artifact = _artifact(
        "kill_switch",
        REVISION,
        COMPLETED,
        (ControlCheck("failed", False, "injected failure"),),
    )

    assert artifact.status == "failed"


def test_writer_is_immutable_and_digest_matches_bytes(tmp_path):
    artifact = run_failure_injection(REVISION, completed_at=COMPLETED)
    path = tmp_path / "failure.json"

    written = write_artifact(path, artifact)

    assert written.sha256 == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError, match="refusing to replace"):
        write_artifact(path, artifact)


def test_suite_writes_exact_gate_compatible_control_set(tmp_path):
    repo, base, current, _ = _git_fixture(tmp_path)
    output = tmp_path / "evidence"

    written = run_control_suite(
        current,
        base,
        output,
        completed_at=COMPLETED,
        repo_path=repo,
    )

    assert {item.kind for item in written} == set(CONTROL_KINDS)
    assert {Path(item.path).name for item in written} == {
        "failure_injection.json",
        "kill_switch.json",
        "rollback_runbook.json",
    }
    for item in written:
        payload = json.loads(Path(item.path).read_text(encoding="utf-8"))
        assert set(payload) == {
            "schema_version",
            "kind",
            "status",
            "revision",
            "completed_at_utc",
            "checks",
        }
        manifest_control = type(
            "Artifact",
            (),
            {
                "kind": item.kind,
                "path": Path(item.path),
                "sha256": item.sha256,
                "revision": item.revision,
                "completed_at_utc": COMPLETED,
            },
        )()
        assert _control_passed(manifest_control) is True


def test_suite_refuses_to_overwrite_any_existing_evidence_set(tmp_path):
    repo, base, current, _ = _git_fixture(tmp_path)
    output = tmp_path / "evidence"
    output.mkdir()
    sentinel = output / "kill_switch.json"
    sentinel.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="existing control evidence set"):
        run_control_suite(
            current,
            base,
            output,
            completed_at=COMPLETED,
            repo_path=repo,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert sorted(path.name for path in output.iterdir()) == ["kill_switch.json"]


def test_suite_publication_is_atomic_when_a_write_fails(tmp_path, monkeypatch):
    repo, base, current, _ = _git_fixture(tmp_path)
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
        run_control_suite(
            current,
            base,
            output,
            completed_at=COMPLETED,
            repo_path=repo,
        )

    assert not output.exists()
    assert list(tmp_path.glob(".evidence.*.tmp")) == []


def test_cli_writes_json_inventory_and_reports_invalid_revision(tmp_path, capsys):
    repo, base, current, _ = _git_fixture(tmp_path)
    output = tmp_path / "evidence"

    assert main(
        [
            "--revision",
            current,
            "--rollback-revision",
            base,
            "--output-dir",
            str(output),
            "--repo",
            str(repo),
        ]
    ) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert {item["kind"] for item in inventory} == set(CONTROL_KINDS)

    assert main(
        [
            "--revision",
            "unknown",
            "--rollback-revision",
            base,
            "--output-dir",
            str(tmp_path / "bad"),
            "--repo",
            str(repo),
        ]
    ) == 2
    assert "Git SHA" in json.loads(capsys.readouterr().out)["error"]


def test_control_tool_import_graph_has_no_live_market_or_delivery_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_controls.py"
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
        "tradebot.marketdata",
        "tradebot.telegram_bot",
        "tradebot.vendors",
    )
    assert not any(module.startswith(forbidden) for module in imports)
