"""Prospectively lock market-wide discovery evidence and empirical identity."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from tradebot.postmarket_discovery_evidence_gate import (
    CALENDAR,
    CAMPAIGN_SCHEMA_VERSION,
    _expected_sessions,
    _parse_policy,
    _sha256,
)


def lock_discovery_evidence_campaign(
    output_path: Path | str,
    *,
    campaign_id: str,
    locked_at: datetime,
    coverage_start: date,
    coverage_end: date,
    experiment_id: str,
    experiment_manifest_sha256: str,
    rank_version: int,
    rank_contract_sha256: str,
    policy: dict,
) -> tuple[str, dict]:
    """Create one immutable campaign before its first XNYS session opens."""
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError("campaign_id must be non-empty")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("experiment_id must be non-empty")
    if not isinstance(rank_version, int) or isinstance(rank_version, bool) or rank_version <= 0:
        raise ValueError("rank_version must be a positive integer")
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise ValueError("locked_at must be timezone-aware")
    locked = locked_at.astimezone(timezone.utc)
    sessions = _expected_sessions(coverage_start, coverage_end)
    if not sessions:
        raise ValueError("campaign coverage contains no XNYS sessions")
    validated_policy = _parse_policy(policy)
    if len(sessions) < validated_policy.min_clean_sessions:
        raise ValueError(
            "campaign coverage contains fewer XNYS sessions than policy.min_clean_sessions"
        )
    first_open = CALENDAR.session_open(sessions[0]).to_pydatetime().astimezone(timezone.utc)
    if locked >= first_open:
        raise ValueError("campaign must be locked before its first session opens")
    payload = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "status": "locked",
        "campaign_id": campaign_id.strip(),
        "locked_at_utc": locked.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "experiment_id": experiment_id.strip(),
        "experiment_manifest_sha256": _sha256(
            experiment_manifest_sha256,
            "experiment_manifest_sha256",
        ),
        "rank_version": rank_version,
        "rank_contract_sha256": _sha256(
            rank_contract_sha256,
            "rank_contract_sha256",
        ),
        "policy": asdict(validated_policy),
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    payload = json.loads(raw)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("campaign output cannot be a symlink")
    descriptor = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        os.link(temporary_path, output, follow_symlinks=False)
        temporary_path.unlink()
        temporary_path = None
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(raw).hexdigest(), payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--coverage-start", type=date.fromisoformat, required=True)
    parser.add_argument("--coverage-end", type=date.fromisoformat, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--experiment-manifest-sha256", required=True)
    parser.add_argument("--rank-version", type=int, required=True)
    parser.add_argument("--rank-contract-sha256", required=True)
    parser.add_argument("--min-clean-sessions", type=int, required=True)
    parser.add_argument("--min-definitive-labels", type=int, required=True)
    parser.add_argument("--min-positive-labels", type=int, required=True)
    parser.add_argument("--min-empirical-recall", type=float, required=True)
    parser.add_argument("--min-empirical-precision", type=float, required=True)
    parser.add_argument("--min-calibration-negative-labels", type=int, required=True)
    parser.add_argument("--min-calibration-bin-labels", type=int, required=True)
    parser.add_argument("--max-calibration-brier-score", type=float, required=True)
    parser.add_argument("--max-expected-calibration-error", type=float, required=True)
    parser.add_argument("--min-primary-recall", type=float, required=True)
    parser.add_argument("--max-primary-detection-latency-seconds", type=float, required=True)
    parser.add_argument("--min-provider-comparable-coverage", type=float, required=True)
    parser.add_argument("--min-provider-bar-overlap-coverage", type=float, required=True)
    parser.add_argument("--min-provider-eligible-pair-agreement", type=float, required=True)
    parser.add_argument("--min-provider-independent-recall", type=float, required=True)
    parser.add_argument("--max-provider-close-difference-bps", type=float, required=True)
    parser.add_argument("--min-window-coverage-pct", type=float, required=True)
    parser.add_argument("--max-discovery-tick-gap-seconds", type=float, required=True)
    parser.add_argument("--max-discovery-processing-latency-seconds", type=float, required=True)
    parser.add_argument("--max-discovery-scheduled-lag-seconds", type=float, required=True)
    parser.add_argument("--allowed-data-feed", action="append", required=True)
    parser.add_argument("--allowed-primary-market-data-provider", action="append", required=True)
    parser.add_argument(
        "--allowed-independent-market-data-provider", action="append", required=True
    )
    parser.add_argument("--allowed-independent-dataset", action="append", required=True)
    parser.add_argument("--allowed-audit-version", action="append", type=int, required=True)
    parser.add_argument("--allowed-discovery-version", action="append", type=int, required=True)
    parser.add_argument("--allowed-audit-code-version", action="append", required=True)
    parser.add_argument("--allowed-observer-code-version", action="append", required=True)
    parser.add_argument("--allowed-census-code-version", action="append", required=True)
    parser.add_argument("--allowed-provider-proof-code-version", action="append", required=True)
    parser.add_argument("--allowed-empirical-code-version", action="append", required=True)
    parser.add_argument("--allowed-calibration-code-version", action="append", required=True)
    parser.add_argument("--allowed-control-code-version", action="append", required=True)
    args = parser.parse_args(argv)
    policy = {
        "min_clean_sessions": args.min_clean_sessions,
        "min_definitive_labels": args.min_definitive_labels,
        "min_positive_labels": args.min_positive_labels,
        "min_empirical_recall": args.min_empirical_recall,
        "min_empirical_precision": args.min_empirical_precision,
        "min_calibration_negative_labels": args.min_calibration_negative_labels,
        "min_calibration_bin_labels": args.min_calibration_bin_labels,
        "max_calibration_brier_score": args.max_calibration_brier_score,
        "max_expected_calibration_error": args.max_expected_calibration_error,
        "min_primary_recall": args.min_primary_recall,
        "max_primary_detection_latency_seconds": args.max_primary_detection_latency_seconds,
        "min_provider_comparable_coverage": args.min_provider_comparable_coverage,
        "min_provider_bar_overlap_coverage": args.min_provider_bar_overlap_coverage,
        "min_provider_eligible_pair_agreement": args.min_provider_eligible_pair_agreement,
        "min_provider_independent_recall": args.min_provider_independent_recall,
        "max_provider_close_difference_bps": args.max_provider_close_difference_bps,
        "min_window_coverage_pct": args.min_window_coverage_pct,
        "max_discovery_tick_gap_seconds": args.max_discovery_tick_gap_seconds,
        "max_discovery_processing_latency_seconds": args.max_discovery_processing_latency_seconds,
        "max_discovery_scheduled_lag_seconds": args.max_discovery_scheduled_lag_seconds,
        "allowed_data_feeds": args.allowed_data_feed,
        "allowed_primary_market_data_providers": args.allowed_primary_market_data_provider,
        "allowed_independent_market_data_providers": (
            args.allowed_independent_market_data_provider
        ),
        "allowed_independent_datasets": args.allowed_independent_dataset,
        "allowed_audit_versions": args.allowed_audit_version,
        "allowed_discovery_versions": args.allowed_discovery_version,
        "allowed_audit_code_versions": args.allowed_audit_code_version,
        "allowed_observer_code_versions": args.allowed_observer_code_version,
        "allowed_census_code_versions": args.allowed_census_code_version,
        "allowed_provider_proof_code_versions": args.allowed_provider_proof_code_version,
        "allowed_empirical_code_versions": args.allowed_empirical_code_version,
        "allowed_calibration_code_versions": args.allowed_calibration_code_version,
        "allowed_control_code_versions": args.allowed_control_code_version,
        "require_zero_dirty_sessions": True,
        "require_complete_session_inventory": True,
        "require_zero_unavailable_symbols": True,
        "require_zero_provider_price_disagreements": True,
        "require_zero_ambiguous_labels": True,
        "require_zero_direction_mismatches": True,
        "require_zero_duplicate_candidates": True,
    }
    digest, payload = lock_discovery_evidence_campaign(
        args.output,
        campaign_id=args.campaign_id,
        locked_at=datetime.now(timezone.utc),
        coverage_start=args.coverage_start,
        coverage_end=args.coverage_end,
        experiment_id=args.experiment_id,
        experiment_manifest_sha256=args.experiment_manifest_sha256,
        rank_version=args.rank_version,
        rank_contract_sha256=args.rank_contract_sha256,
        policy=policy,
    )
    print(
        json.dumps(
            {
                "campaign_id": payload["campaign_id"],
                "coverage_start": payload["coverage_start"],
                "coverage_end": payload["coverage_end"],
                "experiment_id": payload["experiment_id"],
                "experiment_manifest_sha256": payload["experiment_manifest_sha256"],
                "rank_version": payload["rank_version"],
                "rank_contract_sha256": payload["rank_contract_sha256"],
                "campaign_sha256": digest,
                "path": str(args.output),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
