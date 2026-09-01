"""Pure, default-off readiness policy for future postmarket customer alerts.

This module deliberately cannot enqueue, send, trade, query a provider, or
write production state.  It turns an exact lifecycle transition and rank
snapshot into an auditable readiness decision only after a separately issued
owner authorization is supplied.  A later delivery service must add its own
append-only routing ledger and controls before consuming this decision.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from tradebot.postmarket_lifecycle import (
    STATE_CONFIRMED,
    STATE_CLOSED,
    STATE_DEQUALIFIED,
    STATE_REQUALIFIED,
    STATE_STRENGTHENING,
)


DELIVERY_POLICY_VERSION = 2
DECISION_ELIGIBLE = "ELIGIBLE_FOR_DRY_RUN"
DECISION_SUPPRESSED = "SUPPRESSED"
PRESENTATION_ACTIONABLE = "ACTIONABLE"
PRESENTATION_STALE = "STALE"
PRESENTATION_DEGRADED = "DEGRADED"
PRESENTATION_CLOSED = "CLOSED"
ALLOWED_STATES = frozenset({STATE_CONFIRMED, STATE_STRENGTHENING, STATE_REQUALIFIED})
ACKNOWLEDGEMENT = (
    "I approve this exact evidence-bound policy for postmarket customer-alert "
    "readiness review; this does not enable or send alerts."
)
POLICY_FIELDS = frozenset({
    "delivery_policy_version", "router_revision", "evidence_set_sha256",
    "evidence_gate_sha256", "rank_version", "minimum_evidence_score",
    "calibration_version", "calibration_model_sha256",
    "minimum_calibrated_quality", "allowed_calibration_revisions",
    "maximum_ordinal_rank", "minimum_evidence_coverage_pct",
    "maximum_data_age_seconds", "allowed_states",
    "allowed_evidence_revisions", "allowed_providers", "allowed_feeds",
})
AUTHORIZATION_FIELDS = frozenset({
    "schema_version", "release_id", "approved_by", "approved_at_utc",
    "expires_at_utc", "policy_sha256", "evidence_set_sha256",
    "evidence_gate_sha256", "router_revision", "acknowledgement",
    "dry_run_readiness_approved",
})


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256_json(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _valid_revision(value: str) -> bool:
    return 7 <= len(value) <= 40 and all(char in "0123456789abcdef" for char in value)


def _exact_fields(payload: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    observed = set(payload)
    if observed != expected:
        raise ValueError(
            f"{context} fields must match exactly; "
            f"missing={sorted(expected - observed)!r} extra={sorted(observed - expected)!r}"
        )


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{name} must be a non-empty array of non-empty strings")
    return tuple(value)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime") from exc
    return _aware_utc(parsed, name)


@dataclass(frozen=True)
class DeliveryPolicy:
    router_revision: str
    evidence_set_sha256: str
    evidence_gate_sha256: str
    rank_version: int
    minimum_evidence_score: float
    calibration_version: int
    calibration_model_sha256: str
    minimum_calibrated_quality: float
    maximum_ordinal_rank: int
    minimum_evidence_coverage_pct: float
    maximum_data_age_seconds: float
    allowed_states: tuple[str, ...]
    allowed_evidence_revisions: tuple[str, ...]
    allowed_calibration_revisions: tuple[str, ...]
    allowed_providers: tuple[str, ...]
    allowed_feeds: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "delivery_policy_version": DELIVERY_POLICY_VERSION,
            "router_revision": self.router_revision,
            "evidence_set_sha256": self.evidence_set_sha256,
            "evidence_gate_sha256": self.evidence_gate_sha256,
            "rank_version": self.rank_version,
            "minimum_evidence_score": self.minimum_evidence_score,
            "calibration_version": self.calibration_version,
            "calibration_model_sha256": self.calibration_model_sha256,
            "minimum_calibrated_quality": self.minimum_calibrated_quality,
            "maximum_ordinal_rank": self.maximum_ordinal_rank,
            "minimum_evidence_coverage_pct": self.minimum_evidence_coverage_pct,
            "maximum_data_age_seconds": self.maximum_data_age_seconds,
            "allowed_states": list(self.allowed_states),
            "allowed_evidence_revisions": list(self.allowed_evidence_revisions),
            "allowed_calibration_revisions": list(
                self.allowed_calibration_revisions
            ),
            "allowed_providers": list(self.allowed_providers),
            "allowed_feeds": list(self.allowed_feeds),
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.canonical_payload())


@dataclass(frozen=True)
class OwnerAuthorization:
    release_id: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    policy_sha256: str
    evidence_set_sha256: str
    evidence_gate_sha256: str
    router_revision: str
    acknowledgement: str
    dry_run_readiness_approved: bool

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "release_id": self.release_id,
            "approved_by": self.approved_by,
            "approved_at_utc": _aware_utc(
                self.approved_at, "authorization.approved_at"
            ).isoformat(),
            "expires_at_utc": _aware_utc(
                self.expires_at, "authorization.expires_at"
            ).isoformat(),
            "policy_sha256": self.policy_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
            "evidence_gate_sha256": self.evidence_gate_sha256,
            "router_revision": self.router_revision,
            "acknowledgement": self.acknowledgement,
            "dry_run_readiness_approved": self.dry_run_readiness_approved,
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.canonical_payload())


@dataclass(frozen=True)
class DeliveryCandidate:
    transition_id: int
    candidate_id: int
    session: str
    symbol: str
    direction: str
    lifecycle_state: str
    actionability: str
    transition_at: datetime
    evidence_bar_open_at: datetime
    rank_run_id: int
    rank_id: int
    rank_version: int
    rank_status: str
    rankable: bool
    ordinal_rank: int | None
    evidence_score: float
    calibration_projection_id: int | None
    calibration_run_id: int | None
    calibration_version: int | None
    calibration_model_sha256: str | None
    calibrated_quality: float | None
    calibration_projected_at: datetime | None
    calibration_code_version: str | None
    evidence_coverage_pct: float
    exclusion_reasons: tuple[str, ...]
    data_feed: str
    market_data_provider: str
    code_version: str


@dataclass(frozen=True)
class DeliveryReadinessDecision:
    decision: str
    presentation: str
    reason_codes: tuple[str, ...]
    idempotency_key: str
    policy_sha256: str
    release_id: str | None


def parse_delivery_policy(payload: Mapping[str, object]) -> DeliveryPolicy:
    """Parse the exact JSON policy contract; unknown fields are rejected."""
    _exact_fields(payload, POLICY_FIELDS, "delivery policy")
    if payload["delivery_policy_version"] != DELIVERY_POLICY_VERSION:
        raise ValueError("unsupported delivery_policy_version")
    for field in (
        "router_revision",
        "evidence_set_sha256",
        "evidence_gate_sha256",
        "calibration_model_sha256",
    ):
        if not isinstance(payload[field], str):
            raise ValueError(f"{field} must be a string")
    policy = DeliveryPolicy(
        router_revision=payload["router_revision"],
        evidence_set_sha256=payload["evidence_set_sha256"],
        evidence_gate_sha256=payload["evidence_gate_sha256"],
        rank_version=_integer(payload["rank_version"], "rank_version"),
        minimum_evidence_score=_number(
            payload["minimum_evidence_score"], "minimum_evidence_score"
        ),
        calibration_version=_integer(
            payload["calibration_version"], "calibration_version"
        ),
        calibration_model_sha256=payload["calibration_model_sha256"],
        minimum_calibrated_quality=_number(
            payload["minimum_calibrated_quality"], "minimum_calibrated_quality"
        ),
        maximum_ordinal_rank=_integer(
            payload["maximum_ordinal_rank"], "maximum_ordinal_rank"
        ),
        minimum_evidence_coverage_pct=_number(
            payload["minimum_evidence_coverage_pct"],
            "minimum_evidence_coverage_pct",
        ),
        maximum_data_age_seconds=_number(
            payload["maximum_data_age_seconds"], "maximum_data_age_seconds"
        ),
        allowed_states=_string_tuple(payload["allowed_states"], "allowed_states"),
        allowed_evidence_revisions=_string_tuple(
            payload["allowed_evidence_revisions"], "allowed_evidence_revisions"
        ),
        allowed_calibration_revisions=_string_tuple(
            payload["allowed_calibration_revisions"],
            "allowed_calibration_revisions",
        ),
        allowed_providers=_string_tuple(
            payload["allowed_providers"], "allowed_providers"
        ),
        allowed_feeds=_string_tuple(payload["allowed_feeds"], "allowed_feeds"),
    )
    validate_policy(policy)
    return policy


def parse_owner_authorization(payload: Mapping[str, object]) -> OwnerAuthorization:
    """Parse a manual owner record; this function can never create approval."""
    _exact_fields(payload, AUTHORIZATION_FIELDS, "owner authorization")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported owner authorization schema_version")
    string_fields = (
        "release_id", "approved_by", "policy_sha256", "evidence_set_sha256",
        "evidence_gate_sha256", "router_revision", "acknowledgement",
    )
    if any(not isinstance(payload[field], str) for field in string_fields):
        raise ValueError("owner authorization string fields must be strings")
    if payload["dry_run_readiness_approved"] is not True:
        raise ValueError("dry_run_readiness_approved must be explicitly true")
    return OwnerAuthorization(
        release_id=payload["release_id"],
        approved_by=payload["approved_by"],
        approved_at=_datetime(payload["approved_at_utc"], "approved_at_utc"),
        expires_at=_datetime(payload["expires_at_utc"], "expires_at_utc"),
        policy_sha256=payload["policy_sha256"],
        evidence_set_sha256=payload["evidence_set_sha256"],
        evidence_gate_sha256=payload["evidence_gate_sha256"],
        router_revision=payload["router_revision"],
        acknowledgement=payload["acknowledgement"],
        dry_run_readiness_approved=True,
    )


def validate_policy(policy: DeliveryPolicy) -> None:
    if not _valid_revision(policy.router_revision):
        raise ValueError("router_revision must be known")
    for name, value in (
        ("evidence_set_sha256", policy.evidence_set_sha256),
        ("evidence_gate_sha256", policy.evidence_gate_sha256),
    ):
        if not _valid_sha256(value):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    if policy.rank_version <= 0:
        raise ValueError("rank_version must be positive")
    if policy.calibration_version <= 0:
        raise ValueError("calibration_version must be positive")
    if not _valid_sha256(policy.calibration_model_sha256):
        raise ValueError(
            "calibration_model_sha256 must be a lowercase SHA-256 digest"
        )
    if (
        not math.isfinite(policy.minimum_evidence_score)
        or not 0 <= policy.minimum_evidence_score <= 100
    ):
        raise ValueError("minimum_evidence_score must be between 0 and 100")
    if (
        not math.isfinite(policy.minimum_calibrated_quality)
        or not 0 <= policy.minimum_calibrated_quality <= 1
    ):
        raise ValueError("minimum_calibrated_quality must be between 0 and 1")
    if policy.maximum_ordinal_rank <= 0:
        raise ValueError("maximum_ordinal_rank must be positive")
    if (
        not math.isfinite(policy.minimum_evidence_coverage_pct)
        or not 0 <= policy.minimum_evidence_coverage_pct <= 100
    ):
        raise ValueError("minimum_evidence_coverage_pct must be between 0 and 100")
    if (
        not math.isfinite(policy.maximum_data_age_seconds)
        or policy.maximum_data_age_seconds <= 0
    ):
        raise ValueError("maximum_data_age_seconds must be positive")
    if not policy.allowed_states or not set(policy.allowed_states) <= ALLOWED_STATES:
        raise ValueError("allowed_states must be a non-empty subset of rankable states")
    if len(policy.allowed_states) != len(set(policy.allowed_states)):
        raise ValueError("allowed_states cannot contain duplicates")
    if not policy.allowed_evidence_revisions or any(
        not _valid_revision(revision)
        for revision in policy.allowed_evidence_revisions
    ):
        raise ValueError("allowed_evidence_revisions must be known and non-empty")
    if len(policy.allowed_evidence_revisions) != len(set(policy.allowed_evidence_revisions)):
        raise ValueError("allowed_evidence_revisions cannot contain duplicates")
    if not policy.allowed_calibration_revisions or any(
        not _valid_revision(revision)
        for revision in policy.allowed_calibration_revisions
    ):
        raise ValueError("allowed_calibration_revisions must be known and non-empty")
    if len(policy.allowed_calibration_revisions) != len(
        set(policy.allowed_calibration_revisions)
    ):
        raise ValueError("allowed_calibration_revisions cannot contain duplicates")
    if (
        not policy.allowed_providers
        or not policy.allowed_feeds
        or any(not value for value in (*policy.allowed_providers, *policy.allowed_feeds))
    ):
        raise ValueError("allowed providers and feeds must be non-empty")
    if (
        len(policy.allowed_providers) != len(set(policy.allowed_providers))
        or len(policy.allowed_feeds) != len(set(policy.allowed_feeds))
    ):
        raise ValueError("allowed providers and feeds cannot contain duplicates")


def _authorization_reasons(
    authorization: OwnerAuthorization | None,
    policy: DeliveryPolicy,
    now: datetime,
) -> list[str]:
    if authorization is None:
        return ["OWNER_AUTHORIZATION_MISSING"]
    reasons = []
    if not authorization.dry_run_readiness_approved:
        reasons.append("OWNER_AUTHORIZATION_NOT_APPROVED")
    if authorization.acknowledgement != ACKNOWLEDGEMENT:
        reasons.append("OWNER_ACKNOWLEDGEMENT_MISMATCH")
    if authorization.policy_sha256 != policy.sha256:
        reasons.append("AUTHORIZED_POLICY_MISMATCH")
    if authorization.evidence_set_sha256 != policy.evidence_set_sha256:
        reasons.append("AUTHORIZED_EVIDENCE_SET_MISMATCH")
    if authorization.evidence_gate_sha256 != policy.evidence_gate_sha256:
        reasons.append("AUTHORIZED_EVIDENCE_GATE_MISMATCH")
    if authorization.router_revision != policy.router_revision:
        reasons.append("AUTHORIZED_ROUTER_REVISION_MISMATCH")
    approved = _aware_utc(authorization.approved_at, "authorization.approved_at")
    expires = _aware_utc(authorization.expires_at, "authorization.expires_at")
    if approved > now:
        reasons.append("OWNER_AUTHORIZATION_FROM_FUTURE")
    if expires <= approved:
        reasons.append("OWNER_AUTHORIZATION_WINDOW_INVALID")
    elif expires <= now:
        reasons.append("OWNER_AUTHORIZATION_EXPIRED")
    if not authorization.release_id.strip() or not authorization.approved_by.strip():
        reasons.append("OWNER_AUTHORIZATION_IDENTITY_MISSING")
    return reasons


def _candidate_reasons(candidate: DeliveryCandidate) -> list[str]:
    reasons = []
    if (
        candidate.transition_id <= 0
        or candidate.candidate_id <= 0
        or candidate.rank_run_id <= 0
        or candidate.rank_id <= 0
    ):
        reasons.append("EVIDENCE_ID_INVALID")
    if not candidate.session or not candidate.symbol or candidate.direction not in {"up", "down"}:
        reasons.append("CANDIDATE_IDENTITY_INVALID")
    for value in (
        candidate.evidence_score,
        candidate.evidence_coverage_pct,
    ):
        if not math.isfinite(value):
            reasons.append("RANK_VALUE_INVALID")
            break
    if not 0 <= candidate.evidence_score <= 100:
        reasons.append("EVIDENCE_SCORE_INVALID")
    if not 0 <= candidate.evidence_coverage_pct <= 100:
        reasons.append("EVIDENCE_COVERAGE_INVALID")
    if candidate.ordinal_rank is not None and candidate.ordinal_rank <= 0:
        reasons.append("ORDINAL_RANK_INVALID")
    if candidate.calibration_projection_id is None:
        reasons.append("CALIBRATION_PROJECTION_MISSING")
    elif candidate.calibration_projection_id <= 0:
        reasons.append("CALIBRATION_PROJECTION_ID_INVALID")
    if candidate.calibration_run_id is None:
        reasons.append("CALIBRATION_RUN_MISSING")
    elif candidate.calibration_run_id <= 0:
        reasons.append("CALIBRATION_RUN_ID_INVALID")
    if candidate.calibration_version is None:
        reasons.append("CALIBRATION_VERSION_MISSING")
    elif candidate.calibration_version <= 0:
        reasons.append("CALIBRATION_VERSION_INVALID")
    if candidate.calibration_model_sha256 is None:
        reasons.append("CALIBRATION_MODEL_MISSING")
    elif not _valid_sha256(candidate.calibration_model_sha256):
        reasons.append("CALIBRATION_MODEL_INVALID")
    if candidate.calibrated_quality is None:
        reasons.append("CALIBRATED_QUALITY_MISSING")
    elif not math.isfinite(candidate.calibrated_quality) or not (
        0 <= candidate.calibrated_quality <= 1
    ):
        reasons.append("CALIBRATED_QUALITY_INVALID")
    if candidate.calibration_projected_at is None:
        reasons.append("CALIBRATION_TIMESTAMP_MISSING")
    if candidate.calibration_code_version is None:
        reasons.append("CALIBRATION_REVISION_MISSING")
    elif not _valid_revision(candidate.calibration_code_version):
        reasons.append("CALIBRATION_REVISION_INVALID")
    return reasons


def evaluate_delivery_readiness(
    candidate: DeliveryCandidate,
    policy: DeliveryPolicy,
    authorization: OwnerAuthorization | None,
    *,
    now: datetime,
    runtime_router_revision: str | None = None,
    dry_run_enabled: bool = False,
    kill_switch_engaged: bool = True,
    operational_status: str = "degraded",
) -> DeliveryReadinessDecision:
    """Evaluate readiness without writing, enqueueing, or sending anything."""
    validate_policy(policy)
    current = _aware_utc(now, "now")
    transition_at = _aware_utc(candidate.transition_at, "candidate.transition_at")
    evidence_bar = _aware_utc(
        candidate.evidence_bar_open_at, "candidate.evidence_bar_open_at"
    )
    reasons: list[str] = []
    reasons.extend(_candidate_reasons(candidate))
    if runtime_router_revision is None:
        reasons.append("RUNTIME_ROUTER_REVISION_UNKNOWN")
    elif runtime_router_revision != policy.router_revision:
        reasons.append("RUNTIME_ROUTER_REVISION_MISMATCH")
    if not dry_run_enabled:
        reasons.append("DRY_RUN_DISABLED")
    if kill_switch_engaged:
        reasons.append("KILL_SWITCH_ENGAGED")
    if operational_status != "clean":
        reasons.append("OPERATIONAL_STATUS_DEGRADED")
    reasons.extend(_authorization_reasons(authorization, policy, current))
    if candidate.code_version not in policy.allowed_evidence_revisions:
        reasons.append("EVIDENCE_REVISION_NOT_ALLOWED")
    if candidate.rank_version != policy.rank_version:
        reasons.append("RANK_VERSION_MISMATCH")
    if candidate.calibration_version != policy.calibration_version:
        reasons.append("CALIBRATION_VERSION_MISMATCH")
    if candidate.calibration_model_sha256 != policy.calibration_model_sha256:
        reasons.append("CALIBRATION_MODEL_MISMATCH")
    if candidate.calibration_code_version not in policy.allowed_calibration_revisions:
        reasons.append("CALIBRATION_REVISION_NOT_ALLOWED")
    if candidate.rank_status != "complete":
        reasons.append("RANK_RUN_DEGRADED")
    if not candidate.rankable:
        reasons.append("CANDIDATE_NOT_RANKABLE")
    if candidate.exclusion_reasons:
        reasons.append("RANK_HARD_EXCLUSIONS_PRESENT")
    if candidate.lifecycle_state not in policy.allowed_states:
        reasons.append("LIFECYCLE_STATE_NOT_ALLOWED")
    if candidate.actionability != "QUALIFIED":
        reasons.append("LIFECYCLE_NOT_ACTIONABLE")
    if candidate.ordinal_rank is None:
        reasons.append("ORDINAL_RANK_MISSING")
    elif candidate.ordinal_rank > policy.maximum_ordinal_rank:
        reasons.append("ORDINAL_RANK_BELOW_POLICY")
    if candidate.evidence_score < policy.minimum_evidence_score:
        reasons.append("EVIDENCE_SCORE_BELOW_POLICY")
    if (
        candidate.calibrated_quality is not None
        and math.isfinite(candidate.calibrated_quality)
        and candidate.calibrated_quality < policy.minimum_calibrated_quality
    ):
        reasons.append("CALIBRATED_QUALITY_BELOW_POLICY")
    if candidate.evidence_coverage_pct < policy.minimum_evidence_coverage_pct:
        reasons.append("EVIDENCE_COVERAGE_BELOW_POLICY")
    data_age_seconds = (current - (evidence_bar + timedelta(minutes=5))).total_seconds()
    if data_age_seconds < 0:
        reasons.append("DATA_FROM_FUTURE")
    elif data_age_seconds > policy.maximum_data_age_seconds:
        reasons.append("DATA_STALE")
    if candidate.market_data_provider not in policy.allowed_providers:
        reasons.append("PROVIDER_NOT_ALLOWED")
    if candidate.data_feed not in policy.allowed_feeds:
        reasons.append("FEED_NOT_ALLOWED")
    if transition_at > current or evidence_bar > current:
        reasons.append("EVIDENCE_FROM_FUTURE")
    if candidate.calibration_projected_at is not None:
        calibration_projected = _aware_utc(
            candidate.calibration_projected_at,
            "candidate.calibration_projected_at",
        )
        if calibration_projected > current:
            reasons.append("CALIBRATION_FROM_FUTURE")

    unique_reasons = tuple(dict.fromkeys(reasons))
    if (
        candidate.lifecycle_state in {STATE_CLOSED, STATE_DEQUALIFIED}
        or candidate.actionability in {"CLOSED", "NOT_ACTIONABLE"}
    ):
        presentation = PRESENTATION_CLOSED
    elif any(
        reason in unique_reasons
        for reason in (
            "DATA_STALE",
            "DATA_FROM_FUTURE",
            "EVIDENCE_FROM_FUTURE",
            "CALIBRATION_FROM_FUTURE",
        )
    ):
        presentation = PRESENTATION_STALE
    elif any(
        token in reason
        for reason in unique_reasons
        for token in ("DEGRADED", "EXCLUSION", "NOT_RANKABLE", "NOT_ALLOWED", "INVALID")
    ):
        presentation = PRESENTATION_DEGRADED
    else:
        presentation = PRESENTATION_ACTIONABLE
    identity = {
        "delivery_policy_version": DELIVERY_POLICY_VERSION,
        "release_id": authorization.release_id if authorization else "unauthorized",
        "policy_sha256": policy.sha256,
        "candidate_id": candidate.candidate_id,
        "transition_id": candidate.transition_id,
        "rank_run_id": candidate.rank_run_id,
        "rank_id": candidate.rank_id,
        "calibration_projection_id": candidate.calibration_projection_id,
        "calibration_model_sha256": candidate.calibration_model_sha256,
    }
    return DeliveryReadinessDecision(
        decision=DECISION_ELIGIBLE if not unique_reasons else DECISION_SUPPRESSED,
        presentation=presentation,
        reason_codes=unique_reasons,
        idempotency_key=f"postmarket-readiness-v2:{_sha256_json(identity)}",
        policy_sha256=policy.sha256,
        release_id=authorization.release_id if authorization else None,
    )
