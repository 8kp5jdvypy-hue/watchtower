"""The discovery campaign derives identity from locked source contracts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from datetime import date, datetime, timezone

import pytest

from tradebot import postmarket_signal_campaign_lock as campaign_lock_module
from tradebot.postmarket_calibration import (
    CALIBRATION_VERSION,
    ensure_calibration_schema,
)
from tradebot.postmarket_signal_campaign_lock import (
    lock_signal_quality_campaign,
    main,
)
from tradebot.postmarket_empirical import (
    EligibilityRule,
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
)
from tradebot.postmarket_rank import RANK_VERSION, rank_contract_sha256
from tradebot.vendors.historical_reference import REFERENCE_PROVIDER_DATASETS
from tradebot.vendors.historical_reference_qualification import (
    QUALIFICATION_SCHEMA_VERSION,
    REQUIRED_QUALIFICATION_PROOFS,
)


DEVELOPMENT = tuple(
    date(2026, month, day)
    for month, day in (
        (7, 27),
        (7, 28),
        (7, 29),
        (7, 30),
        (7, 31),
        (8, 3),
        (8, 4),
        (8, 5),
        (8, 6),
        (8, 7),
    )
)
HOLDOUT = tuple(
    date(2026, 8, day)
    for day in (17, 18, 19, 20, 21, 24, 25, 26, 27, 28)
)
EXPERIMENT_CREATED = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
CAMPAIGN_LOCKED = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
EXPERIMENT_POLICY = ExperimentPolicy(0.9, 0.95, 100, 30)
CALIBRATOR_FITTED = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)


def _calibration_policy() -> dict:
    return {
        "min_training_labels": 100,
        "min_training_positive_labels": 30,
        "min_training_negative_labels": 70,
        "min_holdout_labels": 100,
        "min_holdout_positive_labels": 30,
        "min_holdout_negative_labels": 70,
        "minimum_bin_labels": 20,
        "max_brier_score": 0.2,
        "max_expected_calibration_error": 0.1,
    }


def _calibrator_model() -> tuple[str, str]:
    development_digest = "d" * 64
    model = {
        "calibration_version": CALIBRATION_VERSION,
        "experiment_id": "marketwide-holdout-1",
        "rank_contract_sha256": rank_contract_sha256(),
        "method": "isotonic_pav",
        "scope": "first_rankable_score_same_direction_quality",
        "development_input_sha256": development_digest,
        "policy": _calibration_policy(),
        "segments": [
            {
                "minimum_score": 0.0,
                "maximum_score": 49.0,
                "calibrated_quality": 0.0,
                "development_labels": 70,
                "development_positives": 0,
            },
            {
                "minimum_score": 50.0,
                "maximum_score": 100.0,
                "calibrated_quality": 1.0,
                "development_labels": 30,
                "development_positives": 30,
            },
        ],
    }
    return (
        json.dumps(model, sort_keys=True, separators=(",", ":")),
        development_digest,
    )


def _policy() -> dict:
    return {
        "min_clean_sessions": 10,
        "min_definitive_labels": 100,
        "min_positive_labels": 30,
        "min_empirical_recall": 0.95,
        "min_empirical_precision": 0.9,
        "min_calibration_negative_labels": 70,
        "min_calibration_bin_labels": 20,
        "max_calibration_brier_score": 0.2,
        "max_expected_calibration_error": 0.1,
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
        "allowed_independent_datasets": [
            REFERENCE_PROVIDER_DATASETS["massive"]
        ],
        "allowed_audit_versions": [4],
        "allowed_discovery_versions": [1],
        "allowed_audit_code_versions": ["abc1234"],
        "allowed_observer_code_versions": ["abc1234"],
        "allowed_census_code_versions": ["abc1234"],
        "allowed_provider_proof_code_versions": ["abc1234"],
        "allowed_empirical_code_versions": ["abc1234"],
        "allowed_calibration_code_versions": ["abc1234"],
        "allowed_control_code_versions": ["abc1234"],
        "require_zero_dirty_sessions": True,
        "require_complete_session_inventory": True,
        "require_zero_unavailable_symbols": True,
        "require_zero_provider_price_disagreements": True,
        "require_zero_ambiguous_labels": True,
        "require_zero_direction_mismatches": True,
        "require_zero_duplicate_candidates": True,
    }


def _qualification(path, *, approved_at="2026-08-08T00:00:00+00:00"):
    payload = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "status": "qualified",
        "provider": "massive",
        "dataset": REFERENCE_PROVIDER_DATASETS["massive"],
        "approved_at_utc": approved_at,
        "approved_by": "evidence-owner",
        "license_reference": "executed-agreement",
        "proofs": [
            {
                "kind": kind,
                "reference": f"evidence/{kind}.json",
                "sha256": hashlib.sha256(kind.encode()).hexdigest(),
            }
            for kind in sorted(REQUIRED_QUALIFICATION_PROOFS)
        ],
    }
    path.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _experiment(
    path,
    *,
    holdout=HOLDOUT,
    with_calibrator=True,
    calibrator_fitted=CALIBRATOR_FITTED,
) -> str:
    conn = sqlite3.connect(path)
    try:
        digest = create_locked_experiment(
            conn,
            experiment_id="marketwide-holdout-1",
            created_at=EXPERIMENT_CREATED,
            created_by="evidence-owner",
            rank_version=RANK_VERSION,
            rank_contract_sha256=rank_contract_sha256(),
            label_method="blind_bar_review",
            development_sessions=DEVELOPMENT,
            holdout_sessions=holdout,
            eligibility_rule=EligibilityRule(5.0, 100_000.0, 2),
            selection_rule=SelectionRule(60.0, 20),
            policy=EXPERIMENT_POLICY,
        )
        ensure_calibration_schema(conn)
        if with_calibrator:
            model_raw, development_digest = _calibrator_model()
            conn.execute(
                """
                INSERT INTO postmarket_rank_calibrators
                    (experiment_id,calibration_version,fitted_at_utc,code_version,
                     method,development_input_sha256,policy_json,model_json,
                     model_sha256,definitive_labels,positive_labels,negative_labels,
                     training_brier_score,training_expected_calibration_error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "marketwide-holdout-1",
                    CALIBRATION_VERSION,
                    calibrator_fitted.isoformat(),
                    "abc1234",
                    "isotonic_pav",
                    development_digest,
                    json.dumps(
                        _calibration_policy(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    model_raw,
                    hashlib.sha256(model_raw.encode()).hexdigest(),
                    100,
                    30,
                    70,
                    0.0,
                    0.0,
                ),
            )
            conn.commit()
        return digest
    finally:
        conn.close()


def test_verified_lock_derives_every_shared_identity_without_mutating_database(
    tmp_path,
):
    database = tmp_path / "shadow.db"
    experiment_digest = _experiment(database)
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)
    database_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    output = tmp_path / "discovery-campaign.json"

    campaign_digest, payload = lock_signal_quality_campaign(
        output,
        database_path=database,
        qualification_path=qualification,
        campaign_id="discovery-campaign-1",
        experiment_id="marketwide-holdout-1",
        locked_at=CAMPAIGN_LOCKED,
        policy=_policy(),
    )

    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_digest
    assert campaign_digest == hashlib.sha256(output.read_bytes()).hexdigest()
    assert payload["coverage_start"] == HOLDOUT[0].isoformat()
    assert payload["coverage_end"] == HOLDOUT[-1].isoformat()
    assert payload["experiment_manifest_sha256"] == experiment_digest
    assert payload["rank_version"] == RANK_VERSION
    assert payload["rank_contract_sha256"] == rank_contract_sha256()
    assert stat.S_IMODE(output.stat().st_mode) == 0o444


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "allowed_independent_market_data_providers",
            ["tiingo"],
            "provider does not match qualification",
        ),
        (
            "allowed_independent_datasets",
            ["foreign/dataset"],
            "dataset does not match qualification",
        ),
        (
            "allowed_primary_market_data_providers",
            ["massive"],
            "independent provider must differ from primary provider",
        ),
        (
            "min_definitive_labels",
            101,
            "empirical floors do not match locked experiment",
        ),
        (
            "min_calibration_bin_labels",
            21,
            "calibration floors do not match frozen calibrator",
        ),
    ),
)
def test_verified_lock_rejects_manually_drifted_shared_contracts(
    tmp_path, field, value, message,
):
    database = tmp_path / "shadow.db"
    _experiment(database)
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)
    policy = _policy()
    policy[field] = value

    with pytest.raises(ValueError, match=message):
        lock_signal_quality_campaign(
            tmp_path / "campaign.json",
            database_path=database,
            qualification_path=qualification,
            campaign_id="discovery-campaign-1",
            experiment_id="marketwide-holdout-1",
            locked_at=CAMPAIGN_LOCKED,
            policy=policy,
        )


def test_verified_lock_requires_qualification_to_precede_campaign(tmp_path):
    database = tmp_path / "shadow.db"
    _experiment(database)
    qualification = tmp_path / "qualification.json"
    _qualification(qualification, approved_at="2026-08-15T00:00:00+00:00")

    with pytest.raises(ValueError, match="future"):
        lock_signal_quality_campaign(
            tmp_path / "campaign.json",
            database_path=database,
            qualification_path=qualification,
            campaign_id="discovery-campaign-1",
            experiment_id="marketwide-holdout-1",
            locked_at=CAMPAIGN_LOCKED,
            policy=_policy(),
        )


def test_verified_lock_requires_a_frozen_development_calibrator(tmp_path):
    database = tmp_path / "shadow.db"
    _experiment(database, with_calibrator=False)
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)

    with pytest.raises(ValueError, match="no frozen calibrator"):
        lock_signal_quality_campaign(
            tmp_path / "campaign.json",
            database_path=database,
            qualification_path=qualification,
            campaign_id="discovery-campaign-1",
            experiment_id="marketwide-holdout-1",
            locked_at=CAMPAIGN_LOCKED,
            policy=_policy(),
        )


def test_verified_lock_cannot_predate_the_frozen_calibrator(tmp_path):
    database = tmp_path / "shadow.db"
    _experiment(
        database,
        calibrator_fitted=datetime(2026, 8, 14, 13, tzinfo=timezone.utc),
    )
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)

    with pytest.raises(ValueError, match="cannot predate"):
        lock_signal_quality_campaign(
            tmp_path / "campaign.json",
            database_path=database,
            qualification_path=qualification,
            campaign_id="discovery-campaign-1",
            experiment_id="marketwide-holdout-1",
            locked_at=CAMPAIGN_LOCKED,
            policy=_policy(),
        )


def test_verified_lock_rejects_a_noncontiguous_holdout(tmp_path):
    database = tmp_path / "shadow.db"
    _experiment(database, holdout=HOLDOUT[:5] + HOLDOUT[6:])
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)

    with pytest.raises(ValueError, match="contiguous XNYS campaign"):
        lock_signal_quality_campaign(
            tmp_path / "campaign.json",
            database_path=database,
            qualification_path=qualification,
            campaign_id="discovery-campaign-1",
            experiment_id="marketwide-holdout-1",
            locked_at=CAMPAIGN_LOCKED,
            policy=_policy(),
        )


def test_verified_lock_reproduces_the_experiment_manifest(tmp_path):
    database = tmp_path / "shadow.db"
    _experiment(database)
    conn = sqlite3.connect(database)
    with conn:
        conn.execute("DROP TRIGGER postmarket_rank_experiments_no_update")
        conn.execute(
            "UPDATE postmarket_rank_experiments SET manifest_sha256=?",
            ("f" * 64,),
        )
    conn.close()
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)

    with pytest.raises(ValueError, match="manifest digest does not reproduce"):
        lock_signal_quality_campaign(
            tmp_path / "campaign.json",
            database_path=database,
            qualification_path=qualification,
            campaign_id="discovery-campaign-1",
            experiment_id="marketwide-holdout-1",
            locked_at=CAMPAIGN_LOCKED,
            policy=_policy(),
        )


def test_operator_cli_uses_policy_file_and_derives_contracts(
    tmp_path, monkeypatch, capsys,
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return CAMPAIGN_LOCKED

    database = tmp_path / "shadow.db"
    _experiment(database)
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_policy(), sort_keys=True), encoding="utf-8")
    output = tmp_path / "campaign.json"
    monkeypatch.setattr(campaign_lock_module, "datetime", FrozenDateTime)

    result = main([
        str(output),
        "--database", str(database),
        "--provider-qualification", str(qualification),
        "--policy", str(policy),
        "--campaign-id", "discovery-campaign-1",
        "--experiment-id", "marketwide-holdout-1",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["campaign_sha256"] == hashlib.sha256(
        output.read_bytes()
    ).hexdigest()
    assert payload["experiment_manifest_sha256"] == json.loads(
        output.read_text(encoding="utf-8")
    )["experiment_manifest_sha256"]
