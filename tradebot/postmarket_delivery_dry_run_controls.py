"""Offline control evidence for the postmarket customer-readiness dry run.

Exercises use deterministic fixtures, in-memory SQLite, and checked-in files.
They cannot call providers, contact customers, trade, edit configuration,
restart services, or touch production data. Passing never enables delivery.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tradebot.postmarket_delivery_dry_run import route_dry_run
from tradebot.postmarket_delivery_dry_run_health import evaluate_dry_run_health
from tradebot.postmarket_delivery_dry_run_shadow import (
    dry_run_shadow_enabled,
)
from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    DECISION_ELIGIBLE,
    DECISION_SUPPRESSED,
    PRESENTATION_STALE,
    DeliveryCandidate,
    DeliveryPolicy,
    OwnerAuthorization,
)
from tradebot.postmarket_lifecycle import STATE_CONFIRMED


CONTROL_SCHEMA_VERSION = 1
CONTROL_KINDS = (
    "customer_dry_run_failure_injection",
    "customer_dry_run_kill_switch",
    "customer_dry_run_delivery_isolation",
    "customer_dry_run_rollback_runbook",
)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "postmarket-customer-delivery-readiness.md"
MODULE_PATHS = (
    REPO_ROOT / "tradebot" / "postmarket_delivery_readiness.py",
    REPO_ROOT / "tradebot" / "postmarket_delivery_dry_run.py",
    REPO_ROOT / "tradebot" / "postmarket_delivery_dry_run_shadow.py",
    REPO_ROOT / "tradebot" / "postmarket_delivery_dry_run_health.py",
    REPO_ROOT / "tradebot" / "postmarket_delivery_dry_run_audit.py",
    REPO_ROOT / "tradebot" / "postmarket_customer_dry_run_campaign.py",
    REPO_ROOT / "tradebot" / "postmarket_customer_dry_run_review.py",
    REPO_ROOT / "tradebot" / "postmarket_customer_dry_run_gate.py",
)
FORBIDDEN_IMPORTS = (
    "tradebot.alerts",
    "tradebot.broker",
    "tradebot.order",
    "tradebot.telegram_bot",
    "requests",
    "alpaca",
)
NOW = datetime(2026, 8, 28, 21, 15, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DryRunControlCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class DryRunControlEvidence:
    schema_version: int
    kind: str
    status: str
    revision: str
    completed_at_utc: str
    checks: tuple[DryRunControlCheck, ...]


@dataclass(frozen=True)
class WrittenDryRunControl:
    kind: str
    path: str
    sha256: str
    revision: str
    completed_at_utc: str


def _revision(raw: str, context: str) -> str:
    value = raw.strip()
    if value == "unknown" or not REVISION_PATTERN.fullmatch(value):
        raise ValueError(f"{context} must be a 7-40 character lowercase Git SHA")
    return value


def _completed_at(raw: datetime | None) -> datetime:
    value = raw or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("completed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _artifact(
    kind: str,
    revision: str,
    completed_at: datetime,
    checks,
) -> DryRunControlEvidence:
    if kind not in CONTROL_KINDS:
        raise ValueError(f"unsupported customer dry-run control kind {kind!r}")
    checks = tuple(checks)
    if not checks:
        raise ValueError("customer dry-run control evidence requires checks")
    return DryRunControlEvidence(
        schema_version=CONTROL_SCHEMA_VERSION,
        kind=kind,
        status="passed" if all(check.passed for check in checks) else "failed",
        revision=_revision(revision, "revision"),
        completed_at_utc=_completed_at(completed_at).isoformat(),
        checks=checks,
    )


def _policy(revision: str) -> DeliveryPolicy:
    return DeliveryPolicy(
        router_revision=revision,
        evidence_set_sha256="a" * 64,
        evidence_gate_sha256="b" * 64,
        rank_version=1,
        minimum_evidence_score=60,
        maximum_ordinal_rank=10,
        minimum_evidence_coverage_pct=90,
        maximum_data_age_seconds=330,
        allowed_states=(STATE_CONFIRMED,),
        allowed_evidence_revisions=(revision,),
        allowed_providers=("alpaca",),
        allowed_feeds=("sip",),
    )


def _authorization(policy: DeliveryPolicy) -> OwnerAuthorization:
    return OwnerAuthorization(
        release_id="control-release",
        approved_by="control-owner",
        approved_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        policy_sha256=policy.sha256,
        evidence_set_sha256=policy.evidence_set_sha256,
        evidence_gate_sha256=policy.evidence_gate_sha256,
        router_revision=policy.router_revision,
        acknowledgement=ACKNOWLEDGEMENT,
        dry_run_readiness_approved=True,
    )


def _candidate(revision: str) -> DeliveryCandidate:
    return DeliveryCandidate(
        transition_id=44,
        candidate_id=12,
        session="2026-08-28",
        symbol="OKTA",
        direction="up",
        lifecycle_state=STATE_CONFIRMED,
        actionability="QUALIFIED",
        transition_at=NOW - timedelta(minutes=2),
        evidence_bar_open_at=NOW - timedelta(minutes=7),
        rank_run_id=17,
        rank_version=1,
        rank_status="complete",
        rankable=True,
        ordinal_rank=3,
        evidence_score=77,
        evidence_coverage_pct=100,
        exclusion_reasons=(),
        data_feed="sip",
        market_data_provider="alpaca",
        code_version=revision,
    )


def _route(conn, candidate, policy, authorization, **overrides):
    values = {
        "now": NOW,
        "runtime_router_revision": policy.router_revision,
        "run_id": "control-run",
        "dry_run_enabled": True,
        "kill_switch_engaged": False,
        "operational_status": "clean",
    }
    values.update(overrides)
    return route_dry_run(conn, candidate, policy, authorization, **values)


def run_failure_injection(
    revision: str,
    *,
    completed_at: datetime | None = None,
) -> DryRunControlEvidence:
    revision = _revision(revision, "revision")
    policy = _policy(revision)
    authorization = _authorization(policy)
    candidate = _candidate(revision)
    conn = sqlite3.connect(":memory:")
    try:
        missing_owner = _route(conn, candidate, policy, None)
        stale = _route(
            conn,
            replace(candidate, evidence_bar_open_at=NOW - timedelta(minutes=20)),
            policy,
            authorization,
        )
        degraded = _route(
            conn, candidate, policy, authorization, operational_status="degraded"
        )
        wrong_revision = _route(
            conn,
            candidate,
            policy,
            authorization,
            runtime_router_revision="deadbee",
        )
        eligible = _route(conn, candidate, policy, authorization)
        duplicate = _route(
            conn, candidate, policy, authorization, run_id="control-rerun"
        )
        eligible_rows = int(conn.execute(
            """
            SELECT COUNT(*) FROM postmarket_delivery_dry_runs
            WHERE decision='ELIGIBLE_FOR_DRY_RUN'
            """
        ).fetchone()[0])
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    checks = (
        DryRunControlCheck(
            "missing_owner_authorization_is_suppressed",
            missing_owner.decision == DECISION_SUPPRESSED
            and "OWNER_AUTHORIZATION_MISSING" in missing_owner.reason_codes,
            f"decision={missing_owner.decision}; reasons={missing_owner.reason_codes!r}",
        ),
        DryRunControlCheck(
            "stale_completed_bar_is_explicitly_suppressed",
            stale.decision == DECISION_SUPPRESSED
            and stale.presentation == PRESENTATION_STALE
            and "DATA_STALE" in stale.reason_codes,
            f"decision={stale.decision}; presentation={stale.presentation}; "
            f"reasons={stale.reason_codes!r}",
        ),
        DryRunControlCheck(
            "degraded_discovery_is_suppressed",
            degraded.decision == DECISION_SUPPRESSED
            and "OPERATIONAL_STATUS_DEGRADED" in degraded.reason_codes,
            f"decision={degraded.decision}; reasons={degraded.reason_codes!r}",
        ),
        DryRunControlCheck(
            "runtime_revision_mismatch_is_suppressed",
            wrong_revision.decision == DECISION_SUPPRESSED
            and "RUNTIME_ROUTER_REVISION_MISMATCH" in wrong_revision.reason_codes,
            f"decision={wrong_revision.decision}; reasons={wrong_revision.reason_codes!r}",
        ),
        DryRunControlCheck(
            "exact_clean_case_is_dry_run_eligible_only",
            eligible.decision == DECISION_ELIGIBLE,
            f"decision={eligible.decision}; presentation={eligible.presentation}",
        ),
        DryRunControlCheck(
            "eligible_identity_is_transactionally_deduplicated",
            duplicate.created is False
            and duplicate.route_id == eligible.route_id
            and eligible_rows == 1,
            f"first_created={eligible.created}; duplicate_created={duplicate.created}; "
            f"eligible_rows={eligible_rows}",
        ),
        DryRunControlCheck(
            "control_ledger_remains_valid_sqlite",
            quick_check == "ok",
            f"PRAGMA quick_check={quick_check}",
        ),
    )
    return _artifact(
        "customer_dry_run_failure_injection",
        revision,
        _completed_at(completed_at),
        checks,
    )


def _compose_service_block(compose: str, service_name: str) -> str:
    marker = f"  {service_name}:"
    if marker not in compose:
        return ""
    tail = compose.split(marker, 1)[1]
    next_service = re.search(r"\n  [a-zA-Z0-9_-]+:\n", tail)
    return tail[:next_service.start()] if next_service else tail


def run_kill_switch(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    supervisor_path: Path = MODULE_PATHS[2],
) -> DryRunControlEvidence:
    revision = _revision(revision, "revision")
    compose_raw = compose_path.read_bytes()
    source_raw = supervisor_path.read_bytes()
    compose = compose_raw.decode("utf-8")
    source = source_raw.decode("utf-8")
    service = _compose_service_block(compose, "postmarket-customer-dry-run")
    ambiguous_rejected = False
    try:
        dry_run_shadow_enabled("maybe")
    except ValueError:
        ambiguous_rejected = True
    policy = _policy(revision)
    conn = sqlite3.connect(":memory:")
    try:
        blocked = route_dry_run(
            conn,
            _candidate(revision),
            policy,
            _authorization(policy),
            now=NOW,
            runtime_router_revision=revision,
            run_id="kill-switch",
        )
        eligible_rows = int(conn.execute(
            """
            SELECT COUNT(*) FROM postmarket_delivery_dry_runs
            WHERE decision='ELIGIBLE_FOR_DRY_RUN'
            """
        ).fetchone()[0])
    finally:
        conn.close()
    main_source = source.split("def main()", 1)[1] if "def main()" in source else ""
    disabled_branch = main_source.find("if not enabled:")
    contract_load = main_source.find("load_contracts(")
    checks = (
        DryRunControlCheck(
            "parser_defaults_off_and_rejects_ambiguity",
            dry_run_shadow_enabled("") is False
            and dry_run_shadow_enabled("0") is False
            and ambiguous_rejected,
            f"empty={dry_run_shadow_enabled('')}; zero={dry_run_shadow_enabled('0')}; "
            f"ambiguous_rejected={ambiguous_rejected}",
        ),
        DryRunControlCheck(
            "compose_service_is_independently_default_off",
            "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED: "
            "${POSTMARKET_CUSTOMER_DRY_RUN_ENABLED:-0}" in service,
            f"service_sha256={hashlib.sha256(service.encode()).hexdigest()}; "
            f"compose_sha256={hashlib.sha256(compose_raw).hexdigest()}",
        ),
        DryRunControlCheck(
            "disabled_supervisor_does_not_load_owner_contracts",
            0 <= disabled_branch < contract_load,
            f"disabled_branch_offset={disabled_branch}; contract_load_offset={contract_load}; "
            f"source_sha256={hashlib.sha256(source_raw).hexdigest()}",
        ),
        DryRunControlCheck(
            "default_policy_call_engages_both_safe_controls",
            blocked.decision == DECISION_SUPPRESSED
            and "DRY_RUN_DISABLED" in blocked.reason_codes
            and "KILL_SWITCH_ENGAGED" in blocked.reason_codes
            and eligible_rows == 0,
            f"decision={blocked.decision}; reasons={blocked.reason_codes!r}; "
            f"eligible_rows={eligible_rows}",
        ),
        DryRunControlCheck(
            "disabled_health_requires_no_contract_or_heartbeat",
            evaluate_dry_run_health(
                Path("/definitely/missing/heartbeat.json"),
                enabled=False,
                expected_revision=revision,
                now=NOW,
            ).healthy,
            "disabled health is healthy without reading a heartbeat",
        ),
    )
    return _artifact(
        "customer_dry_run_kill_switch",
        revision,
        _completed_at(completed_at),
        checks,
    )


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def run_delivery_isolation(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    module_paths: tuple[Path, ...] = MODULE_PATHS,
) -> DryRunControlEvidence:
    revision = _revision(revision, "revision")
    compose_raw = compose_path.read_bytes()
    compose = compose_raw.decode("utf-8")
    service = _compose_service_block(compose, "postmarket-customer-dry-run")
    imports = set()
    tokens = []
    digests = {}
    for path in module_paths:
        raw = path.read_bytes()
        source = raw.decode("utf-8")
        imports |= _imported_modules(source)
        tokens.extend(
            token for token in (
                "enqueue_broadcast(", "send_alert(", "place_order(",
                "submit_order(", "users.db",
            )
            if token in source
        )
        digests[path.name] = hashlib.sha256(raw).hexdigest()
    forbidden = sorted(
        module for module in imports
        if any(module == prefix or module.startswith(f"{prefix}.")
               for prefix in FORBIDDEN_IMPORTS)
    )
    checks = (
        DryRunControlCheck(
            "dry_run_modules_have_no_delivery_provider_or_order_import",
            not forbidden,
            f"forbidden_imports={forbidden!r}; module_sha256={digests!r}",
        ),
        DryRunControlCheck(
            "dry_run_modules_have_no_customer_or_order_callsite",
            not tokens,
            f"forbidden_tokens={sorted(set(tokens))!r}",
        ),
        DryRunControlCheck(
            "compose_service_has_no_worker_bot_or_api_dependency",
            "depends_on:" not in service,
            f"service_lines={len(service.splitlines())}; "
            f"service_sha256={hashlib.sha256(service.encode()).hexdigest()}",
        ),
        DryRunControlCheck(
            "compose_runs_only_readiness_dry_run_supervisor",
            "command: python -m tradebot.postmarket_delivery_dry_run_shadow" in service,
            f"compose_sha256={hashlib.sha256(compose_raw).hexdigest()}",
        ),
        DryRunControlCheck(
            "durable_state_is_shadow_database_only",
            'SHADOW_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"'
            in module_paths[2].read_text(encoding="utf-8"),
            f"supervisor={module_paths[2].name}",
        ),
    )
    return _artifact(
        "customer_dry_run_delivery_isolation",
        revision,
        _completed_at(completed_at),
        checks,
    )


def run_rollback_runbook(
    revision: str,
    *,
    completed_at: datetime | None = None,
    runbook_path: Path = RUNBOOK_PATH,
) -> DryRunControlEvidence:
    revision = _revision(revision, "revision")
    raw = runbook_path.read_bytes()
    text = raw.decode("utf-8")
    required = (
        "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED=0",
        "--no-deps --force-recreate postmarket-customer-dry-run",
        "postmarket_delivery_dry_run_heartbeat.json",
        "PRAGMA quick_check",
    )
    preserves_ledger = bool(re.search(
        r"Never delete or rewrite\s+`postmarket_delivery_dry_runs`", text
    ))
    checks = (
        DryRunControlCheck(
            "runbook_disables_only_the_independent_dry_run_service",
            required[0] in text and required[1] in text,
            f"required_switch={required[0] in text}; "
            f"required_recreate={required[1] in text}",
        ),
        DryRunControlCheck(
            "runbook_verifies_disabled_heartbeat_and_database_integrity",
            required[2] in text and required[3] in text,
            f"heartbeat_check={required[2] in text}; quick_check={required[3] in text}",
        ),
        DryRunControlCheck(
            "runbook_preserves_append_only_evidence",
            preserves_ledger,
            f"preservation_instruction={preserves_ledger}",
        ),
        DryRunControlCheck(
            "runbook_is_revision_bound_checked_in_evidence",
            bool(raw) and "customer delivery remains disabled" in text.lower(),
            f"runbook_sha256={hashlib.sha256(raw).hexdigest()}; revision={revision}",
        ),
    )
    return _artifact(
        "customer_dry_run_rollback_runbook",
        revision,
        _completed_at(completed_at),
        checks,
    )


def _git_commit(repo_path: Path, revision: str) -> str:
    revision = _revision(revision, "Git revision")
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(f"Git revision {revision} is not a commit in {repo_path}")
    return result.stdout.strip().lower()


def _git_head(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(f"cannot resolve checked-out HEAD in {repo_path}")
    return result.stdout.strip().lower()


def _git_is_clean(repo_path: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(f"cannot inspect Git worktree state in {repo_path}")
    return not result.stdout.strip()


def artifact_bytes(artifact: DryRunControlEvidence) -> bytes:
    return (json.dumps(asdict(artifact), sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_artifact(path: Path, artifact: DryRunControlEvidence) -> WrittenDryRunControl:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = artifact_bytes(artifact)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to replace dry-run control artifact {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o444)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return WrittenDryRunControl(
        kind=artifact.kind,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        revision=artifact.revision,
        completed_at_utc=artifact.completed_at_utc,
    )


def run_control_suite(
    revision: str,
    output_dir: Path,
    *,
    completed_at: datetime | None = None,
) -> tuple[WrittenDryRunControl, ...]:
    completed = _completed_at(completed_at)
    tested = _git_commit(REPO_ROOT, revision)
    head = _git_head(REPO_ROOT)
    if tested != head:
        raise ValueError(
            "revision does not match checked-out HEAD; refusing false attribution "
            f"(revision={tested}, HEAD={head})"
        )
    if not _git_is_clean(REPO_ROOT):
        raise ValueError("checked-out Perch worktree is dirty; refusing control evidence")
    exercises: tuple[Callable[[], DryRunControlEvidence], ...] = (
        lambda: run_failure_injection(revision, completed_at=completed),
        lambda: run_kill_switch(revision, completed_at=completed),
        lambda: run_delivery_isolation(revision, completed_at=completed),
        lambda: run_rollback_runbook(revision, completed_at=completed),
    )
    artifacts = tuple(exercise() for exercise in exercises)
    failed = [artifact.kind for artifact in artifacts if artifact.status != "passed"]
    if failed:
        raise RuntimeError(f"dry-run control exercise failed; no artifacts written: {failed}")
    if output_dir.exists():
        raise FileExistsError("refusing to replace existing dry-run control evidence set")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{output_dir.name}.", suffix=".tmp", dir=output_dir.parent
    ))
    try:
        staged = tuple(
            write_artifact(staging / f"{artifact.kind}.json", artifact)
            for artifact in artifacts
        )
        os.rename(staging, output_dir)
        _fsync_directory(output_dir.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return tuple(
        WrittenDryRunControl(
            kind=item.kind,
            path=str(output_dir / Path(item.path).name),
            sha256=item.sha256,
            revision=item.revision,
            completed_at_utc=item.completed_at_utc,
        )
        for item in staged
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        written = run_control_suite(args.revision, args.output_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps([asdict(item) for item in written], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
