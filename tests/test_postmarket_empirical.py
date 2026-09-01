"""Empirical rank qualification is locked, blinded, and fail-closed."""
from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_discovery import DISCOVERY_SCHEMA
from tradebot.postmarket_empirical import (
    EligibilityRule,
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
    ensure_empirical_schema,
    evaluate_rank_experiment,
    export_empirical_report,
    holdout_label_inventory,
    record_independent_label,
    unblind_holdout,
)
from tradebot.postmarket_rank import RANK_SCHEMA


DEV = date(2026, 8, 20)
HOLDOUT = date(2026, 8, 21)
LOCKED_AT = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
RULE = SelectionRule(minimum_evidence_score=60, maximum_ordinal_rank=2)
ELIGIBILITY = EligibilityRule(
    move_pct=8.0, min_cumulative_notional=250_000, persistence_bars=2,
)
POLICY = ExperimentPolicy(
    min_precision=0.90,
    min_recall=0.95,
    min_definitive_labels=3,
    min_positive_labels=2,
)
RANK_CONTRACT = "c" * 64


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DISCOVERY_SCHEMA)
    conn.executescript(RANK_SCHEMA)
    ensure_empirical_schema(conn)
    return conn


def _lock(conn):
    return create_locked_experiment(
        conn,
        experiment_id="rank-v1-exp-1",
        created_at=LOCKED_AT,
        created_by="owner",
        rank_version=1,
        rank_contract_sha256=RANK_CONTRACT,
        label_method="blind_bar_review",
        development_sessions=(DEV,),
        holdout_sessions=(HOLDOUT,),
        eligibility_rule=ELIGIBILITY,
        selection_rule=RULE,
        policy=POLICY,
    )


def _label(conn, session, symbol, classification, direction=None):
    acquired = datetime.combine(session + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return record_independent_label(
        conn,
        experiment_id="rank-v1-exp-1",
        session=session,
        symbol=symbol,
        classification=classification,
        direction=direction,
        eligible_at=(
            datetime(session.year, session.month, session.day, 20, 10, tzinfo=timezone.utc)
            if classification == "eligible"
            else None
        ),
        labeler="blinded-reviewer",
        reason_code="INDEPENDENT_BAR_REVIEW",
        rationale="reviewed without candidate or rank output",
        artifact_sha256="a" * 64,
        artifact_providers=("independent-review-source",),
        artifact_feeds=("reviewed-bars",),
        artifact_acquired_at=acquired,
        recorded_at=acquired + timedelta(minutes=1),
    )


def _seed_session(conn, session):
    session_text = session.isoformat()
    candidate_ids = {}
    for index, (symbol, score) in enumerate((("AAA", 90.0), ("BBB", 70.0), ("CCC", 40.0)), 1):
        cursor = conn.execute(
            """
            INSERT INTO postmarket_discovery_candidates
                (session,symbol,event_date,direction,discovery_version,
                 first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
                 cumulative_volume,cumulative_notional,sources_json,data_feed,
                 market_data_provider,bar_timeframe,code_version,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_text, symbol, session_text, "up", 1,
                f"{session_text}T20:{index:02d}:00+00:00",
                f"{session_text}T20:{index:02d}:00+00:00", 100, 110, 10,
                100_000, 10_000_000, '["market_gainer"]', "sip", "alpaca",
                "5Min", "rank-code", f"candidate-{session_text}-{symbol}",
            ),
        )
        candidate_ids[symbol] = cursor.lastrowid
    run = conn.execute(
        """
        INSERT INTO postmarket_rank_runs
            (session,rank_version,rank_contract_sha256,as_of_utc,
             recorded_at_utc,code_version,run_id,
             input_digest_sha256,input_candidates,rankable_candidates,status,
             weights_json,thresholds_json)
        VALUES (?,1,?,?,?,?,?,?,3,3,'complete','{}','{}')
        """,
        (
            session_text, RANK_CONTRACT, f"{session_text}T20:20:00+00:00",
            f"{session_text}T20:20:01+00:00", "rank-code",
            f"rank-{session_text}", (session_text.replace("-", "") + "0" * 64)[:64],
        ),
    ).lastrowid
    for ordinal, (symbol, score) in enumerate((("AAA", 90.0), ("BBB", 70.0), ("CCC", 40.0)), 1):
        conn.execute(
            """
            INSERT INTO postmarket_candidate_ranks
                (rank_run_id,candidate_id,session,symbol,direction,lifecycle_state,
                 rankable,ordinal_rank,evidence_score,raw_component_score,
                 penalty_total,evidence_coverage_pct,components_json,penalties_json,
                 exclusion_reasons_json,explanation_json)
            VALUES (?,?,?,?,?,'CONFIRMED',1,?,?,?,?,100,'{}','{}','[]','{}')
            """,
            (run, candidate_ids[symbol], session_text, symbol, "up", ordinal, score, score, 0),
        )
    conn.commit()


def _seed_labels(conn, session):
    _label(conn, session, "AAA", "eligible", "up")
    _label(conn, session, "BBB", "eligible", "up")
    _label(conn, session, "CCC", "ineligible")


def test_manifest_locks_disjoint_walk_forward_split_and_owner_policy():
    conn = _conn()
    first = _lock(conn)
    assert first == _lock(conn)

    with pytest.raises(ValueError, match="different manifest"):
        create_locked_experiment(
            conn, experiment_id="rank-v1-exp-1", created_at=LOCKED_AT,
            created_by="owner", rank_version=1, label_method="blind_bar_review",
            rank_contract_sha256=RANK_CONTRACT,
            development_sessions=(DEV,), holdout_sessions=(HOLDOUT,),
            eligibility_rule=ELIGIBILITY,
            selection_rule=SelectionRule(61, 2), policy=POLICY,
        )


def test_legacy_experiment_migrates_but_cannot_claim_empirical_attribution():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE postmarket_rank_experiments (
          experiment_id TEXT PRIMARY KEY, empirical_version INTEGER NOT NULL,
          status TEXT NOT NULL, created_at_utc TEXT NOT NULL,
          created_by TEXT NOT NULL, rank_version INTEGER NOT NULL,
          label_method TEXT NOT NULL, development_sessions_json TEXT NOT NULL,
          holdout_sessions_json TEXT NOT NULL, eligibility_rule_json TEXT,
          selection_rule_json TEXT NOT NULL, policy_json TEXT NOT NULL,
          manifest_sha256 TEXT NOT NULL UNIQUE
        );
        """
    )
    ensure_empirical_schema(conn)
    conn.executescript(DISCOVERY_SCHEMA)
    conn.executescript(RANK_SCHEMA)
    conn.execute(
        """
        INSERT INTO postmarket_rank_experiments
          (experiment_id,empirical_version,status,created_at_utc,created_by,
           rank_version,label_method,development_sessions_json,
           holdout_sessions_json,eligibility_rule_json,selection_rule_json,
           policy_json,manifest_sha256)
        VALUES ('legacy',1,'locked',?,'owner',1,'blind_bar_review',?,?,?,?,?,?)
        """,
        (
            LOCKED_AT.isoformat(),
            json.dumps([DEV.isoformat()]),
            json.dumps([HOLDOUT.isoformat()]),
            json.dumps({"move_pct": 8.0, "min_cumulative_notional": 250000,
                        "persistence_bars": 2}),
            json.dumps({"minimum_evidence_score": 60,
                        "maximum_ordinal_rank": 2}),
            json.dumps({"min_precision": .9, "min_recall": .95,
                        "min_definitive_labels": 3, "min_positive_labels": 2}),
            "a" * 64,
        ),
    )

    with pytest.raises(ValueError, match="missing an attributable rank contract"):
        evaluate_rank_experiment(
            conn,
            experiment_id="legacy",
            split="development",
            evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    with pytest.raises(ValueError, match="disjoint"):
        create_locked_experiment(
            conn, experiment_id="overlap", created_at=LOCKED_AT,
            created_by="owner", rank_version=1, label_method="blind_bar_review",
            rank_contract_sha256=RANK_CONTRACT,
            development_sessions=(DEV,), holdout_sessions=(DEV,),
            eligibility_rule=ELIGIBILITY,
            selection_rule=RULE, policy=POLICY,
        )
    with pytest.raises(ValueError, match="at least 0.95"):
        create_locked_experiment(
            conn, experiment_id="weak", created_at=LOCKED_AT,
            created_by="owner", rank_version=1, label_method="blind_bar_review",
            rank_contract_sha256=RANK_CONTRACT,
            development_sessions=(DEV,), holdout_sessions=(HOLDOUT,),
            eligibility_rule=ELIGIBILITY,
            selection_rule=RULE,
            policy=ExperimentPolicy(.9, .9, 3, 2),
        )
    with pytest.raises(ValueError, match="before the first holdout session opens"):
        create_locked_experiment(
            conn,
            experiment_id="late-lock",
            created_at=datetime(2026, 8, 21, 14, tzinfo=timezone.utc),
            created_by="owner",
            rank_version=1,
            rank_contract_sha256=RANK_CONTRACT,
            label_method="blind_bar_review",
            development_sessions=(DEV,),
            holdout_sessions=(HOLDOUT,),
            eligibility_rule=ELIGIBILITY,
            selection_rule=RULE,
            policy=POLICY,
        )


def test_label_writer_is_rank_blind_append_only_and_freezes_holdout():
    conn = _conn()
    experiment_manifest_sha256 = _lock(conn)
    assert "postmarket_candidate_ranks" not in inspect.getsource(record_independent_label)
    first = _label(conn, HOLDOUT, "AAA", "eligible", "up")
    assert first > 0
    inventory, count, _ = holdout_label_inventory(conn, "rank-v1-exp-1")
    assert count == 1
    with pytest.raises(ValueError, match="digest did not match"):
        unblind_holdout(
            conn, experiment_id="rank-v1-exp-1",
            unblinded_at=datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
            unblinded_by="owner", reason="independent labels are complete",
            expected_inventory_sha256="0" * 64,
        )
    assert conn.execute("SELECT COUNT(*) FROM postmarket_holdout_unblinds").fetchone()[0] == 0
    digest = unblind_holdout(
        conn, experiment_id="rank-v1-exp-1",
        unblinded_at=datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
        unblinded_by="owner", reason="independent labels are complete",
        expected_inventory_sha256=inventory,
    )
    assert len(digest) == 64
    with pytest.raises(ValueError, match="frozen"):
        _label(conn, HOLDOUT, "AAA", "ineligible")
    with pytest.raises(sqlite3.IntegrityError, match="frozen after unblinding"):
        conn.execute(
            """
            INSERT INTO postmarket_independent_labels
                (experiment_id,session,symbol,revision,classification,direction,
                 eligible_at_utc,labeler,label_method,blinded_to_rank,reason_code,
                 rationale,artifact_sha256,artifact_providers_json,
                 artifact_feeds_json,artifact_acquired_at_utc,recorded_at_utc)
            SELECT experiment_id,session,'ZZZ',1,classification,direction,
                   eligible_at_utc,labeler,label_method,blinded_to_rank,reason_code,
                   rationale,artifact_sha256,artifact_providers_json,
                   artifact_feeds_json,artifact_acquired_at_utc,recorded_at_utc
            FROM postmarket_independent_labels WHERE label_id=?
            """,
            (first,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_independent_labels SET rationale='changed'")


def test_development_report_compares_baseline_to_locked_rank_rule():
    conn = _conn()
    _lock(conn)
    _seed_session(conn, DEV)
    _seed_labels(conn, DEV)

    report = evaluate_rank_experiment(
        conn, experiment_id="rank-v1-exp-1", split="development",
        evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        code_version="abc1234",
    )

    assert report.holdout_unblinded is False
    assert report.rank_contract_sha256 == RANK_CONTRACT
    assert report.baseline.precision == pytest.approx(2 / 3)
    assert report.baseline.recall == 1
    assert report.candidate_rank.precision == 1
    assert report.candidate_rank.recall == 1
    assert report.precision_delta == pytest.approx(1 / 3)
    assert report.passed_locked_policy is True
    assert report.blocking_reasons == ()
    assert conn.execute("SELECT COUNT(*) FROM postmarket_rank_empirical_runs").fetchone()[0] == 1
    with pytest.raises(ValueError, match="different attribution"):
        evaluate_rank_experiment(
            conn,
            experiment_id="rank-v1-exp-1",
            split="development",
            evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
            code_version="def5678",
        )
    assert conn.execute(
        "SELECT code_version FROM postmarket_rank_empirical_runs"
    ).fetchone()[0] == "abc1234"


def test_empirical_report_fails_closed_when_same_version_mixes_rank_contracts():
    conn = _conn()
    _lock(conn)
    _seed_session(conn, DEV)
    _seed_labels(conn, DEV)
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
            f"{session_text}T20:30:00+00:00",
            f"{session_text}T20:30:01+00:00",
            "rank-code",
            "mismatched-contract-run",
            "e" * 64,
        ),
    )
    conn.commit()

    report = evaluate_rank_experiment(
        conn,
        experiment_id="rank-v1-exp-1",
        split="development",
        evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        code_version="abc1234",
    )

    assert report.passed_locked_policy is False
    assert "RANK_CONTRACT_MISMATCH_PRESENT" in report.blocking_reasons


def test_empirical_evaluation_cannot_be_backdated_before_input_evidence():
    conn = _conn()
    _lock(conn)
    _seed_session(conn, DEV)
    _seed_labels(conn, DEV)

    with pytest.raises(ValueError, match="latest label evidence"):
        evaluate_rank_experiment(
            conn,
            experiment_id="rank-v1-exp-1",
            split="development",
            evaluated_at=datetime(2026, 8, 21, 0, 0, 30, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_rank_empirical_runs"
    ).fetchone()[0] == 0


def test_exact_empirical_run_exports_as_immutable_digest_bound_artifact(tmp_path):
    conn = _conn()
    experiment_manifest_sha256 = _lock(conn)
    _seed_session(conn, DEV)
    _seed_labels(conn, DEV)
    report = evaluate_rank_experiment(
        conn,
        experiment_id="rank-v1-exp-1",
        split="development",
        evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        code_version="abc1234",
    )

    first = export_empirical_report(
        conn,
        experiment_id="rank-v1-exp-1",
        split="development",
        input_digest_sha256=report.input_digest_sha256,
        output_dir=tmp_path,
    )
    second = export_empirical_report(
        conn,
        experiment_id="rank-v1-exp-1",
        split="development",
        input_digest_sha256=report.input_digest_sha256,
        output_dir=tmp_path,
    )
    artifact_path = Path(first.path)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert first.created is True
    assert second.created is False
    assert second.sha256 == first.sha256
    assert payload["code_version"] == "abc1234"
    assert payload["input_digest_sha256"] == report.input_digest_sha256
    assert payload["experiment_manifest_sha256"] == experiment_manifest_sha256
    assert payload["report"]["passed_locked_policy"] is True
    assert artifact_path.stat().st_mode & 0o777 == 0o444
    assert list(tmp_path.glob("*.tmp")) == []

    artifact_path.chmod(0o644)
    artifact_path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match exact run"):
        export_empirical_report(
            conn,
            experiment_id="rank-v1-exp-1",
            split="development",
            input_digest_sha256=report.input_digest_sha256,
            output_dir=tmp_path,
        )


def test_empirical_evaluation_rejects_unknown_revision_before_append(tmp_path):
    conn = _conn()
    _lock(conn)
    _seed_session(conn, DEV)
    _seed_labels(conn, DEV)
    with pytest.raises(ValueError, match="Git SHA"):
        evaluate_rank_experiment(
            conn,
            experiment_id="rank-v1-exp-1",
            split="development",
            evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
            code_version="unknown",
        )
    assert conn.execute("SELECT COUNT(*) FROM postmarket_rank_empirical_runs").fetchone()[0] == 0
    evaluate_rank_experiment(
        conn, experiment_id="rank-v1-exp-1", split="development",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="abc1234",
    )
    assert conn.execute("SELECT COUNT(*) FROM postmarket_rank_empirical_runs").fetchone()[0] == 1


def test_holdout_cannot_be_read_before_explicit_unblind_and_failures_are_named():
    conn = _conn()
    _lock(conn)
    _seed_session(conn, HOLDOUT)
    _label(conn, HOLDOUT, "AAA", "eligible", "up")
    _label(conn, HOLDOUT, "BBB", "ambiguous")
    _label(conn, HOLDOUT, "CCC", "ineligible")
    with pytest.raises(ValueError, match="sealed"):
        evaluate_rank_experiment(
            conn, experiment_id="rank-v1-exp-1", split="holdout",
            evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    unblind_holdout(
        conn, experiment_id="rank-v1-exp-1",
        unblinded_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        unblinded_by="owner", reason="review complete",
        expected_inventory_sha256=holdout_label_inventory(
            conn, "rank-v1-exp-1"
        )[0],
    )
    with pytest.raises(ValueError, match="cannot predate holdout unblinding"):
        evaluate_rank_experiment(
            conn, experiment_id="rank-v1-exp-1", split="holdout",
            evaluated_at=datetime(2026, 8, 22, 1, 59, tzinfo=timezone.utc),
            code_version="abc1234",
        )
    report = evaluate_rank_experiment(
        conn, experiment_id="rank-v1-exp-1", split="holdout",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="abc1234",
    )
    assert report.holdout_unblinded is True
    assert report.passed_locked_policy is False
    assert set(report.blocking_reasons) >= {
        "MIN_DEFINITIVE_LABELS_NOT_MET",
        "MIN_POSITIVE_LABELS_NOT_MET",
        "AMBIGUOUS_LABELS_PRESENT",
    }
