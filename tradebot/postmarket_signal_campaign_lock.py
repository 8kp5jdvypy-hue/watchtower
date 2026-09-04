"""Lock discovery evidence from verified experiment and provider contracts.

This is an operator-only bridge between the mutable evidence database and the
offline campaign writer. It reads both source contracts, reproduces their
identities, and derives every shared campaign field. It has no market-data,
delivery, alert, or broker path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from tradebot.postmarket_discovery_evidence_campaign import (
    lock_discovery_evidence_campaign,
)
from tradebot.postmarket_discovery_evidence_gate import (
    _expected_sessions,
    _parse_policy,
)
from tradebot.postmarket_empirical import (
    EMPIRICAL_VERSION,
    EligibilityRule,
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
)
from tradebot.postmarket_rank import RANK_VERSION, rank_contract_sha256
from tradebot.vendors.historical_reference import provider_capabilities
from tradebot.vendors.historical_reference_qualification import (
    load_historical_reference_qualification,
)


DEFAULT_DATABASE = Path("data/postmarket_shadow.db")
DEFAULT_QUALIFICATION = Path(
    "data/postmarket_evidence/provider-qualification/qualification.json"
)


def _regular_json(path: Path, name: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{name} must be a regular non-symlink file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return payload


def _canonical_database_json(raw: object, expected: type, field: str):
    if not isinstance(raw, str):
        raise ValueError(f"locked experiment {field} must be JSON text")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"locked experiment {field} is malformed") from exc
    if not isinstance(parsed, expected):
        raise ValueError(f"locked experiment {field} has the wrong JSON type")
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != raw:
        raise ValueError(f"locked experiment {field} is not canonical")
    return parsed


def _verified_locked_experiment(database: Path, experiment_id: str) -> dict:
    if database.is_symlink() or not database.is_file():
        raise ValueError("experiment database must be a regular non-symlink file")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM postmarket_rank_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("unknown locked experiment_id")
    if row["status"] != "locked" or row["empirical_version"] != EMPIRICAL_VERSION:
        raise ValueError("experiment is not a current locked contract")
    if (
        row["rank_version"] != RANK_VERSION
        or row["rank_contract_sha256"] != rank_contract_sha256()
    ):
        raise ValueError("experiment does not use the current rank contract")

    development_raw = _canonical_database_json(
        row["development_sessions_json"], list, "development_sessions_json"
    )
    holdout_raw = _canonical_database_json(
        row["holdout_sessions_json"], list, "holdout_sessions_json"
    )
    eligibility_raw = _canonical_database_json(
        row["eligibility_rule_json"], dict, "eligibility_rule_json"
    )
    selection_raw = _canonical_database_json(
        row["selection_rule_json"], dict, "selection_rule_json"
    )
    empirical_policy_raw = _canonical_database_json(
        row["policy_json"], dict, "policy_json"
    )
    try:
        development_sessions = tuple(
            date.fromisoformat(value) for value in development_raw
        )
        holdout_sessions = tuple(date.fromisoformat(value) for value in holdout_raw)
        created_at = datetime.fromisoformat(row["created_at_utc"])
        eligibility_rule = EligibilityRule(**eligibility_raw)
        selection_rule = SelectionRule(**selection_raw)
        empirical_policy = ExperimentPolicy(**empirical_policy_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("locked experiment contract values are invalid") from exc

    scratch = sqlite3.connect(":memory:")
    try:
        reproduced_digest = create_locked_experiment(
            scratch,
            experiment_id=row["experiment_id"],
            created_at=created_at,
            created_by=row["created_by"],
            rank_version=row["rank_version"],
            rank_contract_sha256=row["rank_contract_sha256"],
            label_method=row["label_method"],
            development_sessions=development_sessions,
            holdout_sessions=holdout_sessions,
            eligibility_rule=eligibility_rule,
            selection_rule=selection_rule,
            policy=empirical_policy,
        )
    finally:
        scratch.close()
    if reproduced_digest != row["manifest_sha256"]:
        raise ValueError("locked experiment manifest digest does not reproduce")
    return {
        "experiment_id": row["experiment_id"],
        "manifest_sha256": reproduced_digest,
        "rank_version": row["rank_version"],
        "rank_contract_sha256": row["rank_contract_sha256"],
        "holdout_sessions": holdout_sessions,
        "policy": empirical_policy,
    }


def lock_signal_quality_campaign(
    output_path: Path | str,
    *,
    database_path: Path | str,
    qualification_path: Path | str,
    campaign_id: str,
    experiment_id: str,
    locked_at: datetime,
    policy: dict,
) -> tuple[str, dict]:
    """Derive every shared campaign identity from verified locked sources."""
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise ValueError("locked_at must be timezone-aware")
    locked = locked_at.astimezone(timezone.utc)
    experiment = _verified_locked_experiment(
        Path(database_path).absolute(), experiment_id
    )
    qualification_file = Path(qualification_path).absolute()
    qualification = load_historical_reference_qualification(
        qualification_file,
        observed_at=locked,
    )
    capabilities = provider_capabilities(
        qualification.provider,
        qualification_manifest=qualification_file,
        observed_at=locked,
    )
    if not capabilities.recall_proof_eligible:
        raise ValueError("qualified provider is not recall-proof eligible")

    validated_policy = _parse_policy(policy)
    if validated_policy.allowed_independent_market_data_providers != (
        qualification.provider,
    ):
        raise ValueError("campaign provider does not match qualification")
    if validated_policy.allowed_independent_datasets != (qualification.dataset,):
        raise ValueError("campaign dataset does not match qualification")
    if qualification.provider in (
        validated_policy.allowed_primary_market_data_providers
    ):
        raise ValueError("independent provider must differ from primary provider")
    empirical_policy = experiment["policy"]
    if (
        validated_policy.min_definitive_labels
        != empirical_policy.min_definitive_labels
        or validated_policy.min_positive_labels != empirical_policy.min_positive_labels
        or validated_policy.min_empirical_recall != empirical_policy.min_recall
        or validated_policy.min_empirical_precision != empirical_policy.min_precision
    ):
        raise ValueError("campaign empirical floors do not match locked experiment")

    holdout_sessions = experiment["holdout_sessions"]
    expected_sessions = _expected_sessions(
        holdout_sessions[0], holdout_sessions[-1]
    )
    if holdout_sessions != expected_sessions:
        raise ValueError("experiment holdout must be one contiguous XNYS campaign")
    return lock_discovery_evidence_campaign(
        output_path,
        campaign_id=campaign_id,
        locked_at=locked,
        coverage_start=holdout_sessions[0],
        coverage_end=holdout_sessions[-1],
        experiment_id=experiment["experiment_id"],
        experiment_manifest_sha256=experiment["manifest_sha256"],
        rank_version=experiment["rank_version"],
        rank_contract_sha256=experiment["rank_contract_sha256"],
        policy=policy,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--provider-qualification", type=Path, default=DEFAULT_QUALIFICATION
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args(argv)
    policy_path = args.policy.absolute()
    policy = _regular_json(policy_path, "campaign policy")
    _parse_policy(policy)
    digest, payload = lock_signal_quality_campaign(
        args.output,
        database_path=args.database,
        qualification_path=args.provider_qualification,
        campaign_id=args.campaign_id,
        experiment_id=args.experiment_id,
        locked_at=datetime.now(timezone.utc),
        policy=policy,
    )
    print(json.dumps({
        "campaign_id": payload["campaign_id"],
        "campaign_sha256": digest,
        "coverage_start": payload["coverage_start"],
        "coverage_end": payload["coverage_end"],
        "experiment_id": payload["experiment_id"],
        "experiment_manifest_sha256": payload["experiment_manifest_sha256"],
        "provider": policy["allowed_independent_market_data_providers"][0],
        "dataset": policy["allowed_independent_datasets"][0],
        "rank_version": payload["rank_version"],
        "rank_contract_sha256": payload["rank_contract_sha256"],
        "path": str(args.output),
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
