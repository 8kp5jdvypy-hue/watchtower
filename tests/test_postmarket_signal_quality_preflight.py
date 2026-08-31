"""Safe, secret-free preflight for the full signal-quality shadow stack."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.postmarket_signal_quality_preflight import (
    REQUIRED_CONTROL_KINDS,
    evaluate_signal_quality_preflight,
)
from tradebot.screening_archive import archive_screening_session
from tradebot.universe import connect as connect_universe


NOW = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
STAMP = "20260829T003000Z"
SECRETS = {
    "ALPACA_KEY_ID": "alpaca-key-secret",
    "ALPACA_SECRET_KEY": "alpaca-secret-secret",
    "MASSIVE_API_KEY": "massive-rest-secret",
    "MASSIVE_S3_ACCESS_KEY_ID": "massive-s3-id-secret",
    "MASSIVE_S3_SECRET_ACCESS_KEY": "massive-s3-secret-secret",
}


def test_script_is_directly_executable_outside_repository(tmp_path):
    script = (
        Path(__file__).parents[1]
        / "scripts"
        / "postmarket_signal_quality_preflight.py"
    )

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--expected-revision" in result.stdout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "preflight@example.invalid")
    _git(repo, "config", "user.name", "Preflight Test")
    (repo / "source.txt").write_text("signal quality\n")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-m", "fixture")
    _git(repo, "branch", "-M", "main")
    revision = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", revision)
    return repo, revision


def _env(path: Path, *, omit: str | None = None) -> None:
    values = {
        **SECRETS,
        "POSTMARKET_SHADOW_ENABLED": "1",
        "POSTMARKET_DISCOVERY_ENABLED": "true",
        "POSTMARKET_EXTERNAL_CONTEXT_ENABLED": "yes",
    }
    if omit is not None:
        values.pop(omit)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


def _databases(data_dir: Path) -> None:
    data_dir.mkdir()
    for name in ("journal", "users", "evaluations", "postmarket_shadow"):
        conn = sqlite3.connect(data_dir / f"{name}.db")
        try:
            conn.execute("CREATE TABLE fixture (value INTEGER)")
            conn.commit()
        finally:
            conn.close()
    universe = connect_universe(data_dir / "universe.db")
    universe.execute(
        """
        INSERT INTO screening_ticks
          (session,tick_utc,run_id,run_mode,screen_version,code_version,
           audit_mode,universe_count,thresholds_json,counts_json,invariant_ok,
           promotion_limit,latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "2026-08-28", "2026-08-28T20:00:00+00:00", "preflight-fixture",
            "live", 2, "abcdef1", 0, 1, "{}", '{"quiet":1}', 1, 25, 10,
        ),
    )
    universe.commit()
    universe.close()
    archive_screening_session(
        data_dir / "universe.db",
        data_dir / "screening_archives",
        session="2026-08-28",
        now=NOW,
    )


def _artifact_archive(
    path: Path,
    data_dir: Path,
    *,
    include_screening: bool = True,
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in (
            "postmarket_audits/audit.json",
            "postmarket_evidence/control.json",
        ):
            raw = b"{}\n"
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        if include_screening:
            screening = next(
                (data_dir / "screening_archives").glob("screening_*.jsonl.gz")
            )
            archive.add(screening, arcname=f"screening_archives/{screening.name}")


def _backup(backup_dir: Path, data_dir: Path) -> Path:
    backup_dir.mkdir()
    files = []
    for name in ("journal", "users", "evaluations", "postmarket_shadow", "universe"):
        path = backup_dir / f"{name}_{STAMP}.db.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(name.encode())
        files.append(path)
    artifacts = backup_dir / f"postmarket_artifacts_{STAMP}.tar.gz"
    _artifact_archive(artifacts, data_dir)
    files.append(artifacts)
    manifest = backup_dir / f"manifest_{STAMP}.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in files
        )
    )
    return manifest


def _controls(root: Path, revision: str) -> list[tuple[str, Path]]:
    root.mkdir()
    result = []
    for kind in sorted(REQUIRED_CONTROL_KINDS):
        path = root / f"{kind}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": kind,
                    "status": "passed",
                    "revision": revision[:7],
                    "completed_at_utc": (NOW - timedelta(minutes=5)).isoformat(),
                    "checks": [
                        {"name": "fixture", "passed": True, "evidence": "passed"}
                    ],
                },
                sort_keys=True,
            )
            + "\n"
        )
        result.append((kind, path))
    return result


def _reference(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "locked",
                "provider": "licensed-vendor",
                "dataset": "sector-float-v1",
                "license_reference": "contract-2026-001",
                "effective_date": "2026-08-28",
                "published_at_utc": "2026-08-28T22:00:00+00:00",
                "created_at_utc": "2026-08-28T22:01:00+00:00",
                "classification_system": "GICS",
                "rows": [
                    {
                        "symbol": "ABC",
                        "sector_code": "45",
                        "sector_name": "Information Technology",
                        "benchmark_symbol": "XLK",
                        "float_shares": 1_000_000,
                        "float_as_of_date": "2026-08-28",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )


def _fixture(tmp_path: Path) -> dict:
    repo, revision = _git_repo(tmp_path)
    env_file = tmp_path / ".env"
    _env(env_file)
    passphrase = tmp_path / ".backup-passphrase"
    passphrase.write_text("backup-secret")
    backup_env = tmp_path / ".backup-env"
    backup_env.write_text(
        "RCLONE_REMOTE=remote:perch-backups\n"
        f"BACKUP_ENCRYPTION_PASSPHRASE_FILE={passphrase}\n"
    )
    data_dir = tmp_path / "data"
    _databases(data_dir)
    backup_manifest = _backup(tmp_path / "backups", data_dir)
    controls = _controls(tmp_path / "controls", revision)
    reference = tmp_path / "reference.json"
    _reference(reference)
    return {
        "repo_root": repo,
        "expected_revision": revision,
        "env_file": env_file,
        "backup_env_file": backup_env,
        "data_dir": data_dir,
        "backup_manifest": backup_manifest,
        "controls": controls,
        "reference_manifest": reference,
        "now": NOW,
        "max_backup_age_seconds": 7_200,
        "min_free_bytes": 0,
    }


def _evaluate(args: dict):
    return evaluate_signal_quality_preflight(**args)


def test_complete_preflight_is_shadow_safe_and_campaign_ready(tmp_path):
    report = _evaluate(_fixture(tmp_path))

    assert report.safe_to_deploy_shadow is True
    assert report.evidence_campaign_ready is True
    assert report.actual_revision == report.expected_revision
    assert all(check.passed for check in report.checks)


def test_missing_independent_provider_key_blocks_campaign_not_safe_shadow(tmp_path):
    args = _fixture(tmp_path)
    _env(args["env_file"], omit="MASSIVE_S3_SECRET_ACCESS_KEY")

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is True
    assert report.evidence_campaign_ready is False
    check = next(
        item
        for item in report.checks
        if item.name == "configured_massive_s3_secret_access_key"
    )
    assert check.scope == "campaign"
    assert check.passed is False


def test_corrupt_live_database_blocks_shadow_deploy(tmp_path):
    args = _fixture(tmp_path)
    (args["data_dir"] / "journal.db").write_bytes(b"not sqlite")

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is False
    assert report.evidence_campaign_ready is False
    assert not next(
        item for item in report.checks if item.name == "database_journal_quick_check"
    ).passed


def test_stale_backup_blocks_shadow_deploy(tmp_path):
    args = _fixture(tmp_path)
    args["now"] = NOW + timedelta(hours=3)

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is False
    assert not next(
        item for item in report.checks if item.name == "recent_verified_backup"
    ).passed


def test_backup_without_universe_screening_evidence_blocks_shadow_deploy(tmp_path):
    args = _fixture(tmp_path)
    manifest = args["backup_manifest"]
    manifest.write_text(
        "\n".join(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if "universe_" not in line
        )
        + "\n",
        encoding="utf-8",
    )

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is False
    check = next(item for item in report.checks if item.name == "verified_backup_set")
    assert check.passed is False
    assert "missing signal-quality databases: ['universe']" in check.evidence


def test_backup_without_session_screening_archives_blocks_shadow_deploy(tmp_path):
    args = _fixture(tmp_path)
    manifest = args["backup_manifest"]
    artifacts = manifest.parent / f"postmarket_artifacts_{STAMP}.tar.gz"
    _artifact_archive(artifacts, args["data_dir"], include_screening=False)
    digest = hashlib.sha256(artifacts.read_bytes()).hexdigest()
    manifest.write_text(
        "\n".join(
            f"{digest}  {artifacts.name}" if artifacts.name in line else line
            for line in manifest.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is False
    check = next(
        item
        for item in report.checks
        if item.name == "backup_contains_screening_archives"
    )
    assert check.scope == "deployment"
    assert check.passed is False


def test_dirty_or_non_main_revision_blocks_shadow_deploy(tmp_path):
    dirty_args = _fixture(tmp_path / "dirty")
    (dirty_args["repo_root"] / "untracked.txt").write_text("dirty\n")
    dirty = _evaluate(dirty_args)
    assert dirty.safe_to_deploy_shadow is False
    assert not next(item for item in dirty.checks if item.name == "clean_worktree").passed

    drift_args = _fixture(tmp_path / "drift")
    (drift_args["repo_root"] / "source.txt").write_text("new revision\n")
    _git(drift_args["repo_root"], "commit", "-am", "drift")
    drift_args["expected_revision"] = _git(drift_args["repo_root"], "rev-parse", "HEAD")
    drift = _evaluate(drift_args)
    assert drift.safe_to_deploy_shadow is False
    assert not next(
        item for item in drift.checks if item.name == "revision_matches_origin_main"
    ).passed


def test_wrong_revision_control_blocks_shadow_deploy(tmp_path):
    args = _fixture(tmp_path)
    kind, path = args["controls"][0]
    payload = json.loads(path.read_text())
    payload["revision"] = "abcdef1"
    path.write_text(json.dumps(payload))

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is False
    assert not next(
        item for item in report.checks if item.name == f"control_{kind}"
    ).passed


def test_malformed_control_is_a_failed_check_not_an_uncaught_error(tmp_path):
    args = _fixture(tmp_path)
    kind, path = args["controls"][0]
    payload = json.loads(path.read_text())
    payload["completed_at_utc"] = None
    path.write_text(json.dumps(payload))

    report = _evaluate(args)

    assert report.safe_to_deploy_shadow is False
    check = next(item for item in report.checks if item.name == f"control_{kind}")
    assert check.passed is False
    assert check.evidence.startswith("TypeError:")


def test_report_never_contains_secret_values(tmp_path):
    args = _fixture(tmp_path)

    rendered = json.dumps(asdict(_evaluate(args)), sort_keys=True)

    for secret in (*SECRETS.values(), "backup-secret"):
        assert secret not in rendered
