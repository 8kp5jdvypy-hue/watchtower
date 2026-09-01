"""Offline aggregate gate for a locked customer-readiness dry-run campaign.

Passing means only that evidence is eligible for a separate customer-delivery
design and owner review.  This module has no customer, outbox, provider,
alert, broker, order, or activation path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tradebot.postmarket_customer_dry_run_campaign import (
    CAMPAIGN_FIELDS,
    CAMPAIGN_VERSION,
    _campaign_settled_at,
    _sessions,
    parse_campaign_policy,
)
from tradebot.postmarket_customer_dry_run_review import (
    REVIEW_ATTESTATION,
    RUBRIC_FIELDS,
    build_review_case,
)
from tradebot.postmarket_delivery_dry_run_audit import (
    AUDIT_VERSION,
    audit_dry_run_session,
    connect_readonly,
)
from tradebot.postmarket_discovery_gate_artifact import (
    verify_discovery_gate_artifact,
)


GATE_VERSION = 2
VERDICT_NOT_READY = "NOT_READY"
VERDICT_REVIEW = "ELIGIBLE_FOR_SEPARATE_CUSTOMER_DELIVERY_REVIEW"
REQUIRED_CONTROL_KINDS = {
    "customer_dry_run_failure_injection",
    "customer_dry_run_kill_switch",
    "customer_dry_run_delivery_isolation",
    "customer_dry_run_rollback_runbook",
}
@dataclass(frozen=True)
class GateCheck:
    code: str
    passed: bool
    observed: Any
    required: Any


@dataclass(frozen=True)
class CustomerDryRunGateMetrics:
    expected_sessions: int
    supplied_audits: int
    clean_sessions: int
    dirty_sessions: int
    minimum_coverage_pct: float | None
    maximum_scheduled_lag_seconds: float | None
    maximum_tick_latency_seconds: float | None
    unique_eligible_decisions: int
    reviewed_cases: int
    approved_cases: int
    rejected_cases: int
    review_approval_rate: float | None
    distinct_reviewed_symbols: int
    critical_review_findings: int
    review_evidence_failures: int
    passing_controls: int


@dataclass(frozen=True)
class CustomerDryRunGateReport:
    gate_version: int
    gate_code_version: str
    generated_at_utc: str
    campaign_id: str
    campaign_sha256: str
    coverage_start: str
    coverage_end: str
    verdict: str
    ready_for_customer_delivery_review: bool
    customer_delivery_enabled: bool
    evidence_package_sha256: str
    metrics: CustomerDryRunGateMetrics
    checks: tuple[GateCheck, ...]
    upstream_digests: tuple[dict[str, str], ...]
    audit_digests: tuple[dict[str, str], ...]
    control_digests: tuple[dict[str, str], ...]


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object, name: str) -> str:
    if not isinstance(value, str) or not 7 <= len(value) <= 40 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{name} must be a concrete git revision")
    return value


def _date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _load_json(path: Path, context: str) -> tuple[dict[str, object], str]:
    if path.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload, _sha256_bytes(raw)


def _check(code: str, observed: Any, required: Any, passed: bool) -> GateCheck:
    return GateCheck(code=code, passed=bool(passed), observed=observed, required=required)


def _parse_campaign(payload: dict[str, object], digest: str) -> dict[str, object]:
    if set(payload) != CAMPAIGN_FIELDS:
        raise ValueError("campaign fields are not exact")
    if payload["schema_version"] != CAMPAIGN_VERSION or payload["status"] != "locked":
        raise ValueError("campaign is not a locked supported contract")
    start = _date(payload["coverage_start"], "coverage_start")
    end = _date(payload["coverage_end"], "coverage_end")
    expected = tuple(session.isoformat() for session in _sessions(start, end))
    if tuple(payload["expected_sessions"]) != expected:
        raise ValueError("campaign expected session inventory is inconsistent")
    policy = parse_campaign_policy(payload["policy"])
    if len(expected) < policy.min_clean_sessions:
        raise ValueError("campaign cannot meet its clean-session floor")
    _sha(payload["delivery_policy_sha256"], "delivery_policy_sha256")
    _sha(payload["owner_authorization_sha256"], "owner_authorization_sha256")
    _sha(
        payload["upstream_discovery_evidence_set_sha256"],
        "upstream_discovery_evidence_set_sha256",
    )
    _sha(
        payload["upstream_discovery_evidence_gate_sha256"],
        "upstream_discovery_evidence_gate_sha256",
    )
    _revision(
        payload["upstream_discovery_gate_code_version"],
        "upstream_discovery_gate_code_version",
    )
    _aware(
        payload["upstream_discovery_gate_evaluated_at_utc"],
        "upstream_discovery_gate_evaluated_at_utc",
    )
    _sha(
        payload["upstream_calibration_artifact_sha256"],
        "upstream_calibration_artifact_sha256",
    )
    _sha(payload["calibration_model_sha256"], "calibration_model_sha256")
    if not isinstance(payload["calibration_version"], int) or isinstance(
        payload["calibration_version"], bool
    ) or payload["calibration_version"] < 1:
        raise ValueError("calibration_version must be a positive integer")
    calibration_evaluated = _aware(
        payload["calibration_evaluated_at_utc"], "calibration_evaluated_at_utc"
    )
    upstream_evaluated = _aware(
        payload["upstream_discovery_gate_evaluated_at_utc"],
        "upstream_discovery_gate_evaluated_at_utc",
    )
    if calibration_evaluated > upstream_evaluated:
        raise ValueError("calibration evidence postdates the upstream gate")
    controls = tuple(_sha(value, "control_evidence_sha256s") for value in payload["control_evidence_sha256s"])
    if len(controls) != 4 or len(set(controls)) != 4:
        raise ValueError("campaign must bind four unique controls")
    _aware(payload["locked_at_utc"], "locked_at_utc")
    _aware(payload["owner_authorization_expires_at_utc"], "authorization expiry")
    return {
        **payload,
        "coverage_start_date": start,
        "coverage_end_date": end,
        "expected_session_tuple": expected,
        "parsed_policy": policy,
        "campaign_sha256": digest,
    }


def _parse_control(path: Path) -> tuple[dict[str, object], str]:
    payload, digest = _load_json(path, f"control {path}")
    expected_fields = {
        "schema_version", "kind", "status", "revision", "completed_at_utc", "checks"
    }
    if set(payload) != expected_fields or payload["schema_version"] != 1:
        raise ValueError(f"control contract is not exact: {path}")
    if payload["kind"] not in REQUIRED_CONTROL_KINDS:
        raise ValueError(f"unknown control kind: {path}")
    _revision(payload["revision"], "control revision")
    _aware(payload["completed_at_utc"], "control completed_at_utc")
    checks = payload["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"control checks are missing: {path}")
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "passed", "evidence"}:
            raise ValueError(f"control check is not exact: {path}")
    return payload, digest


def _parse_audit(path: Path) -> tuple[dict[str, object], str]:
    payload, digest = _load_json(path, f"audit {path}")
    required = {
        "audit_version", "audit_code_version", "session", "database",
        "created_at_utc", "source_evidence_sha256", "operational_clean",
        "session_evidence_eligible", "metrics", "issues",
    }
    if set(payload) != required or payload["audit_version"] != AUDIT_VERSION:
        raise ValueError(f"audit contract is not exact: {path}")
    _date(payload["session"], "audit session")
    _aware(payload["created_at_utc"], "audit created_at_utc")
    _sha(payload["source_evidence_sha256"], "source_evidence_sha256")
    if not isinstance(payload["metrics"], dict) or not isinstance(payload["issues"], list):
        raise ValueError(f"audit evidence shape is invalid: {path}")
    return payload, digest


def _review_rows(conn: sqlite3.Connection, campaign_sha256: str) -> list[sqlite3.Row]:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "postmarket_customer_dry_run_reviews" not in tables:
        return []
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM postmarket_customer_dry_run_reviews WHERE campaign_sha256=? "
            "ORDER BY review_id",
            (campaign_sha256,),
        ).fetchall()
    finally:
        conn.row_factory = original


def evaluate_customer_dry_run_gate(
    *,
    campaign_path: Path | str,
    upstream_discovery_evidence_set_path: Path | str,
    upstream_discovery_evidence_gate_path: Path | str,
    audit_dir: Path | str,
    control_paths: tuple[Path | str, ...],
    db_path: Path | str,
    now: datetime,
    gate_code_version: str,
) -> CustomerDryRunGateReport:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    generated = now.astimezone(timezone.utc)
    revision = _revision(gate_code_version, "gate_code_version")
    campaign_payload, campaign_digest = _load_json(Path(campaign_path), "campaign")
    campaign = _parse_campaign(campaign_payload, campaign_digest)
    upstream = verify_discovery_gate_artifact(
        upstream_discovery_evidence_set_path,
        upstream_discovery_evidence_gate_path,
    )
    policy = campaign["parsed_policy"]
    expected_sessions = campaign["expected_session_tuple"]
    checks: list[GateCheck] = []

    upstream_exact = (
        upstream.evidence_set_sha256
        == campaign["upstream_discovery_evidence_set_sha256"]
        and upstream.gate_artifact_sha256
        == campaign["upstream_discovery_evidence_gate_sha256"]
        and upstream.gate_code_version
        == campaign["upstream_discovery_gate_code_version"]
        and upstream.evaluated_at_utc.isoformat()
        == campaign["upstream_discovery_gate_evaluated_at_utc"]
        and upstream.report.rank_version == campaign["rank_version"]
        and upstream.calibration_artifact_sha256
        == campaign["upstream_calibration_artifact_sha256"]
        and upstream.calibration_model_sha256
        == campaign["calibration_model_sha256"]
        and upstream.calibration_version == campaign["calibration_version"]
        and upstream.calibration_evaluated_at_utc.isoformat()
        == campaign["calibration_evaluated_at_utc"]
    )
    checks.append(_check(
        "UPSTREAM_DISCOVERY_EVIDENCE_EXACT",
        {
            "evidence_set_sha256": upstream.evidence_set_sha256,
            "evidence_gate_sha256": upstream.gate_artifact_sha256,
            "gate_code_version": upstream.gate_code_version,
            "calibration_artifact_sha256": upstream.calibration_artifact_sha256,
            "calibration_model_sha256": upstream.calibration_model_sha256,
            "calibration_version": upstream.calibration_version,
        },
        {
            "evidence_set_sha256": campaign[
                "upstream_discovery_evidence_set_sha256"
            ],
            "evidence_gate_sha256": campaign[
                "upstream_discovery_evidence_gate_sha256"
            ],
            "gate_code_version": campaign[
                "upstream_discovery_gate_code_version"
            ],
            "calibration_artifact_sha256": campaign[
                "upstream_calibration_artifact_sha256"
            ],
            "calibration_model_sha256": campaign["calibration_model_sha256"],
            "calibration_version": campaign["calibration_version"],
        },
        upstream_exact,
    ))

    campaign_complete = generated > _campaign_settled_at(campaign["coverage_end_date"])
    checks.append(_check("CAMPAIGN_COMPLETE", campaign_complete, True, campaign_complete))
    revision_allowed = revision in policy.allowed_runtime_router_revisions
    checks.append(_check("GATE_REVISION_ALLOWED", revision, policy.allowed_runtime_router_revisions, revision_allowed))
    auth_valid = _aware(
        campaign["owner_authorization_expires_at_utc"], "authorization expiry"
    ) > generated
    checks.append(_check("OWNER_AUTHORIZATION_CURRENT", auth_valid, True, auth_valid))

    controls: list[tuple[dict[str, object], str, Path]] = []
    for raw_path in control_paths:
        path = Path(raw_path)
        payload, digest = _parse_control(path)
        controls.append((payload, digest, path))
    control_digests = tuple(sorted(digest for _, digest, _ in controls))
    expected_control_digests = tuple(sorted(campaign["control_evidence_sha256s"]))
    kinds = {payload["kind"] for payload, _, _ in controls}
    passing_controls = sum(
        payload["status"] == "passed"
        and payload["revision"] == revision
        and all(item["passed"] is True for item in payload["checks"])
        for payload, _, _ in controls
    )
    checks.extend((
        _check("CONTROL_DIGESTS_EXACT", control_digests, expected_control_digests, control_digests == expected_control_digests),
        _check("CONTROL_KINDS_COMPLETE", sorted(kinds), sorted(REQUIRED_CONTROL_KINDS), kinds == REQUIRED_CONTROL_KINDS),
        _check("CONTROLS_PASS", passing_controls, 4, passing_controls == 4),
    ))

    audit_paths = {
        path.name.split("postmarket_customer_dry_run_audit_", 1)[1].split("_v", 1)[0]: path
        for path in Path(audit_dir).glob(
            f"postmarket_customer_dry_run_audit_*_v{AUDIT_VERSION}.json"
        )
    }
    supplied_sessions = tuple(sorted(set(audit_paths) & set(expected_sessions)))
    checks.append(_check(
        "AUDIT_SESSION_INVENTORY", supplied_sessions,
        expected_sessions,
        supplied_sessions == expected_sessions,
    ))

    conn = connect_readonly(db_path)
    try:
        audit_rows: list[dict[str, object]] = []
        audit_digests: list[dict[str, str]] = []
        reaudit_failures = 0
        for session_value in supplied_sessions:
            payload, digest = _parse_audit(audit_paths[session_value])
            if payload["session"] != session_value:
                raise ValueError(f"audit filename/session mismatch: {audit_paths[session_value]}")
            audit_rows.append(payload)
            audit_digests.append({"session": session_value, "sha256": digest})
            recomputed = audit_dry_run_session(
                conn,
                date.fromisoformat(session_value),
                database=str(db_path),
                audit_code_version=payload["audit_code_version"],
                created_at=generated,
            )
            if (
                recomputed.source_evidence_sha256 != payload["source_evidence_sha256"]
                or recomputed.operational_clean != payload["operational_clean"]
                or recomputed.session_evidence_eligible
                != payload["session_evidence_eligible"]
            ):
                reaudit_failures += 1
        checks.append(_check("AUDITS_RECOMPUTE_EXACTLY", reaudit_failures, 0, reaudit_failures == 0))

        clean_rows = [
            row for row in audit_rows
            if row["operational_clean"] is True
            and row["session_evidence_eligible"] is True
            and not row["issues"]
        ]
        coverage_values = [float(row["metrics"]["coverage_pct"]) for row in audit_rows]
        lag_values = [
            float(row["metrics"]["max_scheduled_lag_ms"]) / 1000
            for row in audit_rows if row["metrics"]["max_scheduled_lag_ms"] is not None
        ]
        latency_values = [
            float(row["metrics"]["max_latency_ms"]) / 1000
            for row in audit_rows if row["metrics"]["max_latency_ms"] is not None
        ]
        min_coverage = min(coverage_values) if coverage_values else None
        max_lag = max(lag_values) if lag_values else None
        max_latency = max(latency_values) if latency_values else None
        dirty_sessions = len(audit_rows) - len(clean_rows)
        zero_metric_fields = (
            "degraded_ticks", "failed_invariants", "conservation_failures",
            "link_failures", "identity_failures", "input_digest_failures",
            "decision_attribution_failures", "orphan_routes",
            "duplicate_eligible_identities", "actionability_failures",
            "calibration_link_failures", "calibration_attribution_failures",
        )
        zero_failures = sum(
            int(row["metrics"][field])
            for row in audit_rows for field in zero_metric_fields
        )
        provenance_ok = all(
            tuple(row["metrics"]["policy_sha256s"]) == (campaign["delivery_policy_sha256"],)
            and tuple(row["metrics"]["authorization_sha256s"])
            == (campaign["owner_authorization_sha256"],)
            and tuple(row["metrics"]["runtime_router_revisions"])
            == tuple(policy.allowed_runtime_router_revisions)
            and tuple(row["metrics"]["router_versions"]) == (campaign["router_version"],)
            and (
                int(row["metrics"]["eligible_candidates"]) == 0
                or tuple(row["metrics"]["calibration_model_sha256s"])
                == (campaign["calibration_model_sha256"],)
            )
            and row["audit_code_version"] in policy.allowed_audit_code_versions
            for row in audit_rows
        )
        checks.extend((
            _check("MIN_CLEAN_SESSIONS", len(clean_rows), policy.min_clean_sessions, len(clean_rows) >= policy.min_clean_sessions),
            _check("ZERO_DIRTY_SESSIONS", dirty_sessions, 0, dirty_sessions == 0),
            _check("SESSION_COVERAGE", min_coverage, policy.min_session_coverage_pct, min_coverage is not None and min_coverage >= policy.min_session_coverage_pct),
            _check("MAX_SCHEDULED_LAG", max_lag, policy.max_scheduled_lag_seconds, max_lag is not None and max_lag <= policy.max_scheduled_lag_seconds),
            _check("MAX_TICK_LATENCY", max_latency, policy.max_tick_latency_seconds, max_latency is not None and max_latency <= policy.max_tick_latency_seconds),
            _check("ZERO_LEDGER_FAILURES", zero_failures, 0, zero_failures == 0),
            _check("SESSION_PROVENANCE_EXACT", provenance_ok, True, provenance_ok and bool(audit_rows)),
        ))

        placeholders = ",".join("?" for _ in expected_sessions)
        base_route_rows = conn.execute(
            f"""
            SELECT route_id,session,symbol,decision_fingerprint_sha256
            FROM postmarket_delivery_dry_runs
            WHERE session IN ({placeholders})
              AND decision='ELIGIBLE_FOR_DRY_RUN'
              AND presentation='ACTIONABLE'
              AND policy_sha256=? AND authorization_sha256=?
              AND runtime_router_revision=?
            ORDER BY route_id
            """,
            (*expected_sessions, campaign["delivery_policy_sha256"],
             campaign["owner_authorization_sha256"], revision),
        ).fetchall()
        route_rows = conn.execute(
            f"""
            SELECT d.route_id,d.session,d.symbol,d.decision_fingerprint_sha256
            FROM postmarket_delivery_dry_runs d
            JOIN postmarket_delivery_dry_run_calibrations q
              ON q.route_id=d.route_id
            WHERE d.session IN ({placeholders})
              AND d.decision='ELIGIBLE_FOR_DRY_RUN'
              AND d.presentation='ACTIONABLE'
              AND d.policy_sha256=? AND d.authorization_sha256=?
              AND d.runtime_router_revision=?
              AND q.model_sha256=?
              AND q.calibration_version=?
            ORDER BY d.route_id
            """,
            (*expected_sessions, campaign["delivery_policy_sha256"],
             campaign["owner_authorization_sha256"], revision,
             campaign["calibration_model_sha256"], campaign["calibration_version"]),
        ).fetchall()
        calibration_bound = len(route_rows) == len(base_route_rows)
        checks.append(_check(
            "ELIGIBLE_ROUTES_CALIBRATION_EXACT",
            len(route_rows), len(base_route_rows), calibration_bound,
        ))
        eligible_route_ids = {int(row[0]) for row in route_rows}
        checks.append(_check(
            "MIN_ELIGIBLE_DECISIONS", len(eligible_route_ids),
            policy.min_eligible_decisions,
            len(eligible_route_ids) >= policy.min_eligible_decisions,
        ))

        reviews = _review_rows(conn, campaign_digest)
        review_failures = 0
        reviews_by_case: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in reviews:
            reviews_by_case[str(row["case_evidence_sha256"])].append(row)
            try:
                rubric = json.loads(row["rubric_json"])
                rebuilt = build_review_case(
                    conn,
                    campaign_sha256=campaign_digest,
                    route_id=int(row["route_id"]),
                    exported_at=_aware(row["reviewed_at_utc"], "reviewed_at_utc"),
                )
                derived_verdict = (
                    "APPROVE"
                    if not bool(row["critical_finding"])
                    and set(rubric) == set(RUBRIC_FIELDS)
                    and all(value == "PASS" for value in rubric.values())
                    else "REJECT"
                )
                payload = {
                    "review_version": int(row["review_version"]),
                    "campaign_sha256": row["campaign_sha256"],
                    "case_evidence_sha256": row["case_evidence_sha256"],
                    "route_id": int(row["route_id"]),
                    "reviewer_id": row["reviewer_id"],
                    "reviewer_role": row["reviewer_role"],
                    "reviewed_at_utc": row["reviewed_at_utc"],
                    "independent_of_implementation": True,
                    "blinded_to_future_outcomes": True,
                    "verdict": row["verdict"],
                    "rubric": rubric,
                    "critical_finding": bool(row["critical_finding"]),
                    "notes": row["notes"],
                    "attestation": row["attestation"],
                }
                payload_sha = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if (
                    int(row["route_id"]) not in eligible_route_ids
                    or rebuilt["case_evidence_sha256"] != row["case_evidence_sha256"]
                    or row["verdict"] != derived_verdict
                    or row["attestation"] != REVIEW_ATTESTATION
                    or not bool(row["independent_of_implementation"])
                    or not bool(row["blinded_to_future_outcomes"])
                    or payload_sha != row["review_payload_sha256"]
                ):
                    review_failures += 1
            except (ValueError, TypeError, json.JSONDecodeError, KeyError):
                review_failures += 1
        approved_cases = sum(
            all(row["verdict"] == "APPROVE" for row in rows)
            for rows in reviews_by_case.values()
        )
        rejected_cases = len(reviews_by_case) - approved_cases
        approval_rate = (
            approved_cases / len(reviews_by_case) if reviews_by_case else None
        )
        reviewed_symbols = {row["symbol"] for row in reviews}
        critical = sum(bool(row["critical_finding"]) for row in reviews)
        checks.extend((
            _check("REVIEW_EVIDENCE_VALID", review_failures, 0, review_failures == 0),
            _check("MIN_REVIEWED_CASES", len(reviews_by_case), policy.min_independently_reviewed_cases, len(reviews_by_case) >= policy.min_independently_reviewed_cases),
            _check("MIN_REVIEWED_SYMBOLS", len(reviewed_symbols), policy.min_distinct_reviewed_symbols, len(reviewed_symbols) >= policy.min_distinct_reviewed_symbols),
            _check("MIN_REVIEW_APPROVAL_RATE", approval_rate, policy.min_owner_review_approval_rate, approval_rate is not None and approval_rate >= policy.min_owner_review_approval_rate),
            _check("ZERO_CRITICAL_REVIEW_FINDINGS", critical, 0, critical == 0),
        ))

        metrics = CustomerDryRunGateMetrics(
            expected_sessions=len(expected_sessions), supplied_audits=len(audit_rows),
            clean_sessions=len(clean_rows), dirty_sessions=dirty_sessions,
            minimum_coverage_pct=min_coverage,
            maximum_scheduled_lag_seconds=max_lag,
            maximum_tick_latency_seconds=max_latency,
            unique_eligible_decisions=len(eligible_route_ids),
            reviewed_cases=len(reviews_by_case), approved_cases=approved_cases,
            rejected_cases=rejected_cases, review_approval_rate=approval_rate,
            distinct_reviewed_symbols=len(reviewed_symbols),
            critical_review_findings=critical,
            review_evidence_failures=review_failures,
            passing_controls=passing_controls,
        )
        package_payload = {
            "campaign": campaign_digest,
            "upstream": [
                upstream.evidence_set_sha256,
                upstream.gate_artifact_sha256,
                upstream.calibration_artifact_sha256,
                upstream.calibration_model_sha256,
            ],
            "audits": sorted(item["sha256"] for item in audit_digests),
            "controls": list(control_digests),
            "eligible_routes": sorted(row["decision_fingerprint_sha256"] for row in route_rows),
            "reviews": sorted(row["review_payload_sha256"] for row in reviews),
        }
        package_digest = hashlib.sha256(
            json.dumps(package_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    finally:
        conn.close()

    ready = all(check.passed for check in checks)
    return CustomerDryRunGateReport(
        gate_version=GATE_VERSION, gate_code_version=revision,
        generated_at_utc=generated.isoformat(), campaign_id=str(campaign["campaign_id"]),
        campaign_sha256=campaign_digest,
        coverage_start=str(campaign["coverage_start"]),
        coverage_end=str(campaign["coverage_end"]),
        verdict=VERDICT_REVIEW if ready else VERDICT_NOT_READY,
        ready_for_customer_delivery_review=ready,
        customer_delivery_enabled=False,
        evidence_package_sha256=package_digest,
        metrics=metrics, checks=tuple(checks),
        upstream_digests=(
            {"kind": "discovery_evidence_set", "sha256": upstream.evidence_set_sha256},
            {"kind": "discovery_evidence_gate", "sha256": upstream.gate_artifact_sha256},
            {"kind": "rank_calibration_artifact", "sha256": upstream.calibration_artifact_sha256},
            {"kind": "rank_calibration_model", "sha256": upstream.calibration_model_sha256},
        ),
        audit_digests=tuple(sorted(audit_digests, key=lambda item: item["session"])),
        control_digests=tuple(sorted(
            ({"kind": payload["kind"], "sha256": digest} for payload, digest, _ in controls),
            key=lambda item: item["kind"],
        )),
    )


def write_gate_report_atomic(path: Path | str, report: CustomerDryRunGateReport) -> str:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("gate report output cannot be a symlink")
    expected_ready = all(check.passed for check in report.checks)
    expected_verdict = VERDICT_REVIEW if expected_ready else VERDICT_NOT_READY
    if (
        report.ready_for_customer_delivery_review != expected_ready
        or report.verdict != expected_verdict
        or report.customer_delivery_enabled is not False
    ):
        raise ValueError("gate report verdict contradicts its evidence checks")
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
    return _sha256_bytes(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--upstream-discovery-evidence-set", required=True, type=Path)
    parser.add_argument("--upstream-discovery-evidence-gate", required=True, type=Path)
    parser.add_argument("--audit-dir", required=True, type=Path)
    parser.add_argument("--control", required=True, action="append", type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--gate-code-version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = evaluate_customer_dry_run_gate(
        campaign_path=args.campaign,
        upstream_discovery_evidence_set_path=args.upstream_discovery_evidence_set,
        upstream_discovery_evidence_gate_path=args.upstream_discovery_evidence_gate,
        audit_dir=args.audit_dir,
        control_paths=tuple(args.control), db_path=args.db,
        now=datetime.now(timezone.utc), gate_code_version=args.gate_code_version,
    )
    if args.output:
        write_gate_report_atomic(args.output, report)
    else:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.ready_for_customer_delivery_review else 1


if __name__ == "__main__":
    raise SystemExit(main())
