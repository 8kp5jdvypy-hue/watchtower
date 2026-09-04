import json
import hashlib
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_customer_dry_run_review import (
    REVIEW_ATTESTATION,
    build_review_case,
    list_eligible_review_cases,
    record_independent_review,
    write_review_case_atomic,
)
from tradebot.postmarket_customer_dry_run_campaign import POLICY_FIELDS
from tradebot.postmarket_delivery_dry_run import ensure_dry_run_schema


NOW = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)
CAMPAIGN = "a" * 64


def _conn():
    conn = sqlite3.connect(":memory:")
    ensure_dry_run_schema(conn)
    conn.executescript("""
    CREATE TABLE postmarket_candidate_ranks (
      rank_id INTEGER, rank_run_id INTEGER, candidate_id INTEGER, observation_seq INTEGER,
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
    CREATE TABLE postmarket_rank_calibration_projections (
      projection_id INTEGER, calibration_run_id INTEGER,
      calibration_version INTEGER, model_sha256 TEXT, rank_id INTEGER,
      rank_run_id INTEGER, candidate_id INTEGER, calibrated_quality REAL,
      projected_at_utc TEXT, code_version TEXT
    );
    """)
    conn.execute(
        "INSERT INTO postmarket_candidate_ranks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (17, 7, 11, 13, 2, 82.5, 100, '{"move":30}', '{"spread":0}', '[]',
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
    conn.execute(
        "INSERT INTO postmarket_rank_calibration_projections VALUES (?,?,?,?,?,?,?,?,?,?)",
        (55, 9, 1, "e" * 64, 17, 7, 11, 0.8,
         (NOW - timedelta(minutes=1)).isoformat(), "abc1234"),
    )
    conn.execute(
        """
        INSERT INTO postmarket_delivery_dry_run_calibrations
          (route_id,projection_id,calibration_run_id,calibration_version,
           model_sha256,calibrated_quality,projected_at_utc,code_version)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (cursor.lastrowid, 55, 9, 1, "e" * 64, 0.8,
         (NOW - timedelta(minutes=1)).isoformat(), "abc1234"),
    )
    preview = json.dumps({
        "customer_state": "ACTIONABLE",
        "disclaimer": (
            "Derived market-intelligence signal; not a quote, chart, "
            "recommendation, or trade instruction."
        ),
        "generated_at_utc": NOW.isoformat(),
        "license_semantic": "non_reconstructable_derived_only_v1",
        "lifecycle": "CONFIRMED",
        "ordinal_rank": 2,
        "presentation_version": 1,
        "quality_status": "MEETS_LOCKED_POLICY",
        "schema_version": 1,
        "signal": "POSTMARKET_STRENGTH",
        "symbol": "OKTA",
    }, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO postmarket_customer_presentation_previews
          (route_id,presentation_version,license_semantic,payload_json,
           payload_sha256,generated_at_utc)
        VALUES (?,1,'non_reconstructable_derived_only_v1',?,?,?)
        """,
        (
            cursor.lastrowid,
            preview,
            hashlib.sha256(preview.encode()).hexdigest(),
            NOW.isoformat(),
        ),
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


def _campaign():
    return {
        "schema_version": 3,
        "status": "locked",
        "campaign_id": "campaign-1",
        "locked_at_utc": "2026-08-27T12:00:00+00:00",
        "coverage_start": "2026-08-28",
        "coverage_end": "2026-08-28",
        "expected_sessions": ["2026-08-28"],
        "delivery_policy_sha256": "c" * 64,
        "owner_authorization_sha256": "d" * 64,
        "owner_authorization_expires_at_utc": "2026-10-01T00:00:00+00:00",
        "release_id": "release-1",
        "router_version": 1,
        "rank_version": 1,
        "control_evidence_sha256s": [str(index) * 64 for index in range(1, 5)],
        "upstream_discovery_evidence_set_sha256": "5" * 64,
        "upstream_discovery_evidence_gate_sha256": "6" * 64,
        "upstream_discovery_gate_code_version": "abc1234",
        "upstream_discovery_gate_evaluated_at_utc": "2026-08-26T12:00:00+00:00",
        "upstream_calibration_artifact_sha256": "7" * 64,
        "calibration_model_sha256": "e" * 64,
        "calibration_version": 1,
        "calibration_evaluated_at_utc": "2026-08-25T12:00:00+00:00",
        "policy": {
            "min_clean_sessions": 10,
            "min_eligible_decisions": 20,
            "min_independently_reviewed_cases": 20,
            "min_distinct_reviewed_symbols": 10,
            "min_owner_review_approval_rate": 0.9,
            "min_session_coverage_pct": 100,
            "max_scheduled_lag_seconds": 30,
            "max_tick_latency_seconds": 10,
            "allowed_audit_versions": [2],
            "allowed_audit_code_versions": ["abc1234"],
            "allowed_runtime_router_revisions": ["abc1234"],
            **{name: True for name in POLICY_FIELDS if name.startswith("require_")},
        },
    }


def test_case_is_point_in_time_digest_bound_and_excludes_future_outcomes(tmp_path):
    conn, route_id = _conn()
    case = build_review_case(
        conn, campaign_sha256=CAMPAIGN, route_id=route_id, exported_at=NOW
    )
    raw = json.dumps(case, sort_keys=True)
    assert case["blinded_to_future_outcomes"] is True
    assert case["evidence"]["symbol"] == "OKTA"
    assert case["evidence"]["rank_components"] == {"move": 30}
    assert case["evidence"]["customer_preview_payload"]["signal"] == (
        "POSTMARKET_STRENGTH"
    )
    assert "outcome_marks" in case["excluded_evidence_classes"]
    assert "mark_price" not in raw and "future_return" not in raw
    path = tmp_path / "case.json"
    digest = write_review_case_atomic(path, case)
    assert len(digest) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError):
        write_review_case_atomic(path, case)


def test_list_is_campaign_scoped_and_does_not_require_review_table():
    conn, route_id = _conn()
    rows = list_eligible_review_cases(
        conn, campaign=_campaign(), campaign_sha256=CAMPAIGN
    )
    assert rows == ({
        "route_id": route_id,
        "session": "2026-08-28",
        "symbol": "OKTA",
        "direction": "up",
        "evaluated_at_utc": NOW.isoformat(),
        "rank_run_id": 7,
        "candidate_id": 11,
        "transition_id": 5,
        "review_count": 0,
    },)


def test_route_without_customer_preview_cannot_enter_review_inventory():
    conn, route_id = _conn()
    conn.execute("DROP TRIGGER postmarket_customer_presentation_previews_no_delete")
    conn.execute(
        "DELETE FROM postmarket_customer_presentation_previews WHERE route_id=?",
        (route_id,),
    )
    rows = list_eligible_review_cases(
        conn, campaign=_campaign(), campaign_sha256=CAMPAIGN
    )
    assert rows == ()


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
        "truth/postmarket_customer_dry_run_review_case_v3.schema.json"
    ).read_text())
    review_schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_review_assessment_v1.schema.json"
    ).read_text())
    assert set(case) == set(case_schema["required"])
    assert set(case["evidence"]) == set(
        case_schema["properties"]["evidence"]["required"]
    )
    assert set(_assessment()) == set(review_schema["required"])
