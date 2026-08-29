"""Offline aggregate acceptance gate for postmarket shadow evidence.

The gate reads a locked manifest plus immutable, digest-pinned session and
control artifacts. It has no network, database, alert, Telegram, broker, or
write path. Passing means eligible for explicit owner review, never automatic
customer-alert activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as ecals


EVIDENCE_SCHEMA_VERSION = 2
CAMPAIGN_SCHEMA_VERSION = 1
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_CONTROL_KINDS = {"failure_injection", "kill_switch", "rollback_runbook"}
VERDICT_NOT_READY = "NOT_READY"
VERDICT_OWNER_REVIEW = "ELIGIBLE_FOR_OWNER_REVIEW"
ROOT_FIELDS = {
    "schema_version",
    "status",
    "evidence_set_version",
    "created_at_utc",
    "coverage_start",
    "coverage_end",
    "campaign_artifact",
    "policy",
    "session_reports",
    "control_artifacts",
}
POLICY_FIELDS = {
    "min_clean_sessions",
    "min_definitive_labels",
    "min_positive_labels",
    "min_recall",
    "min_precision",
    "max_detection_latency_seconds",
    "allowed_data_feeds",
    "allowed_market_data_providers",
    "allowed_audit_versions",
    "allowed_observer_versions",
    "allowed_audit_code_versions",
    "allowed_observer_code_versions",
    "require_zero_dirty_sessions",
    "require_zero_direction_mismatches",
    "require_complete_session_inventory",
}
REPORT_ARTIFACT_FIELDS = {"session", "path", "sha256"}
CONTROL_ARTIFACT_FIELDS = {"kind", "path", "sha256", "revision", "completed_at_utc"}
CAMPAIGN_ARTIFACT_FIELDS = {"path", "sha256"}
CAMPAIGN_FIELDS = {
    "schema_version", "status", "campaign_id", "locked_at_utc",
    "coverage_start", "coverage_end", "policy",
}
CONTROL_EVIDENCE_FIELDS = {
    "schema_version",
    "kind",
    "status",
    "revision",
    "completed_at_utc",
    "checks",
}
CONTROL_CHECK_FIELDS = {"name", "passed", "evidence"}


@dataclass(frozen=True)
class GatePolicy:
    min_clean_sessions: int
    min_definitive_labels: int
    min_positive_labels: int
    min_recall: float
    min_precision: float
    max_detection_latency_seconds: float
    allowed_data_feeds: tuple[str, ...]
    allowed_market_data_providers: tuple[str, ...]
    allowed_audit_versions: tuple[int, ...]
    allowed_observer_versions: tuple[int, ...]
    allowed_audit_code_versions: tuple[str, ...]
    allowed_observer_code_versions: tuple[str, ...]
    require_zero_dirty_sessions: bool
    require_zero_direction_mismatches: bool
    require_complete_session_inventory: bool


@dataclass(frozen=True)
class SessionArtifact:
    session: date
    path: Path
    sha256: str


@dataclass(frozen=True)
class ControlArtifact:
    kind: str
    path: Path
    sha256: str
    revision: str
    completed_at_utc: datetime


@dataclass(frozen=True)
class CampaignArtifact:
    campaign_id: str
    locked_at_utc: datetime
    path: Path
    sha256: str


@dataclass(frozen=True)
class EvidenceManifest:
    evidence_set_version: str
    created_at_utc: datetime
    coverage_start: date
    coverage_end: date
    campaign_artifact: CampaignArtifact
    policy: GatePolicy
    session_reports: tuple[SessionArtifact, ...]
    control_artifacts: tuple[ControlArtifact, ...]


@dataclass(frozen=True)
class AggregateMetrics:
    expected_sessions: int
    supplied_sessions: int
    clean_sessions: int
    dirty_sessions: int
    empirically_scored_sessions: int
    session_evidence_eligible: int
    definitive_labels: int
    ambiguous_labels: int
    positive_labels: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    direction_mismatches: int
    mean_detection_latency_seconds: float | None
    max_detection_latency_seconds: float | None
    candidate_observations: int
    unique_candidates: int
    audit_code_versions: tuple[str, ...]
    audit_versions: tuple[int, ...]
    observer_versions: tuple[int, ...]
    observer_code_versions: tuple[str, ...]
    data_feeds: tuple[str, ...]
    market_data_providers: tuple[str, ...]


@dataclass(frozen=True)
class GateCheck:
    code: str
    passed: bool
    observed: Any
    required: Any


@dataclass(frozen=True)
class EvidenceGateReport:
    schema_version: int
    evidence_set_version: str
    verdict: str
    coverage_start: str
    coverage_end: str
    campaign_id: str
    campaign_locked_at_utc: str
    campaign_sha256: str
    policy: GatePolicy
    metrics: AggregateMetrics
    checks: tuple[GateCheck, ...]
    session_digests: tuple[dict[str, str], ...]
    control_digests: tuple[dict[str, str], ...]


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _exact_fields(mapping: dict[str, Any], expected: set[str], context: str) -> None:
    missing = expected - mapping.keys()
    extra = mapping.keys() - expected
    if missing or extra:
        raise ValueError(
            f"{context} fields are invalid; missing={sorted(missing)} extra={sorted(extra)}"
        )


def _nonempty_string(raw: Any, context: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return raw.strip()


def _finite_number(raw: Any, context: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(f"{context} must be numeric")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{context} must be finite")
    return value


def _aware_datetime(raw: Any, context: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{context} must be an ISO-8601 string")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO-8601 string") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso_date(raw: Any, context: str) -> date:
    if not isinstance(raw, str):
        raise ValueError(f"{context} must be an ISO date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO date") from exc


def _positive_int(raw: Any, context: str, *, minimum: int = 1) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < minimum:
        raise ValueError(f"{context} must be an integer >= {minimum}")
    return raw


def _strict_bool(raw: Any, context: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{context} must be boolean")
    return raw


def _nonnegative_int(raw: Any, context: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return raw


def _optional_ratio(raw: Any, context: str) -> float | None:
    if raw is None:
        return None
    value = _finite_number(raw, context)
    if not 0 <= value <= 1:
        raise ValueError(f"{context} must be between 0 and 1")
    return value


def _string_list(raw: Any, context: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{context} must be a non-empty list")
    values = tuple(_nonempty_string(value, context) for value in raw)
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must not contain duplicates")
    return values


def _positive_int_list(raw: Any, context: str) -> tuple[int, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{context} must be a non-empty list")
    values = tuple(_positive_int(value, context) for value in raw)
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must not contain duplicates")
    return values


def _relative_artifact_path(root: Path, raw: Any, context: str) -> Path:
    value = Path(_nonempty_string(raw, context))
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"{context} must stay inside the evidence-set directory")
    resolved_root = root.resolve()
    resolved = (root / value).resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"{context} must stay inside the evidence-set directory")
    return resolved


def _sha256(raw: Any, context: str) -> str:
    digest = _nonempty_string(raw, context).lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError(f"{context} must be a 64-character hexadecimal digest")
    return digest


def _parse_policy(raw: Any) -> GatePolicy:
    if not isinstance(raw, dict):
        raise ValueError("policy must be an object")
    _exact_fields(raw, POLICY_FIELDS, "policy")
    min_recall = _finite_number(_required(raw, "min_recall", "policy"), "policy.min_recall")
    min_precision = _finite_number(
        _required(raw, "min_precision", "policy"), "policy.min_precision"
    )
    max_latency = _finite_number(
        _required(raw, "max_detection_latency_seconds", "policy"),
        "policy.max_detection_latency_seconds",
    )
    if not 0.95 <= min_recall <= 1:
        raise ValueError("policy.min_recall must be between 0.95 and 1")
    if not 0 < min_precision <= 1:
        raise ValueError("policy.min_precision must be between 0 and 1")
    if not 0 < max_latency <= 330:
        raise ValueError(
            "policy.max_detection_latency_seconds must be within one 5-minute bar "
            "plus 30 seconds processing"
        )
    zero_dirty = _strict_bool(
        _required(raw, "require_zero_dirty_sessions", "policy"),
        "policy.require_zero_dirty_sessions",
    )
    zero_direction = _strict_bool(
        _required(raw, "require_zero_direction_mismatches", "policy"),
        "policy.require_zero_direction_mismatches",
    )
    complete_inventory = _strict_bool(
        _required(raw, "require_complete_session_inventory", "policy"),
        "policy.require_complete_session_inventory",
    )
    if not (zero_dirty and zero_direction and complete_inventory):
        raise ValueError("required fail-closed policy booleans must all be true")
    return GatePolicy(
        min_clean_sessions=_positive_int(
            _required(raw, "min_clean_sessions", "policy"),
            "policy.min_clean_sessions",
            minimum=10,
        ),
        min_definitive_labels=_positive_int(
            _required(raw, "min_definitive_labels", "policy"),
            "policy.min_definitive_labels",
        ),
        min_positive_labels=_positive_int(
            _required(raw, "min_positive_labels", "policy"),
            "policy.min_positive_labels",
        ),
        min_recall=min_recall,
        min_precision=min_precision,
        max_detection_latency_seconds=max_latency,
        allowed_data_feeds=_string_list(
            _required(raw, "allowed_data_feeds", "policy"),
            "policy.allowed_data_feeds",
        ),
        allowed_market_data_providers=_string_list(
            _required(raw, "allowed_market_data_providers", "policy"),
            "policy.allowed_market_data_providers",
        ),
        allowed_audit_versions=_positive_int_list(
            _required(raw, "allowed_audit_versions", "policy"),
            "policy.allowed_audit_versions",
        ),
        allowed_observer_versions=_positive_int_list(
            _required(raw, "allowed_observer_versions", "policy"),
            "policy.allowed_observer_versions",
        ),
        allowed_audit_code_versions=_string_list(
            _required(raw, "allowed_audit_code_versions", "policy"),
            "policy.allowed_audit_code_versions",
        ),
        allowed_observer_code_versions=_string_list(
            _required(raw, "allowed_observer_code_versions", "policy"),
            "policy.allowed_observer_code_versions",
        ),
        require_zero_dirty_sessions=zero_dirty,
        require_zero_direction_mismatches=zero_direction,
        require_complete_session_inventory=complete_inventory,
    )


def _parse_campaign_artifact(
    raw: Any,
    *,
    root: Path,
    coverage_start: date,
    coverage_end: date,
    policy: GatePolicy,
) -> CampaignArtifact:
    if not isinstance(raw, dict):
        raise ValueError("campaign_artifact must be an object")
    _exact_fields(raw, CAMPAIGN_ARTIFACT_FIELDS, "campaign_artifact")
    path = _relative_artifact_path(
        root, _required(raw, "path", "campaign_artifact"), "campaign_artifact.path"
    )
    digest = _sha256(
        _required(raw, "sha256", "campaign_artifact"), "campaign_artifact.sha256"
    )
    campaign_raw = _verify_digest(path, digest, "prospective campaign")
    try:
        payload = json.loads(campaign_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("prospective campaign is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("prospective campaign root must be an object")
    _exact_fields(payload, CAMPAIGN_FIELDS, "prospective campaign")
    schema = payload["schema_version"]
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != CAMPAIGN_SCHEMA_VERSION
    ):
        raise ValueError("prospective campaign schema_version must be 1")
    if payload["status"] != "locked":
        raise ValueError("prospective campaign status must be locked")
    campaign_start = _iso_date(payload["coverage_start"], "campaign.coverage_start")
    campaign_end = _iso_date(payload["coverage_end"], "campaign.coverage_end")
    if (campaign_start, campaign_end) != (coverage_start, coverage_end):
        raise ValueError("prospective campaign coverage does not match evidence set")
    campaign_policy = _parse_policy(payload["policy"])
    if campaign_policy != policy:
        raise ValueError("prospective campaign policy does not match evidence set")
    expected = _expected_sessions(campaign_start, campaign_end)
    if not expected:
        raise ValueError("prospective campaign contains no XNYS sessions")
    if len(expected) < campaign_policy.min_clean_sessions:
        raise ValueError(
            "prospective campaign contains fewer XNYS sessions than "
            "policy.min_clean_sessions"
        )
    locked_at = _aware_datetime(payload["locked_at_utc"], "campaign.locked_at_utc")
    first_open = CALENDAR.session_open(expected[0]).to_pydatetime().astimezone(timezone.utc)
    if locked_at >= first_open:
        raise ValueError("prospective campaign must be locked before its first session opens")
    return CampaignArtifact(
        _nonempty_string(payload["campaign_id"], "campaign.campaign_id"),
        locked_at,
        path,
        digest,
    )


def _parse_report_artifact(raw: Any, index: int, root: Path) -> SessionArtifact:
    context = f"session_reports[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _exact_fields(raw, REPORT_ARTIFACT_FIELDS, context)
    return SessionArtifact(
        session=_iso_date(_required(raw, "session", context), f"{context}.session"),
        path=_relative_artifact_path(root, _required(raw, "path", context), f"{context}.path"),
        sha256=_sha256(_required(raw, "sha256", context), f"{context}.sha256"),
    )


def _parse_control_artifact(raw: Any, index: int, root: Path) -> ControlArtifact:
    context = f"control_artifacts[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _exact_fields(raw, CONTROL_ARTIFACT_FIELDS, context)
    kind = _required(raw, "kind", context)
    if kind not in REQUIRED_CONTROL_KINDS:
        raise ValueError(f"{context}.kind must be one of {sorted(REQUIRED_CONTROL_KINDS)}")
    return ControlArtifact(
        kind=kind,
        path=_relative_artifact_path(root, _required(raw, "path", context), f"{context}.path"),
        sha256=_sha256(_required(raw, "sha256", context), f"{context}.sha256"),
        revision=_nonempty_string(
            _required(raw, "revision", context), f"{context}.revision"
        ),
        completed_at_utc=_aware_datetime(
            _required(raw, "completed_at_utc", context), f"{context}.completed_at_utc"
        ),
    )


def _expected_sessions(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise ValueError("coverage_end must not precede coverage_start")
    return tuple(timestamp.date() for timestamp in CALENDAR.sessions_in_range(start, end))


def _coverage_end_utc(session: date) -> datetime:
    return datetime.combine(session, time(20, 5), tzinfo=ET).astimezone(timezone.utc)


def load_evidence_manifest(path: Path | str) -> EvidenceManifest:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evidence manifest root must be an object")
    _exact_fields(payload, ROOT_FIELDS, "evidence manifest")
    schema = _required(payload, "schema_version", "evidence manifest")
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError(
            f"unsupported evidence schema version {schema!r}; "
            f"expected {EVIDENCE_SCHEMA_VERSION}"
        )
    if payload.get("status") != "locked":
        raise ValueError("evidence manifest status must be 'locked'")
    created_at = _aware_datetime(
        _required(payload, "created_at_utc", "evidence manifest"),
        "evidence manifest.created_at_utc",
    )
    coverage_start = _iso_date(
        _required(payload, "coverage_start", "evidence manifest"),
        "evidence manifest.coverage_start",
    )
    coverage_end = _iso_date(
        _required(payload, "coverage_end", "evidence manifest"),
        "evidence manifest.coverage_end",
    )
    expected = _expected_sessions(coverage_start, coverage_end)
    if not expected:
        raise ValueError("coverage range contains no XNYS sessions")
    if created_at <= _coverage_end_utc(expected[-1]):
        raise ValueError("evidence manifest cannot be locked before coverage ends")
    reports_raw = _required(payload, "session_reports", "evidence manifest")
    controls_raw = _required(payload, "control_artifacts", "evidence manifest")
    if not isinstance(reports_raw, list) or not reports_raw:
        raise ValueError("evidence manifest.session_reports must be a non-empty list")
    if not isinstance(controls_raw, list) or not controls_raw:
        raise ValueError("evidence manifest.control_artifacts must be a non-empty list")
    root = manifest_path.parent
    reports = tuple(
        _parse_report_artifact(raw, index, root) for index, raw in enumerate(reports_raw)
    )
    controls = tuple(
        _parse_control_artifact(raw, index, root) for index, raw in enumerate(controls_raw)
    )
    sessions = [artifact.session for artifact in reports]
    if len(sessions) != len(set(sessions)):
        raise ValueError("session report dates must be unique")
    kinds = [artifact.kind for artifact in controls]
    if len(kinds) != len(set(kinds)):
        raise ValueError("control artifact kinds must be unique")
    if any(control.completed_at_utc > created_at for control in controls):
        raise ValueError("control artifacts cannot complete after the manifest is locked")
    policy = _parse_policy(_required(payload, "policy", "evidence manifest"))
    campaign = _parse_campaign_artifact(
        _required(payload, "campaign_artifact", "evidence manifest"),
        root=root,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        policy=policy,
    )
    return EvidenceManifest(
        evidence_set_version=_nonempty_string(
            _required(payload, "evidence_set_version", "evidence manifest"),
            "evidence manifest.evidence_set_version",
        ),
        created_at_utc=created_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        campaign_artifact=campaign,
        policy=policy,
        session_reports=reports,
        control_artifacts=controls,
    )


def _verify_digest(path: Path, expected: str, context: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{context} cannot be read: {exc}") from exc
    observed = hashlib.sha256(raw).hexdigest()
    if observed != expected:
        raise ValueError(
            f"{context} digest mismatch: expected {expected}, observed {observed}"
        )
    return raw


def _control_passed(artifact: ControlArtifact) -> bool:
    if artifact.revision == "unknown":
        raise ValueError(f"control {artifact.kind} lacks an attributable revision")
    raw = _verify_digest(artifact.path, artifact.sha256, f"control {artifact.kind}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"control {artifact.kind} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"control {artifact.kind} root must be an object")
    _exact_fields(payload, CONTROL_EVIDENCE_FIELDS, f"control {artifact.kind}")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise ValueError(f"control {artifact.kind} schema_version must be 1")
    if payload["kind"] != artifact.kind:
        raise ValueError(f"control {artifact.kind} kind does not match manifest")
    if payload["revision"] != artifact.revision:
        raise ValueError(f"control {artifact.kind} revision does not match manifest")
    completed_at = _aware_datetime(
        payload["completed_at_utc"], f"control {artifact.kind}.completed_at_utc"
    )
    if completed_at != artifact.completed_at_utc:
        raise ValueError(f"control {artifact.kind} completion time does not match manifest")
    if payload["status"] not in {"passed", "failed"}:
        raise ValueError(f"control {artifact.kind} status must be passed or failed")
    checks = payload["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"control {artifact.kind} checks must be a non-empty list")
    check_results = []
    for index, check in enumerate(checks):
        context = f"control {artifact.kind}.checks[{index}]"
        if not isinstance(check, dict):
            raise ValueError(f"{context} must be an object")
        _exact_fields(check, CONTROL_CHECK_FIELDS, context)
        _nonempty_string(check["name"], f"{context}.name")
        _nonempty_string(check["evidence"], f"{context}.evidence")
        check_results.append(_strict_bool(check["passed"], f"{context}.passed"))
    all_checks_passed = all(check_results)
    if (payload["status"] == "passed") != all_checks_passed:
        raise ValueError(f"control {artifact.kind} status contradicts its checks")
    return all_checks_passed


def _report_metrics(artifact: SessionArtifact) -> dict[str, Any]:
    raw = _verify_digest(artifact.path, artifact.sha256, f"session {artifact.session}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"session {artifact.session} report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"session {artifact.session} report root must be an object")
    if payload.get("session") != artifact.session.isoformat():
        raise ValueError(f"session {artifact.session} report date does not match manifest")
    audit_version = payload.get("audit_version")
    if not isinstance(audit_version, int) or isinstance(audit_version, bool):
        raise ValueError(f"session {artifact.session} audit_version must be an integer")
    if payload.get("audit_code_version") in {None, "", "unknown"}:
        raise ValueError(f"session {artifact.session} lacks an attributable audit revision")
    if not isinstance(payload.get("operational_clean"), bool):
        raise ValueError(f"session {artifact.session} operational_clean must be boolean")
    if not isinstance(payload.get("session_evidence_eligible"), bool):
        raise ValueError(
            f"session {artifact.session} session_evidence_eligible must be boolean"
        )
    operational = payload.get("operational")
    empirical = payload.get("empirical")
    catalyst = payload.get("catalyst_ledger")
    issues = payload.get("issues")
    if not isinstance(operational, dict) or not isinstance(empirical, dict):
        raise ValueError(f"session {artifact.session} metrics must be objects")
    if not isinstance(catalyst, dict) or catalyst.get("status") != "success":
        raise ValueError(f"session {artifact.session} lacks successful catalyst evidence")
    if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
        raise ValueError(f"session {artifact.session} issues must be a list of objects")
    required_operational = {
        "candidate_observations",
        "unique_candidates",
        "code_versions",
        "data_feeds",
        "market_data_providers",
        "observer_versions",
        "threshold_snapshots",
        "fetch_errors",
        "failed_invariants",
    }
    required_empirical = {
        "status",
        "definitive_labels",
        "ambiguous_labels",
        "true_positives",
        "false_positives",
        "true_negatives",
        "false_negatives",
        "precision",
        "recall",
        "direction_mismatches",
        "mean_detection_latency_seconds",
        "max_detection_latency_seconds",
    }
    if not required_operational <= operational.keys():
        raise ValueError(f"session {artifact.session} operational metrics are incomplete")
    if not required_empirical <= empirical.keys():
        raise ValueError(f"session {artifact.session} empirical metrics are incomplete")
    operational_clean = payload["operational_clean"]
    session_eligible = payload["session_evidence_eligible"]
    candidate_observations = _nonnegative_int(
        operational["candidate_observations"],
        f"session {artifact.session} candidate_observations",
    )
    unique_candidates = _nonnegative_int(
        operational["unique_candidates"],
        f"session {artifact.session} unique_candidates",
    )
    if unique_candidates > candidate_observations:
        raise ValueError(
            f"session {artifact.session} unique candidates exceed observations"
        )
    for key in ("code_versions", "data_feeds", "market_data_providers"):
        _string_list(operational[key], f"session {artifact.session} {key}")
    observer_versions = operational["observer_versions"]
    if (
        not isinstance(observer_versions, list)
        or not observer_versions
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in observer_versions
        )
    ):
        raise ValueError(f"session {artifact.session} observer_versions must be integers")
    threshold_snapshots = _nonnegative_int(
        operational["threshold_snapshots"],
        f"session {artifact.session} threshold_snapshots",
    )
    fetch_errors = _nonnegative_int(
        operational["fetch_errors"], f"session {artifact.session} fetch_errors"
    )
    failed_invariants = _nonnegative_int(
        operational["failed_invariants"],
        f"session {artifact.session} failed_invariants",
    )
    if operational_clean and (
        threshold_snapshots != 1 or fetch_errors != 0 or failed_invariants != 0
    ):
        raise ValueError(
            f"session {artifact.session} clean verdict contradicts operational counters"
        )
    if empirical["status"] not in {"NOT_PROVIDED", "COMPLETE", "INCOMPLETE"}:
        raise ValueError(f"session {artifact.session} empirical status is invalid")
    count_keys = (
        "definitive_labels",
        "ambiguous_labels",
        "true_positives",
        "false_positives",
        "true_negatives",
        "false_negatives",
        "direction_mismatches",
    )
    counts = {
        key: _nonnegative_int(empirical[key], f"session {artifact.session} {key}")
        for key in count_keys
    }
    confusion_total = sum(
        counts[key]
        for key in (
            "true_positives",
            "false_positives",
            "true_negatives",
            "false_negatives",
        )
    )
    if confusion_total != counts["definitive_labels"]:
        raise ValueError(f"session {artifact.session} confusion matrix does not conserve labels")
    observed_precision = _optional_ratio(
        empirical["precision"], f"session {artifact.session} precision"
    )
    observed_recall = _optional_ratio(
        empirical["recall"], f"session {artifact.session} recall"
    )
    expected_precision = _ratio(
        counts["true_positives"],
        counts["true_positives"] + counts["false_positives"],
    )
    expected_recall = _ratio(
        counts["true_positives"],
        counts["true_positives"] + counts["false_negatives"],
    )
    if observed_precision != expected_precision or observed_recall != expected_recall:
        raise ValueError(f"session {artifact.session} reported ratios do not match counts")
    for key in ("mean_detection_latency_seconds", "max_detection_latency_seconds"):
        value = empirical[key]
        if value is not None and _finite_number(value, f"session {artifact.session} {key}") < 0:
            raise ValueError(f"session {artifact.session} {key} must be non-negative")
    if (
        empirical["mean_detection_latency_seconds"] is not None
        and empirical["max_detection_latency_seconds"] is not None
        and empirical["mean_detection_latency_seconds"]
        > empirical["max_detection_latency_seconds"]
    ):
        raise ValueError(f"session {artifact.session} mean latency exceeds maximum")
    operational_blockers = [
        issue
        for issue in issues
        if issue.get("severity") == "blocker"
        and not str(issue.get("code", "")).startswith("EMPIRICAL_")
    ]
    if operational_clean == bool(operational_blockers):
        raise ValueError(
            f"session {artifact.session} operational verdict contradicts blocker issues"
        )
    all_blockers = [issue for issue in issues if issue.get("severity") == "blocker"]
    eligible_expected = (
        operational_clean
        and empirical["status"] == "COMPLETE"
        and not all_blockers
        and counts["ambiguous_labels"] == 0
        and counts["false_positives"] == 0
        and counts["false_negatives"] == 0
        and counts["direction_mismatches"] == 0
    )
    if session_eligible != eligible_expected:
        raise ValueError(
            f"session {artifact.session} eligibility contradicts its evidence"
        )
    return payload


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _check(code: str, observed: Any, required: Any, passed: bool) -> GateCheck:
    return GateCheck(code=code, passed=passed, observed=observed, required=required)


def evaluate_evidence_gate(manifest: EvidenceManifest) -> EvidenceGateReport:
    expected_sessions = _expected_sessions(manifest.coverage_start, manifest.coverage_end)
    expected_set = set(expected_sessions)
    supplied_set = {artifact.session for artifact in manifest.session_reports}
    session_payloads = [
        (artifact, _report_metrics(artifact)) for artifact in manifest.session_reports
    ]
    control_results = {
        control.kind: _control_passed(control) for control in manifest.control_artifacts
    }

    clean = sum(payload["operational_clean"] for _, payload in session_payloads)
    dirty = len(session_payloads) - clean
    scored = sum(
        payload["empirical"]["status"] == "COMPLETE" for _, payload in session_payloads
    )
    eligible = sum(payload["session_evidence_eligible"] for _, payload in session_payloads)
    empirical_rows = [payload["empirical"] for _, payload in session_payloads]
    definitive = sum(row["definitive_labels"] for row in empirical_rows)
    ambiguous = sum(row["ambiguous_labels"] for row in empirical_rows)
    tp = sum(row["true_positives"] for row in empirical_rows)
    fp = sum(row["false_positives"] for row in empirical_rows)
    tn = sum(row["true_negatives"] for row in empirical_rows)
    fn = sum(row["false_negatives"] for row in empirical_rows)
    positive = tp + fn
    direction_mismatches = sum(row["direction_mismatches"] for row in empirical_rows)
    latency_weights = [
        (row["mean_detection_latency_seconds"], row["true_positives"])
        for row in empirical_rows
        if row["mean_detection_latency_seconds"] is not None and row["true_positives"] > 0
    ]
    latency_denominator = sum(weight for _, weight in latency_weights)
    mean_latency = (
        sum(value * weight for value, weight in latency_weights) / latency_denominator
        if latency_denominator
        else None
    )
    max_latencies = [
        row["max_detection_latency_seconds"]
        for row in empirical_rows
        if row["max_detection_latency_seconds"] is not None
    ]
    max_latency = max(max_latencies) if max_latencies else None
    operational_rows = [payload["operational"] for _, payload in session_payloads]
    audit_versions = tuple(
        sorted({payload["audit_version"] for _, payload in session_payloads})
    )
    audit_code_versions = tuple(
        sorted({payload["audit_code_version"] for _, payload in session_payloads})
    )
    observer_code_versions = tuple(
        sorted(
            {
                version
                for row in operational_rows
                for version in row["code_versions"]
            }
        )
    )
    evaluator_versions = tuple(
        sorted(
            {
                version
                for row in operational_rows
                for version in row["observer_versions"]
            }
        )
    )
    data_feeds = tuple(
        sorted({feed for row in operational_rows for feed in row["data_feeds"]})
    )
    providers = tuple(
        sorted(
            {
                provider
                for row in operational_rows
                for provider in row["market_data_providers"]
            }
        )
    )
    metrics = AggregateMetrics(
        expected_sessions=len(expected_sessions),
        supplied_sessions=len(manifest.session_reports),
        clean_sessions=clean,
        dirty_sessions=dirty,
        empirically_scored_sessions=scored,
        session_evidence_eligible=eligible,
        definitive_labels=definitive,
        ambiguous_labels=ambiguous,
        positive_labels=positive,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=_ratio(tp, tp + fp),
        recall=_ratio(tp, tp + fn),
        direction_mismatches=direction_mismatches,
        mean_detection_latency_seconds=mean_latency,
        max_detection_latency_seconds=max_latency,
        candidate_observations=sum(row["candidate_observations"] for row in operational_rows),
        unique_candidates=sum(row["unique_candidates"] for row in operational_rows),
        audit_code_versions=audit_code_versions,
        audit_versions=audit_versions,
        observer_versions=evaluator_versions,
        observer_code_versions=observer_code_versions,
        data_feeds=data_feeds,
        market_data_providers=providers,
    )
    policy = manifest.policy
    control_kinds = {artifact.kind for artifact in manifest.control_artifacts}
    evidence_revisions = set(audit_code_versions) | set(observer_code_versions)
    control_revisions = {artifact.revision for artifact in manifest.control_artifacts}
    checks = (
        _check(
            "COMPLETE_SESSION_INVENTORY",
            sorted(session.isoformat() for session in supplied_set),
            sorted(session.isoformat() for session in expected_set),
            supplied_set == expected_set,
        ),
        _check(
            "MIN_CLEAN_SESSIONS",
            clean,
            policy.min_clean_sessions,
            clean >= policy.min_clean_sessions,
        ),
        _check("ZERO_DIRTY_SESSIONS", dirty, 0, dirty == 0),
        _check(
            "MIN_DEFINITIVE_LABELS",
            definitive,
            policy.min_definitive_labels,
            definitive >= policy.min_definitive_labels,
        ),
        _check(
            "MIN_POSITIVE_LABELS",
            positive,
            policy.min_positive_labels,
            positive >= policy.min_positive_labels,
        ),
        _check(
            "MIN_RECALL",
            metrics.recall,
            policy.min_recall,
            metrics.recall is not None and metrics.recall >= policy.min_recall,
        ),
        _check(
            "MIN_PRECISION",
            metrics.precision,
            policy.min_precision,
            metrics.precision is not None and metrics.precision >= policy.min_precision,
        ),
        _check(
            "MAX_DETECTION_LATENCY",
            max_latency,
            policy.max_detection_latency_seconds,
            max_latency is not None and max_latency <= policy.max_detection_latency_seconds,
        ),
        _check("ZERO_AMBIGUOUS_LABELS", ambiguous, 0, ambiguous == 0),
        _check(
            "ZERO_DIRECTION_MISMATCHES",
            direction_mismatches,
            0,
            direction_mismatches == 0,
        ),
        _check(
            "REQUIRED_CONTROL_ARTIFACTS",
            sorted(control_kinds),
            sorted(REQUIRED_CONTROL_KINDS),
            control_kinds == REQUIRED_CONTROL_KINDS,
        ),
        _check(
            "CONTROL_ARTIFACTS_PASSED",
            sorted(kind for kind, passed in control_results.items() if passed),
            sorted(REQUIRED_CONTROL_KINDS),
            control_kinds == REQUIRED_CONTROL_KINDS and all(control_results.values()),
        ),
        _check(
            "CONTROL_REVISIONS_COVERED",
            sorted(control_revisions),
            sorted(evidence_revisions),
            control_revisions <= evidence_revisions,
        ),
        _check(
            "ALLOWED_DATA_FEEDS",
            list(data_feeds),
            list(policy.allowed_data_feeds),
            set(data_feeds) <= set(policy.allowed_data_feeds),
        ),
        _check(
            "ALLOWED_MARKET_DATA_PROVIDERS",
            list(providers),
            list(policy.allowed_market_data_providers),
            set(providers) <= set(policy.allowed_market_data_providers),
        ),
        _check(
            "ALLOWED_AUDIT_VERSIONS",
            list(audit_versions),
            list(policy.allowed_audit_versions),
            set(audit_versions) <= set(policy.allowed_audit_versions),
        ),
        _check(
            "ALLOWED_OBSERVER_VERSIONS",
            list(evaluator_versions),
            list(policy.allowed_observer_versions),
            set(evaluator_versions) <= set(policy.allowed_observer_versions),
        ),
        _check(
            "ALLOWED_AUDIT_CODE_VERSIONS",
            list(audit_code_versions),
            list(policy.allowed_audit_code_versions),
            set(audit_code_versions) <= set(policy.allowed_audit_code_versions),
        ),
        _check(
            "ALLOWED_OBSERVER_CODE_VERSIONS",
            list(observer_code_versions),
            list(policy.allowed_observer_code_versions),
            set(observer_code_versions) <= set(policy.allowed_observer_code_versions),
        ),
    )
    verdict = VERDICT_OWNER_REVIEW if all(check.passed for check in checks) else VERDICT_NOT_READY
    return EvidenceGateReport(
        schema_version=EVIDENCE_SCHEMA_VERSION,
        evidence_set_version=manifest.evidence_set_version,
        verdict=verdict,
        coverage_start=manifest.coverage_start.isoformat(),
        coverage_end=manifest.coverage_end.isoformat(),
        campaign_id=manifest.campaign_artifact.campaign_id,
        campaign_locked_at_utc=manifest.campaign_artifact.locked_at_utc.isoformat(),
        campaign_sha256=manifest.campaign_artifact.sha256,
        policy=policy,
        metrics=metrics,
        checks=checks,
        session_digests=tuple(
            {
                "session": artifact.session.isoformat(),
                "path": str(artifact.path),
                "sha256": artifact.sha256,
            }
            for artifact in manifest.session_reports
        ),
        control_digests=tuple(
            {
                "kind": artifact.kind,
                "path": str(artifact.path),
                "sha256": artifact.sha256,
                "revision": artifact.revision,
            }
            for artifact in manifest.control_artifacts
        ),
    )


def report_json(report: EvidenceGateReport, *, compact: bool = False) -> str:
    return json.dumps(
        asdict(report),
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = evaluate_evidence_gate(load_evidence_manifest(args.manifest))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(report_json(report, compact=args.compact))
    return 0 if report.verdict == VERDICT_OWNER_REVIEW else 1


if __name__ == "__main__":
    raise SystemExit(main())
