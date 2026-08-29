"""Empirical rank qualification is locked, blinded, and fail-closed."""
from __future__ import annotations

import inspect
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.postmarket_discovery import DISCOVERY_SCHEMA
from tradebot.postmarket_empirical import (
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
    ensure_empirical_schema,
    evaluate_rank_experiment,
    record_independent_label,
    unblind_holdout,
)
from tradebot.postmarket_rank import RANK_SCHEMA


DEV = date(2026, 8, 20)
HOLDOUT = date(2026, 8, 21)
LOCKED_AT = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
RULE = SelectionRule(minimum_evidence_score=60, maximum_ordinal_rank=2)
POLICY = ExperimentPolicy(
    min_precision=0.90,
    min_recall=0.95,
    min_definitive_labels=3,
    min_positive_labels=2,
)


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
        label_method="blind_bar_review",
        development_sessions=(DEV,),
        holdout_sessions=(HOLDOUT,),
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
            (session,rank_version,as_of_utc,recorded_at_utc,code_version,run_id,
             input_digest_sha256,input_candidates,rankable_candidates,status,
             weights_json,thresholds_json)
        VALUES (?,1,?,?,?,?,?,3,3,'complete','{}','{}')
        """,
        (
            session_text, f"{session_text}T20:20:00+00:00",
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
            development_sessions=(DEV,), holdout_sessions=(HOLDOUT,),
            selection_rule=SelectionRule(61, 2), policy=POLICY,
        )
    with pytest.raises(ValueError, match="disjoint"):
        create_locked_experiment(
            conn, experiment_id="overlap", created_at=LOCKED_AT,
            created_by="owner", rank_version=1, label_method="blind_bar_review",
            development_sessions=(DEV,), holdout_sessions=(DEV,),
            selection_rule=RULE, policy=POLICY,
        )
    with pytest.raises(ValueError, match="at least 0.95"):
        create_locked_experiment(
            conn, experiment_id="weak", created_at=LOCKED_AT,
            created_by="owner", rank_version=1, label_method="blind_bar_review",
            development_sessions=(DEV,), holdout_sessions=(HOLDOUT,),
            selection_rule=RULE,
            policy=ExperimentPolicy(.9, .9, 3, 2),
        )


def test_label_writer_is_rank_blind_append_only_and_freezes_holdout():
    conn = _conn()
    _lock(conn)
    assert "postmarket_candidate_ranks" not in inspect.getsource(record_independent_label)
    first = _label(conn, HOLDOUT, "AAA", "eligible", "up")
    assert first > 0
    digest = unblind_holdout(
        conn, experiment_id="rank-v1-exp-1",
        unblinded_at=datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
        unblinded_by="owner", reason="independent labels are complete",
    )
    assert len(digest) == 64
    with pytest.raises(ValueError, match="frozen"):
        _label(conn, HOLDOUT, "AAA", "ineligible")
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
        code_version="empirical-code",
    )

    assert report.holdout_unblinded is False
    assert report.baseline.precision == pytest.approx(2 / 3)
    assert report.baseline.recall == 1
    assert report.candidate_rank.precision == 1
    assert report.candidate_rank.recall == 1
    assert report.precision_delta == pytest.approx(1 / 3)
    assert report.passed_locked_policy is True
    assert report.blocking_reasons == ()
    assert conn.execute("SELECT COUNT(*) FROM postmarket_rank_empirical_runs").fetchone()[0] == 1
    evaluate_rank_experiment(
        conn, experiment_id="rank-v1-exp-1", split="development",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        code_version="empirical-code",
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
            evaluated_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc), code_version="x",
        )
    unblind_holdout(
        conn, experiment_id="rank-v1-exp-1",
        unblinded_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        unblinded_by="owner", reason="review complete",
    )
    report = evaluate_rank_experiment(
        conn, experiment_id="rank-v1-exp-1", split="holdout",
        evaluated_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc), code_version="x",
    )
    assert report.holdout_unblinded is True
    assert report.passed_locked_policy is False
    assert set(report.blocking_reasons) >= {
        "MIN_DEFINITIVE_LABELS_NOT_MET",
        "MIN_POSITIVE_LABELS_NOT_MET",
        "AMBIGUOUS_LABELS_PRESENT",
    }
