"""Aggregate market-wide discovery evidence campaign and readiness gate."""
from __future__ import annotations

import ast
import hashlib
import json
import stat
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_discovery_evidence_campaign import (
    lock_discovery_evidence_campaign,
)
from tradebot.postmarket_discovery_evidence_set import (
    seal_discovery_evidence_set,
)
from tradebot.postmarket_discovery_evidence_gate import (
    CALENDAR,
    POLICY_FIELDS,
    REQUIRED_CONTROL_KINDS,
    VERDICT_NOT_READY,
    VERDICT_OWNER_REVIEW,
    _parse_policy,
    evaluate_discovery_evidence_gate,
    load_discovery_evidence_manifest,
    main,
)


START = date(2026, 8, 17)
END = date(2026, 8, 28)
SESSIONS = tuple(timestamp.date() for timestamp in CALENDAR.sessions_in_range(START, END))
LOCKED_AT = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
CREATED_AT = datetime(2026, 8, 31, 18, tzinfo=timezone.utc)
CONTROL_COMPLETED = datetime(2026, 8, 31, 17, tzinfo=timezone.utc)
EXPERIMENT_ID = "marketwide-rank-v1-holdout"
EXPERIMENT_MANIFEST = "1" * 64
INPUT_DIGEST = "2" * 64
AUDIT_CODE = "a" * 7
OBSERVER_CODE = "b" * 7
CENSUS_CODE = "c" * 7
PROVIDER_CODE = "d" * 7
EMPIRICAL_CODE = "e" * 7
CALIBRATION_CODE = "9" * 7
CONTROL_CODE = "f" * 7


def _policy() -> dict:
    return {
        "min_clean_sessions": 10,
        "min_definitive_labels": 100,
        "min_positive_labels": 30,
        "min_empirical_recall": 0.95,
        "min_empirical_precision": 0.90,
        "min_calibration_negative_labels": 70,
        "min_calibration_bin_labels": 20,
        "max_calibration_brier_score": 0.20,
        "max_expected_calibration_error": 0.10,
        "min_primary_recall": 0.95,
        "max_primary_detection_latency_seconds": 330,
        "min_provider_comparable_coverage": 0.99,
        "min_provider_bar_overlap_coverage": 0.95,
        "min_provider_eligible_pair_agreement": 0.95,
        "min_provider_independent_recall": 0.95,
        "max_provider_close_difference_bps": 50,
        "min_window_coverage_pct": 99,
        "max_discovery_tick_gap_seconds": 150,
        "max_discovery_processing_latency_seconds": 30,
        "max_discovery_scheduled_lag_seconds": 30,
        "allowed_data_feeds": ["sip"],
        "allowed_primary_market_data_providers": ["alpaca"],
        "allowed_independent_market_data_providers": ["massive"],
        "allowed_independent_datasets": ["us_stocks_sip/minute_aggs_v1"],
        "allowed_audit_versions": [3],
        "allowed_discovery_versions": [1],
        "allowed_audit_code_versions": [AUDIT_CODE],
        "allowed_observer_code_versions": [OBSERVER_CODE],
        "allowed_census_code_versions": [CENSUS_CODE],
        "allowed_provider_proof_code_versions": [PROVIDER_CODE],
        "allowed_empirical_code_versions": [EMPIRICAL_CODE],
        "allowed_calibration_code_versions": [CALIBRATION_CODE],
        "allowed_control_code_versions": [CONTROL_CODE],
        "require_zero_dirty_sessions": True,
        "require_complete_session_inventory": True,
        "require_zero_unavailable_symbols": True,
        "require_zero_provider_price_disagreements": True,
        "require_zero_ambiguous_labels": True,
        "require_zero_direction_mismatches": True,
        "require_zero_duplicate_candidates": True,
    }


def _write(root: Path, relative: str, payload: dict) -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def _classification(*, definitive: int, positive: int, tp: int, tn: int) -> dict:
    fp = 0
    fn = positive - tp
    return {
        "definitive_labels": definitive,
        "ambiguous_labels": 0,
        "positive_labels": positive,
        "selected": tp + fp,
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives": fn,
        "direction_mismatches": 0,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def _audit(session: date) -> dict:
    return {
        "audit_version": 3,
        "audit_code_version": AUDIT_CODE,
        "session": session.isoformat(),
        "database": "data/postmarket_shadow.db",
        "operational_clean": True,
        "session_evidence_eligible": True,
        "operational": {
            "window_coverage_pct": 100.0,
            "max_tick_gap_seconds": 60.0,
            "fetch_errors": 0,
            "failed_invariants": 0,
            "max_latency_ms": 1500,
            "max_scheduled_lag_ms": 500,
            "missed_cycles": 0,
            "discovery_versions": [1],
            "code_versions": [OBSERVER_CODE],
            "data_feeds": ["sip"],
            "market_data_providers": ["alpaca"],
        },
        "candidates": [],
        "near_miss_symbols": [],
        "issues": [],
    }


def _census(session: date, census_id: int) -> dict:
    return {
        "report_version": 1,
        "census_id": census_id,
        "session": session.isoformat(),
        "attempt": 1,
        "code_version": CENSUS_CODE,
        "operational_complete": True,
        "evidence_eligible": False,
        "metrics": {
            "universe_symbols": 100,
            "requested_chunks": 1,
            "fetched_symbols": 100,
            "evaluated_symbols": 100,
            "unavailable_symbols": 0,
            "stage1_seen_symbols": 10,
            "stage1_candidate_pairs": 10,
            "eligible_pairs": 10,
            "true_positive_pairs": 10,
            "false_negative_pairs": 0,
            "false_positive_pairs": 0,
            "recall": 1.0,
            "average_detection_delay_seconds": 60.0,
            "max_detection_delay_seconds": 60.0,
            "provider_comparison_status": "NOT_CONFIGURED",
        },
        "false_negatives": [],
        "false_positives": [],
        "unavailable": [],
        "issue_codes": ["PROVIDER_COMPARISON_NOT_CONFIGURED"],
    }


def _provider(session: date, census_id: int) -> dict:
    modified = datetime.combine(
        date.fromordinal(session.toordinal() + 1),
        time(16, 0),
        tzinfo=timezone.utc,
    )
    return {
        "report_version": 1,
        "comparison_id": census_id,
        "census_id": census_id,
        "session": session.isoformat(),
        "attempt": 1,
        "code_version": PROVIDER_CODE,
        "operational_complete": True,
        "evidence_eligible": True,
        "source": {
            "primary_provider": "alpaca",
            "primary_feed": "sip",
            "independent_provider": "massive",
            "independent_feed": "sip",
            "independent_dataset": "us_stocks_sip/minute_aggs_v1",
            "object_key": f"us_stocks_sip/minute_aggs_v1/{session}.csv.gz",
            "object_etag": f"etag-{session}",
            "object_last_modified_utc": modified.isoformat(),
            "selected_rows_sha256": hashlib.sha256(session.isoformat().encode()).hexdigest(),
        },
        "metrics": {
            "universe_symbols": 100,
            "primary_evaluated_symbols": 100,
            "independent_evaluated_symbols": 100,
            "comparable_symbols": 100,
            "comparable_coverage": 1.0,
            "primary_eligible_pairs": 10,
            "independent_eligible_pairs": 10,
            "agreed_eligible_pairs": 10,
            "eligible_pair_agreement": 1.0,
            "independent_true_positive_pairs": 10,
            "independent_false_negative_pairs": 0,
            "independent_false_positive_pairs": 0,
            "independent_recall": 1.0,
            "primary_comparison_bars": 100,
            "independent_comparison_bars": 100,
            "compared_bars": 100,
            "bar_overlap_coverage": 1.0,
            "price_disagreement_bars": 0,
            "max_abs_close_difference_bps": 0.0,
        },
        "independent_misses": [],
        "provider_disagreements": [],
        "issue_codes": [],
    }


def _empirical() -> dict:
    per_session = []
    for session in SESSIONS:
        row = _classification(definitive=10, positive=3, tp=3, tn=7)
        per_session.append(
            {
                "session": session.isoformat(),
                "baseline": row,
                "candidate_rank": row,
                "discovered_symbols": 10,
                "rankable_symbols": 10,
                "duplicate_candidate_rows": 0,
            }
        )
    aggregate = _classification(definitive=100, positive=30, tp=30, tn=70)
    report = {
        "empirical_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "split": "holdout",
        "rank_version": 1,
        "sessions": [session.isoformat() for session in SESSIONS],
        "eligibility_rule": {
            "move_pct": 8.0,
            "min_notional": 250000,
            "persistence_bars": 2,
        },
        "selection_rule": {"minimum_evidence_score": 60, "maximum_ordinal_rank": 10},
        "policy": {
            "min_precision": 0.90,
            "min_recall": 0.95,
            "min_definitive_labels": 100,
            "min_positive_labels": 30,
        },
        "baseline": aggregate,
        "candidate_rank": aggregate,
        "session_metrics": per_session,
        "precision_delta": 0.0,
        "recall_delta": 0.0,
        "passed_locked_policy": True,
        "blocking_reasons": [],
        "holdout_unblinded": True,
        "input_digest_sha256": INPUT_DIGEST,
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "artifact_type": "postmarket_rank_empirical",
        "empirical_run_id": 1,
        "experiment_id": EXPERIMENT_ID,
        "split": "holdout",
        "evaluated_at_utc": datetime(2026, 8, 29, 18, tzinfo=timezone.utc).isoformat(),
        "code_version": EMPIRICAL_CODE,
        "input_digest_sha256": INPUT_DIGEST,
        "report_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "experiment_manifest_sha256": EXPERIMENT_MANIFEST,
        "report": report,
    }


def _wilson(positives: int, labels: int) -> tuple[float, float]:
    observed = positives / labels
    center = (observed + 1.96**2 / (2 * labels)) / (1 + 1.96**2 / labels)
    margin = 1.96 * (
        (observed * (1 - observed) + 1.96**2 / (4 * labels)) / labels
    ) ** 0.5 / (1 + 1.96**2 / labels)
    return max(0.0, center - margin), min(1.0, center + margin)


def _calibration() -> dict:
    bins = []
    for score, labels, positives, predicted in (
        (20.0, 70, 0, 0.0),
        (80.0, 30, 30, 1.0),
    ):
        lower, upper = _wilson(positives, labels)
        observed = positives / labels
        bins.append({
            "minimum_score": score,
            "maximum_score": score,
            "predicted_quality": predicted,
            "labels": labels,
            "positives": positives,
            "observed_quality": observed,
            "absolute_error": abs(predicted - observed),
            "wilson_lower_95": lower,
            "wilson_upper_95": upper,
        })
    report = {
        "calibration_version": 1,
        "calibration_id": 1,
        "experiment_id": EXPERIMENT_ID,
        "split": "holdout",
        "sessions": [session.isoformat() for session in SESSIONS],
        "code_version": CALIBRATION_CODE,
        "model_sha256": "4" * 64,
        "development_input_sha256": "5" * 64,
        "policy": {
            "min_training_labels": 100,
            "min_training_positive_labels": 30,
            "min_training_negative_labels": 70,
            "min_holdout_labels": 100,
            "min_holdout_positive_labels": 30,
            "min_holdout_negative_labels": 70,
            "minimum_bin_labels": 20,
            "max_brier_score": 0.20,
            "max_expected_calibration_error": 0.10,
        },
        "definitive_labels": 100,
        "positive_labels": 30,
        "negative_labels": 70,
        "ambiguous_labels": 0,
        "unmatched_rank_labels": 0,
        "brier_score": 0.0,
        "expected_calibration_error": 0.0,
        "reliability_bins": bins,
        "holdout_unblinded": True,
        "calibrated_quality_claim_valid": True,
        "blocking_reasons": [],
        "input_digest_sha256": "3" * 64,
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "artifact_type": "postmarket_rank_calibration",
        "calibration_run_id": 1,
        "experiment_id": EXPERIMENT_ID,
        "split": "holdout",
        "evaluated_at_utc": datetime(2026, 8, 31, 18, tzinfo=timezone.utc).isoformat(),
        "code_version": CALIBRATION_CODE,
        "input_digest_sha256": "3" * 64,
        "report_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "model_sha256": "4" * 64,
        "report": report,
    }


def _control(kind: str) -> dict:
    return {
        "schema_version": 1,
        "kind": kind,
        "status": "passed",
        "revision": CONTROL_CODE,
        "completed_at_utc": CONTROL_COMPLETED.isoformat(),
        "checks": [{"name": "control", "passed": True, "evidence": "deterministic"}],
    }


def _package(tmp_path: Path) -> tuple[Path, dict]:
    assert len(SESSIONS) == 10
    root = tmp_path / "evidence"
    root.mkdir(parents=True)
    campaign_path = root / "campaign.json"
    campaign_digest, _ = lock_discovery_evidence_campaign(
        campaign_path,
        campaign_id="marketwide-campaign-1",
        locked_at=LOCKED_AT,
        coverage_start=START,
        coverage_end=END,
        experiment_id=EXPERIMENT_ID,
        experiment_manifest_sha256=EXPERIMENT_MANIFEST,
        rank_version=1,
        policy=_policy(),
    )
    audits = []
    censuses = []
    providers = []
    for index, session in enumerate(SESSIONS, start=1):
        audit = _write(root, f"audits/audit-{session}.json", _audit(session))
        census = _write(root, f"census/census-{session}.json", _census(session, index))
        provider = _write(
            root,
            f"providers/provider-{session}.json",
            _provider(session, index),
        )
        audits.append({"session": session.isoformat(), **audit})
        censuses.append({"session": session.isoformat(), **census})
        providers.append({"session": session.isoformat(), **provider})
    empirical = _write(root, "empirical/holdout.json", _empirical())
    calibration = _write(root, "empirical/calibration.json", _calibration())
    controls = []
    for kind in sorted(REQUIRED_CONTROL_KINDS):
        reference = _write(root, f"controls/{kind}.json", _control(kind))
        controls.append(
            {
                "kind": kind,
                **reference,
                "revision": CONTROL_CODE,
                "completed_at_utc": CONTROL_COMPLETED.isoformat(),
            }
        )
    manifest = {
        "schema_version": 2,
        "status": "locked",
        "evidence_set_version": "marketwide-v1",
        "created_at_utc": CREATED_AT.isoformat(),
        "coverage_start": START.isoformat(),
        "coverage_end": END.isoformat(),
        "campaign_artifact": {"path": "campaign.json", "sha256": campaign_digest},
        "policy": _policy(),
        "discovery_audits": audits,
        "recall_census_reports": censuses,
        "provider_proof_reports": providers,
        "empirical_artifact": empirical,
        "calibration_artifact": calibration,
        "control_artifacts": controls,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path, manifest


def _rewrite_artifact(
    manifest_path: Path,
    inventory: str,
    index: int | None,
    mutate,
) -> None:
    manifest = json.loads(manifest_path.read_text())
    reference = manifest[inventory] if index is None else manifest[inventory][index]
    path = manifest_path.parent / reference["path"]
    payload = json.loads(path.read_text())
    mutate(payload)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    reference["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _sealer_inputs(manifest_path: Path, manifest: dict) -> dict:
    root = manifest_path.parent
    return {
        "evidence_set_version": manifest["evidence_set_version"],
        "created_at": CREATED_AT,
        "campaign_path": root / manifest["campaign_artifact"]["path"],
        "discovery_audits": [
            (date.fromisoformat(item["session"]), root / item["path"])
            for item in manifest["discovery_audits"]
        ],
        "recall_census_reports": [
            (date.fromisoformat(item["session"]), root / item["path"])
            for item in manifest["recall_census_reports"]
        ],
        "provider_proof_reports": [
            (date.fromisoformat(item["session"]), root / item["path"])
            for item in manifest["provider_proof_reports"]
        ],
        "empirical_artifact": root / manifest["empirical_artifact"]["path"],
        "calibration_artifact": root / manifest["calibration_artifact"]["path"],
        "control_artifacts": [
            (item["kind"], root / item["path"])
            for item in manifest["control_artifacts"]
        ],
    }


def test_campaign_is_prospective_immutable_and_binds_empirical_identity(tmp_path):
    path = tmp_path / "campaign.json"
    digest, payload = lock_discovery_evidence_campaign(
        path,
        campaign_id="campaign-1",
        locked_at=LOCKED_AT,
        coverage_start=START,
        coverage_end=END,
        experiment_id=EXPERIMENT_ID,
        experiment_manifest_sha256=EXPERIMENT_MANIFEST,
        rank_version=1,
        policy=_policy(),
    )

    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload["experiment_id"] == EXPERIMENT_ID
    assert payload["experiment_manifest_sha256"] == EXPERIMENT_MANIFEST
    assert payload["rank_version"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError):
        lock_discovery_evidence_campaign(
            path,
            campaign_id="campaign-1",
            locked_at=LOCKED_AT,
            coverage_start=START,
            coverage_end=END,
            experiment_id=EXPERIMENT_ID,
            experiment_manifest_sha256=EXPERIMENT_MANIFEST,
            rank_version=1,
            policy=_policy(),
        )


def test_campaign_rejects_late_lock_and_permissive_policy(tmp_path):
    first_open = CALENDAR.session_open(SESSIONS[0]).to_pydatetime().astimezone(timezone.utc)
    with pytest.raises(ValueError, match="before its first session opens"):
        lock_discovery_evidence_campaign(
            tmp_path / "late.json",
            campaign_id="late",
            locked_at=first_open,
            coverage_start=START,
            coverage_end=END,
            experiment_id=EXPERIMENT_ID,
            experiment_manifest_sha256=EXPERIMENT_MANIFEST,
            rank_version=1,
            policy=_policy(),
        )
    policy = _policy()
    policy["min_primary_recall"] = 0.90
    with pytest.raises(ValueError, match="min_primary_recall"):
        _parse_policy(policy)


def test_complete_reconciled_package_is_eligible_for_owner_review(tmp_path):
    manifest_path, _ = _package(tmp_path)

    manifest = load_discovery_evidence_manifest(manifest_path)
    report = evaluate_discovery_evidence_gate(manifest)

    assert report.verdict == VERDICT_OWNER_REVIEW
    assert report.metrics.expected_sessions == 10
    assert report.metrics.clean_sessions == 10
    assert report.metrics.primary_recall == 1
    assert report.metrics.minimum_provider_independent_recall == 1
    assert report.metrics.empirical_precision == 1
    assert report.metrics.empirical_recall == 1
    assert report.metrics.calibration_brier_score == 0
    assert report.metrics.calibration_expected_calibration_error == 0
    assert all(check.passed for check in report.checks)
    assert len(report.artifact_digests) == 36


def test_explicit_sealer_publishes_only_a_passing_immutable_package(tmp_path):
    source_path, manifest = _package(tmp_path)
    output = source_path.parent / "sealed.json"

    sealed = seal_discovery_evidence_set(
        output,
        **_sealer_inputs(source_path, manifest),
    )

    assert sealed.report.verdict == VERDICT_OWNER_REVIEW
    assert sealed.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    payload = json.loads(output.read_text())
    assert [item["session"] for item in payload["discovery_audits"]] == [
        session.isoformat() for session in SESSIONS
    ]
    assert len(sealed.report.artifact_digests) == 36
    with pytest.raises(FileExistsError, match="refusing to replace"):
        seal_discovery_evidence_set(
            output,
            **_sealer_inputs(source_path, manifest),
        )


def test_sealer_rejects_missing_explicit_session_without_publishing(tmp_path):
    source_path, manifest = _package(tmp_path)
    inputs = _sealer_inputs(source_path, manifest)
    inputs["provider_proof_reports"] = inputs["provider_proof_reports"][:-1]
    output = source_path.parent / "sealed.json"

    with pytest.raises(ValueError, match="exact campaign sessions"):
        seal_discovery_evidence_set(output, **inputs)

    assert not output.exists()


def test_sealer_rejects_not_ready_evidence_without_publishing(tmp_path):
    source_path, manifest = _package(tmp_path)

    def degrade(payload):
        payload["operational_complete"] = True
        payload["evidence_eligible"] = False
        payload["issue_codes"] = ["PROVIDER_SYMBOL_COVERAGE_BELOW_99_PERCENT"]
        payload["metrics"]["comparable_symbols"] = 90
        payload["metrics"]["comparable_coverage"] = 0.9

    _rewrite_artifact(source_path, "provider_proof_reports", 0, degrade)
    output = source_path.parent / "sealed.json"

    with pytest.raises(ValueError, match="not eligible for owner review"):
        seal_discovery_evidence_set(
            output,
            **_sealer_inputs(source_path, manifest),
        )

    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_sealer_rejects_external_and_symlink_artifacts(tmp_path):
    source_path, manifest = _package(tmp_path)
    inputs = _sealer_inputs(source_path, manifest)
    outside = tmp_path / "outside.json"
    outside.write_text(Path(inputs["empirical_artifact"]).read_text())
    inputs["empirical_artifact"] = outside

    with pytest.raises(ValueError, match="inside the evidence-set directory"):
        seal_discovery_evidence_set(
            source_path.parent / "outside-sealed.json",
            **inputs,
        )

    inputs = _sealer_inputs(source_path, manifest)
    symlink = source_path.parent / "empirical-link.json"
    symlink.symlink_to(Path(inputs["empirical_artifact"]))
    inputs["empirical_artifact"] = symlink
    with pytest.raises(ValueError, match="cannot be a symlink"):
        seal_discovery_evidence_set(
            source_path.parent / "symlink-sealed.json",
            **inputs,
        )


def test_missing_session_inventory_is_not_ready(tmp_path):
    manifest_path, manifest = _package(tmp_path)
    manifest["provider_proof_reports"].pop()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    report = evaluate_discovery_evidence_gate(
        load_discovery_evidence_manifest(manifest_path)
    )

    assert report.verdict == VERDICT_NOT_READY
    checks = {check.code: check for check in report.checks}
    assert checks["COMPLETE_SESSION_INVENTORIES"].passed is False
    assert checks["CENSUS_PROVIDER_IDENTITY"].passed is False


def test_primary_census_only_reconciles_exact_provider_not_configured_issue(tmp_path):
    manifest_path, _ = _package(tmp_path)
    _rewrite_artifact(
        manifest_path,
        "recall_census_reports",
        0,
        lambda payload: payload["issue_codes"].append("RECALL_BELOW_95_PERCENT"),
    )

    report = evaluate_discovery_evidence_gate(
        load_discovery_evidence_manifest(manifest_path)
    )

    assert report.verdict == VERDICT_NOT_READY
    check = next(
        check
        for check in report.checks
        if check.code == "PRIMARY_CENSUS_RECONCILED_WITH_SEPARATE_PROOF"
    )
    assert check.passed is False


def test_primary_census_cannot_claim_eligibility_without_embedded_provider_proof(tmp_path):
    manifest_path, _ = _package(tmp_path)
    _rewrite_artifact(
        manifest_path,
        "recall_census_reports",
        0,
        lambda payload: payload.__setitem__("evidence_eligible", True),
    )

    with pytest.raises(ValueError, match="cannot be independently eligible"):
        evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(manifest_path)
        )


def test_provider_coverage_failure_is_durable_not_ready_evidence(tmp_path):
    manifest_path, _ = _package(tmp_path)

    def degrade(payload):
        payload["operational_complete"] = True
        payload["evidence_eligible"] = False
        payload["issue_codes"] = ["PROVIDER_SYMBOL_COVERAGE_BELOW_99_PERCENT"]
        payload["metrics"]["comparable_symbols"] = 90
        payload["metrics"]["comparable_coverage"] = 0.9

    _rewrite_artifact(manifest_path, "provider_proof_reports", 0, degrade)

    report = evaluate_discovery_evidence_gate(
        load_discovery_evidence_manifest(manifest_path)
    )

    assert report.verdict == VERDICT_NOT_READY
    checks = {check.code: check for check in report.checks}
    assert checks["PROVIDER_PROOFS_CLEAN"].passed is False
    assert checks["MIN_PROVIDER_COMPARABLE_COVERAGE"].passed is False


def test_same_provider_cannot_masquerade_as_independent(tmp_path):
    manifest_path, _ = _package(tmp_path)
    _rewrite_artifact(
        manifest_path,
        "provider_proof_reports",
        0,
        lambda payload: payload["source"].__setitem__("independent_provider", "alpaca"),
    )

    with pytest.raises(ValueError, match="must differ from primary"):
        evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(manifest_path)
        )


def test_empirical_artifact_must_match_prelocked_experiment_digest(tmp_path):
    manifest_path, _ = _package(tmp_path)
    _rewrite_artifact(
        manifest_path,
        "empirical_artifact",
        None,
        lambda payload: payload.__setitem__("experiment_manifest_sha256", "9" * 64),
    )

    with pytest.raises(ValueError, match="does not match campaign"):
        evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(manifest_path)
        )


def test_empirical_per_session_counts_must_reconcile_to_aggregate(tmp_path):
    manifest_path, _ = _package(tmp_path)

    def drift(payload):
        payload["report"]["session_metrics"][0]["candidate_rank"]["true_negatives"] = 6
        canonical = json.dumps(payload["report"], sort_keys=True, separators=(",", ":"))
        payload["report_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()

    _rewrite_artifact(manifest_path, "empirical_artifact", None, drift)

    with pytest.raises(ValueError, match="confusion matrix does not conserve"):
        evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(manifest_path)
        )


def test_calibration_is_mandatory_and_bad_holdout_is_not_ready(tmp_path):
    missing_path, missing = _package(tmp_path / "missing-calibration")
    missing.pop("calibration_artifact")
    missing_path.write_text(json.dumps(missing, indent=2, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="calibration_artifact"):
        load_discovery_evidence_manifest(missing_path)

    manifest_path, _ = _package(tmp_path / "bad-calibration")

    def degrade(payload):
        for item in payload["report"]["reliability_bins"]:
            item["predicted_quality"] = 0.5
            item["absolute_error"] = abs(0.5 - item["observed_quality"])
        payload["report"]["brier_score"] = 0.25
        payload["report"]["expected_calibration_error"] = 0.5
        payload["report"]["calibrated_quality_claim_valid"] = False
        payload["report"]["blocking_reasons"] = [
            "BRIER_SCORE_FLOOR_NOT_MET",
            "EXPECTED_CALIBRATION_ERROR_FLOOR_NOT_MET",
        ]
        canonical = json.dumps(payload["report"], sort_keys=True, separators=(",", ":"))
        payload["report_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()

    _rewrite_artifact(manifest_path, "calibration_artifact", None, degrade)
    report = evaluate_discovery_evidence_gate(
        load_discovery_evidence_manifest(manifest_path)
    )
    checks = {check.code: check for check in report.checks}
    assert report.verdict == VERDICT_NOT_READY
    assert checks["CALIBRATED_QUALITY_HOLDOUT_PASSED"].passed is False
    assert checks["MAX_CALIBRATION_BRIER_SCORE"].passed is False
    assert checks["MAX_EXPECTED_CALIBRATION_ERROR"].passed is False


def test_calibration_wilson_interval_is_independently_recomputed(tmp_path):
    manifest_path, _ = _package(tmp_path)

    def tamper(payload):
        payload["report"]["reliability_bins"][0]["wilson_lower_95"] = 0.0
        payload["report"]["reliability_bins"][0]["wilson_upper_95"] = 1.0
        canonical = json.dumps(payload["report"], sort_keys=True, separators=(",", ":"))
        payload["report_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()

    _rewrite_artifact(manifest_path, "calibration_artifact", None, tamper)
    with pytest.raises(ValueError, match="Wilson interval does not match counts"):
        evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(manifest_path)
        )


def test_missing_or_failed_control_keeps_package_not_ready(tmp_path):
    missing_path, missing = _package(tmp_path / "missing")
    missing["control_artifacts"].pop()
    missing_path.write_text(json.dumps(missing, indent=2, sort_keys=True) + "\n")
    missing_report = evaluate_discovery_evidence_gate(
        load_discovery_evidence_manifest(missing_path)
    )
    assert missing_report.verdict == VERDICT_NOT_READY

    failed_path, _ = _package(tmp_path / "failed")

    def fail(payload):
        payload["status"] = "failed"
        payload["checks"][0]["passed"] = False

    _rewrite_artifact(failed_path, "control_artifacts", 0, fail)
    failed_report = evaluate_discovery_evidence_gate(
        load_discovery_evidence_manifest(failed_path)
    )
    assert failed_report.verdict == VERDICT_NOT_READY
    checks = {check.code: check for check in failed_report.checks}
    assert checks["CONTROL_ARTIFACTS_PASSED"].passed is False


def test_digest_tampering_and_policy_drift_are_structural_errors(tmp_path):
    manifest_path, manifest = _package(tmp_path / "digest")
    target = manifest_path.parent / manifest["discovery_audits"][0]["path"]
    target.write_text(target.read_text() + " ")
    with pytest.raises(ValueError, match="digest mismatch"):
        evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(manifest_path)
        )

    policy_path, policy_manifest = _package(tmp_path / "policy")
    policy_manifest["policy"]["min_clean_sessions"] = 11
    policy_path.write_text(json.dumps(policy_manifest, indent=2, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="campaign policy does not match"):
        load_discovery_evidence_manifest(policy_path)


def test_cli_exit_codes_distinguish_ready_not_ready_and_invalid(tmp_path, capsys):
    ready_path, _ = _package(tmp_path / "ready")
    assert main([str(ready_path)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == VERDICT_OWNER_REVIEW

    not_ready_path, not_ready = _package(tmp_path / "not-ready")
    not_ready["provider_proof_reports"].pop()
    not_ready_path.write_text(json.dumps(not_ready, indent=2, sort_keys=True) + "\n")
    assert main([str(not_ready_path)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == VERDICT_NOT_READY

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    assert main([str(invalid)]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_schemas_and_runtime_policy_inventory_are_exact():
    root = Path(__file__).parents[1] / "truth"
    campaign = json.loads(
        (root / "postmarket_discovery_evidence_campaign_v2.schema.json").read_text()
    )
    evidence = json.loads(
        (root / "postmarket_discovery_evidence_set_v2.schema.json").read_text()
    )
    assert set(campaign["$defs"]["policy"]["required"]) == POLICY_FIELDS
    assert set(
        evidence["$defs"]["controlArtifact"]["properties"]["kind"]["enum"]
    ) == REQUIRED_CONTROL_KINDS


def test_gate_import_graph_has_no_live_market_database_or_delivery_dependency():
    root = Path(__file__).parents[1] / "tradebot"
    imports = set()
    for name in (
        "postmarket_discovery_evidence_gate.py",
        "postmarket_discovery_evidence_campaign.py",
        "postmarket_discovery_evidence_set.py",
    ):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    forbidden = (
        "requests",
        "sqlite3",
        "tradebot.alerts",
        "tradebot.broker",
        "tradebot.order",
        "tradebot.telegram_bot",
        "tradebot.vendors",
    )
    assert not any(module.startswith(forbidden) for module in imports)
