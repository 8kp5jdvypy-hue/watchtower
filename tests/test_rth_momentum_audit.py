from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone

from tradebot.rth_momentum import RTH_SCHEMA, ensure_rth_schema
from tradebot.rth_momentum_audit import (
    build_rth_momentum_audit,
    latest_rth_audit_summary,
    rth_audit_session_due,
    write_completed_rth_audits,
)
import tradebot.postmarket_discovery_shadow as discovery_shadow


SESSION = date(2026, 8, 31)
WINDOW_START = datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)


def _insert_tick(
    conn: sqlite3.Connection,
    *,
    scheduled: datetime,
    selected: int = 2,
    evaluated: int = 2,
    invariant_ok: int = 1,
    errors: int = 0,
    missed: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO rth_momentum_ticks
          (session,scheduled_tick_utc,tick_utc,completed_utc,window_start_utc,
           session_close_utc,momentum_version,run_mode,run_id,code_version,
           data_feed,market_data_provider,universe_symbols,provider_screen_rows,
           provider_screen_unique_symbols,scheduled_symbols,selected_symbols,
           intraday_symbols_fetched,daily_symbols_fetched,evaluated_symbols,
           candidate_observations,new_candidates,invariant_ok,error_count,
           missed_cycles,scheduled_lag_ms,screen_latency_ms,selection_latency_ms,
           bar_fetch_latency_ms,evaluation_latency_ms,total_latency_ms,thresholds_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), scheduled.isoformat(), scheduled.isoformat(),
            (scheduled + timedelta(seconds=1)).isoformat(), WINDOW_START.isoformat(),
            CLOSE.isoformat(), 1, "test", f"run-{scheduled.minute}", "abc1234",
            "sip", "alpaca", 13_000, 200, 170, 0, selected, selected, selected,
            evaluated, 0, 0, invariant_ok, errors, missed, 0, 10, 5, 100, 20,
            135, "{}",
        ),
    )


def _insert_full_window(conn: sqlite3.Connection) -> None:
    ensure_rth_schema(conn)
    for minute in range(31):
        _insert_tick(conn, scheduled=WINDOW_START + timedelta(minutes=minute))
    conn.commit()


def test_full_window_is_clean_and_restores_connection_row_factory():
    conn = sqlite3.connect(":memory:")
    _insert_full_window(conn)
    sentinel = lambda cursor, row: tuple(row)  # noqa: E731
    conn.row_factory = sentinel

    report = build_rth_momentum_audit(
        conn,
        session=SESSION,
        database="shadow.db",
        audit_code_version="abc1234",
    )

    assert conn.row_factory is sentinel
    assert report.operational_clean is True
    assert report.session_evidence_eligible is True
    assert report.operational.expected_ticks == 31
    assert report.operational.ticks == 31
    assert report.operational.coverage_pct == 100.0
    assert report.operational.average_stage_latency_ms == {
        "screen_latency_ms": 10.0,
        "selection_latency_ms": 5.0,
        "bar_fetch_latency_ms": 100.0,
        "evaluation_latency_ms": 20.0,
    }
    assert report.issues == ()


def test_gap_and_conservation_failure_block_evidence():
    conn = sqlite3.connect(":memory:")
    ensure_rth_schema(conn)
    for minute in range(31):
        if minute == 10:
            continue
        _insert_tick(
            conn,
            scheduled=WINDOW_START + timedelta(minutes=minute),
            evaluated=1 if minute == 20 else 2,
            invariant_ok=0 if minute == 20 else 1,
        )
    conn.commit()

    report = build_rth_momentum_audit(
        conn,
        session=SESSION,
        database="shadow.db",
        audit_code_version="abc1234",
    )
    codes = {issue.code for issue in report.issues}

    assert report.operational_clean is False
    assert report.session_evidence_eligible is False
    assert report.operational.coverage_pct == 96.77
    assert {
        "TICK_GAP",
        "FAILED_INVARIANT",
        "SELECTION_EVALUATION_MISMATCH",
    } <= codes


def test_candidate_without_seed_handoff_is_detected():
    conn = sqlite3.connect(":memory:")
    _insert_full_window(conn)
    conn.execute(
        """
        INSERT INTO rth_momentum_candidates
          (session,symbol,direction,momentum_version,first_detected_at,
           bar_open_ts_utc,prior_close,close,move_pct,cumulative_volume,
           cumulative_notional,sources_json,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), "GPRO", "up", 1, CLOSE.isoformat(),
            (CLOSE - timedelta(minutes=5)).isoformat(), 1.0, 1.5, 50.0,
            220_000_000, 250_000_000.0, '["market_gainer"]', "sip", "alpaca",
            "5Min", "abc1234", "candidate-run",
        ),
    )
    conn.commit()

    report = build_rth_momentum_audit(
        conn,
        session=SESSION,
        database="shadow.db",
        audit_code_version="abc1234",
    )

    assert "HANDOFF_SEED_MISMATCH" in {issue.code for issue in report.issues}


def test_qualified_handoff_requires_matching_postmarket_candidate_identity():
    conn = sqlite3.connect(":memory:")
    _insert_full_window(conn)
    cursor = conn.execute(
        """
        INSERT INTO rth_momentum_candidates
          (session,symbol,direction,momentum_version,first_detected_at,
           bar_open_ts_utc,prior_close,close,move_pct,cumulative_volume,
           cumulative_notional,sources_json,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), "GPRO", "up", 1, CLOSE.isoformat(),
            (CLOSE - timedelta(minutes=5)).isoformat(), 1.0, 1.5, 50.0,
            220_000_000, 250_000_000.0, '["market_gainer"]', "sip", "alpaca",
            "5Min", "abc1234", "candidate-run",
        ),
    )
    candidate_id = int(cursor.lastrowid)
    common = (
        candidate_id,
        SESSION.isoformat(),
        "GPRO",
        "up",
        CLOSE.isoformat(),
        "test",
        "abc1234",
    )
    conn.execute(
        """
        INSERT INTO rth_postmarket_handoffs
          (rth_candidate_id,postmarket_candidate_id,session,symbol,direction,
           state,transition_at_utc,reason,code_version,run_id)
        VALUES (?,NULL,?,?,?,'RTH_QUALIFIED',?,?,?,'seed')
        """,
        common,
    )
    conn.execute(
        """
        INSERT INTO rth_postmarket_handoffs
          (rth_candidate_id,postmarket_candidate_id,session,symbol,direction,
           state,transition_at_utc,reason,code_version,run_id)
        VALUES (?,999,?,?,?,'POSTMARKET_QUALIFIED',?,?,?,'link')
        """,
        common,
    )
    conn.commit()

    report = build_rth_momentum_audit(
        conn,
        session=SESSION,
        database="shadow.db",
        audit_code_version="abc1234",
    )

    assert "POSTMARKET_HANDOFF_IDENTITY_MISMATCH" in {
        issue.code for issue in report.issues
    }


def test_completed_audit_write_is_immutable_and_summarized(tmp_path):
    db_path = tmp_path / "shadow.db"
    conn = sqlite3.connect(db_path)
    _insert_full_window(conn)
    conn.close()
    audit_dir = tmp_path / "audits"

    assert write_completed_rth_audits(
        db_path,
        audit_dir,
        now=CLOSE + timedelta(minutes=4, seconds=59),
        audit_code_version="abc1234",
    ) == ()
    written = write_completed_rth_audits(
        db_path,
        audit_dir,
        now=CLOSE + timedelta(minutes=5),
        audit_code_version="abc1234",
    )
    assert len(written) == 1
    path = audit_dir / "rth_momentum_audit_2026-08-31_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session_evidence_eligible"] is True
    assert path.stat().st_mode & 0o222 == 0
    assert write_completed_rth_audits(
        db_path,
        audit_dir,
        now=CLOSE + timedelta(hours=1),
        audit_code_version="different",
    ) == ()
    assert latest_rth_audit_summary(audit_dir) == {
        "session": "2026-08-31",
        "operational_clean": True,
        "session_evidence_eligible": True,
        "coverage_pct": 100.0,
        "issue_codes": [],
    }


def test_due_session_and_zero_tick_outage_are_audited(tmp_path):
    assert rth_audit_session_due(CLOSE + timedelta(minutes=4, seconds=59)) is None
    assert rth_audit_session_due(CLOSE + timedelta(minutes=5)) == SESSION
    assert rth_audit_session_due(
        datetime(2026, 8, 30, 22, 0, tzinfo=timezone.utc)
    ) is None

    db_path = tmp_path / "shadow.db"
    conn = sqlite3.connect(db_path)
    ensure_rth_schema(conn)
    conn.close()
    reports = write_completed_rth_audits(
        db_path,
        tmp_path / "audits",
        now=CLOSE + timedelta(minutes=5),
        audit_code_version="abc1234",
        expected_sessions=(SESSION,),
    )

    assert len(reports) == 1
    assert reports[0].operational.ticks == 0
    assert reports[0].operational.coverage_pct == 0.0
    assert {issue.code for issue in reports[0].issues} >= {"NO_TICKS", "TICK_GAP"}
    assert reports[0].session_evidence_eligible is False


def test_writer_is_backward_compatible_with_database_without_rth_schema(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
    conn.close()

    assert write_completed_rth_audits(
        db_path,
        tmp_path / "audits",
        now=CLOSE + timedelta(hours=1),
        audit_code_version="abc1234",
    ) == ()


def test_heartbeat_surfaces_latest_rth_audit(monkeypatch):
    report = SimpleNamespace(
        session=SESSION.isoformat(),
        operational_clean=False,
        session_evidence_eligible=False,
        issues=(SimpleNamespace(code="TICK_GAP"),),
    )
    latest = {
        "session": SESSION.isoformat(),
        "operational_clean": False,
        "session_evidence_eligible": False,
        "coverage_pct": 96.77,
        "issue_codes": ["TICK_GAP"],
    }
    monkeypatch.setattr(
        discovery_shadow,
        "write_completed_rth_audits",
        lambda *args, **kwargs: (report,),
    )
    monkeypatch.setattr(
        discovery_shadow,
        "latest_rth_audit_summary",
        lambda path: latest,
    )

    fields = discovery_shadow.rth_audit_heartbeat_fields(
        CLOSE + timedelta(minutes=5)
    )

    assert fields == {
        "rth_audit_status": "written",
        "rth_audits_written": 1,
        "latest_rth_audit": latest,
    }


def test_heartbeat_makes_rth_audit_failure_visible(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("malformed RTH evidence")

    monkeypatch.setattr(discovery_shadow, "write_completed_rth_audits", fail)

    fields = discovery_shadow.rth_audit_heartbeat_fields(
        CLOSE + timedelta(minutes=5)
    )

    assert fields["rth_audit_status"] == "error"
    assert "malformed RTH evidence" in fields["rth_audit_error"]


def test_rth_schema_remains_append_only_under_direct_audit_fixture():
    conn = sqlite3.connect(":memory:")
    conn.executescript(RTH_SCHEMA)
    _insert_tick(conn, scheduled=WINDOW_START)
    conn.commit()
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
