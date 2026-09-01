"""Offline aggregate readiness gate for market-wide postmarket discovery.

The gate reads one prospective campaign and digest-pinned immutable artifacts.
It has no network, database, alert, broker, or write path. Passing means only
that the locked evidence package is eligible for explicit owner review; it
never enables customer delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from tradebot.postmarket_evidence_gate import (
    CALENDAR,
    ET,
    ControlArtifact,
    _aware_datetime,
    _control_passed,
    _exact_fields,
    _expected_sessions,
    _finite_number,
    _iso_date,
    _nonempty_string,
    _nonnegative_int,
    _optional_ratio,
    _positive_int,
    _positive_int_list,
    _relative_artifact_path,
    _required,
    _sha256,
    _strict_bool,
    _string_list,
    _verify_digest,
)


EVIDENCE_SCHEMA_VERSION = 2
CAMPAIGN_SCHEMA_VERSION = 2
VERDICT_NOT_READY = "NOT_READY"
VERDICT_OWNER_REVIEW = "ELIGIBLE_FOR_OWNER_REVIEW"
REQUIRED_CONTROL_KINDS = {
    "discovery_failure_injection",
    "discovery_kill_switch",
    "discovery_delivery_isolation",
    "rollback_runbook",
}
PRIMARY_CENSUS_RECONCILED_ISSUES = {"PROVIDER_COMPARISON_NOT_CONFIGURED"}
ROOT_FIELDS = {
    "schema_version",
    "status",
    "evidence_set_version",
    "created_at_utc",
    "coverage_start",
    "coverage_end",
    "campaign_artifact",
    "policy",
    "discovery_audits",
    "recall_census_reports",
    "provider_proof_reports",
    "empirical_artifact",
    "calibration_artifact",
    "control_artifacts",
}
CAMPAIGN_FIELDS = {
    "schema_version",
    "status",
    "campaign_id",
    "locked_at_utc",
    "coverage_start",
    "coverage_end",
    "experiment_id",
    "experiment_manifest_sha256",
    "rank_version",
    "policy",
}
POLICY_FIELDS = {
    "min_clean_sessions",
    "min_definitive_labels",
    "min_positive_labels",
    "min_empirical_recall",
    "min_empirical_precision",
    "min_calibration_negative_labels",
    "min_calibration_bin_labels",
    "max_calibration_brier_score",
    "max_expected_calibration_error",
    "min_primary_recall",
    "max_primary_detection_latency_seconds",
    "min_provider_comparable_coverage",
    "min_provider_bar_overlap_coverage",
    "min_provider_eligible_pair_agreement",
    "min_provider_independent_recall",
    "max_provider_close_difference_bps",
    "min_window_coverage_pct",
    "max_discovery_tick_gap_seconds",
    "max_discovery_processing_latency_seconds",
    "max_discovery_scheduled_lag_seconds",
    "allowed_data_feeds",
    "allowed_primary_market_data_providers",
    "allowed_independent_market_data_providers",
    "allowed_independent_datasets",
    "allowed_audit_versions",
    "allowed_discovery_versions",
    "allowed_audit_code_versions",
    "allowed_observer_code_versions",
    "allowed_census_code_versions",
    "allowed_provider_proof_code_versions",
    "allowed_empirical_code_versions",
    "allowed_calibration_code_versions",
    "allowed_control_code_versions",
    "require_zero_dirty_sessions",
    "require_complete_session_inventory",
    "require_zero_unavailable_symbols",
    "require_zero_provider_price_disagreements",
    "require_zero_ambiguous_labels",
    "require_zero_direction_mismatches",
    "require_zero_duplicate_candidates",
}
SESSION_ARTIFACT_FIELDS = {"session", "path", "sha256"}
SINGLE_ARTIFACT_FIELDS = {"path", "sha256"}
CONTROL_ARTIFACT_FIELDS = {
    "kind",
    "path",
    "sha256",
    "revision",
    "completed_at_utc",
}
DISCOVERY_AUDIT_FIELDS = {
    "audit_version",
    "audit_code_version",
    "session",
    "database",
    "operational_clean",
    "session_evidence_eligible",
    "operational",
    "candidates",
    "near_miss_symbols",
    "issues",
}
CENSUS_FIELDS = {
    "report_version",
    "census_id",
    "session",
    "attempt",
    "code_version",
    "operational_complete",
    "evidence_eligible",
    "metrics",
    "false_negatives",
    "false_positives",
    "unavailable",
    "issue_codes",
}
CENSUS_METRIC_FIELDS = {
    "universe_symbols",
    "requested_chunks",
    "fetched_symbols",
    "evaluated_symbols",
    "unavailable_symbols",
    "stage1_seen_symbols",
    "stage1_candidate_pairs",
    "eligible_pairs",
    "true_positive_pairs",
    "false_negative_pairs",
    "false_positive_pairs",
    "recall",
    "average_detection_delay_seconds",
    "max_detection_delay_seconds",
    "provider_comparison_status",
}
PROVIDER_FIELDS = {
    "report_version",
    "comparison_id",
    "census_id",
    "session",
    "attempt",
    "code_version",
    "operational_complete",
    "evidence_eligible",
    "source",
    "metrics",
    "independent_misses",
    "provider_disagreements",
    "issue_codes",
}
PROVIDER_SOURCE_FIELDS = {
    "primary_provider",
    "primary_feed",
    "independent_provider",
    "independent_feed",
    "independent_dataset",
    "object_key",
    "object_etag",
    "object_last_modified_utc",
    "selected_rows_sha256",
}
PROVIDER_METRIC_FIELDS = {
    "universe_symbols",
    "primary_evaluated_symbols",
    "independent_evaluated_symbols",
    "comparable_symbols",
    "comparable_coverage",
    "primary_eligible_pairs",
    "independent_eligible_pairs",
    "agreed_eligible_pairs",
    "eligible_pair_agreement",
    "independent_true_positive_pairs",
    "independent_false_negative_pairs",
    "independent_false_positive_pairs",
    "independent_recall",
    "primary_comparison_bars",
    "independent_comparison_bars",
    "compared_bars",
    "bar_overlap_coverage",
    "price_disagreement_bars",
    "max_abs_close_difference_bps",
}
EMPIRICAL_ARTIFACT_FIELDS = {
    "schema_version",
    "artifact_type",
    "empirical_run_id",
    "experiment_id",
    "split",
    "evaluated_at_utc",
    "code_version",
    "input_digest_sha256",
    "report_sha256",
    "experiment_manifest_sha256",
    "report",
}
EMPIRICAL_REPORT_FIELDS = {
    "empirical_version",
    "experiment_id",
    "split",
    "rank_version",
    "sessions",
    "eligibility_rule",
    "selection_rule",
    "policy",
    "baseline",
    "candidate_rank",
    "session_metrics",
    "precision_delta",
    "recall_delta",
    "passed_locked_policy",
    "blocking_reasons",
    "holdout_unblinded",
    "input_digest_sha256",
}
CLASSIFICATION_FIELDS = {
    "definitive_labels",
    "ambiguous_labels",
    "positive_labels",
    "selected",
    "true_positives",
    "false_positives",
    "true_negatives",
    "false_negatives",
    "direction_mismatches",
    "precision",
    "recall",
}
EMPIRICAL_POLICY_FIELDS = {
    "min_precision",
    "min_recall",
    "min_definitive_labels",
    "min_positive_labels",
}
CALIBRATION_ARTIFACT_FIELDS = {
    "schema_version",
    "artifact_type",
    "calibration_run_id",
    "experiment_id",
    "split",
    "evaluated_at_utc",
    "code_version",
    "input_digest_sha256",
    "report_sha256",
    "model_sha256",
    "report",
}
CALIBRATION_REPORT_FIELDS = {
    "calibration_version",
    "calibration_id",
    "experiment_id",
    "split",
    "sessions",
    "code_version",
    "model_sha256",
    "development_input_sha256",
    "policy",
    "definitive_labels",
    "positive_labels",
    "negative_labels",
    "ambiguous_labels",
    "unmatched_rank_labels",
    "brier_score",
    "expected_calibration_error",
    "reliability_bins",
    "holdout_unblinded",
    "calibrated_quality_claim_valid",
    "blocking_reasons",
    "input_digest_sha256",
}
CALIBRATION_POLICY_FIELDS = {
    "min_training_labels",
    "min_training_positive_labels",
    "min_training_negative_labels",
    "min_holdout_labels",
    "min_holdout_positive_labels",
    "min_holdout_negative_labels",
    "minimum_bin_labels",
    "max_brier_score",
    "max_expected_calibration_error",
}
RELIABILITY_BIN_FIELDS = {
    "minimum_score",
    "maximum_score",
    "predicted_quality",
    "labels",
    "positives",
    "observed_quality",
    "absolute_error",
    "wilson_lower_95",
    "wilson_upper_95",
}
SESSION_METRIC_FIELDS = {
    "session",
    "baseline",
    "candidate_rank",
    "discovered_symbols",
    "rankable_symbols",
    "duplicate_candidate_rows",
}


@dataclass(frozen=True)
class DiscoveryGatePolicy:
    min_clean_sessions: int
    min_definitive_labels: int
    min_positive_labels: int
    min_empirical_recall: float
    min_empirical_precision: float
    min_calibration_negative_labels: int
    min_calibration_bin_labels: int
    max_calibration_brier_score: float
    max_expected_calibration_error: float
    min_primary_recall: float
    max_primary_detection_latency_seconds: float
    min_provider_comparable_coverage: float
    min_provider_bar_overlap_coverage: float
    min_provider_eligible_pair_agreement: float
    min_provider_independent_recall: float
    max_provider_close_difference_bps: float
    min_window_coverage_pct: float
    max_discovery_tick_gap_seconds: float
    max_discovery_processing_latency_seconds: float
    max_discovery_scheduled_lag_seconds: float
    allowed_data_feeds: tuple[str, ...]
    allowed_primary_market_data_providers: tuple[str, ...]
    allowed_independent_market_data_providers: tuple[str, ...]
    allowed_independent_datasets: tuple[str, ...]
    allowed_audit_versions: tuple[int, ...]
    allowed_discovery_versions: tuple[int, ...]
    allowed_audit_code_versions: tuple[str, ...]
    allowed_observer_code_versions: tuple[str, ...]
    allowed_census_code_versions: tuple[str, ...]
    allowed_provider_proof_code_versions: tuple[str, ...]
    allowed_empirical_code_versions: tuple[str, ...]
    allowed_calibration_code_versions: tuple[str, ...]
    allowed_control_code_versions: tuple[str, ...]
    require_zero_dirty_sessions: bool
    require_complete_session_inventory: bool
    require_zero_unavailable_symbols: bool
    require_zero_provider_price_disagreements: bool
    require_zero_ambiguous_labels: bool
    require_zero_direction_mismatches: bool
    require_zero_duplicate_candidates: bool


@dataclass(frozen=True)
class SessionArtifact:
    session: date
    path: Path
    sha256: str


@dataclass(frozen=True)
class SingleArtifact:
    path: Path
    sha256: str


@dataclass(frozen=True)
class CampaignArtifact:
    campaign_id: str
    locked_at_utc: datetime
    experiment_id: str
    experiment_manifest_sha256: str
    rank_version: int
    path: Path
    sha256: str


@dataclass(frozen=True)
class DiscoveryEvidenceManifest:
    evidence_set_version: str
    created_at_utc: datetime
    coverage_start: date
    coverage_end: date
    campaign_artifact: CampaignArtifact
    policy: DiscoveryGatePolicy
    discovery_audits: tuple[SessionArtifact, ...]
    recall_census_reports: tuple[SessionArtifact, ...]
    provider_proof_reports: tuple[SessionArtifact, ...]
    empirical_artifact: SingleArtifact
    calibration_artifact: SingleArtifact
    control_artifacts: tuple[ControlArtifact, ...]


@dataclass(frozen=True)
class DiscoveryAggregateMetrics:
    expected_sessions: int
    clean_sessions: int
    primary_census_sessions: int
    provider_proof_sessions: int
    minimum_window_coverage_pct: float | None
    maximum_tick_gap_seconds: float | None
    maximum_processing_latency_seconds: float | None
    maximum_scheduled_lag_seconds: float | None
    missed_cycles: int
    primary_unavailable_symbols: int
    primary_eligible_pairs: int
    primary_true_positive_pairs: int
    primary_false_negative_pairs: int
    primary_recall: float | None
    maximum_primary_detection_latency_seconds: float | None
    minimum_provider_comparable_coverage: float | None
    minimum_provider_bar_overlap_coverage: float | None
    minimum_provider_eligible_pair_agreement: float | None
    minimum_provider_independent_recall: float | None
    provider_price_disagreement_bars: int
    maximum_provider_close_difference_bps: float | None
    definitive_labels: int
    ambiguous_labels: int
    positive_labels: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    empirical_precision: float | None
    empirical_recall: float | None
    direction_mismatches: int
    duplicate_candidate_rows: int
    calibration_brier_score: float | None
    calibration_expected_calibration_error: float | None
    calibration_reliability_bins: int
    audit_versions: tuple[int, ...]
    discovery_versions: tuple[int, ...]
    audit_code_versions: tuple[str, ...]
    observer_code_versions: tuple[str, ...]
    census_code_versions: tuple[str, ...]
    provider_proof_code_versions: tuple[str, ...]
    empirical_code_versions: tuple[str, ...]
    calibration_code_versions: tuple[str, ...]
    control_code_versions: tuple[str, ...]
    data_feeds: tuple[str, ...]
    primary_market_data_providers: tuple[str, ...]
    independent_market_data_providers: tuple[str, ...]
    independent_datasets: tuple[str, ...]


@dataclass(frozen=True)
class GateCheck:
    code: str
    passed: bool
    observed: Any
    required: Any


@dataclass(frozen=True)
class DiscoveryEvidenceGateReport:
    schema_version: int
    evidence_set_version: str
    verdict: str
    coverage_start: str
    coverage_end: str
    campaign_id: str
    campaign_locked_at_utc: str
    campaign_sha256: str
    experiment_id: str
    experiment_manifest_sha256: str
    rank_version: int
    policy: DiscoveryGatePolicy
    metrics: DiscoveryAggregateMetrics
    checks: tuple[GateCheck, ...]
    artifact_digests: tuple[dict[str, str], ...]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _wilson_interval(positives: int, labels: int) -> tuple[float, float]:
    observed = positives / labels
    z = 1.96
    denominator = 1 + z**2 / labels
    center = (observed + z**2 / (2 * labels)) / denominator
    margin = (
        z
        * math.sqrt(
            (observed * (1 - observed) + z**2 / (4 * labels)) / labels
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _ratio_floor(raw: Any, context: str, *, minimum: float) -> float:
    value = _finite_number(raw, context)
    if not minimum <= value <= 1:
        raise ValueError(f"{context} must be between {minimum} and 1")
    return value


def _positive_limit(raw: Any, context: str, *, maximum: float) -> float:
    value = _finite_number(raw, context)
    if not 0 < value <= maximum:
        raise ValueError(f"{context} must be in (0,{maximum}]")
    return value


def _parse_policy(raw: Any) -> DiscoveryGatePolicy:
    if not isinstance(raw, dict):
        raise ValueError("policy must be an object")
    _exact_fields(raw, POLICY_FIELDS, "policy")
    strict_flags = {
        key: _strict_bool(_required(raw, key, "policy"), f"policy.{key}")
        for key in (
            "require_zero_dirty_sessions",
            "require_complete_session_inventory",
            "require_zero_unavailable_symbols",
            "require_zero_provider_price_disagreements",
            "require_zero_ambiguous_labels",
            "require_zero_direction_mismatches",
            "require_zero_duplicate_candidates",
        )
    }
    if not all(strict_flags.values()):
        raise ValueError("required fail-closed discovery policy booleans must all be true")
    min_definitive = _positive_int(
        raw["min_definitive_labels"], "policy.min_definitive_labels"
    )
    min_positive = _positive_int(raw["min_positive_labels"], "policy.min_positive_labels")
    if min_positive > min_definitive:
        raise ValueError("policy.min_positive_labels cannot exceed min_definitive_labels")
    return DiscoveryGatePolicy(
        min_clean_sessions=_positive_int(
            raw["min_clean_sessions"], "policy.min_clean_sessions", minimum=10
        ),
        min_definitive_labels=min_definitive,
        min_positive_labels=min_positive,
        min_empirical_recall=_ratio_floor(
            raw["min_empirical_recall"], "policy.min_empirical_recall", minimum=0.95
        ),
        min_empirical_precision=_ratio_floor(
            raw["min_empirical_precision"], "policy.min_empirical_precision", minimum=0.01
        ),
        min_calibration_negative_labels=_positive_int(
            raw["min_calibration_negative_labels"],
            "policy.min_calibration_negative_labels",
        ),
        min_calibration_bin_labels=_positive_int(
            raw["min_calibration_bin_labels"], "policy.min_calibration_bin_labels"
        ),
        max_calibration_brier_score=_positive_limit(
            raw["max_calibration_brier_score"],
            "policy.max_calibration_brier_score",
            maximum=0.25,
        ),
        max_expected_calibration_error=_positive_limit(
            raw["max_expected_calibration_error"],
            "policy.max_expected_calibration_error",
            maximum=0.25,
        ),
        min_primary_recall=_ratio_floor(
            raw["min_primary_recall"], "policy.min_primary_recall", minimum=0.95
        ),
        max_primary_detection_latency_seconds=_positive_limit(
            raw["max_primary_detection_latency_seconds"],
            "policy.max_primary_detection_latency_seconds",
            maximum=330,
        ),
        min_provider_comparable_coverage=_ratio_floor(
            raw["min_provider_comparable_coverage"],
            "policy.min_provider_comparable_coverage",
            minimum=0.99,
        ),
        min_provider_bar_overlap_coverage=_ratio_floor(
            raw["min_provider_bar_overlap_coverage"],
            "policy.min_provider_bar_overlap_coverage",
            minimum=0.95,
        ),
        min_provider_eligible_pair_agreement=_ratio_floor(
            raw["min_provider_eligible_pair_agreement"],
            "policy.min_provider_eligible_pair_agreement",
            minimum=0.95,
        ),
        min_provider_independent_recall=_ratio_floor(
            raw["min_provider_independent_recall"],
            "policy.min_provider_independent_recall",
            minimum=0.95,
        ),
        max_provider_close_difference_bps=_positive_limit(
            raw["max_provider_close_difference_bps"],
            "policy.max_provider_close_difference_bps",
            maximum=50,
        ),
        min_window_coverage_pct=_ratio_floor(
            _finite_number(raw["min_window_coverage_pct"], "policy.min_window_coverage_pct")
            / 100,
            "policy.min_window_coverage_pct/100",
            minimum=0.99,
        )
        * 100,
        max_discovery_tick_gap_seconds=_positive_limit(
            raw["max_discovery_tick_gap_seconds"],
            "policy.max_discovery_tick_gap_seconds",
            maximum=150,
        ),
        max_discovery_processing_latency_seconds=_positive_limit(
            raw["max_discovery_processing_latency_seconds"],
            "policy.max_discovery_processing_latency_seconds",
            maximum=30,
        ),
        max_discovery_scheduled_lag_seconds=_positive_limit(
            raw["max_discovery_scheduled_lag_seconds"],
            "policy.max_discovery_scheduled_lag_seconds",
            maximum=30,
        ),
        allowed_data_feeds=_string_list(raw["allowed_data_feeds"], "policy.allowed_data_feeds"),
        allowed_primary_market_data_providers=_string_list(
            raw["allowed_primary_market_data_providers"],
            "policy.allowed_primary_market_data_providers",
        ),
        allowed_independent_market_data_providers=_string_list(
            raw["allowed_independent_market_data_providers"],
            "policy.allowed_independent_market_data_providers",
        ),
        allowed_independent_datasets=_string_list(
            raw["allowed_independent_datasets"], "policy.allowed_independent_datasets"
        ),
        allowed_audit_versions=_positive_int_list(
            raw["allowed_audit_versions"], "policy.allowed_audit_versions"
        ),
        allowed_discovery_versions=_positive_int_list(
            raw["allowed_discovery_versions"], "policy.allowed_discovery_versions"
        ),
        allowed_audit_code_versions=_string_list(
            raw["allowed_audit_code_versions"], "policy.allowed_audit_code_versions"
        ),
        allowed_observer_code_versions=_string_list(
            raw["allowed_observer_code_versions"], "policy.allowed_observer_code_versions"
        ),
        allowed_census_code_versions=_string_list(
            raw["allowed_census_code_versions"], "policy.allowed_census_code_versions"
        ),
        allowed_provider_proof_code_versions=_string_list(
            raw["allowed_provider_proof_code_versions"],
            "policy.allowed_provider_proof_code_versions",
        ),
        allowed_empirical_code_versions=_string_list(
            raw["allowed_empirical_code_versions"], "policy.allowed_empirical_code_versions"
        ),
        allowed_calibration_code_versions=_string_list(
            raw["allowed_calibration_code_versions"],
            "policy.allowed_calibration_code_versions",
        ),
        allowed_control_code_versions=_string_list(
            raw["allowed_control_code_versions"], "policy.allowed_control_code_versions"
        ),
        **strict_flags,
    )


def _parse_session_artifacts(raw: Any, root: Path, context: str) -> tuple[SessionArtifact, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{context} must be a non-empty list")
    artifacts = []
    for index, item in enumerate(raw):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_context} must be an object")
        _exact_fields(item, SESSION_ARTIFACT_FIELDS, item_context)
        artifacts.append(
            SessionArtifact(
                _iso_date(item["session"], f"{item_context}.session"),
                _relative_artifact_path(root, item["path"], f"{item_context}.path"),
                _sha256(item["sha256"], f"{item_context}.sha256"),
            )
        )
    sessions = [artifact.session for artifact in artifacts]
    if len(sessions) != len(set(sessions)):
        raise ValueError(f"{context} session dates must be unique")
    return tuple(artifacts)


def _parse_single_artifact(raw: Any, root: Path, context: str) -> SingleArtifact:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _exact_fields(raw, SINGLE_ARTIFACT_FIELDS, context)
    return SingleArtifact(
        _relative_artifact_path(root, raw["path"], f"{context}.path"),
        _sha256(raw["sha256"], f"{context}.sha256"),
    )


def _parse_control_artifacts(raw: Any, root: Path) -> tuple[ControlArtifact, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("control_artifacts must be a non-empty list")
    artifacts = []
    for index, item in enumerate(raw):
        context = f"control_artifacts[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{context} must be an object")
        _exact_fields(item, CONTROL_ARTIFACT_FIELDS, context)
        kind = _nonempty_string(item["kind"], f"{context}.kind")
        if kind not in REQUIRED_CONTROL_KINDS:
            raise ValueError(f"{context}.kind is not a required discovery control")
        artifacts.append(
            ControlArtifact(
                kind,
                _relative_artifact_path(root, item["path"], f"{context}.path"),
                _sha256(item["sha256"], f"{context}.sha256"),
                _nonempty_string(item["revision"], f"{context}.revision"),
                _aware_datetime(item["completed_at_utc"], f"{context}.completed_at_utc"),
            )
        )
    kinds = [artifact.kind for artifact in artifacts]
    if len(kinds) != len(set(kinds)):
        raise ValueError("control_artifact kinds must be unique")
    return tuple(artifacts)


def _proof_available_at(session: date) -> datetime:
    next_day = date.fromordinal(session.toordinal() + 1)
    return datetime.combine(next_day, time(13, 5), tzinfo=ET).astimezone(timezone.utc)


def _parse_campaign(
    raw: Any,
    *,
    root: Path,
    coverage_start: date,
    coverage_end: date,
    policy: DiscoveryGatePolicy,
) -> CampaignArtifact:
    artifact = _parse_single_artifact(raw, root, "campaign_artifact")
    payload = json.loads(_verify_digest(artifact.path, artifact.sha256, "campaign artifact"))
    if not isinstance(payload, dict):
        raise ValueError("campaign root must be an object")
    _exact_fields(payload, CAMPAIGN_FIELDS, "campaign")
    if payload["schema_version"] != CAMPAIGN_SCHEMA_VERSION or isinstance(
        payload["schema_version"], bool
    ):
        raise ValueError(
            f"campaign schema_version must be {CAMPAIGN_SCHEMA_VERSION}"
        )
    if payload["status"] != "locked":
        raise ValueError("campaign status must be locked")
    campaign_start = _iso_date(payload["coverage_start"], "campaign.coverage_start")
    campaign_end = _iso_date(payload["coverage_end"], "campaign.coverage_end")
    if (campaign_start, campaign_end) != (coverage_start, coverage_end):
        raise ValueError("campaign coverage does not match evidence set")
    if _parse_policy(payload["policy"]) != policy:
        raise ValueError("campaign policy does not match evidence set")
    sessions = _expected_sessions(campaign_start, campaign_end)
    if len(sessions) < policy.min_clean_sessions:
        raise ValueError("campaign contains fewer sessions than min_clean_sessions")
    locked_at = _aware_datetime(payload["locked_at_utc"], "campaign.locked_at_utc")
    first_open = CALENDAR.session_open(sessions[0]).to_pydatetime().astimezone(timezone.utc)
    if locked_at >= first_open:
        raise ValueError("campaign must be locked before its first session opens")
    experiment_digest = _sha256(
        payload["experiment_manifest_sha256"], "campaign.experiment_manifest_sha256"
    )
    return CampaignArtifact(
        _nonempty_string(payload["campaign_id"], "campaign.campaign_id"),
        locked_at,
        _nonempty_string(payload["experiment_id"], "campaign.experiment_id"),
        experiment_digest,
        _positive_int(payload["rank_version"], "campaign.rank_version"),
        artifact.path,
        artifact.sha256,
    )


def load_discovery_evidence_manifest(
    path: Path | str,
) -> DiscoveryEvidenceManifest:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("discovery evidence manifest root must be an object")
    _exact_fields(payload, ROOT_FIELDS, "discovery evidence manifest")
    if payload["schema_version"] != EVIDENCE_SCHEMA_VERSION or isinstance(
        payload["schema_version"], bool
    ):
        raise ValueError(
            f"discovery evidence schema_version must be {EVIDENCE_SCHEMA_VERSION}"
        )
    if payload["status"] != "locked":
        raise ValueError("discovery evidence manifest status must be locked")
    coverage_start = _iso_date(payload["coverage_start"], "evidence.coverage_start")
    coverage_end = _iso_date(payload["coverage_end"], "evidence.coverage_end")
    expected = _expected_sessions(coverage_start, coverage_end)
    if not expected:
        raise ValueError("discovery evidence coverage contains no XNYS sessions")
    created_at = _aware_datetime(payload["created_at_utc"], "evidence.created_at_utc")
    if created_at <= _proof_available_at(expected[-1]):
        raise ValueError("evidence manifest predates the last required provider proof")
    policy = _parse_policy(payload["policy"])
    root = manifest_path.parent
    audits = _parse_session_artifacts(payload["discovery_audits"], root, "discovery_audits")
    censuses = _parse_session_artifacts(
        payload["recall_census_reports"], root, "recall_census_reports"
    )
    providers = _parse_session_artifacts(
        payload["provider_proof_reports"], root, "provider_proof_reports"
    )
    empirical = _parse_single_artifact(payload["empirical_artifact"], root, "empirical_artifact")
    calibration = _parse_single_artifact(
        payload["calibration_artifact"], root, "calibration_artifact"
    )
    controls = _parse_control_artifacts(payload["control_artifacts"], root)
    if any(control.completed_at_utc > created_at for control in controls):
        raise ValueError("control artifacts cannot complete after the manifest is locked")
    campaign = _parse_campaign(
        payload["campaign_artifact"],
        root=root,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        policy=policy,
    )
    return DiscoveryEvidenceManifest(
        _nonempty_string(payload["evidence_set_version"], "evidence.evidence_set_version"),
        created_at,
        coverage_start,
        coverage_end,
        campaign,
        policy,
        audits,
        censuses,
        providers,
        empirical,
        calibration,
        controls,
    )


def _load_json(artifact: SessionArtifact | SingleArtifact, context: str) -> dict:
    try:
        payload = json.loads(_verify_digest(artifact.path, artifact.sha256, context))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload


def _nonnegative_number(raw: Any, context: str) -> float:
    value = _finite_number(raw, context)
    if value < 0:
        raise ValueError(f"{context} must be non-negative")
    return value


def _string_array(raw: Any, context: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(raw, list) or (not allow_empty and not raw):
        raise ValueError(f"{context} must be a list")
    values = tuple(_nonempty_string(value, context) for value in raw)
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must not contain duplicates")
    return values


def _load_audit(artifact: SessionArtifact) -> dict:
    context = f"discovery audit {artifact.session}"
    payload = _load_json(artifact, context)
    _exact_fields(payload, DISCOVERY_AUDIT_FIELDS, context)
    if payload["session"] != artifact.session.isoformat():
        raise ValueError(f"{context} session does not match manifest")
    audit_version = _positive_int(payload["audit_version"], f"{context}.audit_version")
    audit_code = _nonempty_string(payload["audit_code_version"], f"{context}.audit_code_version")
    if audit_code == "unknown":
        raise ValueError(f"{context} audit code_version is unknown")
    clean = _strict_bool(payload["operational_clean"], f"{context}.operational_clean")
    eligible = _strict_bool(
        payload["session_evidence_eligible"], f"{context}.session_evidence_eligible"
    )
    operational = payload["operational"]
    issues = payload["issues"]
    if not isinstance(operational, dict):
        raise ValueError(f"{context}.operational must be an object")
    if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
        raise ValueError(f"{context}.issues must be a list of objects")
    if not isinstance(payload["candidates"], list) or not isinstance(
        payload["near_miss_symbols"], list
    ):
        raise ValueError(f"{context} candidate and near-miss inventories must be lists")
    required = {
        "window_coverage_pct",
        "max_tick_gap_seconds",
        "fetch_errors",
        "failed_invariants",
        "max_latency_ms",
        "max_scheduled_lag_ms",
        "missed_cycles",
        "discovery_versions",
        "code_versions",
        "data_feeds",
        "market_data_providers",
    }
    if not required <= operational.keys():
        raise ValueError(f"{context}.operational is missing gate metrics")
    metrics = {
        "audit_version": audit_version,
        "audit_code_version": audit_code,
        "operational_clean": clean,
        "session_evidence_eligible": eligible,
        "window_coverage_pct": _nonnegative_number(
            operational["window_coverage_pct"], f"{context}.window_coverage_pct"
        ),
        "max_tick_gap_seconds": _nonnegative_number(
            operational["max_tick_gap_seconds"], f"{context}.max_tick_gap_seconds"
        ),
        "fetch_errors": _nonnegative_int(operational["fetch_errors"], f"{context}.fetch_errors"),
        "failed_invariants": _nonnegative_int(
            operational["failed_invariants"], f"{context}.failed_invariants"
        ),
        "max_latency_seconds": _nonnegative_number(
            operational["max_latency_ms"], f"{context}.max_latency_ms"
        )
        / 1000,
        "max_scheduled_lag_seconds": _nonnegative_number(
            operational["max_scheduled_lag_ms"], f"{context}.max_scheduled_lag_ms"
        )
        / 1000,
        "missed_cycles": _nonnegative_int(
            operational["missed_cycles"], f"{context}.missed_cycles"
        ),
        "discovery_versions": tuple(
            _positive_int(value, f"{context}.discovery_versions")
            for value in operational["discovery_versions"]
        ),
        "code_versions": _string_array(
            operational["code_versions"], f"{context}.code_versions", allow_empty=False
        ),
        "data_feeds": _string_array(
            operational["data_feeds"], f"{context}.data_feeds", allow_empty=False
        ),
        "providers": _string_array(
            operational["market_data_providers"],
            f"{context}.market_data_providers",
            allow_empty=False,
        ),
        "issues": issues,
    }
    blocker_issues = [item for item in issues if item.get("severity") == "blocker"]
    expected_clean = (
        not blocker_issues
        and metrics["fetch_errors"] == 0
        and metrics["failed_invariants"] == 0
    )
    if clean != expected_clean or eligible != (clean and audit_code != "unknown"):
        raise ValueError(f"{context} clean/eligible verdict contradicts evidence")
    return metrics


def _load_census(artifact: SessionArtifact) -> dict:
    context = f"recall census {artifact.session}"
    payload = _load_json(artifact, context)
    _exact_fields(payload, CENSUS_FIELDS, context)
    if payload["session"] != artifact.session.isoformat():
        raise ValueError(f"{context} session does not match manifest")
    if payload["report_version"] != payload["attempt"]:
        raise ValueError(f"{context} report_version must match attempt")
    metrics = payload["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError(f"{context}.metrics must be an object")
    _exact_fields(metrics, CENSUS_METRIC_FIELDS, f"{context}.metrics")
    for key in ("false_negatives", "false_positives", "unavailable"):
        if not isinstance(payload[key], list) or any(
            not isinstance(item, dict) for item in payload[key]
        ):
            raise ValueError(f"{context}.{key} must be a list of objects")
    counts = {
        key: _nonnegative_int(metrics[key], f"{context}.{key}")
        for key in (
            "universe_symbols",
            "requested_chunks",
            "fetched_symbols",
            "evaluated_symbols",
            "unavailable_symbols",
            "stage1_seen_symbols",
            "stage1_candidate_pairs",
            "eligible_pairs",
            "true_positive_pairs",
            "false_negative_pairs",
            "false_positive_pairs",
        )
    }
    recall = _optional_ratio(metrics["recall"], f"{context}.recall")
    expected_recall = _ratio(counts["true_positive_pairs"], counts["eligible_pairs"])
    if counts["true_positive_pairs"] + counts["false_negative_pairs"] != counts[
        "eligible_pairs"
    ]:
        raise ValueError(f"{context} eligible-pair counts do not conserve")
    if counts["true_positive_pairs"] + counts["false_positive_pairs"] != counts[
        "stage1_candidate_pairs"
    ]:
        raise ValueError(f"{context} Stage-1 pair counts do not conserve")
    if not _same_number(recall, expected_recall):
        raise ValueError(f"{context} recall does not match counts")
    if counts["fetched_symbols"] != counts["evaluated_symbols"]:
        raise ValueError(f"{context} fetched/evaluated counts disagree")
    if counts["evaluated_symbols"] + counts["unavailable_symbols"] != counts[
        "universe_symbols"
    ]:
        raise ValueError(f"{context} universe counts do not conserve")
    false_negative_pairs = sum(
        len(_string_array(item.get("directions"), f"{context}.false_negatives.directions"))
        for item in payload["false_negatives"]
    )
    false_positive_pairs = sum(
        len(_string_array(item.get("directions"), f"{context}.false_positives.directions"))
        for item in payload["false_positives"]
    )
    if false_negative_pairs != counts["false_negative_pairs"]:
        raise ValueError(f"{context} false-negative detail does not match counts")
    if false_positive_pairs != counts["false_positive_pairs"]:
        raise ValueError(f"{context} false-positive detail does not match counts")
    if not isinstance(payload["unavailable"], list) or len(payload["unavailable"]) != counts[
        "unavailable_symbols"
    ]:
        raise ValueError(f"{context} unavailable detail does not match counts")
    issues = set(_string_array(payload["issue_codes"], f"{context}.issue_codes"))
    operational = _strict_bool(
        payload["operational_complete"], f"{context}.operational_complete"
    )
    eligible = _strict_bool(payload["evidence_eligible"], f"{context}.evidence_eligible")
    if operational and issues == PRIMARY_CENSUS_RECONCILED_ISSUES:
        if eligible:
            raise ValueError(f"{context} cannot be independently eligible without provider proof")
    elif eligible != (operational and not issues):
        raise ValueError(f"{context} eligibility contradicts issue codes")
    return {
        "census_id": _positive_int(payload["census_id"], f"{context}.census_id"),
        "code_version": _nonempty_string(payload["code_version"], f"{context}.code_version"),
        "operational_complete": operational,
        "evidence_eligible": eligible,
        "issue_codes": issues,
        **counts,
        "recall": recall,
        "max_detection_latency_seconds": (
            None
            if metrics["max_detection_delay_seconds"] is None
            else _nonnegative_number(
                metrics["max_detection_delay_seconds"],
                f"{context}.max_detection_delay_seconds",
            )
        ),
    }


def _load_provider(artifact: SessionArtifact) -> dict:
    context = f"provider proof {artifact.session}"
    payload = _load_json(artifact, context)
    _exact_fields(payload, PROVIDER_FIELDS, context)
    if payload["session"] != artifact.session.isoformat():
        raise ValueError(f"{context} session does not match manifest")
    source = payload["source"]
    metrics = payload["metrics"]
    if not isinstance(source, dict) or not isinstance(metrics, dict):
        raise ValueError(f"{context} source and metrics must be objects")
    _exact_fields(source, PROVIDER_SOURCE_FIELDS, f"{context}.source")
    _exact_fields(metrics, PROVIDER_METRIC_FIELDS, f"{context}.metrics")
    _positive_int(payload["report_version"], f"{context}.report_version")
    _positive_int(payload["comparison_id"], f"{context}.comparison_id")
    _positive_int(payload["attempt"], f"{context}.attempt")
    for key in ("independent_misses", "provider_disagreements"):
        if not isinstance(payload[key], list) or any(
            not isinstance(item, dict) for item in payload[key]
        ):
            raise ValueError(f"{context}.{key} must be a list of objects")
    counts = {
        key: _nonnegative_int(metrics[key], f"{context}.{key}")
        for key in (
            "universe_symbols",
            "primary_evaluated_symbols",
            "independent_evaluated_symbols",
            "comparable_symbols",
            "primary_eligible_pairs",
            "independent_eligible_pairs",
            "agreed_eligible_pairs",
            "independent_true_positive_pairs",
            "independent_false_negative_pairs",
            "independent_false_positive_pairs",
            "primary_comparison_bars",
            "independent_comparison_bars",
            "compared_bars",
            "price_disagreement_bars",
        )
    }
    ratios = {
        key: _optional_ratio(metrics[key], f"{context}.{key}")
        for key in (
            "comparable_coverage",
            "eligible_pair_agreement",
            "independent_recall",
            "bar_overlap_coverage",
        )
    }
    expected_coverage = _ratio(counts["comparable_symbols"], counts["universe_symbols"])
    union_bars = (
        counts["primary_comparison_bars"]
        + counts["independent_comparison_bars"]
        - counts["compared_bars"]
    )
    expected_overlap = _ratio(counts["compared_bars"], union_bars)
    expected_recall = _ratio(
        counts["independent_true_positive_pairs"], counts["independent_eligible_pairs"]
    )
    union_pairs = (
        counts["primary_eligible_pairs"]
        + counts["independent_eligible_pairs"]
        - counts["agreed_eligible_pairs"]
    )
    expected_agreement = _ratio(counts["agreed_eligible_pairs"], union_pairs)
    if not _same_number(ratios["comparable_coverage"], expected_coverage):
        raise ValueError(f"{context} comparable coverage does not match counts")
    if not _same_number(ratios["bar_overlap_coverage"], expected_overlap):
        raise ValueError(f"{context} bar overlap does not match counts")
    if not _same_number(ratios["independent_recall"], expected_recall):
        raise ValueError(f"{context} independent recall does not match counts")
    if not _same_number(ratios["eligible_pair_agreement"], expected_agreement):
        raise ValueError(f"{context} eligible-pair agreement does not match counts")
    if counts["independent_true_positive_pairs"] + counts[
        "independent_false_negative_pairs"
    ] != counts["independent_eligible_pairs"]:
        raise ValueError(f"{context} independent eligible pairs do not conserve")
    max_difference = (
        None
        if metrics["max_abs_close_difference_bps"] is None
        else _nonnegative_number(
            metrics["max_abs_close_difference_bps"],
            f"{context}.max_abs_close_difference_bps",
        )
    )
    issues = _string_array(payload["issue_codes"], f"{context}.issue_codes")
    operational = _strict_bool(
        payload["operational_complete"], f"{context}.operational_complete"
    )
    eligible = _strict_bool(payload["evidence_eligible"], f"{context}.evidence_eligible")
    if eligible != (operational and not issues):
        raise ValueError(f"{context} eligibility contradicts issue codes")
    object_modified = _aware_datetime(
        source["object_last_modified_utc"], f"{context}.object_last_modified_utc"
    )
    if object_modified < _proof_available_at(artifact.session) - timedelta(hours=2, minutes=5):
        raise ValueError(f"{context} independent object predates the completed session")
    primary_provider = _nonempty_string(
        source["primary_provider"], f"{context}.primary_provider"
    )
    independent_provider = _nonempty_string(
        source["independent_provider"], f"{context}.independent_provider"
    )
    if primary_provider == independent_provider:
        raise ValueError(f"{context} independent provider must differ from primary")
    _nonempty_string(source["object_key"], f"{context}.object_key")
    _nonempty_string(source["object_etag"], f"{context}.object_etag")
    return {
        "census_id": _positive_int(payload["census_id"], f"{context}.census_id"),
        "code_version": _nonempty_string(payload["code_version"], f"{context}.code_version"),
        "operational_complete": operational,
        "evidence_eligible": eligible,
        "issue_codes": issues,
        "primary_provider": primary_provider,
        "primary_feed": _nonempty_string(source["primary_feed"], f"{context}.primary_feed"),
        "independent_provider": independent_provider,
        "independent_feed": _nonempty_string(
            source["independent_feed"], f"{context}.independent_feed"
        ),
        "independent_dataset": _nonempty_string(
            source["independent_dataset"], f"{context}.independent_dataset"
        ),
        "selected_rows_sha256": _sha256(
            source["selected_rows_sha256"], f"{context}.selected_rows_sha256"
        ),
        "object_last_modified_utc": object_modified,
        **counts,
        **ratios,
        "max_abs_close_difference_bps": max_difference,
    }


def _classification(raw: Any, context: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _exact_fields(raw, CLASSIFICATION_FIELDS, context)
    counts = {
        key: _nonnegative_int(raw[key], f"{context}.{key}")
        for key in CLASSIFICATION_FIELDS
        if key not in {"precision", "recall"}
    }
    if sum(counts[key] for key in (
        "true_positives", "false_positives", "true_negatives", "false_negatives"
    )) != counts["definitive_labels"]:
        raise ValueError(f"{context} confusion matrix does not conserve")
    if counts["positive_labels"] != counts["true_positives"] + counts["false_negatives"]:
        raise ValueError(f"{context} positive labels do not conserve")
    precision = _optional_ratio(raw["precision"], f"{context}.precision")
    recall = _optional_ratio(raw["recall"], f"{context}.recall")
    expected_precision = _ratio(
        counts["true_positives"],
        counts["true_positives"] + counts["false_positives"],
    )
    expected_recall = _ratio(
        counts["true_positives"],
        counts["true_positives"] + counts["false_negatives"],
    )
    if not _same_number(precision, expected_precision) or not _same_number(
        recall, expected_recall
    ):
        raise ValueError(f"{context} ratios do not match confusion counts")
    return {**counts, "precision": precision, "recall": recall}


def _load_empirical(
    artifact: SingleArtifact,
    campaign: CampaignArtifact,
    expected_sessions: tuple[date, ...],
    policy: DiscoveryGatePolicy,
) -> dict:
    payload = _load_json(artifact, "empirical artifact")
    _exact_fields(payload, EMPIRICAL_ARTIFACT_FIELDS, "empirical artifact")
    if payload["schema_version"] != 1 or payload["artifact_type"] != "postmarket_rank_empirical":
        raise ValueError("empirical artifact identity is invalid")
    if payload["experiment_id"] != campaign.experiment_id or payload["split"] != "holdout":
        raise ValueError("empirical artifact does not match campaign holdout")
    if payload["experiment_manifest_sha256"] != campaign.experiment_manifest_sha256:
        raise ValueError("empirical experiment manifest digest does not match campaign")
    input_digest = _sha256(payload["input_digest_sha256"], "empirical.input_digest_sha256")
    report_digest = _sha256(payload["report_sha256"], "empirical.report_sha256")
    report = payload["report"]
    if not isinstance(report, dict):
        raise ValueError("empirical report must be an object")
    canonical_report = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical_report.encode()).hexdigest() != report_digest:
        raise ValueError("empirical report digest does not match embedded report")
    _exact_fields(report, EMPIRICAL_REPORT_FIELDS, "empirical report")
    sessions = tuple(_iso_date(value, "empirical report.sessions") for value in report["sessions"])
    if sessions != expected_sessions:
        raise ValueError("empirical holdout sessions do not match exact campaign inventory")
    if (
        report["experiment_id"] != campaign.experiment_id
        or report["split"] != "holdout"
        or report["rank_version"] != campaign.rank_version
        or report["input_digest_sha256"] != input_digest
    ):
        raise ValueError("empirical report identity does not match artifact/campaign")
    if report["holdout_unblinded"] is not True:
        raise ValueError("empirical holdout is not explicitly unblinded")
    empirical_policy = report["policy"]
    if not isinstance(empirical_policy, dict):
        raise ValueError("empirical report policy must be an object")
    _exact_fields(empirical_policy, EMPIRICAL_POLICY_FIELDS, "empirical report.policy")
    expected_policy = {
        "min_precision": policy.min_empirical_precision,
        "min_recall": policy.min_empirical_recall,
        "min_definitive_labels": policy.min_definitive_labels,
        "min_positive_labels": policy.min_positive_labels,
    }
    if empirical_policy != expected_policy:
        raise ValueError("empirical locked policy does not match campaign")
    candidate_rank = _classification(report["candidate_rank"], "empirical candidate_rank")
    _classification(report["baseline"], "empirical baseline")
    session_metrics = report["session_metrics"]
    if not isinstance(session_metrics, list) or len(session_metrics) != len(expected_sessions):
        raise ValueError("empirical session_metrics inventory is incomplete")
    per_session = []
    for index, item in enumerate(session_metrics):
        context = f"empirical session_metrics[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{context} must be an object")
        _exact_fields(item, SESSION_METRIC_FIELDS, context)
        per_session.append(
            {
                "session": _iso_date(item["session"], f"{context}.session"),
                "candidate_rank": _classification(
                    item["candidate_rank"], f"{context}.candidate_rank"
                ),
                "duplicate_candidate_rows": _nonnegative_int(
                    item["duplicate_candidate_rows"], f"{context}.duplicate_candidate_rows"
                ),
            }
        )
        _classification(item["baseline"], f"{context}.baseline")
        _nonnegative_int(item["discovered_symbols"], f"{context}.discovered_symbols")
        _nonnegative_int(item["rankable_symbols"], f"{context}.rankable_symbols")
    if tuple(row["session"] for row in per_session) != expected_sessions:
        raise ValueError("empirical per-session order/inventory does not match campaign")
    aggregate_keys = CLASSIFICATION_FIELDS - {"precision", "recall"}
    for key in aggregate_keys:
        if sum(row["candidate_rank"][key] for row in per_session) != candidate_rank[key]:
            raise ValueError(f"empirical per-session {key} does not match aggregate")
    passed = _strict_bool(report["passed_locked_policy"], "empirical.passed_locked_policy")
    blockers = _string_array(report["blocking_reasons"], "empirical.blocking_reasons")
    expected_pass = (
        candidate_rank["definitive_labels"] >= policy.min_definitive_labels
        and candidate_rank["positive_labels"] >= policy.min_positive_labels
        and candidate_rank["ambiguous_labels"] == 0
        and candidate_rank["direction_mismatches"] == 0
        and candidate_rank["precision"] is not None
        and candidate_rank["precision"] >= policy.min_empirical_precision
        and candidate_rank["recall"] is not None
        and candidate_rank["recall"] >= policy.min_empirical_recall
    )
    if passed != (expected_pass and not blockers):
        raise ValueError("empirical pass verdict contradicts metrics/blockers")
    evaluated_at = _aware_datetime(payload["evaluated_at_utc"], "empirical.evaluated_at_utc")
    last_end = datetime.combine(expected_sessions[-1], time(20, 0), tzinfo=ET).astimezone(
        timezone.utc
    )
    if evaluated_at <= last_end:
        raise ValueError("empirical artifact was evaluated before holdout coverage ended")
    return {
        "code_version": _nonempty_string(payload["code_version"], "empirical.code_version"),
        "candidate_rank": candidate_rank,
        "duplicate_candidate_rows": sum(
            row["duplicate_candidate_rows"] for row in per_session
        ),
        "passed_locked_policy": passed,
        "blocking_reasons": blockers,
    }


def _load_calibration(
    artifact: SingleArtifact,
    campaign: CampaignArtifact,
    expected_sessions: tuple[date, ...],
    policy: DiscoveryGatePolicy,
) -> dict:
    payload = _load_json(artifact, "calibration artifact")
    _exact_fields(payload, CALIBRATION_ARTIFACT_FIELDS, "calibration artifact")
    if (
        payload["schema_version"] != 1
        or isinstance(payload["schema_version"], bool)
        or payload["artifact_type"] != "postmarket_rank_calibration"
    ):
        raise ValueError("calibration artifact identity is invalid")
    if payload["experiment_id"] != campaign.experiment_id or payload["split"] != "holdout":
        raise ValueError("calibration artifact does not match campaign holdout")
    input_digest = _sha256(
        payload["input_digest_sha256"], "calibration.input_digest_sha256"
    )
    report_digest = _sha256(payload["report_sha256"], "calibration.report_sha256")
    model_digest = _sha256(payload["model_sha256"], "calibration.model_sha256")
    report = payload["report"]
    if not isinstance(report, dict):
        raise ValueError("calibration report must be an object")
    canonical_report = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical_report.encode()).hexdigest() != report_digest:
        raise ValueError("calibration report digest does not match embedded report")
    _exact_fields(report, CALIBRATION_REPORT_FIELDS, "calibration report")
    sessions = tuple(
        _iso_date(value, "calibration report.sessions") for value in report["sessions"]
    )
    if sessions != expected_sessions:
        raise ValueError("calibration holdout sessions do not match exact campaign inventory")
    if (
        report["experiment_id"] != campaign.experiment_id
        or report["split"] != "holdout"
        or report["input_digest_sha256"] != input_digest
        or report["model_sha256"] != model_digest
        or report["code_version"] != payload["code_version"]
    ):
        raise ValueError("calibration report identity does not match artifact/campaign")
    _positive_int(report["calibration_id"], "calibration.calibration_id")
    _positive_int(report["calibration_version"], "calibration.calibration_version")
    _sha256(
        report["development_input_sha256"], "calibration.development_input_sha256"
    )
    calibration_policy = report["policy"]
    if not isinstance(calibration_policy, dict):
        raise ValueError("calibration report policy must be an object")
    _exact_fields(calibration_policy, CALIBRATION_POLICY_FIELDS, "calibration report.policy")
    for key in (
        "min_training_labels",
        "min_training_positive_labels",
        "min_training_negative_labels",
        "min_holdout_labels",
        "min_holdout_positive_labels",
        "min_holdout_negative_labels",
        "minimum_bin_labels",
    ):
        _positive_int(calibration_policy[key], f"calibration policy.{key}")
    max_brier = _optional_ratio(
        calibration_policy["max_brier_score"], "calibration policy.max_brier_score"
    )
    max_ece = _optional_ratio(
        calibration_policy["max_expected_calibration_error"],
        "calibration policy.max_expected_calibration_error",
    )
    expected_policy = {
        "min_holdout_labels": policy.min_definitive_labels,
        "min_holdout_positive_labels": policy.min_positive_labels,
        "min_holdout_negative_labels": policy.min_calibration_negative_labels,
        "minimum_bin_labels": policy.min_calibration_bin_labels,
        "max_brier_score": policy.max_calibration_brier_score,
        "max_expected_calibration_error": policy.max_expected_calibration_error,
    }
    if any(calibration_policy[key] != value for key, value in expected_policy.items()):
        raise ValueError("calibration locked policy does not match campaign")
    definitive = _nonnegative_int(
        report["definitive_labels"], "calibration.definitive_labels"
    )
    positives = _nonnegative_int(report["positive_labels"], "calibration.positive_labels")
    negatives = _nonnegative_int(report["negative_labels"], "calibration.negative_labels")
    ambiguous = _nonnegative_int(
        report["ambiguous_labels"], "calibration.ambiguous_labels"
    )
    unmatched = _nonnegative_int(
        report["unmatched_rank_labels"], "calibration.unmatched_rank_labels"
    )
    if positives + negatives != definitive:
        raise ValueError("calibration definitive labels do not conserve")
    brier = _optional_ratio(report["brier_score"], "calibration.brier_score")
    ece = _optional_ratio(
        report["expected_calibration_error"],
        "calibration.expected_calibration_error",
    )
    bins_raw = report["reliability_bins"]
    if not isinstance(bins_raw, list):
        raise ValueError("calibration reliability_bins must be a list")
    bins = []
    for index, raw in enumerate(bins_raw):
        context = f"calibration reliability_bins[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{context} must be an object")
        _exact_fields(raw, RELIABILITY_BIN_FIELDS, context)
        minimum_score = _finite_number(raw["minimum_score"], f"{context}.minimum_score")
        maximum_score = _finite_number(raw["maximum_score"], f"{context}.maximum_score")
        if not 0 <= minimum_score <= maximum_score <= 100:
            raise ValueError(f"{context} score range is invalid")
        labels = _positive_int(raw["labels"], f"{context}.labels")
        bin_positives = _nonnegative_int(raw["positives"], f"{context}.positives")
        if bin_positives > labels:
            raise ValueError(f"{context} positives exceed labels")
        predicted = _optional_ratio(raw["predicted_quality"], f"{context}.predicted_quality")
        observed = _optional_ratio(raw["observed_quality"], f"{context}.observed_quality")
        absolute_error = _optional_ratio(raw["absolute_error"], f"{context}.absolute_error")
        lower = _optional_ratio(raw["wilson_lower_95"], f"{context}.wilson_lower_95")
        upper = _optional_ratio(raw["wilson_upper_95"], f"{context}.wilson_upper_95")
        if None in (predicted, observed, absolute_error, lower, upper):
            raise ValueError(f"{context} ratios cannot be null")
        expected_observed = bin_positives / labels
        if not _same_number(observed, expected_observed) or not _same_number(
            absolute_error, abs(predicted - observed)
        ):
            raise ValueError(f"{context} observed quality/error does not match counts")
        expected_lower, expected_upper = _wilson_interval(bin_positives, labels)
        if not _same_number(lower, expected_lower) or not _same_number(
            upper, expected_upper
        ):
            raise ValueError(f"{context} Wilson interval does not match counts")
        bins.append({
            "labels": labels,
            "positives": bin_positives,
            "predicted": predicted,
            "absolute_error": absolute_error,
        })
    if sum(item["labels"] for item in bins) != definitive or sum(
        item["positives"] for item in bins
    ) != positives:
        raise ValueError("calibration reliability bins do not conserve aggregate labels")
    expected_brier = (
        sum(
            item["positives"] * (1 - item["predicted"]) ** 2
            + (item["labels"] - item["positives"]) * item["predicted"] ** 2
            for item in bins
        )
        / definitive
        if definitive else None
    )
    expected_ece = (
        sum(item["labels"] * item["absolute_error"] for item in bins) / definitive
        if definitive else None
    )
    if not _same_number(brier, expected_brier) or not _same_number(ece, expected_ece):
        raise ValueError("calibration Brier/ECE does not match reliability bins")
    holdout_unblinded = _strict_bool(
        report["holdout_unblinded"], "calibration.holdout_unblinded"
    )
    claim = _strict_bool(
        report["calibrated_quality_claim_valid"],
        "calibration.calibrated_quality_claim_valid",
    )
    blockers = _string_array(report["blocking_reasons"], "calibration.blocking_reasons")
    expected_claim = (
        holdout_unblinded
        and definitive >= policy.min_definitive_labels
        and positives >= policy.min_positive_labels
        and negatives >= policy.min_calibration_negative_labels
        and ambiguous == 0
        and unmatched == 0
        and bool(bins)
        and all(item["labels"] >= policy.min_calibration_bin_labels for item in bins)
        and brier is not None
        and brier <= policy.max_calibration_brier_score
        and ece is not None
        and ece <= policy.max_expected_calibration_error
        and not blockers
    )
    if claim != expected_claim:
        raise ValueError("calibration claim contradicts locked policy/metrics/blockers")
    evaluated_at = _aware_datetime(
        payload["evaluated_at_utc"], "calibration.evaluated_at_utc"
    )
    last_end = datetime.combine(expected_sessions[-1], time(20, 0), tzinfo=ET).astimezone(
        timezone.utc
    )
    if evaluated_at <= last_end:
        raise ValueError("calibration artifact was evaluated before holdout coverage ended")
    return {
        "code_version": _nonempty_string(
            payload["code_version"], "calibration.code_version"
        ),
        "claim_valid": claim,
        "blocking_reasons": blockers,
        "definitive_labels": definitive,
        "positive_labels": positives,
        "negative_labels": negatives,
        "brier_score": brier,
        "expected_calibration_error": ece,
        "reliability_bins": len(bins),
        "max_brier_score": max_brier,
        "max_expected_calibration_error": max_ece,
    }


def _check(code: str, observed: Any, required: Any, passed: bool) -> GateCheck:
    return GateCheck(code, passed, observed, required)


def _minimum(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _maximum(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def evaluate_discovery_evidence_gate(
    manifest: DiscoveryEvidenceManifest,
) -> DiscoveryEvidenceGateReport:
    expected_sessions = _expected_sessions(manifest.coverage_start, manifest.coverage_end)
    expected_set = set(expected_sessions)
    audit_set = {artifact.session for artifact in manifest.discovery_audits}
    census_set = {artifact.session for artifact in manifest.recall_census_reports}
    provider_set = {artifact.session for artifact in manifest.provider_proof_reports}
    audits = {artifact.session: _load_audit(artifact) for artifact in manifest.discovery_audits}
    censuses = {
        artifact.session: _load_census(artifact) for artifact in manifest.recall_census_reports
    }
    providers = {
        artifact.session: _load_provider(artifact) for artifact in manifest.provider_proof_reports
    }
    empirical = _load_empirical(
        manifest.empirical_artifact,
        manifest.campaign_artifact,
        expected_sessions,
        manifest.policy,
    )
    calibration = _load_calibration(
        manifest.calibration_artifact,
        manifest.campaign_artifact,
        expected_sessions,
        manifest.policy,
    )
    control_results = {
        artifact.kind: _control_passed(artifact) for artifact in manifest.control_artifacts
    }
    policy = manifest.policy

    audit_rows = list(audits.values())
    census_rows = list(censuses.values())
    provider_rows = list(providers.values())
    clean_sessions = sum(
        row["operational_clean"] and row["session_evidence_eligible"]
        for row in audit_rows
    )
    window_coverage = _minimum([row["window_coverage_pct"] for row in audit_rows])
    tick_gap = _maximum([row["max_tick_gap_seconds"] for row in audit_rows])
    processing_latency = _maximum([row["max_latency_seconds"] for row in audit_rows])
    scheduled_lag = _maximum([row["max_scheduled_lag_seconds"] for row in audit_rows])
    missed_cycles = sum(row["missed_cycles"] for row in audit_rows)
    primary_eligible = sum(row["eligible_pairs"] for row in census_rows)
    primary_tp = sum(row["true_positive_pairs"] for row in census_rows)
    primary_fn = sum(row["false_negative_pairs"] for row in census_rows)
    primary_recall = _ratio(primary_tp, primary_eligible)
    primary_max_latency = _maximum(
        [row["max_detection_latency_seconds"] for row in census_rows]
    )
    candidate_rank = empirical["candidate_rank"]

    audit_versions = tuple(sorted({row["audit_version"] for row in audit_rows}))
    discovery_versions = tuple(
        sorted({value for row in audit_rows for value in row["discovery_versions"]})
    )
    audit_codes = tuple(sorted({row["audit_code_version"] for row in audit_rows}))
    observer_codes = tuple(sorted({value for row in audit_rows for value in row["code_versions"]}))
    census_codes = tuple(sorted({row["code_version"] for row in census_rows}))
    provider_codes = tuple(sorted({row["code_version"] for row in provider_rows}))
    empirical_codes = (empirical["code_version"],)
    calibration_codes = (calibration["code_version"],)
    control_codes = tuple(sorted({artifact.revision for artifact in manifest.control_artifacts}))
    data_feeds = tuple(
        sorted(
            {value for row in audit_rows for value in row["data_feeds"]}
            | {row["primary_feed"] for row in provider_rows}
            | {row["independent_feed"] for row in provider_rows}
        )
    )
    primary_providers = tuple(
        sorted(
            {value for row in audit_rows for value in row["providers"]}
            | {row["primary_provider"] for row in provider_rows}
        )
    )
    independent_providers = tuple(sorted({row["independent_provider"] for row in provider_rows}))
    independent_datasets = tuple(sorted({row["independent_dataset"] for row in provider_rows}))

    metrics = DiscoveryAggregateMetrics(
        expected_sessions=len(expected_sessions),
        clean_sessions=clean_sessions,
        primary_census_sessions=len(census_rows),
        provider_proof_sessions=len(provider_rows),
        minimum_window_coverage_pct=window_coverage,
        maximum_tick_gap_seconds=tick_gap,
        maximum_processing_latency_seconds=processing_latency,
        maximum_scheduled_lag_seconds=scheduled_lag,
        missed_cycles=missed_cycles,
        primary_unavailable_symbols=sum(row["unavailable_symbols"] for row in census_rows),
        primary_eligible_pairs=primary_eligible,
        primary_true_positive_pairs=primary_tp,
        primary_false_negative_pairs=primary_fn,
        primary_recall=primary_recall,
        maximum_primary_detection_latency_seconds=primary_max_latency,
        minimum_provider_comparable_coverage=_minimum(
            [row["comparable_coverage"] for row in provider_rows]
        ),
        minimum_provider_bar_overlap_coverage=_minimum(
            [row["bar_overlap_coverage"] for row in provider_rows]
        ),
        minimum_provider_eligible_pair_agreement=_minimum(
            [row["eligible_pair_agreement"] for row in provider_rows]
        ),
        minimum_provider_independent_recall=_minimum(
            [row["independent_recall"] for row in provider_rows]
        ),
        provider_price_disagreement_bars=sum(
            row["price_disagreement_bars"] for row in provider_rows
        ),
        maximum_provider_close_difference_bps=_maximum(
            [row["max_abs_close_difference_bps"] for row in provider_rows]
        ),
        definitive_labels=candidate_rank["definitive_labels"],
        ambiguous_labels=candidate_rank["ambiguous_labels"],
        positive_labels=candidate_rank["positive_labels"],
        true_positives=candidate_rank["true_positives"],
        false_positives=candidate_rank["false_positives"],
        true_negatives=candidate_rank["true_negatives"],
        false_negatives=candidate_rank["false_negatives"],
        empirical_precision=candidate_rank["precision"],
        empirical_recall=candidate_rank["recall"],
        direction_mismatches=candidate_rank["direction_mismatches"],
        duplicate_candidate_rows=empirical["duplicate_candidate_rows"],
        calibration_brier_score=calibration["brier_score"],
        calibration_expected_calibration_error=calibration[
            "expected_calibration_error"
        ],
        calibration_reliability_bins=calibration["reliability_bins"],
        audit_versions=audit_versions,
        discovery_versions=discovery_versions,
        audit_code_versions=audit_codes,
        observer_code_versions=observer_codes,
        census_code_versions=census_codes,
        provider_proof_code_versions=provider_codes,
        empirical_code_versions=empirical_codes,
        calibration_code_versions=calibration_codes,
        control_code_versions=control_codes,
        data_feeds=data_feeds,
        primary_market_data_providers=primary_providers,
        independent_market_data_providers=independent_providers,
        independent_datasets=independent_datasets,
    )

    inventories_match = audit_set == census_set == provider_set == expected_set
    census_reconciled = all(
        row["operational_complete"]
        and not row["evidence_eligible"]
        and row["issue_codes"] == PRIMARY_CENSUS_RECONCILED_ISSUES
        for row in census_rows
    )
    providers_clean = all(
        row["operational_complete"] and row["evidence_eligible"] and not row["issue_codes"]
        for row in provider_rows
    )
    provider_causality = all(
        row["object_last_modified_utc"] <= manifest.created_at_utc
        for row in provider_rows
    )
    census_provider_identity = all(
        session in censuses
        and providers[session]["census_id"] == censuses[session]["census_id"]
        and providers[session]["universe_symbols"] == censuses[session]["universe_symbols"]
        for session in expected_sessions
        if session in providers
    ) and provider_set == expected_set
    control_kinds = set(control_results)
    checks = (
        _check(
            "COMPLETE_SESSION_INVENTORIES",
            {
                "audits": sorted(value.isoformat() for value in audit_set),
                "censuses": sorted(value.isoformat() for value in census_set),
                "provider_proofs": sorted(value.isoformat() for value in provider_set),
            },
            sorted(value.isoformat() for value in expected_set),
            inventories_match,
        ),
        _check(
            "MIN_CLEAN_SESSIONS",
            clean_sessions,
            policy.min_clean_sessions,
            clean_sessions >= policy.min_clean_sessions,
        ),
        _check(
            "ZERO_DIRTY_SESSIONS",
            len(audit_rows) - clean_sessions,
            0,
            len(audit_rows) == clean_sessions,
        ),
        _check(
            "MIN_WINDOW_COVERAGE",
            window_coverage,
            policy.min_window_coverage_pct,
            window_coverage is not None
            and window_coverage >= policy.min_window_coverage_pct,
        ),
        _check(
            "MAX_DISCOVERY_TICK_GAP",
            tick_gap,
            policy.max_discovery_tick_gap_seconds,
            tick_gap is not None and tick_gap <= policy.max_discovery_tick_gap_seconds,
        ),
        _check(
            "MAX_DISCOVERY_PROCESSING_LATENCY",
            processing_latency,
            policy.max_discovery_processing_latency_seconds,
            processing_latency is not None
            and processing_latency <= policy.max_discovery_processing_latency_seconds,
        ),
        _check(
            "MAX_DISCOVERY_SCHEDULED_LAG",
            scheduled_lag,
            policy.max_discovery_scheduled_lag_seconds,
            scheduled_lag is not None
            and scheduled_lag <= policy.max_discovery_scheduled_lag_seconds,
        ),
        _check("ZERO_MISSED_CYCLES", missed_cycles, 0, missed_cycles == 0),
        _check(
            "PRIMARY_CENSUS_RECONCILED_WITH_SEPARATE_PROOF",
            census_reconciled,
            True,
            census_reconciled,
        ),
        _check(
            "ZERO_PRIMARY_UNAVAILABLE_SYMBOLS",
            metrics.primary_unavailable_symbols,
            0,
            metrics.primary_unavailable_symbols == 0,
        ),
        _check(
            "MIN_PRIMARY_RECALL",
            primary_recall,
            policy.min_primary_recall,
            primary_recall is not None and primary_recall >= policy.min_primary_recall,
        ),
        _check(
            "MAX_PRIMARY_DETECTION_LATENCY",
            primary_max_latency,
            policy.max_primary_detection_latency_seconds,
            primary_max_latency is not None
            and primary_max_latency <= policy.max_primary_detection_latency_seconds,
        ),
        _check("PROVIDER_PROOFS_CLEAN", providers_clean, True, providers_clean),
        _check("PROVIDER_OBJECT_CAUSALITY", provider_causality, True, provider_causality),
        _check(
            "CENSUS_PROVIDER_IDENTITY",
            census_provider_identity,
            True,
            census_provider_identity,
        ),
        _check(
            "MIN_PROVIDER_COMPARABLE_COVERAGE",
            metrics.minimum_provider_comparable_coverage,
            policy.min_provider_comparable_coverage,
            metrics.minimum_provider_comparable_coverage is not None
            and metrics.minimum_provider_comparable_coverage
            >= policy.min_provider_comparable_coverage,
        ),
        _check(
            "MIN_PROVIDER_BAR_OVERLAP",
            metrics.minimum_provider_bar_overlap_coverage,
            policy.min_provider_bar_overlap_coverage,
            metrics.minimum_provider_bar_overlap_coverage is not None
            and metrics.minimum_provider_bar_overlap_coverage
            >= policy.min_provider_bar_overlap_coverage,
        ),
        _check(
            "MIN_PROVIDER_ELIGIBLE_AGREEMENT",
            metrics.minimum_provider_eligible_pair_agreement,
            policy.min_provider_eligible_pair_agreement,
            metrics.minimum_provider_eligible_pair_agreement is not None
            and metrics.minimum_provider_eligible_pair_agreement
            >= policy.min_provider_eligible_pair_agreement,
        ),
        _check(
            "MIN_PROVIDER_INDEPENDENT_RECALL",
            metrics.minimum_provider_independent_recall,
            policy.min_provider_independent_recall,
            metrics.minimum_provider_independent_recall is not None
            and metrics.minimum_provider_independent_recall
            >= policy.min_provider_independent_recall,
        ),
        _check(
            "ZERO_PROVIDER_PRICE_DISAGREEMENTS",
            metrics.provider_price_disagreement_bars,
            0,
            metrics.provider_price_disagreement_bars == 0,
        ),
        _check(
            "MAX_PROVIDER_CLOSE_DIFFERENCE",
            metrics.maximum_provider_close_difference_bps,
            policy.max_provider_close_difference_bps,
            metrics.maximum_provider_close_difference_bps is not None
            and metrics.maximum_provider_close_difference_bps
            <= policy.max_provider_close_difference_bps,
        ),
        _check(
            "EMPIRICAL_LOCKED_POLICY_PASSED",
            {
                "passed": empirical["passed_locked_policy"],
                "blockers": list(empirical["blocking_reasons"]),
            },
            {"passed": True, "blockers": []},
            empirical["passed_locked_policy"] and not empirical["blocking_reasons"],
        ),
        _check(
            "CALIBRATED_QUALITY_HOLDOUT_PASSED",
            {
                "claim_valid": calibration["claim_valid"],
                "blockers": list(calibration["blocking_reasons"]),
            },
            {"claim_valid": True, "blockers": []},
            calibration["claim_valid"] and not calibration["blocking_reasons"],
        ),
        _check(
            "MAX_CALIBRATION_BRIER_SCORE",
            metrics.calibration_brier_score,
            policy.max_calibration_brier_score,
            metrics.calibration_brier_score is not None
            and metrics.calibration_brier_score <= policy.max_calibration_brier_score,
        ),
        _check(
            "MAX_EXPECTED_CALIBRATION_ERROR",
            metrics.calibration_expected_calibration_error,
            policy.max_expected_calibration_error,
            metrics.calibration_expected_calibration_error is not None
            and metrics.calibration_expected_calibration_error
            <= policy.max_expected_calibration_error,
        ),
        _check(
            "MIN_DEFINITIVE_LABELS",
            metrics.definitive_labels,
            policy.min_definitive_labels,
            metrics.definitive_labels >= policy.min_definitive_labels,
        ),
        _check(
            "MIN_POSITIVE_LABELS",
            metrics.positive_labels,
            policy.min_positive_labels,
            metrics.positive_labels >= policy.min_positive_labels,
        ),
        _check(
            "MIN_EMPIRICAL_PRECISION",
            metrics.empirical_precision,
            policy.min_empirical_precision,
            metrics.empirical_precision is not None
            and metrics.empirical_precision >= policy.min_empirical_precision,
        ),
        _check(
            "MIN_EMPIRICAL_RECALL",
            metrics.empirical_recall,
            policy.min_empirical_recall,
            metrics.empirical_recall is not None
            and metrics.empirical_recall >= policy.min_empirical_recall,
        ),
        _check(
            "ZERO_AMBIGUOUS_LABELS",
            metrics.ambiguous_labels,
            0,
            metrics.ambiguous_labels == 0,
        ),
        _check(
            "ZERO_DIRECTION_MISMATCHES",
            metrics.direction_mismatches,
            0,
            metrics.direction_mismatches == 0,
        ),
        _check(
            "ZERO_DUPLICATE_CANDIDATES",
            metrics.duplicate_candidate_rows,
            0,
            metrics.duplicate_candidate_rows == 0,
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
            "ALLOWED_DATA_FEEDS",
            list(data_feeds),
            list(policy.allowed_data_feeds),
            set(data_feeds) <= set(policy.allowed_data_feeds),
        ),
        _check(
            "ALLOWED_PRIMARY_PROVIDERS",
            list(primary_providers),
            list(policy.allowed_primary_market_data_providers),
            set(primary_providers) <= set(policy.allowed_primary_market_data_providers),
        ),
        _check(
            "ALLOWED_INDEPENDENT_PROVIDERS",
            list(independent_providers),
            list(policy.allowed_independent_market_data_providers),
            set(independent_providers)
            <= set(policy.allowed_independent_market_data_providers),
        ),
        _check(
            "ALLOWED_INDEPENDENT_DATASETS",
            list(independent_datasets),
            list(policy.allowed_independent_datasets),
            set(independent_datasets) <= set(policy.allowed_independent_datasets),
        ),
        _check(
            "ALLOWED_AUDIT_VERSIONS",
            list(audit_versions),
            list(policy.allowed_audit_versions),
            set(audit_versions) <= set(policy.allowed_audit_versions),
        ),
        _check(
            "ALLOWED_DISCOVERY_VERSIONS",
            list(discovery_versions),
            list(policy.allowed_discovery_versions),
            set(discovery_versions) <= set(policy.allowed_discovery_versions),
        ),
        _check(
            "ALLOWED_AUDIT_CODE_VERSIONS",
            list(audit_codes),
            list(policy.allowed_audit_code_versions),
            set(audit_codes) <= set(policy.allowed_audit_code_versions),
        ),
        _check(
            "ALLOWED_OBSERVER_CODE_VERSIONS",
            list(observer_codes),
            list(policy.allowed_observer_code_versions),
            set(observer_codes) <= set(policy.allowed_observer_code_versions),
        ),
        _check(
            "ALLOWED_CENSUS_CODE_VERSIONS",
            list(census_codes),
            list(policy.allowed_census_code_versions),
            set(census_codes) <= set(policy.allowed_census_code_versions),
        ),
        _check(
            "ALLOWED_PROVIDER_PROOF_CODE_VERSIONS",
            list(provider_codes),
            list(policy.allowed_provider_proof_code_versions),
            set(provider_codes) <= set(policy.allowed_provider_proof_code_versions),
        ),
        _check(
            "ALLOWED_EMPIRICAL_CODE_VERSIONS",
            list(empirical_codes),
            list(policy.allowed_empirical_code_versions),
            set(empirical_codes) <= set(policy.allowed_empirical_code_versions),
        ),
        _check(
            "ALLOWED_CALIBRATION_CODE_VERSIONS",
            list(calibration_codes),
            list(policy.allowed_calibration_code_versions),
            set(calibration_codes) <= set(policy.allowed_calibration_code_versions),
        ),
        _check(
            "ALLOWED_CONTROL_CODE_VERSIONS",
            list(control_codes),
            list(policy.allowed_control_code_versions),
            set(control_codes) <= set(policy.allowed_control_code_versions),
        ),
    )
    verdict = VERDICT_OWNER_REVIEW if all(check.passed for check in checks) else VERDICT_NOT_READY
    artifacts = []
    for kind, inventory in (
        ("discovery_audit", manifest.discovery_audits),
        ("recall_census", manifest.recall_census_reports),
        ("provider_proof", manifest.provider_proof_reports),
    ):
        artifacts.extend(
            {
                "kind": kind,
                "session": item.session.isoformat(),
                "path": str(item.path),
                "sha256": item.sha256,
            }
            for item in inventory
        )
    artifacts.append(
        {
            "kind": "empirical_holdout",
            "session": "aggregate",
            "path": str(manifest.empirical_artifact.path),
            "sha256": manifest.empirical_artifact.sha256,
        }
    )
    artifacts.append(
        {
            "kind": "calibrated_quality_holdout",
            "session": "aggregate",
            "path": str(manifest.calibration_artifact.path),
            "sha256": manifest.calibration_artifact.sha256,
        }
    )
    artifacts.extend(
        {
            "kind": item.kind,
            "session": "control",
            "path": str(item.path),
            "sha256": item.sha256,
        }
        for item in manifest.control_artifacts
    )
    return DiscoveryEvidenceGateReport(
        EVIDENCE_SCHEMA_VERSION,
        manifest.evidence_set_version,
        verdict,
        manifest.coverage_start.isoformat(),
        manifest.coverage_end.isoformat(),
        manifest.campaign_artifact.campaign_id,
        manifest.campaign_artifact.locked_at_utc.isoformat(),
        manifest.campaign_artifact.sha256,
        manifest.campaign_artifact.experiment_id,
        manifest.campaign_artifact.experiment_manifest_sha256,
        manifest.campaign_artifact.rank_version,
        policy,
        metrics,
        checks,
        tuple(artifacts),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(args.manifest)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.verdict == VERDICT_OWNER_REVIEW else 1


if __name__ == "__main__":
    raise SystemExit(main())
