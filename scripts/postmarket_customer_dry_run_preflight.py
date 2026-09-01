#!/usr/bin/env python3
"""Read-only preflight for a locked customer-readiness dry-run campaign.

Passing does not edit configuration, start a service, enable the observer, or
contact a customer.  It proves only that the exact default-off campaign stack
is safe for a separate owner-controlled enablement step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.postmarket_signal_quality_preflight import (
    _backup_checks,
    _parse_env_file,
    _quick_check,
    _run_git,
)
from scripts.verify_backup import _parse_manifest, validate_artifact_archive
from tradebot.postmarket_customer_dry_run_campaign import (
    CAMPAIGN_FIELDS,
    CAMPAIGN_VERSION,
)
from tradebot.postmarket_discovery_gate_artifact import (
    verify_discovery_gate_artifact,
)


PREFLIGHT_VERSION = 1
DRY_RUN_SWITCH = "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED"
FALSE_VALUES = {"", "0", "false", "no", "off"}
ET = ZoneInfo("America/New_York")
FINAL_BAR_GRACE = timedelta(minutes=5)
AUDIT_SETTLE_GRACE = timedelta(seconds=90)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ACKNOWLEDGEMENT = (
    "I approve this exact evidence-bound policy for postmarket customer-alert "
    "readiness review; this does not enable or send alerts."
)
REQUIRED_CONTROL_KINDS = {
    "customer_dry_run_failure_injection",
    "customer_dry_run_kill_switch",
    "customer_dry_run_delivery_isolation",
    "customer_dry_run_rollback_runbook",
}
CAMPAIGN_POLICY_FIELDS = {
    "min_clean_sessions", "min_eligible_decisions",
    "min_independently_reviewed_cases", "min_distinct_reviewed_symbols",
    "min_owner_review_approval_rate", "min_session_coverage_pct",
    "max_scheduled_lag_seconds", "max_tick_latency_seconds",
    "allowed_audit_versions", "allowed_audit_code_versions",
    "allowed_runtime_router_revisions", "require_zero_dirty_sessions",
    "require_complete_session_inventory", "require_zero_degraded_ticks",
    "require_zero_failed_invariants", "require_zero_conservation_failures",
    "require_zero_link_failures", "require_zero_identity_failures",
    "require_zero_input_digest_failures",
    "require_zero_decision_attribution_failures", "require_zero_orphan_routes",
    "require_zero_duplicate_eligible_identities",
    "require_zero_actionability_failures",
    "require_zero_critical_review_findings", "require_independent_owner_review",
}
DELIVERY_POLICY_FIELDS = {
    "delivery_policy_version", "router_revision", "evidence_set_sha256",
    "evidence_gate_sha256", "rank_version", "minimum_evidence_score",
    "maximum_ordinal_rank", "minimum_evidence_coverage_pct",
    "maximum_data_age_seconds", "allowed_states",
    "allowed_evidence_revisions", "allowed_providers", "allowed_feeds",
}
AUTHORIZATION_FIELDS = {
    "schema_version", "release_id", "approved_by", "approved_at_utc",
    "expires_at_utc", "policy_sha256", "evidence_set_sha256",
    "evidence_gate_sha256", "router_revision", "acknowledgement",
    "dry_run_readiness_approved",
}
ALLOWED_STATES = {"CONFIRMED", "STRENGTHENING", "REQUALIFIED"}
REQUIRED_UPSTREAM_TABLES = {
    "postmarket_rank_runs",
    "postmarket_candidate_ranks",
    "postmarket_candidate_lifecycle",
    "postmarket_candidate_lifecycle_observations",
}
DRY_RUN_TABLES = {
    "postmarket_delivery_dry_runs",
    "postmarket_delivery_dry_run_ticks",
    "postmarket_delivery_dry_run_tick_decisions",
}


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class CustomerDryRunPreflightReport:
    preflight_version: int
    checked_at_utc: str
    expected_revision: str
    actual_revision: str | None
    campaign_id: str | None
    campaign_sha256: str | None
    safe_to_begin_customer_dry_run_campaign: bool
    customer_delivery_enabled: bool
    checks: tuple[PreflightCheck, ...]


def _check(name: str, passed: bool, evidence: str) -> PreflightCheck:
    return PreflightCheck(name=name, passed=bool(passed), evidence=evidence)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _sha256_json(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path, context: str) -> tuple[dict[str, object], str]:
    if path.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{context} must be a regular file")
    raw = resolved.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload, hashlib.sha256(raw).hexdigest()


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object, name: str) -> str:
    if not isinstance(value, str) or REVISION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a concrete git revision")
    return value


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{name} must be a non-empty string array")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} cannot contain duplicates")
    return list(value)


def _integer(value: object, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or (
        maximum is not None and parsed > maximum
    ):
        raise ValueError(f"{name} is outside its allowed range")
    return parsed


def _parse_delivery_policy(payload: dict[str, object]) -> dict[str, object]:
    if set(payload) != DELIVERY_POLICY_FIELDS or payload["delivery_policy_version"] != 1:
        raise ValueError("delivery policy contract is not exact")
    states = _string_list(payload["allowed_states"], "allowed_states")
    if not set(states) <= ALLOWED_STATES:
        raise ValueError("allowed_states contains a non-rankable state")
    revisions = _string_list(
        payload["allowed_evidence_revisions"], "allowed_evidence_revisions"
    )
    for revision in revisions:
        _revision(revision, "allowed_evidence_revisions")
    canonical = {
        "delivery_policy_version": 1,
        "router_revision": _revision(payload["router_revision"], "router_revision"),
        "evidence_set_sha256": _digest(
            payload["evidence_set_sha256"], "evidence_set_sha256"
        ),
        "evidence_gate_sha256": _digest(
            payload["evidence_gate_sha256"], "evidence_gate_sha256"
        ),
        "rank_version": _integer(payload["rank_version"], "rank_version"),
        "minimum_evidence_score": _number(
            payload["minimum_evidence_score"], "minimum_evidence_score",
            minimum=0, maximum=100,
        ),
        "maximum_ordinal_rank": _integer(
            payload["maximum_ordinal_rank"], "maximum_ordinal_rank"
        ),
        "minimum_evidence_coverage_pct": _number(
            payload["minimum_evidence_coverage_pct"],
            "minimum_evidence_coverage_pct", minimum=0, maximum=100,
        ),
        "maximum_data_age_seconds": _number(
            payload["maximum_data_age_seconds"], "maximum_data_age_seconds",
            minimum=0.000000001,
        ),
        "allowed_states": states,
        "allowed_evidence_revisions": revisions,
        "allowed_providers": _string_list(
            payload["allowed_providers"], "allowed_providers"
        ),
        "allowed_feeds": _string_list(payload["allowed_feeds"], "allowed_feeds"),
    }
    return {**canonical, "sha256": _sha256_json(canonical)}


def _parse_authorization(payload: dict[str, object]) -> dict[str, object]:
    if set(payload) != AUTHORIZATION_FIELDS or payload["schema_version"] != 1:
        raise ValueError("owner authorization contract is not exact")
    if payload["dry_run_readiness_approved"] is not True:
        raise ValueError("owner authorization is not explicitly approved")
    for name in (
        "release_id", "approved_by", "policy_sha256", "evidence_set_sha256",
        "evidence_gate_sha256", "router_revision", "acknowledgement",
    ):
        if not isinstance(payload[name], str):
            raise ValueError(f"authorization {name} must be a string")
    if not payload["release_id"].strip() or not payload["approved_by"].strip():
        raise ValueError("owner authorization identity is missing")
    if payload["acknowledgement"] != ACKNOWLEDGEMENT:
        raise ValueError("owner acknowledgement is not exact")
    approved = _aware(payload["approved_at_utc"], "approved_at_utc")
    expires = _aware(payload["expires_at_utc"], "expires_at_utc")
    if expires <= approved:
        raise ValueError("owner authorization window is invalid")
    canonical = {
        "schema_version": 1,
        "release_id": payload["release_id"],
        "approved_by": payload["approved_by"],
        "approved_at_utc": approved.isoformat(),
        "expires_at_utc": expires.isoformat(),
        "policy_sha256": _digest(payload["policy_sha256"], "policy_sha256"),
        "evidence_set_sha256": _digest(
            payload["evidence_set_sha256"], "evidence_set_sha256"
        ),
        "evidence_gate_sha256": _digest(
            payload["evidence_gate_sha256"], "evidence_gate_sha256"
        ),
        "router_revision": _revision(payload["router_revision"], "router_revision"),
        "acknowledgement": payload["acknowledgement"],
        "dry_run_readiness_approved": True,
    }
    return {
        **canonical,
        "approved_at": approved,
        "expires_at": expires,
        "sha256": _sha256_json(canonical),
    }


def _parse_campaign(payload: dict[str, object], digest: str) -> dict[str, object]:
    if set(payload) != CAMPAIGN_FIELDS:
        raise ValueError("campaign fields are not exact")
    if payload["schema_version"] != CAMPAIGN_VERSION or payload["status"] != "locked":
        raise ValueError("campaign is not a locked supported contract")
    if not isinstance(payload["campaign_id"], str) or not payload["campaign_id"].strip():
        raise ValueError("campaign_id must be non-empty")
    start = date.fromisoformat(str(payload["coverage_start"]))
    end = date.fromisoformat(str(payload["coverage_end"]))
    if end < start:
        raise ValueError("campaign coverage is reversed")
    sessions = _string_list(payload["expected_sessions"], "expected_sessions")
    parsed_sessions = tuple(date.fromisoformat(item) for item in sessions)
    if (
        tuple(sorted(parsed_sessions)) != parsed_sessions
        or parsed_sessions[0] < start
        or parsed_sessions[-1] > end
    ):
        raise ValueError("campaign session inventory is inconsistent")
    policy = payload["policy"]
    if not isinstance(policy, dict) or set(policy) != CAMPAIGN_POLICY_FIELDS:
        raise ValueError("campaign policy contract is not exact")
    required_true = {
        name for name in CAMPAIGN_POLICY_FIELDS if name.startswith("require_")
    }
    if any(policy[name] is not True for name in required_true):
        raise ValueError("every fail-closed campaign requirement must be true")
    min_clean = _integer(
        policy["min_clean_sessions"], "min_clean_sessions", minimum=10
    )
    if len(sessions) < min_clean:
        raise ValueError("campaign cannot meet its clean-session floor")
    _integer(policy["min_eligible_decisions"], "min_eligible_decisions", minimum=20)
    reviewed = _integer(
        policy["min_independently_reviewed_cases"],
        "min_independently_reviewed_cases", minimum=20,
    )
    distinct = _integer(
        policy["min_distinct_reviewed_symbols"],
        "min_distinct_reviewed_symbols", minimum=10,
    )
    if distinct > reviewed:
        raise ValueError("distinct reviewed symbols exceeds reviewed cases")
    _number(
        policy["min_owner_review_approval_rate"], "approval rate",
        minimum=0.9, maximum=1,
    )
    _number(
        policy["min_session_coverage_pct"], "coverage pct",
        minimum=100, maximum=100,
    )
    _number(
        policy["max_scheduled_lag_seconds"], "scheduled lag",
        minimum=0.000000001, maximum=30,
    )
    _number(
        policy["max_tick_latency_seconds"], "tick latency",
        minimum=0.000000001, maximum=10,
    )
    if policy["allowed_audit_versions"] != [1]:
        raise ValueError("campaign must pin audit version 1")
    audit_revisions = _string_list(
        policy["allowed_audit_code_versions"], "allowed_audit_code_versions"
    )
    router_revisions = _string_list(
        policy["allowed_runtime_router_revisions"],
        "allowed_runtime_router_revisions",
    )
    for revision in (*audit_revisions, *router_revisions):
        _revision(revision, "campaign revision")
    if audit_revisions != router_revisions or len(router_revisions) != 1:
        raise ValueError("campaign must pin one exact audit/router revision")
    raw_controls = payload["control_evidence_sha256s"]
    if not isinstance(raw_controls, list):
        raise ValueError("control_evidence_sha256s must be an array")
    controls = tuple(
        _digest(item, "control_evidence_sha256s") for item in raw_controls
    )
    if len(controls) != 4 or len(set(controls)) != 4:
        raise ValueError("campaign must bind four unique controls")
    _aware(payload["locked_at_utc"], "locked_at_utc")
    _digest(
        payload["upstream_discovery_evidence_set_sha256"],
        "upstream_discovery_evidence_set_sha256",
    )
    _digest(
        payload["upstream_discovery_evidence_gate_sha256"],
        "upstream_discovery_evidence_gate_sha256",
    )
    _revision(
        payload["upstream_discovery_gate_code_version"],
        "upstream_discovery_gate_code_version",
    )
    upstream_evaluated = _aware(
        payload["upstream_discovery_gate_evaluated_at_utc"],
        "upstream_discovery_gate_evaluated_at_utc",
    )
    expires = _aware(
        payload["owner_authorization_expires_at_utc"],
        "owner_authorization_expires_at_utc",
    )
    return {
        **payload,
        "coverage_start_date": start,
        "coverage_end_date": end,
        "expected_session_tuple": tuple(sessions),
        "allowed_runtime_router_revisions": tuple(router_revisions),
        "control_evidence_sha256s": controls,
        "authorization_expires_at": expires,
        "upstream_discovery_gate_evaluated_at": upstream_evaluated,
        "campaign_sha256": digest,
    }


def _parse_control(path: Path) -> tuple[dict[str, object], str]:
    payload, digest = _load_json(path, f"control {path}")
    if set(payload) != {
        "schema_version", "kind", "status", "revision", "completed_at_utc", "checks"
    } or payload["schema_version"] != 1:
        raise ValueError("control contract is not exact")
    if payload["kind"] not in REQUIRED_CONTROL_KINDS:
        raise ValueError("control kind is unknown")
    _revision(payload["revision"], "control revision")
    _aware(payload["completed_at_utc"], "control completion")
    items = payload["checks"]
    if not isinstance(items, list) or not items:
        raise ValueError("control checks are missing")
    if any(
        not isinstance(item, dict)
        or set(item) != {"name", "passed", "evidence"}
        or not isinstance(item["name"], str)
        or not item["name"].strip()
        or not isinstance(item["passed"], bool)
        or not isinstance(item["evidence"], str)
        or not item["evidence"].strip()
        for item in items
    ):
        raise ValueError("control check is not exact")
    if len({item["name"] for item in items}) != len(items):
        raise ValueError("control check names must be unique")
    return payload, digest


def _campaign_settled_at(session: date) -> datetime:
    end = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return end + FINAL_BAR_GRACE + AUDIT_SETTLE_GRACE


def _database_tables(path: Path) -> set[str]:
    resolved = path.resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def evaluate_customer_dry_run_preflight(
    *,
    repo_root: Path,
    expected_revision: str,
    env_file: Path,
    compose_file: Path,
    campaign_path: Path,
    upstream_discovery_evidence_set_path: Path,
    upstream_discovery_evidence_gate_path: Path,
    delivery_policy_path: Path,
    owner_authorization_path: Path,
    control_paths: tuple[Path, ...],
    database_path: Path,
    backup_manifest: Path,
    now: datetime,
    max_backup_age_seconds: int = 7_200,
    min_free_bytes: int = 1_073_741_824,
) -> CustomerDryRunPreflightReport:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    checked = now.astimezone(timezone.utc)
    if max_backup_age_seconds <= 0 or min_free_bytes < 0:
        raise ValueError("backup age must be positive and free-space floor non-negative")
    checks: list[PreflightCheck] = []
    actual_revision: str | None = None
    campaign: dict[str, object] | None = None
    campaign_digest: str | None = None

    try:
        expected = _run_git(repo_root, "rev-parse", "--verify", expected_revision)
        actual_revision = _run_git(repo_root, "rev-parse", "HEAD")
        origin_main = _run_git(repo_root, "rev-parse", "origin/main")
        clean = not _run_git(repo_root, "status", "--porcelain")
        checks.extend((
            _check("exact_expected_revision", actual_revision == expected,
                   f"actual={actual_revision} expected={expected}"),
            _check("revision_matches_origin_main", actual_revision == origin_main,
                   f"actual={actual_revision} origin_main={origin_main}"),
            _check("clean_worktree", clean, f"clean={clean}"),
        ))
    except (OSError, subprocess.CalledProcessError) as exc:
        checks.append(_check("git_identity", False, _safe_error(exc)))

    try:
        env = _parse_env_file(env_file)
        raw_switch = env.get(DRY_RUN_SWITCH, "")
        default_off = raw_switch.strip().lower() in FALSE_VALUES
        checks.append(_check(
            "customer_dry_run_switch_off",
            default_off,
            "off_or_unset" if default_off else "enabled_or_invalid",
        ))
    except (OSError, UnicodeError, ValueError) as exc:
        checks.append(_check("environment_contract", False, _safe_error(exc)))

    try:
        compose = compose_file.read_text(encoding="utf-8")
        service = compose.split("  postmarket-customer-dry-run:", 1)[1].split(
            "\n  api:", 1
        )[0]
        compose_safe = (
            "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED:-0" in service
            and "tradebot.postmarket_delivery_dry_run_shadow" in service
            and "tradebot.postmarket_delivery_dry_run_health" in service
            and "depends_on" not in service
        )
        checks.append(_check(
            "compose_service_isolated_default_off", compose_safe,
            f"module_bound={compose_safe}",
        ))
    except (OSError, UnicodeError, IndexError) as exc:
        checks.append(_check("compose_contract", False, _safe_error(exc)))

    try:
        payload, campaign_digest = _load_json(campaign_path, "campaign")
        campaign = _parse_campaign(payload, campaign_digest)
        upstream = verify_discovery_gate_artifact(
            upstream_discovery_evidence_set_path,
            upstream_discovery_evidence_gate_path,
        )
        policy_payload, _ = _load_json(delivery_policy_path, "delivery policy")
        authorization_payload, _ = _load_json(
            owner_authorization_path, "owner authorization"
        )
        policy = _parse_delivery_policy(policy_payload)
        authorization = _parse_authorization(authorization_payload)
        revision = policy["router_revision"]
        revision_match = (
            actual_revision is not None
            and actual_revision.startswith(revision)
            and campaign["allowed_runtime_router_revisions"] == (revision,)
        )
        contract_match = (
            policy["sha256"] == campaign["delivery_policy_sha256"]
            and authorization["sha256"] == campaign["owner_authorization_sha256"]
            and authorization["release_id"] == campaign["release_id"]
            and authorization["router_revision"] == revision
            and authorization["policy_sha256"] == policy["sha256"]
            and authorization["evidence_set_sha256"]
            == policy["evidence_set_sha256"]
            and authorization["evidence_gate_sha256"]
            == policy["evidence_gate_sha256"]
        )
        upstream_exact = (
            upstream.evidence_set_sha256
            == campaign["upstream_discovery_evidence_set_sha256"]
            == policy["evidence_set_sha256"]
            and upstream.gate_artifact_sha256
            == campaign["upstream_discovery_evidence_gate_sha256"]
            == policy["evidence_gate_sha256"]
            and upstream.gate_code_version
            == campaign["upstream_discovery_gate_code_version"]
            and upstream.gate_code_version in policy["allowed_evidence_revisions"]
            and upstream.report.rank_version
            == campaign["rank_version"]
            == policy["rank_version"]
            and upstream.evaluated_at_utc
            == campaign["upstream_discovery_gate_evaluated_at"]
            and upstream.evaluated_at_utc <= authorization["approved_at"]
        )
        first_session = date.fromisoformat(campaign["expected_session_tuple"][0])
        first_open = datetime.combine(
            first_session, time(9, 30), tzinfo=ET
        ).astimezone(timezone.utc)
        timing_safe = checked < first_open
        authorization_safe = authorization["expires_at"] > _campaign_settled_at(
            campaign["coverage_end_date"]
        ) and authorization["approved_at"] <= checked
        checks.extend((
            _check("campaign_locked_and_exact", True,
                   f"campaign_id={campaign['campaign_id']} sha256={campaign_digest}"),
            _check("contracts_match_campaign", contract_match,
                   f"policy_match={policy['sha256'] == campaign['delivery_policy_sha256']} authorization_match={authorization['sha256'] == campaign['owner_authorization_sha256']}"),
            _check(
                "upstream_discovery_evidence_exact",
                upstream_exact,
                "exact evidence set and reproducible passing gate artifact bound to policy, authorization, and campaign",
            ),
            _check("campaign_revision_exact", revision_match,
                   f"campaign_revision={revision} actual={actual_revision}"),
            _check("campaign_not_started", timing_safe,
                   f"first_open_utc={first_open.isoformat()}"),
            _check("authorization_covers_campaign", authorization_safe,
                   f"authorization_expires_utc={authorization['expires_at'].isoformat()}"),
        ))
    except (OSError, UnicodeError, ValueError, KeyError, IndexError) as exc:
        checks.append(_check("campaign_contracts", False, _safe_error(exc)))

    try:
        controls = [_parse_control(path) for path in control_paths]
        digests = tuple(sorted(digest for _, digest in controls))
        kinds = {payload["kind"] for payload, _ in controls}
        expected_digests = (
            tuple(sorted(campaign["control_evidence_sha256s"]))
            if campaign is not None else ()
        )
        control_revision = (
            campaign["allowed_runtime_router_revisions"][0]
            if campaign is not None else None
        )
        passing = sum(
            payload["status"] == "passed"
            and payload["revision"] == control_revision
            and datetime.fromisoformat(payload["completed_at_utc"]).astimezone(
                timezone.utc
            ) <= checked
            and all(item["passed"] is True for item in payload["checks"])
            for payload, _ in controls
        )
        control_safe = (
            kinds == REQUIRED_CONTROL_KINDS
            and digests == expected_digests
            and passing == 4
        )
        checks.append(_check(
            "exact_passing_control_set", control_safe,
            f"kinds={sorted(kinds)!r} passing={passing} digests_match={digests == expected_digests}",
        ))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        checks.append(_check("control_evidence", False, _safe_error(exc)))

    quick_ok, quick_evidence = _quick_check(database_path)
    checks.append(_check("postmarket_database_quick_check", quick_ok, quick_evidence))
    try:
        tables = _database_tables(database_path)
        upstream_ok = REQUIRED_UPSTREAM_TABLES <= tables
        observed_dry = DRY_RUN_TABLES & tables
        dry_schema_ok = not observed_dry or observed_dry == DRY_RUN_TABLES
        checks.extend((
            _check("upstream_evidence_schema_complete", upstream_ok,
                   f"missing={sorted(REQUIRED_UPSTREAM_TABLES - tables)!r}"),
            _check("dry_run_schema_absent_or_complete", dry_schema_ok,
                   f"observed={sorted(observed_dry)!r}"),
        ))
    except (OSError, sqlite3.DatabaseError) as exc:
        checks.append(_check("database_schema", False, _safe_error(exc)))

    for item in _backup_checks(
        backup_manifest, now=checked, max_age_seconds=max_backup_age_seconds
    ):
        checks.append(_check(item.name, item.passed, item.evidence))
    try:
        _, manifest_rows = _parse_manifest(backup_manifest)
        artifact_name = next(
            name for name, _, kind in manifest_rows if kind == "postmarket_artifacts"
        )
        archive_path = backup_manifest.parent / artifact_name
        members = validate_artifact_archive(archive_path)
        expected_contracts = {
            "postmarket_customer_delivery_policy.json": delivery_policy_path,
            "postmarket_customer_delivery_authorization.json": owner_authorization_path,
            "postmarket_customer_dry_run_campaign.json": campaign_path,
        }
        upstream_expected = {
            hashlib.sha256(
                upstream_discovery_evidence_set_path.read_bytes()
            ).hexdigest(),
            hashlib.sha256(
                upstream_discovery_evidence_gate_path.read_bytes()
            ).hexdigest(),
        }
        archived_digests: set[str] = set()
        contract_matches = True
        with tarfile.open(archive_path, "r:gz") as archive:
            for member_name in members:
                source = archive.extractfile(member_name)
                if source is None:
                    raise tarfile.ExtractError(
                        f"cannot read archived artifact {member_name}"
                    )
                with source:
                    raw = source.read()
                archived_digests.add(hashlib.sha256(raw).hexdigest())
                expected_path = expected_contracts.get(member_name)
                if expected_path is not None and raw != expected_path.read_bytes():
                    contract_matches = False
        contract_inventory = set(expected_contracts) <= set(members)
        control_inventory = all(
            hashlib.sha256(path.read_bytes()).hexdigest() in archived_digests
            for path in control_paths
        )
        upstream_inventory = upstream_expected <= archived_digests
        backup_bound = (
            contract_inventory
            and contract_matches
            and control_inventory
            and upstream_inventory
        )
        checks.append(_check(
            "backup_binds_campaign_contracts_controls_and_upstream",
            backup_bound,
            f"contracts_present={contract_inventory} contracts_match={contract_matches} controls_present={control_inventory} upstream_present={upstream_inventory}",
        ))
    except (
        OSError, ValueError, StopIteration, tarfile.TarError, tarfile.ExtractError
    ) as exc:
        checks.append(_check("campaign_backup_binding", False, _safe_error(exc)))
    try:
        free_bytes = shutil.disk_usage(database_path.parent).free
        checks.append(_check(
            "minimum_free_disk", free_bytes >= min_free_bytes,
            f"free_bytes={free_bytes} required={min_free_bytes}",
        ))
    except OSError as exc:
        checks.append(_check("disk_capacity", False, _safe_error(exc)))

    safe = bool(checks) and all(item.passed for item in checks)
    return CustomerDryRunPreflightReport(
        preflight_version=PREFLIGHT_VERSION,
        checked_at_utc=checked.isoformat(),
        expected_revision=expected_revision,
        actual_revision=actual_revision,
        campaign_id=str(campaign["campaign_id"]) if campaign else None,
        campaign_sha256=campaign_digest,
        safe_to_begin_customer_dry_run_campaign=safe,
        customer_delivery_enabled=False,
        checks=tuple(checks),
    )


def write_preflight_atomic(path: Path | str, report: CustomerDryRunPreflightReport) -> str:
    if report.safe_to_begin_customer_dry_run_campaign != all(
        item.passed for item in report.checks
    ) or report.customer_delivery_enabled is not False:
        raise ValueError("preflight verdict contradicts checks")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(asdict(report), sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--compose-file", required=True, type=Path)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--upstream-discovery-evidence-set", required=True, type=Path)
    parser.add_argument("--upstream-discovery-evidence-gate", required=True, type=Path)
    parser.add_argument("--delivery-policy", required=True, type=Path)
    parser.add_argument("--owner-authorization", required=True, type=Path)
    parser.add_argument("--control", required=True, action="append", type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--backup-manifest", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = evaluate_customer_dry_run_preflight(
        repo_root=args.repo_root, expected_revision=args.expected_revision,
        env_file=args.env_file, compose_file=args.compose_file,
        campaign_path=args.campaign, delivery_policy_path=args.delivery_policy,
        upstream_discovery_evidence_set_path=args.upstream_discovery_evidence_set,
        upstream_discovery_evidence_gate_path=args.upstream_discovery_evidence_gate,
        owner_authorization_path=args.owner_authorization,
        control_paths=tuple(args.control), database_path=args.database,
        backup_manifest=args.backup_manifest, now=datetime.now(timezone.utc),
    )
    if args.output:
        write_preflight_atomic(args.output, report)
    else:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.safe_to_begin_customer_dry_run_campaign else 1


if __name__ == "__main__":
    raise SystemExit(main())
