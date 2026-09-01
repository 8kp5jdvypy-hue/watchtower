import hashlib
import json
import sqlite3
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradebot import postmarket_customer_dry_run_gate as gate
from tradebot import postmarket_customer_dry_run_campaign as campaign_module
from tradebot.postmarket_customer_dry_run_campaign import (
    POLICY_FIELDS,
    lock_customer_dry_run_campaign,
)
from tradebot.postmarket_customer_dry_run_gate import (
    VERDICT_NOT_READY,
    VERDICT_REVIEW,
    evaluate_customer_dry_run_gate,
    write_gate_report_atomic,
)
from tradebot.postmarket_customer_dry_run_review import REVIEW_ATTESTATION
from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    parse_delivery_policy,
)


REVISION = "abc1234"
LOCKED = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
START = date(2026, 8, 31)
END = date(2026, 9, 18)


def _delivery_policy():
    return {
        "delivery_policy_version": 2,
        "router_revision": REVISION,
        "evidence_set_sha256": "1" * 64,
        "evidence_gate_sha256": "2" * 64,
        "rank_version": 1,
        "minimum_evidence_score": 70,
        "calibration_version": 1,
        "calibration_model_sha256": "c" * 64,
        "minimum_calibrated_quality": 0.70,
        "maximum_ordinal_rank": 10,
        "minimum_evidence_coverage_pct": 95,
        "maximum_data_age_seconds": 330,
        "allowed_states": ["CONFIRMED"],
        "allowed_evidence_revisions": [REVISION],
        "allowed_calibration_revisions": [REVISION],
        "allowed_providers": ["alpaca"],
        "allowed_feeds": ["sip"],
    }


def _authorization():
    policy = parse_delivery_policy(_delivery_policy())
    return {
        "schema_version": 1,
        "release_id": "release-1",
        "approved_by": "owner@example.com",
        "approved_at_utc": "2026-08-27T12:00:00+00:00",
        "expires_at_utc": "2026-10-01T00:00:00+00:00",
        "policy_sha256": policy.sha256,
        "evidence_set_sha256": policy.evidence_set_sha256,
        "evidence_gate_sha256": policy.evidence_gate_sha256,
        "router_revision": REVISION,
        "acknowledgement": ACKNOWLEDGEMENT,
        "dry_run_readiness_approved": True,
    }


def _campaign_policy():
    return {
        "min_clean_sessions": 10,
        "min_eligible_decisions": 20,
        "min_independently_reviewed_cases": 20,
        "min_distinct_reviewed_symbols": 10,
        "min_owner_review_approval_rate": 0.9,
        "min_session_coverage_pct": 100,
        "max_scheduled_lag_seconds": 30,
        "max_tick_latency_seconds": 10,
        "allowed_audit_versions": [2],
        "allowed_audit_code_versions": [REVISION],
        "allowed_runtime_router_revisions": [REVISION],
        **{name: True for name in POLICY_FIELDS if name.startswith("require_")},
    }


def _control(path, kind):
    payload = {
        "schema_version": 1,
        "kind": kind,
        "status": "passed",
        "revision": REVISION,
        "completed_at_utc": "2026-08-28T13:00:00+00:00",
        "checks": [{"name": "deterministic", "passed": True, "evidence": "passed"}],
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _review_payload(route_id, case_sha, reviewed_at, rubric):
    payload = {
        "review_version": 1,
        "campaign_sha256": None,
        "case_evidence_sha256": case_sha,
        "route_id": route_id,
        "reviewer_id": f"reviewer-{route_id}@example.com",
        "reviewer_role": "independent_market_reviewer",
        "reviewed_at_utc": reviewed_at,
        "independent_of_implementation": True,
        "blinded_to_future_outcomes": True,
        "verdict": "APPROVE",
        "rubric": rubric,
        "critical_finding": False,
        "notes": "approved point-in-time case",
        "attestation": REVIEW_ATTESTATION,
    }
    return payload


def _fixture(tmp_path, monkeypatch):
    upstream_set = tmp_path / "upstream-evidence-set.json"
    upstream_gate = tmp_path / "upstream-evidence-gate.json"
    upstream_set.write_text("{}\n")
    upstream_gate.write_text("{}\n")
    upstream = SimpleNamespace(
        evidence_set_sha256="1" * 64,
        gate_artifact_sha256="2" * 64,
        gate_code_version=REVISION,
        evaluated_at_utc=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
        calibration_artifact_sha256="7" * 64,
        calibration_model_sha256="c" * 64,
        calibration_version=1,
        calibration_evaluated_at_utc=datetime(
            2026, 8, 25, 12, tzinfo=timezone.utc
        ),
        report=SimpleNamespace(rank_version=1),
    )
    monkeypatch.setattr(
        campaign_module, "verify_discovery_gate_artifact", lambda *args: upstream
    )
    monkeypatch.setattr(gate, "verify_discovery_gate_artifact", lambda *args: upstream)
    controls = []
    control_digests = []
    for index, kind in enumerate(sorted(gate.REQUIRED_CONTROL_KINDS)):
        path = tmp_path / f"control-{index}.json"
        control_digests.append(_control(path, kind))
        controls.append(path)
    campaign_path = tmp_path / "campaign.json"
    _, campaign = lock_customer_dry_run_campaign(
        campaign_path,
        campaign_id="campaign-1",
        locked_at=LOCKED,
        coverage_start=START,
        coverage_end=END,
        delivery_policy_payload=_delivery_policy(),
        owner_authorization_payload=_authorization(),
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        control_evidence_sha256s=tuple(control_digests),
        policy=_campaign_policy(),
    )
    campaign_sha = hashlib.sha256(campaign_path.read_bytes()).hexdigest()
    sessions = campaign["expected_sessions"]
    audit_dir = tmp_path / "audits"
    audit_dir.mkdir()
    source_by_session = {}
    for session in sessions:
        source = hashlib.sha256(session.encode()).hexdigest()
        source_by_session[session] = source
        payload = {
            "audit_version": 2,
            "audit_code_version": REVISION,
            "session": session,
            "database": "shadow.db",
            "created_at_utc": f"{session}T23:00:00+00:00",
            "source_evidence_sha256": source,
            "operational_clean": True,
            "session_evidence_eligible": True,
            "metrics": {
                "coverage_pct": 100,
                "max_scheduled_lag_ms": 100,
                "max_latency_ms": 20,
                "degraded_ticks": 0,
                "failed_invariants": 0,
                "conservation_failures": 0,
                "link_failures": 0,
                "identity_failures": 0,
                "input_digest_failures": 0,
                "decision_attribution_failures": 0,
                "orphan_routes": 0,
                "duplicate_eligible_identities": 0,
                "actionability_failures": 0,
                "eligible_candidates": 20,
                "calibrated_routes": 20,
                "calibration_link_failures": 0,
                "calibration_attribution_failures": 0,
                "calibration_model_sha256s": [campaign["calibration_model_sha256"]],
                "policy_sha256s": [campaign["delivery_policy_sha256"]],
                "authorization_sha256s": [campaign["owner_authorization_sha256"]],
                "runtime_router_revisions": [REVISION],
                "router_versions": [1],
            },
            "issues": [],
        }
        (audit_dir / f"postmarket_customer_dry_run_audit_{session}_v2.json").write_text(
            json.dumps(payload, sort_keys=True)
        )
    monkeypatch.setattr(
        gate,
        "audit_dry_run_session",
        lambda conn, session, **kwargs: SimpleNamespace(
            source_evidence_sha256=source_by_session[session.isoformat()],
            operational_clean=True,
            session_evidence_eligible=True,
        ),
    )

    database = tmp_path / "shadow.db"
    conn = sqlite3.connect(database)
    conn.executescript("""
    CREATE TABLE postmarket_delivery_dry_runs (
      route_id INTEGER, session TEXT, symbol TEXT, decision_fingerprint_sha256 TEXT,
      decision TEXT, presentation TEXT, policy_sha256 TEXT,
      authorization_sha256 TEXT, runtime_router_revision TEXT
    );
    CREATE TABLE postmarket_customer_dry_run_reviews (
      review_id INTEGER, review_version INTEGER, campaign_sha256 TEXT,
      case_evidence_sha256 TEXT, route_id INTEGER, session TEXT, symbol TEXT,
      direction TEXT, reviewer_id TEXT, reviewer_role TEXT, reviewed_at_utc TEXT,
      independent_of_implementation INTEGER, blinded_to_future_outcomes INTEGER,
      verdict TEXT, rubric_json TEXT, critical_finding INTEGER, notes TEXT,
      attestation TEXT, review_payload_sha256 TEXT, recorded_at_utc TEXT
    );
    CREATE TABLE postmarket_delivery_dry_run_calibrations (
      route_id INTEGER PRIMARY KEY, projection_id INTEGER,
      calibration_run_id INTEGER, calibration_version INTEGER,
      model_sha256 TEXT, calibrated_quality REAL,
      projected_at_utc TEXT, code_version TEXT
    );
    """)
    rubric = {field: "PASS" for field in gate.RUBRIC_FIELDS}
    case_by_route = {}
    for index in range(20):
        route_id = index + 1
        session = sessions[index % len(sessions)]
        symbol = f"SYM{index % 10}"
        fingerprint = hashlib.sha256(f"route-{route_id}".encode()).hexdigest()
        conn.execute(
            "INSERT INTO postmarket_delivery_dry_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (route_id, session, symbol, fingerprint, "ELIGIBLE_FOR_DRY_RUN",
             "ACTIONABLE", campaign["delivery_policy_sha256"],
             campaign["owner_authorization_sha256"], REVISION),
        )
        conn.execute(
            "INSERT INTO postmarket_delivery_dry_run_calibrations VALUES (?,?,?,?,?,?,?,?)",
            (route_id, route_id, 1, 1, campaign["calibration_model_sha256"],
             0.91, "2026-08-31T20:01:00+00:00", REVISION),
        )
        case_sha = hashlib.sha256(f"case-{route_id}".encode()).hexdigest()
        case_by_route[route_id] = case_sha
        reviewed_at = "2026-09-20T12:00:00+00:00"
        review = _review_payload(route_id, case_sha, reviewed_at, rubric)
        review["campaign_sha256"] = campaign_sha
        payload_sha = hashlib.sha256(
            json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        conn.execute(
            "INSERT INTO postmarket_customer_dry_run_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (route_id, 1, campaign_sha, case_sha, route_id, session, symbol, "up",
             review["reviewer_id"], review["reviewer_role"], reviewed_at, 1, 1,
             "APPROVE", json.dumps(rubric, sort_keys=True, separators=(",", ":")),
             0, review["notes"], REVIEW_ATTESTATION, payload_sha,
             "2026-09-20T12:01:00+00:00"),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        gate,
        "build_review_case",
        lambda conn, route_id, **kwargs: {
            "case_evidence_sha256": case_by_route[route_id]
        },
    )
    return campaign_path, upstream_set, upstream_gate, audit_dir, tuple(controls), database


def test_complete_locked_campaign_is_only_eligible_for_separate_review(tmp_path, monkeypatch):
    campaign, upstream_set, upstream_gate, audits, controls, database = _fixture(
        tmp_path, monkeypatch
    )
    report = evaluate_customer_dry_run_gate(
        campaign_path=campaign,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        audit_dir=audits, control_paths=controls,
        db_path=database, now=NOW, gate_code_version=REVISION,
    )
    assert report.verdict == VERDICT_REVIEW
    assert report.ready_for_customer_delivery_review is True
    assert report.customer_delivery_enabled is False
    assert report.metrics.clean_sessions >= 10
    assert report.metrics.unique_eligible_decisions == 20
    assert report.metrics.reviewed_cases == 20
    assert report.metrics.review_approval_rate == 1
    assert all(check.passed for check in report.checks)


def test_missing_session_or_premature_campaign_fails_closed(tmp_path, monkeypatch):
    campaign, upstream_set, upstream_gate, audits, controls, database = _fixture(
        tmp_path, monkeypatch
    )
    for path in list(audits.glob("*.json"))[:5]:
        path.unlink()
    report = evaluate_customer_dry_run_gate(
        campaign_path=campaign,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        audit_dir=audits, control_paths=controls,
        db_path=database,
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        gate_code_version=REVISION,
    )
    assert report.verdict == VERDICT_NOT_READY
    assert not report.ready_for_customer_delivery_review
    failed = {check.code for check in report.checks if not check.passed}
    assert {"CAMPAIGN_COMPLETE", "AUDIT_SESSION_INVENTORY", "MIN_CLEAN_SESSIONS"} <= failed


def test_gate_report_is_immutable_and_never_enables_delivery(tmp_path, monkeypatch):
    campaign, upstream_set, upstream_gate, audits, controls, database = _fixture(
        tmp_path, monkeypatch
    )
    report = evaluate_customer_dry_run_gate(
        campaign_path=campaign,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        audit_dir=audits, control_paths=controls,
        db_path=database, now=NOW, gate_code_version=REVISION,
    )
    output = tmp_path / "gate.json"
    digest = write_gate_report_atomic(output, report)
    assert len(digest) == 64
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    assert json.loads(output.read_text())["customer_delivery_enabled"] is False
    schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_gate_v2.schema.json"
    ).read_text())
    assert set(json.loads(output.read_text())) == set(schema["required"])
    assert set(json.loads(output.read_text())["metrics"]) == set(
        schema["properties"]["metrics"]["required"]
    )
    with pytest.raises(FileExistsError):
        write_gate_report_atomic(output, report)


def test_gate_rejects_upstream_artifact_that_no_longer_matches_campaign(
    tmp_path, monkeypatch
):
    campaign, upstream_set, upstream_gate, audits, controls, database = _fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        gate,
        "verify_discovery_gate_artifact",
        lambda *args: SimpleNamespace(
            evidence_set_sha256="f" * 64,
            gate_artifact_sha256="2" * 64,
            gate_code_version=REVISION,
            evaluated_at_utc=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
            calibration_artifact_sha256="7" * 64,
            calibration_model_sha256="c" * 64,
            calibration_version=1,
            calibration_evaluated_at_utc=datetime(
                2026, 8, 25, 12, tzinfo=timezone.utc
            ),
            report=SimpleNamespace(rank_version=1),
        ),
    )
    report = evaluate_customer_dry_run_gate(
        campaign_path=campaign,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        audit_dir=audits,
        control_paths=controls,
        db_path=database,
        now=NOW,
        gate_code_version=REVISION,
    )
    assert report.verdict == VERDICT_NOT_READY
    assert "UPSTREAM_DISCOVERY_EVIDENCE_EXACT" in {
        check.code for check in report.checks if not check.passed
    }


def test_gate_rejects_substituted_calibration_model(tmp_path, monkeypatch):
    campaign, upstream_set, upstream_gate, audits, controls, database = _fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        gate,
        "verify_discovery_gate_artifact",
        lambda *args: SimpleNamespace(
            evidence_set_sha256="1" * 64,
            gate_artifact_sha256="2" * 64,
            gate_code_version=REVISION,
            evaluated_at_utc=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
            calibration_artifact_sha256="7" * 64,
            calibration_model_sha256="d" * 64,
            calibration_version=1,
            calibration_evaluated_at_utc=datetime(
                2026, 8, 25, 12, tzinfo=timezone.utc
            ),
            report=SimpleNamespace(rank_version=1),
        ),
    )
    report = evaluate_customer_dry_run_gate(
        campaign_path=campaign,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        audit_dir=audits, control_paths=controls,
        db_path=database, now=NOW, gate_code_version=REVISION,
    )
    failed = {check.code for check in report.checks if not check.passed}
    assert report.verdict == VERDICT_NOT_READY
    assert "UPSTREAM_DISCOVERY_EVIDENCE_EXACT" in failed


def test_gate_rejects_eligible_route_without_exact_calibration_link(
    tmp_path, monkeypatch
):
    campaign, upstream_set, upstream_gate, audits, controls, database = _fixture(
        tmp_path, monkeypatch
    )
    conn = sqlite3.connect(database)
    conn.execute(
        "DELETE FROM postmarket_delivery_dry_run_calibrations WHERE route_id=1"
    )
    conn.commit()
    conn.close()
    report = evaluate_customer_dry_run_gate(
        campaign_path=campaign,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        audit_dir=audits, control_paths=controls,
        db_path=database, now=NOW, gate_code_version=REVISION,
    )
    failed = {check.code for check in report.checks if not check.passed}
    assert report.verdict == VERDICT_NOT_READY
    assert "ELIGIBLE_ROUTES_CALIBRATION_EXACT" in failed
