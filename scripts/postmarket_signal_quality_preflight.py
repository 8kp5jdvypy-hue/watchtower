#!/usr/bin/env python3
"""Fail-closed preflight for a complete Perch signal-quality shadow deploy.

The command is read-only. It does not build images, restart services, alter
configuration, fetch providers, enable alerts, or print secret values. It
separates basic shadow-deploy safety from full evidence-campaign readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_backup import _parse_manifest, _verify_files, validate_artifact_archive
from tradebot.postmarket_reference_manifest import parse_reference_manifest


SCHEMA_VERSION = 1
REQUIRED_DATABASES = (
    "journal",
    "users",
    "evaluations",
    "postmarket_shadow",
    "universe",
)
REQUIRED_SHADOW_SWITCHES = (
    "POSTMARKET_SHADOW_ENABLED",
    "POSTMARKET_DISCOVERY_ENABLED",
)
REQUIRED_MARKET_DATA_KEYS = ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY")
REQUIRED_CAMPAIGN_KEYS = (
    "MASSIVE_API_KEY",
    "MASSIVE_S3_ACCESS_KEY_ID",
    "MASSIVE_S3_SECRET_ACCESS_KEY",
)
REQUIRED_CONTROL_KINDS = {
    "discovery_failure_injection",
    "discovery_kill_switch",
    "discovery_delivery_isolation",
    "rollback_runbook",
}
TRUE_VALUES = {"1", "true", "yes", "on"}
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER_VALUES = {
    "changeme",
    "change-me",
    "placeholder",
    "replace-me",
    "todo",
    "unset",
    "your-key-here",
}


@dataclass(frozen=True)
class PreflightCheck:
    scope: str
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class SignalQualityPreflightReport:
    schema_version: int
    checked_at_utc: str
    expected_revision: str
    actual_revision: str | None
    safe_to_deploy_shadow: bool
    evidence_campaign_ready: bool
    checks: tuple[PreflightCheck, ...]


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _check(scope: str, name: str, passed: bool, evidence: str) -> PreflightCheck:
    return PreflightCheck(scope, name, bool(passed), evidence)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"environment file must be a regular non-symlink file: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid environment assignment on line {line_number}")
        try:
            tokens = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(
                f"invalid quoted environment value on line {line_number}"
            ) from exc
        value = " ".join(tokens) if tokens else ""
        if key in values:
            raise ValueError(f"duplicate environment key {key!r}")
        values[key] = value
    return values


def _configured(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    normalized = value.strip().lower()
    return normalized not in PLACEHOLDER_VALUES and not normalized.startswith("your_")


def _enabled(value: str | None) -> bool:
    return value is not None and value.strip().lower() in TRUE_VALUES


def _quick_check(path: Path) -> tuple[bool, str]:
    if not path.is_file() or path.is_symlink():
        return False, "missing or non-regular database"
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            result = [row[0] for row in conn.execute("PRAGMA quick_check")]
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        return False, _safe_error(exc)
    return result == ["ok"], f"quick_check={result!r}"


def _control_checks(
    controls: Iterable[tuple[str, Path]],
    *,
    actual_revision: str | None,
    now: datetime,
) -> tuple[PreflightCheck, ...]:
    items = tuple(controls)
    kinds = tuple(kind for kind, _ in items)
    inventory_ok = len(kinds) == len(set(kinds)) and set(kinds) == REQUIRED_CONTROL_KINDS
    checks = [
        _check(
            "deployment",
            "exact_operational_control_inventory",
            inventory_ok,
            f"observed={sorted(kinds)!r} required={sorted(REQUIRED_CONTROL_KINDS)!r}",
        )
    ]
    if not inventory_ok:
        return tuple(checks)
    for kind, path in sorted(items):
        try:
            if not path.is_file() or path.is_symlink():
                raise ValueError("artifact must be a regular non-symlink file")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("artifact root must be an object")
            if set(payload) != {
                "schema_version",
                "kind",
                "status",
                "revision",
                "completed_at_utc",
                "checks",
            }:
                raise ValueError("artifact fields do not match the control contract")
            revision = payload.get("revision")
            artifact_checks = payload.get("checks")
            completed_at = datetime.fromisoformat(payload["completed_at_utc"])
            if completed_at.tzinfo is None or completed_at.utcoffset() is None:
                raise ValueError("control completion timestamp must be timezone-aware")
            completed_at = completed_at.astimezone(timezone.utc)
            revision_ok = (
                isinstance(revision, str)
                and REVISION_PATTERN.fullmatch(revision) is not None
                and actual_revision is not None
                and actual_revision.startswith(revision)
            )
            passed = (
                payload.get("schema_version") == 1
                and payload.get("kind") == kind
                and payload.get("status") == "passed"
                and revision_ok
                and isinstance(artifact_checks, list)
                and bool(artifact_checks)
                and all(
                    isinstance(item, dict)
                    and set(item) == {"name", "passed", "evidence"}
                    and isinstance(item.get("name"), str)
                    and bool(item["name"].strip())
                    and item.get("passed") is True
                    and isinstance(item.get("evidence"), str)
                    and bool(item["evidence"].strip())
                    for item in artifact_checks
                )
                and len({item["name"] for item in artifact_checks})
                == len(artifact_checks)
                and completed_at <= now
            )
            evidence = (
                f"sha256={hashlib.sha256(path.read_bytes()).hexdigest()} "
                f"revision_match={revision_ok} checks="
                f"{len(artifact_checks) if isinstance(artifact_checks, list) else 0}"
            )
        except (OSError, TypeError, UnicodeError, ValueError) as exc:
            passed = False
            evidence = _safe_error(exc)
        checks.append(_check("deployment", f"control_{kind}", passed, evidence))
    return tuple(checks)


def _backup_checks(
    manifest: Path,
    *,
    now: datetime,
    max_age_seconds: int,
) -> tuple[PreflightCheck, ...]:
    checks = []
    try:
        if manifest.is_symlink():
            raise ValueError("backup manifest cannot be a symlink")
        stamp, rows = _parse_manifest(manifest)
        verified = _verify_files(manifest.parent, rows)
        backed_up_databases = {
            kind for _, _, kind in rows if kind != "postmarket_artifacts"
        }
        missing_databases = set(REQUIRED_DATABASES) - backed_up_databases
        if missing_databases:
            raise ValueError(
                "backup set is missing signal-quality databases: "
                f"{sorted(missing_databases)!r}"
            )
        backup_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        age = (now - backup_at).total_seconds()
        age_ok = 0 <= age <= max_age_seconds
        checks.append(
            _check(
                "deployment",
                "recent_verified_backup",
                age_ok,
                f"stamp={stamp} age_seconds={age:.0f} verified_files={len(verified)}",
            )
        )
        artifact_rows = [row for row in rows if row[2] == "postmarket_artifacts"]
        if len(artifact_rows) != 1:
            raise ValueError("backup set must contain one postmarket artifact archive")
        archive = manifest.parent / artifact_rows[0][0]
        artifact_files = validate_artifact_archive(archive)
        roots = {name.split("/", 1)[0] for name in artifact_files}
        roots_ok = {"postmarket_audits", "postmarket_evidence"} <= roots
        checks.append(
            _check(
                "deployment",
                "backup_contains_audits_and_evidence",
                roots_ok,
                f"artifact_files={len(artifact_files)} roots={sorted(roots)!r}",
            )
        )
    except (OSError, ValueError, sqlite3.DatabaseError, tarfile.TarError) as exc:
        checks.append(
            _check("deployment", "verified_backup_set", False, _safe_error(exc))
        )
    return tuple(checks)


def evaluate_signal_quality_preflight(
    *,
    repo_root: Path,
    expected_revision: str,
    env_file: Path,
    backup_env_file: Path,
    data_dir: Path,
    backup_manifest: Path,
    controls: Iterable[tuple[str, Path]],
    reference_manifest: Path | None,
    now: datetime,
    max_backup_age_seconds: int = 7_200,
    min_free_bytes: int = 1_073_741_824,
) -> SignalQualityPreflightReport:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    checked_at = now.astimezone(timezone.utc)
    if max_backup_age_seconds <= 0 or min_free_bytes < 0:
        raise ValueError("backup age must be positive and minimum free bytes non-negative")
    checks: list[PreflightCheck] = []
    actual_revision = None

    try:
        resolved_expected = _run_git(repo_root, "rev-parse", "--verify", expected_revision)
        actual_revision = _run_git(repo_root, "rev-parse", "HEAD")
        origin_main = _run_git(repo_root, "rev-parse", "origin/main")
        clean = not _run_git(repo_root, "status", "--porcelain")
        checks.extend(
            (
                _check(
                    "deployment",
                    "exact_expected_revision",
                    actual_revision == resolved_expected,
                    f"actual={actual_revision} expected={resolved_expected}",
                ),
                _check(
                    "deployment",
                    "revision_matches_origin_main",
                    actual_revision == origin_main,
                    f"actual={actual_revision} origin_main={origin_main}",
                ),
                _check(
                    "deployment",
                    "clean_worktree",
                    clean,
                    f"clean={clean}",
                ),
            )
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        checks.append(_check("deployment", "git_identity", False, _safe_error(exc)))

    try:
        env = _parse_env_file(env_file)
        for key in REQUIRED_MARKET_DATA_KEYS:
            checks.append(
                _check(
                    "deployment",
                    f"configured_{key.lower()}",
                    _configured(env.get(key)),
                    "configured" if _configured(env.get(key)) else "missing_or_placeholder",
                )
            )
        for key in REQUIRED_SHADOW_SWITCHES:
            checks.append(
                _check(
                    "deployment",
                    f"enabled_{key.lower()}",
                    _enabled(env.get(key)),
                    "enabled" if _enabled(env.get(key)) else "not_enabled",
                )
            )
        checks.append(
            _check(
                "campaign",
                "enabled_postmarket_external_context",
                _enabled(env.get("POSTMARKET_EXTERNAL_CONTEXT_ENABLED")),
                (
                    "enabled"
                    if _enabled(env.get("POSTMARKET_EXTERNAL_CONTEXT_ENABLED"))
                    else "not_enabled"
                ),
            )
        )
        for key in REQUIRED_CAMPAIGN_KEYS:
            checks.append(
                _check(
                    "campaign",
                    f"configured_{key.lower()}",
                    _configured(env.get(key)),
                    "configured" if _configured(env.get(key)) else "missing_or_placeholder",
                )
            )
    except (OSError, UnicodeError, ValueError) as exc:
        checks.append(_check("deployment", "application_environment", False, _safe_error(exc)))

    try:
        backup_env = _parse_env_file(backup_env_file)
        remote = backup_env.get("RCLONE_REMOTE")
        passphrase_path = backup_env.get("BACKUP_ENCRYPTION_PASSPHRASE_FILE")
        remote_ok = _configured(remote) and ":" in (remote or "") and bool(
            (remote or "").split(":", 1)[1]
        )
        passphrase_ok = _configured(passphrase_path) and Path(passphrase_path or "").is_file()
        checks.append(
            _check(
                "deployment",
                "offbox_backup_configuration",
                remote_ok and passphrase_ok,
                f"remote_configured={remote_ok} passphrase_file_present={passphrase_ok}",
            )
        )
    except (OSError, UnicodeError, ValueError) as exc:
        checks.append(
            _check("deployment", "offbox_backup_configuration", False, _safe_error(exc))
        )

    for database in REQUIRED_DATABASES:
        passed, evidence = _quick_check(data_dir / f"{database}.db")
        checks.append(
            _check("deployment", f"database_{database}_quick_check", passed, evidence)
        )

    try:
        free_bytes = shutil.disk_usage(data_dir).free
        checks.append(
            _check(
                "deployment",
                "minimum_free_disk",
                free_bytes >= min_free_bytes,
                f"free_bytes={free_bytes} required={min_free_bytes}",
            )
        )
    except OSError as exc:
        checks.append(_check("deployment", "minimum_free_disk", False, _safe_error(exc)))

    checks.extend(
        _backup_checks(
            backup_manifest,
            now=checked_at,
            max_age_seconds=max_backup_age_seconds,
        )
    )
    checks.extend(
        _control_checks(
            controls,
            actual_revision=actual_revision,
            now=checked_at,
        )
    )

    if reference_manifest is None:
        checks.append(
            _check(
                "campaign",
                "licensed_reference_manifest",
                False,
                "not_supplied",
            )
        )
    else:
        try:
            if not reference_manifest.is_file() or reference_manifest.is_symlink():
                raise ValueError("manifest must be a regular non-symlink file")
            reference = parse_reference_manifest(
                reference_manifest.read_bytes(), observed_at=checked_at
            )
            checks.append(
                _check(
                    "campaign",
                    "licensed_reference_manifest",
                    True,
                    f"provider={reference.provider} dataset={reference.dataset} "
                    f"rows={len(reference.rows)} sha256={reference.manifest_sha256}",
                )
            )
        except (OSError, ValueError) as exc:
            checks.append(
                _check("campaign", "licensed_reference_manifest", False, _safe_error(exc))
            )

    deployment_checks = [item for item in checks if item.scope == "deployment"]
    campaign_checks = [item for item in checks if item.scope == "campaign"]
    safe = bool(deployment_checks) and all(item.passed for item in deployment_checks)
    campaign_ready = safe and bool(campaign_checks) and all(
        item.passed for item in campaign_checks
    )
    return SignalQualityPreflightReport(
        SCHEMA_VERSION,
        checked_at.isoformat(),
        expected_revision,
        actual_revision,
        safe,
        campaign_ready,
        tuple(checks),
    )


def _control_path(raw: str) -> tuple[str, Path]:
    kind, separator, path_text = raw.partition("=")
    if not separator or not path_text:
        raise argparse.ArgumentTypeError("control must use KIND=PATH")
    return kind, Path(path_text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--backup-env-file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--backup-manifest", type=Path, required=True)
    parser.add_argument("--control", action="append", type=_control_path, required=True)
    parser.add_argument("--reference-manifest", type=Path)
    parser.add_argument("--max-backup-age-seconds", type=int, default=7_200)
    parser.add_argument("--min-free-bytes", type=int, default=1_073_741_824)
    args = parser.parse_args(argv)
    try:
        report = evaluate_signal_quality_preflight(
            repo_root=args.repo_root,
            expected_revision=args.expected_revision,
            env_file=args.env_file,
            backup_env_file=args.backup_env_file,
            data_dir=args.data_dir,
            backup_manifest=args.backup_manifest,
            controls=args.control,
            reference_manifest=args.reference_manifest,
            now=datetime.now(timezone.utc),
            max_backup_age_seconds=args.max_backup_age_seconds,
            min_free_bytes=args.min_free_bytes,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"error": _safe_error(exc)}))
        return 2
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.evidence_campaign_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
