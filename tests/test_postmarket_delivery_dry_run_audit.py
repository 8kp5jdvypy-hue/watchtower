import hashlib
import json
import sqlite3
import stat
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_delivery_dry_run_audit as audit
from tradebot.postmarket_delivery_dry_run import ensure_dry_run_schema


SESSION = date(2026, 8, 28)
START = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
REVISION = "abcdef1"
POLICY = "1" * 64
AUTHORIZATION = "2" * 64


def _short_window(monkeypatch):
    slots = tuple(START + timedelta(minutes=index) for index in range(3))
    monkeypatch.setattr(audit, "_expected_slots", lambda session: slots)
    monkeypatch.setattr(audit, "_session_window", lambda session: (slots[0], slots[-1]))
    return slots


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_dry_run_schema(conn)
    return conn


def _tick(conn, scheduled, *, status="clean", reasons=(), invariant=True, inputs=0,
          eligible=0, suppressed=0, written=0, duplicates=0, rank_run_id=None,
          route_ids=()):
    started = scheduled + timedelta(milliseconds=100)
    completed = started + timedelta(milliseconds=20)
    cursor = conn.execute(
        """
        INSERT INTO postmarket_delivery_dry_run_ticks
          (router_version,session,scheduled_at_utc,started_at_utc,completed_at_utc,
           rank_run_id,input_candidates,decisions_written,eligible_candidates,
           suppressed_candidates,duplicate_decisions,operational_status,
           operational_reasons_json,input_digest_sha256,scheduled_lag_ms,
           latency_ms,invariant_ok,policy_sha256,authorization_sha256,
           runtime_router_revision,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (1, SESSION.isoformat(), scheduled.isoformat(), started.isoformat(),
         completed.isoformat(), rank_run_id, inputs, written, eligible, suppressed,
         duplicates, status, json.dumps(reasons), audit._digest({
             "router_version": 1,
             "session": SESSION.isoformat(),
             "scheduled_at_utc": scheduled.isoformat(),
             "rank_run_id": rank_run_id,
             "input_candidates": inputs,
             "eligible_candidates": eligible,
             "suppressed_candidates": suppressed,
             "operational_status": status,
             "operational_reasons": list(reasons),
             "invariant_ok": invariant,
             "policy_sha256": POLICY,
             "authorization_sha256": AUTHORIZATION,
             "runtime_router_revision": REVISION,
             "route_ids": sorted(route_ids),
         }), 100, 20,
         int(invariant), POLICY, AUTHORIZATION, REVISION, "run-1"),
    )
    return cursor.lastrowid


def _route(conn, *, decision="SUPPRESSED", presentation="DEGRADED", reasons=("X",)):
    cursor = conn.execute(
        """
        INSERT INTO postmarket_delivery_dry_runs
          (router_version,idempotency_key,decision_fingerprint_sha256,evaluated_at_utc,
           recorded_at_utc,session,symbol,direction,candidate_id,transition_id,
           rank_run_id,decision,presentation,reason_codes_json,policy_sha256,
           authorization_sha256,release_id,dry_run_enabled,kill_switch_engaged,
           operational_status,runtime_router_revision,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (1, "identity-1", "4" * 64, START.isoformat(), START.isoformat(),
         SESSION.isoformat(), "TEST", "up", 1, 2, 7, decision, presentation,
         json.dumps(reasons), POLICY, AUTHORIZATION, "release-1", 1, 0,
         "clean", REVISION, "run-1"),
    )
    return cursor.lastrowid


def test_clean_audit_requires_exact_persisted_schedule(monkeypatch):
    slots = _short_window(monkeypatch)
    conn = _connection()
    for slot in slots:
        _tick(conn, slot)
    report = audit.audit_dry_run_session(
        conn, SESSION, database="memory", audit_code_version=REVISION,
        created_at=START + timedelta(hours=1),
    )
    assert report.operational_clean is True
    assert report.session_evidence_eligible is True
    assert report.metrics.expected_ticks == 3
    assert report.metrics.observed_ticks == 3
    assert report.metrics.coverage_pct == 100
    assert report.metrics.missing_scheduled_slots_utc == ()
    assert report.issues == ()


def test_audit_fails_closed_on_gap_degradation_or_orphan_route(monkeypatch):
    slots = _short_window(monkeypatch)
    conn = _connection()
    _tick(conn, slots[0])
    _tick(conn, slots[2], status="degraded", reasons=("DISCOVERY_STALE",), invariant=False)
    _route(conn)
    report = audit.audit_dry_run_session(
        conn, SESSION, database="memory", audit_code_version=REVISION,
    )
    codes = {issue.code for issue in report.issues}
    assert report.operational_clean is False
    assert report.session_evidence_eligible is False
    assert report.metrics.coverage_pct == pytest.approx(66.67)
    assert report.metrics.orphan_routes == 1
    assert report.metrics.operational_reason_counts == {"DISCOVERY_STALE": 1}
    assert {"TICK_GAP", "DEGRADED_TICKS", "FAILED_INVARIANTS", "ORPHAN_ROUTE_DECISIONS"} <= codes


def test_audit_reconciles_exact_route_links_and_actionability(monkeypatch):
    slots = _short_window(monkeypatch)
    conn = _connection()
    route_id = _route(
        conn, decision="ELIGIBLE_FOR_DRY_RUN", presentation="ACTIONABLE", reasons=()
    )
    tick_id = _tick(
        conn, slots[0], inputs=1, eligible=1, written=1, rank_run_id=7,
        route_ids=(route_id,),
    )
    conn.execute(
        "INSERT INTO postmarket_delivery_dry_run_tick_decisions (tick_id,route_id) VALUES (?,?)",
        (tick_id, route_id),
    )
    duplicate_tick_id = _tick(
        conn, slots[1], inputs=1, eligible=1, duplicates=1, rank_run_id=7,
        route_ids=(route_id,),
    )
    conn.execute(
        "INSERT INTO postmarket_delivery_dry_run_tick_decisions (tick_id,route_id) VALUES (?,?)",
        (duplicate_tick_id, route_id),
    )
    _tick(conn, slots[2])
    report = audit.audit_dry_run_session(
        conn, SESSION, database="memory", audit_code_version=REVISION,
    )
    assert report.operational_clean is True
    assert report.metrics.input_candidates == 2
    assert report.metrics.eligible_candidates == 2
    assert report.metrics.decisions_written == 1
    assert report.metrics.duplicate_decisions == 1
    assert report.metrics.linked_decisions == 2
    assert report.metrics.orphan_routes == 0
    assert report.metrics.actionability_failures == 0
    assert report.metrics.decision_attribution_failures == 0


def test_schema_contract_and_exclusive_read_only_writer(tmp_path, monkeypatch):
    slots = _short_window(monkeypatch)
    conn = _connection()
    for slot in slots:
        _tick(conn, slot)
    report = audit.audit_dry_run_session(
        conn, SESSION, database="memory", audit_code_version=REVISION,
    )
    schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_audit_v1.schema.json"
    ).read_text())
    assert set(asdict(report)) == set(schema["required"])
    assert set(asdict(report.metrics)) == set(schema["properties"]["metrics"]["required"])
    path = tmp_path / "audit.json"
    assert audit.write_report_atomic(path, report) is True
    assert audit.write_report_atomic(path, report) is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert hashlib.sha256(path.read_bytes()).hexdigest()


def test_completed_writer_waits_for_full_window_and_is_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    conn = sqlite3.connect(database)
    ensure_dry_run_schema(conn)
    conn.close()
    ready = START + timedelta(minutes=10)
    monkeypatch.setattr(audit, "_audit_ready_at", lambda session: ready)
    assert audit.write_completed_dry_run_audits(
        database, tmp_path / "audits", now=ready,
        audit_code_version=REVISION,
    ) == ()
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    _tick(conn, START)
    conn.commit()
    conn.close()
    reports = audit.write_completed_dry_run_audits(
        database, tmp_path / "audits", now=ready + timedelta(seconds=1),
        audit_code_version=REVISION,
    )
    assert len(reports) == 1
    assert audit.write_completed_dry_run_audits(
        database, tmp_path / "audits", now=ready + timedelta(seconds=2),
        audit_code_version=REVISION,
    ) == ()

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    _tick(conn, START + timedelta(minutes=1))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="changed after immutable audit"):
        audit.write_completed_dry_run_audits(
            database, tmp_path / "audits", now=ready + timedelta(seconds=3),
            audit_code_version=REVISION,
        )


def test_unknown_or_mismatched_audit_revision_fails_closed(monkeypatch):
    slots = _short_window(monkeypatch)
    conn = _connection()
    for slot in slots:
        _tick(conn, slot)
    unknown = audit.audit_dry_run_session(
        conn, SESSION, database="memory", audit_code_version="unknown"
    )
    mismatch = audit.audit_dry_run_session(
        conn, SESSION, database="memory", audit_code_version="def5678"
    )
    assert "AUDIT_REVISION_UNKNOWN" in {issue.code for issue in unknown.issues}
    assert "AUDIT_RUNTIME_REVISION_MISMATCH" in {
        issue.code for issue in mismatch.issues
    }
    assert not unknown.session_evidence_eligible
    assert not mismatch.session_evidence_eligible
