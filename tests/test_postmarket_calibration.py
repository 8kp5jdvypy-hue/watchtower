"""Quality calibration is monotonic, holdout-sealed, and fail-closed."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_calibration import (
    CalibrationPolicy,
    ensure_calibration_schema,
    evaluate_rank_calibration,
    export_calibration_report,
    fit_rank_calibrator,
    project_rank_calibration,
)
from tradebot.postmarket_discovery import DISCOVERY_SCHEMA
from tradebot.postmarket_empirical import (
    EligibilityRule,
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
    holdout_label_inventory,
    record_independent_label,
    unblind_holdout,
)
from tradebot.postmarket_rank import RANK_SCHEMA


DEV = date(2026, 8, 20)
HOLDOUT = date(2026, 8, 21)
LIVE = date(2026, 8, 24)
LOCKED_AT = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
FITTED_AT = datetime(2026, 8, 21, 12, 30, tzinfo=timezone.utc)
POLICY = CalibrationPolicy(6, 3, 3, 6, 3, 3, 2, 0.20, 0.10)
RANK_CONTRACT = "c" * 64


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DISCOVERY_SCHEMA)
    conn.executescript(RANK_SCHEMA)
    ensure_calibration_schema(conn)
    create_locked_experiment(
        conn,
        experiment_id="calibration-v1",
        created_at=LOCKED_AT,
        created_by="owner",
        rank_version=1,
        rank_contract_sha256=RANK_CONTRACT,
        label_method="blind_bar_review",
        development_sessions=(DEV,),
        holdout_sessions=(HOLDOUT,),
        eligibility_rule=EligibilityRule(8.0, 250_000, 2),
        selection_rule=SelectionRule(60, 10),
        policy=ExperimentPolicy(0.9, 0.95, 6, 3),
    )
    return conn


def _seed_rank(conn: sqlite3.Connection, session: date, rows: list[tuple[str, float]]) -> None:
    session_text = session.isoformat()
    rank_run_id = conn.execute(
        """
        INSERT INTO postmarket_rank_runs
            (session,rank_version,rank_contract_sha256,as_of_utc,
             recorded_at_utc,code_version,run_id,
             input_digest_sha256,input_candidates,rankable_candidates,status,
             weights_json,thresholds_json)
        VALUES (?,1,?,?,?,?,?,?,?,?,'complete','{}','{}')
        """,
        (
            session_text,
            RANK_CONTRACT,
            f"{session_text}T20:10:00+00:00",
            f"{session_text}T20:10:01+00:00",
            "abc1234",
            f"rank-{session_text}",
            (session_text.replace("-", "") + "0" * 64)[:64],
            len(rows),
            len(rows),
        ),
    ).lastrowid
    for ordinal, (symbol, score) in enumerate(rows, 1):
        candidate_id = conn.execute(
            """
            INSERT INTO postmarket_discovery_candidates
                (session,symbol,event_date,direction,discovery_version,
                 first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
                 cumulative_volume,cumulative_notional,sources_json,data_feed,
                 market_data_provider,bar_timeframe,code_version,run_id)
            VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_text,
                symbol,
                session_text,
                "up",
                f"{session_text}T20:05:00+00:00",
                f"{session_text}T20:00:00+00:00",
                100,
                110,
                10,
                100_000,
                10_000_000,
                '["full_universe_sweep"]',
                "sip",
                "alpaca",
                "5Min",
                "abc1234",
                f"candidate-{session_text}-{symbol}",
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO postmarket_candidate_ranks
                (rank_run_id,candidate_id,session,symbol,direction,lifecycle_state,
                 rankable,ordinal_rank,evidence_score,raw_component_score,
                 penalty_total,evidence_coverage_pct,components_json,penalties_json,
                 exclusion_reasons_json,explanation_json)
            VALUES (?,?,?,?,?,'CONFIRMED',1,?,?,?,?,100,'{}','{}','[]','{}')
            """,
            (
                rank_run_id,
                candidate_id,
                session_text,
                symbol,
                "up",
                ordinal,
                score,
                score,
                0,
            ),
        )
    conn.commit()


def _seed_labels(conn: sqlite3.Connection, session: date, prefix: str) -> None:
    acquired = datetime.combine(
        session + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    for index in range(6):
        positive = index >= 3
        record_independent_label(
            conn,
            experiment_id="calibration-v1",
            session=session,
            symbol=f"{prefix}{index}",
            classification="eligible" if positive else "ineligible",
            direction="up" if positive else None,
            eligible_at=(
                datetime(session.year, session.month, session.day, 20, 10, tzinfo=timezone.utc)
                if positive else None
            ),
            labeler="blinded-reviewer",
            reason_code="INDEPENDENT_BAR_REVIEW",
            rationale="reviewed independently of rank output",
            artifact_sha256=f"{index + 1:x}" * 64,
            artifact_providers=("independent-source",),
            artifact_feeds=("reviewed-bars",),
            artifact_acquired_at=acquired,
            recorded_at=acquired + timedelta(minutes=index),
        )


def _seed_split(conn: sqlite3.Connection, session: date, prefix: str) -> None:
    _seed_rank(
        conn,
        session,
        [(f"{prefix}{index}", 20.0 if index < 3 else 80.0) for index in range(6)],
    )
    _seed_labels(conn, session, prefix)


def _fit(conn: sqlite3.Connection):
    return fit_rank_calibrator(
        conn,
        experiment_id="calibration-v1",
        fitted_at=FITTED_AT,
        code_version="abc1234",
        policy=POLICY,
    )


def _validate_holdout(conn: sqlite3.Connection):
    _seed_split(conn, HOLDOUT, "H")
    unblind_holdout(
        conn,
        experiment_id="calibration-v1",
        unblinded_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        unblinded_by="owner",
        reason="independent labels complete",
        expected_inventory_sha256=holdout_label_inventory(
            conn, "calibration-v1"
        )[0],
    )
    return evaluate_rank_calibration(
        conn,
        experiment_id="calibration-v1",
        split="holdout",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="abc1234",
    )


def test_development_only_isotonic_model_is_frozen_before_holdout_open():
    conn = _conn()
    _seed_split(conn, DEV, "D")

    first = _fit(conn)
    second = _fit(conn)

    assert first.created is True
    assert second.created is False
    assert first.rank_contract_sha256 == RANK_CONTRACT
    assert first.model_sha256 == second.model_sha256
    replay = fit_rank_calibrator(
        conn,
        experiment_id="calibration-v1",
        fitted_at=FITTED_AT + timedelta(minutes=5),
        code_version="def5678",
        policy=POLICY,
    )
    assert replay.created is False
    assert replay.fitted_at_utc == FITTED_AT.isoformat()
    assert replay.code_version == "abc1234"
    assert [segment.calibrated_quality for segment in first.segments] == [0.0, 1.0]
    assert first.training_brier_score == 0
    assert first.training_expected_calibration_error == 0
    with pytest.raises(sqlite3.IntegrityError, match="frozen after calibration"):
        record_independent_label(
            conn,
            experiment_id="calibration-v1",
            session=DEV,
            symbol="LATE",
            classification="ineligible",
            direction=None,
            eligible_at=None,
            labeler="reviewer",
            reason_code="LATE",
            rationale="must not change frozen development evidence",
            artifact_sha256="f" * 64,
            artifact_providers=("independent-source",),
            artifact_feeds=("reviewed-bars",),
            artifact_acquired_at=datetime(2026, 8, 21, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 8, 21, 1, 1, tzinfo=timezone.utc),
        )


def test_calibrator_rejects_mixed_rank_contract_development_evidence():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    session_text = DEV.isoformat()
    conn.execute(
        """
        INSERT INTO postmarket_rank_runs
            (session,rank_version,rank_contract_sha256,as_of_utc,
             recorded_at_utc,code_version,run_id,input_digest_sha256,
             input_candidates,rankable_candidates,status,weights_json,
             thresholds_json)
        VALUES (?,1,?,?,?,?,?,?,0,0,'complete','{}','{}')
        """,
        (
            session_text,
            "d" * 64,
            f"{session_text}T20:20:00+00:00",
            f"{session_text}T20:20:01+00:00",
            "abc1234",
            "mismatched-contract-run",
            "e" * 64,
        ),
    )
    conn.commit()

    with pytest.raises(ValueError, match="mismatched rank contracts"):
        _fit(conn)


def test_calibrator_rejects_late_fit_and_insufficient_development_sample():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    with pytest.raises(ValueError, match="before the first holdout session opens"):
        fit_rank_calibrator(
            conn,
            experiment_id="calibration-v1",
            fitted_at=datetime(2026, 8, 21, 15, tzinfo=timezone.utc),
            code_version="abc1234",
            policy=POLICY,
        )

    conn = _conn()
    _seed_rank(conn, DEV, [("ONLY", 50)])
    acquired = datetime(2026, 8, 21, 0, tzinfo=timezone.utc)
    record_independent_label(
        conn,
        experiment_id="calibration-v1",
        session=DEV,
        symbol="ONLY",
        classification="ineligible",
        direction=None,
        eligible_at=None,
        labeler="reviewer",
        reason_code="REVIEW",
        rationale="one label is not enough",
        artifact_sha256="a" * 64,
        artifact_providers=("independent-source",),
        artifact_feeds=("reviewed-bars",),
        artifact_acquired_at=acquired,
        recorded_at=acquired + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="minimum training labels"):
        _fit(conn)


def test_development_cannot_validate_quality_and_holdout_stays_sealed():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    _fit(conn)
    development = evaluate_rank_calibration(
        conn,
        experiment_id="calibration-v1",
        split="development",
        evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        code_version="abc1234",
    )
    assert development.calibrated_quality_claim_valid is False
    assert "HOLDOUT_VALIDATION_REQUIRED" in development.blocking_reasons

    _seed_split(conn, HOLDOUT, "H")
    with pytest.raises(ValueError, match="holdout is sealed"):
        evaluate_rank_calibration(
            conn,
            experiment_id="calibration-v1",
            split="holdout",
            evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
            code_version="abc1234",
        )


def test_calibration_evaluation_cannot_be_backdated_before_input_evidence():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    _fit(conn)

    with pytest.raises(ValueError, match="latest input evidence"):
        evaluate_rank_calibration(
            conn,
            experiment_id="calibration-v1",
            split="development",
            evaluated_at=datetime(2026, 8, 21, 0, 4, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_rank_calibration_runs"
    ).fetchone()[0] == 0


def test_unblinded_holdout_validates_calibration_and_exports_immutable_artifact(tmp_path):
    conn = _conn()
    _seed_split(conn, DEV, "D")
    calibrator = _fit(conn)
    _seed_split(conn, HOLDOUT, "H")
    inventory = holdout_label_inventory(conn, "calibration-v1")[0]
    unblind_holdout(
        conn,
        experiment_id="calibration-v1",
        unblinded_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        unblinded_by="owner",
        reason="independent labels complete",
        expected_inventory_sha256=inventory,
    )
    with pytest.raises(ValueError, match="cannot predate holdout unblinding"):
        evaluate_rank_calibration(
            conn,
            experiment_id="calibration-v1",
            split="holdout",
            evaluated_at=datetime(2026, 8, 22, 1, 59, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    report = evaluate_rank_calibration(
        conn,
        experiment_id="calibration-v1",
        split="holdout",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="abc1234",
    )

    assert report.holdout_unblinded is True
    assert report.calibrated_quality_claim_valid is True
    assert report.blocking_reasons == ()
    assert report.brier_score == 0
    assert report.expected_calibration_error == 0
    assert [item.labels for item in report.reliability_bins] == [3, 3]
    first = export_calibration_report(
        conn,
        experiment_id="calibration-v1",
        split="holdout",
        input_digest_sha256=report.input_digest_sha256,
        output_dir=tmp_path,
    )
    second = export_calibration_report(
        conn,
        experiment_id="calibration-v1",
        split="holdout",
        input_digest_sha256=report.input_digest_sha256,
        output_dir=tmp_path,
    )
    payload = json.loads(Path(first.path).read_text(encoding="utf-8"))
    assert first.created is True
    assert second.created is False
    assert payload["model_sha256"] == calibrator.model_sha256
    assert payload["fitted_at_utc"] == FITTED_AT.isoformat()
    assert payload["fitting_code_version"] == "abc1234"
    assert payload["model"]["method"] == "isotonic_pav"
    assert payload["development_definitive_labels"] == 6
    assert payload["report"]["calibrated_quality_claim_valid"] is True
    assert Path(first.path).stat().st_mode & 0o777 == 0o444
    with pytest.raises(ValueError, match="different attribution"):
        evaluate_rank_calibration(
            conn,
            experiment_id="calibration-v1",
            split="holdout",
            evaluated_at=datetime(2026, 8, 22, 4, tzinfo=timezone.utc),
            code_version="def5678",
        )
    assert conn.execute(
        "SELECT code_version FROM postmarket_rank_calibration_runs"
    ).fetchone()[0] == "abc1234"


def test_bad_holdout_calibration_fails_named_quality_gates():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    _fit(conn)
    # Reverse the relationship: low scores are positive and high scores negative.
    _seed_rank(
        conn,
        HOLDOUT,
        [(f"H{index}", 20.0 if index < 3 else 80.0) for index in range(6)],
    )
    acquired = datetime(2026, 8, 22, 0, tzinfo=timezone.utc)
    for index in range(6):
        positive = index < 3
        record_independent_label(
            conn,
            experiment_id="calibration-v1",
            session=HOLDOUT,
            symbol=f"H{index}",
            classification="eligible" if positive else "ineligible",
            direction="up" if positive else None,
            eligible_at=(datetime(2026, 8, 21, 20, 10, tzinfo=timezone.utc) if positive else None),
            labeler="blinded-reviewer",
            reason_code="INDEPENDENT_BAR_REVIEW",
            rationale="reviewed independently of rank output",
            artifact_sha256=f"{index + 7:x}" * 64,
            artifact_providers=("independent-source",),
            artifact_feeds=("reviewed-bars",),
            artifact_acquired_at=acquired,
            recorded_at=acquired + timedelta(minutes=index),
        )
    unblind_holdout(
        conn,
        experiment_id="calibration-v1",
        unblinded_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        unblinded_by="owner",
        reason="review complete",
        expected_inventory_sha256=holdout_label_inventory(conn, "calibration-v1")[0],
    )
    report = evaluate_rank_calibration(
        conn,
        experiment_id="calibration-v1",
        split="holdout",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="abc1234",
    )
    assert report.calibrated_quality_claim_valid is False
    assert set(report.blocking_reasons) >= {
        "BRIER_SCORE_FLOOR_NOT_MET",
        "EXPECTED_CALIBRATION_ERROR_FLOOR_NOT_MET",
    }


def test_exact_passing_model_projects_append_only_candidate_quality():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    calibrator = _fit(conn)
    _validate_holdout(conn)
    _seed_rank(conn, LIVE, [("LOW", 20), ("HIGH", 80)])
    rank_run_id = int(conn.execute(
        "SELECT MAX(rank_run_id) FROM postmarket_rank_runs"
    ).fetchone()[0])

    first = project_rank_calibration(
        conn,
        rank_run_id=rank_run_id,
        model_sha256=calibrator.model_sha256,
        projected_at=datetime(2026, 8, 24, 20, 11, tzinfo=timezone.utc),
        code_version="abc1234",
    )
    replay = project_rank_calibration(
        conn,
        rank_run_id=rank_run_id,
        model_sha256=calibrator.model_sha256,
        projected_at=datetime(2026, 8, 24, 20, 12, tzinfo=timezone.utc),
        code_version="def5678",
    )

    assert first.projected_rows == 2
    assert first.created_rows == 2
    assert replay.created_rows == 0
    assert replay.projection_digest_sha256 == first.projection_digest_sha256
    assert [row.calibrated_quality for row in first.projections] == [0, 1]
    assert {row.model_sha256 for row in first.projections} == {
        calibrator.model_sha256
    }
    assert {row.calibration_run_id for row in first.projections} == {
        first.calibration_run_id
    }
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE postmarket_rank_calibration_projections "
            "SET calibrated_quality=0.5"
        )


def test_projection_fails_closed_before_validation_or_for_late_model():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    calibrator = _fit(conn)
    _seed_rank(conn, LIVE, [("LIVE", 80)])
    live_run = int(conn.execute(
        "SELECT MAX(rank_run_id) FROM postmarket_rank_runs"
    ).fetchone()[0])
    with pytest.raises(ValueError, match="no holdout evaluation"):
        project_rank_calibration(
            conn,
            rank_run_id=live_run,
            model_sha256=calibrator.model_sha256,
            projected_at=datetime(2026, 8, 24, 20, 11, tzinfo=timezone.utc),
            code_version="abc1234",
        )

    _validate_holdout(conn)
    development_run = int(conn.execute(
        "SELECT MIN(rank_run_id) FROM postmarket_rank_runs"
    ).fetchone()[0])
    with pytest.raises(ValueError, match="frozen after|validation postdates"):
        project_rank_calibration(
            conn,
            rank_run_id=development_run,
            model_sha256=calibrator.model_sha256,
            projected_at=datetime(2026, 8, 24, 20, 11, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    with pytest.raises(ValueError, match="exactly one frozen calibrator"):
        project_rank_calibration(
            conn,
            rank_run_id=live_run,
            model_sha256="f" * 64,
            projected_at=datetime(2026, 8, 24, 20, 11, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_rank_calibration_projections"
    ).fetchone()[0] == 0


def test_failed_holdout_can_never_produce_customer_quality_projection():
    conn = _conn()
    _seed_split(conn, DEV, "D")
    calibrator = _fit(conn)
    _seed_rank(
        conn,
        HOLDOUT,
        [(f"H{index}", 20.0 if index < 3 else 80.0) for index in range(6)],
    )
    acquired = datetime(2026, 8, 22, 0, tzinfo=timezone.utc)
    for index in range(6):
        positive = index < 3
        record_independent_label(
            conn,
            experiment_id="calibration-v1",
            session=HOLDOUT,
            symbol=f"H{index}",
            classification="eligible" if positive else "ineligible",
            direction="up" if positive else None,
            eligible_at=(
                datetime(2026, 8, 21, 20, 10, tzinfo=timezone.utc)
                if positive else None
            ),
            labeler="blinded-reviewer",
            reason_code="INDEPENDENT_BAR_REVIEW",
            rationale="reviewed independently of rank output",
            artifact_sha256=f"{index + 7:x}" * 64,
            artifact_providers=("independent-source",),
            artifact_feeds=("reviewed-bars",),
            artifact_acquired_at=acquired,
            recorded_at=acquired + timedelta(minutes=index),
        )
    unblind_holdout(
        conn,
        experiment_id="calibration-v1",
        unblinded_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        unblinded_by="owner",
        reason="review complete",
        expected_inventory_sha256=holdout_label_inventory(
            conn, "calibration-v1"
        )[0],
    )
    report = evaluate_rank_calibration(
        conn,
        experiment_id="calibration-v1",
        split="holdout",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="abc1234",
    )
    assert report.calibrated_quality_claim_valid is False
    _seed_rank(conn, LIVE, [("LIVE", 80)])
    with pytest.raises(ValueError, match="not passed holdout"):
        project_rank_calibration(
            conn,
            rank_run_id=int(conn.execute(
                "SELECT MAX(rank_run_id) FROM postmarket_rank_runs"
            ).fetchone()[0]),
            model_sha256=calibrator.model_sha256,
            projected_at=datetime(2026, 8, 24, 20, 11, tzinfo=timezone.utc),
            code_version="abc1234",
        )
