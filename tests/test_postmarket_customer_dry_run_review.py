import json
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_customer_dry_run_review import (
    REVIEW_ATTESTATION,
    build_review_case,
    record_independent_review,
    write_review_case_atomic,
)
from tradebot.postmarket_delivery_dry_run import ensure_dry_run_schema


NOW = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)
CAMPAIGN = "a" * 64


def _conn():
    conn = sqlite3.connect(":memory:")
    ensure_dry_run_schema(conn)
    conn.executescript("""
    CREATE TABLE postmarket_candidate_ranks (
      rank_run_id INTEGER, candidate_id INTEGER, observation_seq INTEGER,
      ordinal_rank INTEGER, evidence_score REAL, evidence_coverage_pct REAL,
      components_json TEXT, penalties_json TEXT, exclusion_reasons_json TEXT,
      explanation_json TEXT
    );
    CREATE TABLE postmarket_candidate_lifecycle (
      transition_id INTEGER, state TEXT, actionability TEXT, transition_at_utc TEXT
    );
    CREATE TABLE postmarket_candidate_lifecycle_observations (
      seq INTEGER, candidate_id INTEGER, evidence_bar_open_ts_utc TEXT,
      move_pct REAL, cumulative_notional REAL, data_age_seconds REAL,
      data_feed TEXT, market_data_provider TEXT
    );
    """)
    conn.execute(
        "INSERT INTO postmarket_candidate_ranks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (7, 11, 13, 2, 82.5, 100, '{"move":30}', '{"spread":0}', '[]',
         '["large persistent move","liquid"]'),
    )
    conn.execute(
        "INSERT INTO postmarket_candidate_lifecycle VALUES (?,?,?,?)",
        (5, "CONFIRMED", "QUALIFIED", (NOW - timedelta(minutes=2)).isoformat()),
    )
    conn.execute(
        "INSERT INTO postmarket_candidate_lifecycle_observations VALUES (?,?,?,?,?,?,?,?)",
        (13, 11, (NOW - timedelta(minutes=5)).isoformat(), 12.5, 5_000_000,
         30, "sip", "alpaca"),
    )
    cursor = conn.execute(
        """
        INSERT INTO postmarket_delivery_dry_runs
          (router_version,idempotency_key,decision_fingerprint_sha256,
           evaluated_at_utc,recorded_at_utc,session,symbol,direction,candidate_id,
           transition_id,rank_run_id,decision,presentation,reason_codes_json,
           policy_sha256,authorization_sha256,release_id,dry_run_enabled,
           kill_switch_engaged,operational_status,runtime_router_revision,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (1, "case-1", "b" * 64, NOW.isoformat(), NOW.isoformat(), "2026-08-28",
         "OKTA", "up", 11, 5, 7, "ELIGIBLE_FOR_DRY_RUN", "ACTIONABLE", "[]",
         "c" * 64, "d" * 64, "release-1", 1, 0, "clean", "abc1234", "run-1"),
    )
    conn.commit()
    return conn, cursor.lastrowid


def _assessment(**changes):
    payload = {
        "schema_version": 1,
        "reviewer_id": "reviewer@example.com",
        "reviewer_role": "independent_market_reviewer",
        "reviewed_at_utc": (NOW + timedelta(minutes=5)).isoformat(),
        "independent_of_implementation": True,
        "blinded_to_future_outcomes": True,
        "rubric": {
            "signal_relevance": "PASS",
            "timeliness": "PASS",
            "evidence_sufficiency": "PASS",
            "explanation_clarity": "PASS",
            "risk_disclosure": "PASS",
        },
        "critical_finding": False,
        "notes": "Evidence is sufficient for a dry-run presentation.",
        "attestation": REVIEW_ATTESTATION,
    }
    payload.update(changes)
    return payload


def test_case_is_point_in_time_digest_bound_and_excludes_future_outcomes(tmp_path):
    conn, route_id = _conn()
    case = build_review_case(
        conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
    )
    raw = json.dumps(case, sort_keys=True)
    assert case["blinded_to_future_outcomes"] is True
    assert case["evidence"]["symbol"] == "OKTA"
    assert case["evidence"]["rank_components"] == {"move": 30}
    assert "outcome_marks" in case["excluded_evidence_classes"]
    assert "mark_price" not in raw and "future_return" not in raw
    path = tmp_path / "case.json"
    digest = write_review_case_atomic(path, case)
    assert len(digest) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError):
        write_review_case_atomic(path, case)


def test_approved_review_is_append_only_and_exactly_bound():
    conn, route_id = _conn()
    case = build_review_case(
        conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
    )
    recorded = record_independent_review(
        conn, case=case, assessment=_assessment()
    )
    assert recorded.verdict == "APPROVE"
    row = conn.execute(
        "SELECT campaign_sha256,route_id,symbol,verdict FROM postmarket_customer_dry_run_reviews"
    ).fetchone()
    assert row == (CAMPAIGN, route_id, "OKTA", "APPROVE")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        record_independent_review(conn, case=case, assessment=_assessment())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_customer_dry_run_reviews SET verdict='REJECT'")


def test_failed_rubric_or_critical_finding_derives_rejection():
    conn, route_id = _conn()
    case = build_review_case(
        conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
    )
    rubric = _assessment()["rubric"] | {"risk_disclosure": "FAIL"}
    recorded = record_independent_review(
        conn,
        case=case,
        assessment=_assessment(rubric=rubric, critical_finding=True),
    )
    assert recorded.verdict == "REJECT"


def test_review_fails_closed_on_tampering_nonindependence_or_inexact_rubric():
    conn, route_id = _conn()
    case = build_review_case(
        conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
    )
    tampered = json.loads(json.dumps(case))
    tampered["evidence"]["evidence_score"] = 99
    with pytest.raises(ValueError, match="digest mismatch"):
        record_independent_review(conn, case=tampered, assessment=_assessment())
    with pytest.raises(ValueError, match="independence"):
        record_independent_review(
            conn, case=case,
            assessment=_assessment(independent_of_implementation=False),
        )
    with pytest.raises(ValueError, match="exact PASS/FAIL"):
        record_independent_review(
            conn, case=case,
            assessment=_assessment(rubric={"signal_relevance": "PASS"}),
        )


def test_only_actionable_eligible_routes_can_be_exported():
    conn, route_id = _conn()
    conn.execute("DROP TRIGGER postmarket_delivery_dry_runs_no_update")
    conn.execute(
        "UPDATE postmarket_delivery_dry_runs SET decision='SUPPRESSED' WHERE route_id=?",
        (route_id,),
    )
    with pytest.raises(ValueError, match="only actionable eligible"):
        build_review_case(
            conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
        )


def test_case_and_assessment_contract_shapes_match_truth_schemas():
    conn, route_id = _conn()
    case = build_review_case(
        conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
    )
    case_schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_review_case_v1.schema.json"
    ).read_text())
    review_schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_review_assessment_v1.schema.json"
    ).read_text())
    assert set(case) == set(case_schema["required"])
    assert set(case["evidence"]) == set(
        case_schema["properties"]["evidence"]["required"]
    )
    assert set(_assessment()) == set(review_schema["required"])
