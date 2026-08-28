"""Automated outcome backfill and immutable quality report coverage."""
from __future__ import annotations

import ast
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradebot.detectors import Bar
from tradebot.postmarket_discovery import connect as connect_discovery
from tradebot.postmarket_quality import CHECKPOINTS
from tradebot.postmarket_quality_backfill import (
    QualityBackfillResult,
    build_daily_quality_report,
    latest_quality_report_summaries,
    plan_due_backfill,
    run_due_quality_backfill,
    write_completed_quality_reports,
)
from tradebot import postmarket_discovery_shadow as discovery_shadow


SESSION = date(2026, 8, 27)
UTC = timezone.utc


def _bar(
    symbol: str,
    ts: datetime,
    *,
    open: float = 100,
    high: float | None = None,
    low: float | None = None,
    close: float = 101,
    volume: int = 1000,
) -> Bar:
    resolved_high = max(open, close) + 1 if high is None else high
    resolved_low = min(open, close) - 1 if low is None else low
    return Bar(symbol, ts, open, resolved_high, resolved_low, close, volume)


def _seed_candidate(conn, symbol="TEST") -> int:
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
            SESSION.isoformat(),
            symbol,
            SESSION.isoformat(),
            "up",
            1,
            "2026-08-27T20:15:00+00:00",
            "2026-08-27T20:10:00+00:00",
            92,
            100,
            8.7,
            1000,
            100_000,
            '["market_gainer"]',
            "sip",
            "alpaca",
            "5Min",
            "candidate-code",
            "candidate-run",
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _seed_scheduled_candidate(conn, symbol="TEST") -> int:
    cursor = conn.execute(
        """
        INSERT INTO postmarket_candidates
            (session,symbol,event_date,direction,observer_version,
             first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
             cumulative_volume,cumulative_notional,data_feed,
             market_data_provider,bar_timeframe,catalyst_source,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(),
            symbol,
            SESSION.isoformat(),
            "up",
            1,
            "2026-08-27T20:15:00+00:00",
            "2026-08-27T20:10:00+00:00",
            92,
            100,
            8.7,
            1000,
            100_000,
            "sip",
            "alpaca",
            "5Min",
            "nasdaq_earnings",
            "candidate-code",
            "candidate-run",
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _bars(symbol: str, session: date) -> list[Bar]:
    if session == SESSION:
        return [
            _bar(symbol, datetime(2026, 8, 27, 20, 10, tzinfo=UTC), close=100),
            _bar(symbol, datetime(2026, 8, 27, 20, 15, tzinfo=UTC), close=104),
            _bar(symbol, datetime(2026, 8, 27, 20, 25, tzinfo=UTC), close=106),
            _bar(symbol, datetime(2026, 8, 27, 20, 55, tzinfo=UTC), close=108),
            _bar(symbol, datetime(2026, 8, 27, 21, 15, tzinfo=UTC), close=109),
            _bar(symbol, datetime(2026, 8, 27, 23, 55, tzinfo=UTC), close=110),
        ]
    return [
        _bar(symbol, datetime(2026, 8, 28, 13, 30, tzinfo=UTC), open=112, close=113),
        _bar(symbol, datetime(2026, 8, 28, 19, 55, tzinfo=UTC), close=114),
    ]


def test_backfill_progresses_by_finalized_phase_and_is_idempotent(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)
    calls = []

    def fetch(symbols, session):
        calls.append((tuple(symbols), session))
        return {symbol: _bars(symbol, session) for symbol in symbols}

    postmarket_result = run_due_quality_backfill(
        conn,
        now=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
        data_feed="sip",
        code_version="quality-code",
        run_id="quality-run-1",
        bars_fetch=fetch,
    )

    assert postmarket_result.candidates_planned == 1
    assert postmarket_result.candidate_sessions_fetched == 1
    assert postmarket_result.marks_written == 5
    assert postmarket_result.fetch_errors == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_candidate_mark_events"
    ).fetchone()[0] == 5

    replay = run_due_quality_backfill(
        conn,
        now=datetime(2026, 8, 28, 0, 10, tzinfo=UTC),
        data_feed="sip",
        code_version="quality-code",
        run_id="quality-run-2",
        bars_fetch=fetch,
    )
    assert replay.candidates_planned == 0
    assert replay.candidate_sessions_fetched == 0

    final = run_due_quality_backfill(
        conn,
        now=datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
        data_feed="sip",
        code_version="quality-code",
        run_id="quality-run-3",
        bars_fetch=fetch,
    )
    assert final.candidate_sessions_fetched == 2
    assert final.marks_written == 2
    assert {
        row[0]
        for row in conn.execute(
            "SELECT checkpoint FROM postmarket_candidate_mark_events"
        )
    } == CHECKPOINTS


def test_missing_bulk_symbol_stays_unresolved_and_retries(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)

    result = run_due_quality_backfill(
        conn,
        now=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
        data_feed="sip",
        code_version="quality-code",
        run_id="quality-run",
        bars_fetch=lambda symbols, session: {},
    )

    assert result.fetch_errors == 1
    assert result.unresolved_checkpoints == 5
    assert result.marks_written == 0
    assert len(plan_due_backfill(conn, now=datetime(2026, 8, 28, 0, 10, tzinfo=UTC))) == 1


def test_backfill_covers_both_candidate_streams_with_one_bulk_request(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)
    _seed_scheduled_candidate(conn)
    calls = []

    result = run_due_quality_backfill(
        conn,
        now=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
        data_feed="sip",
        code_version="quality-code",
        run_id="quality-run",
        bars_fetch=lambda symbols, session: (
            calls.append((tuple(symbols), session))
            or {symbol: _bars(symbol, session) for symbol in symbols}
        ),
    )

    assert result.candidates_planned == 2
    assert calls == [(('TEST',), SESSION)]
    assert result.marks_written == 10
    assert conn.execute(
        "SELECT COUNT(DISTINCT candidate_stream) FROM postmarket_candidate_mark_events"
    ).fetchone()[0] == 2


def test_daily_report_waits_for_complete_marks_and_is_immutable(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)
    now = datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC)

    assert write_completed_quality_reports(
        conn, tmp_path / "audits", now=now, report_code_version="quality-code"
    ) == ()

    run_due_quality_backfill(
        conn,
        now=now,
        data_feed="sip",
        code_version="quality-code",
        run_id="quality-run",
        bars_fetch=lambda symbols, session: {
            symbol: _bars(symbol, session) for symbol in symbols
        },
    )
    written = write_completed_quality_reports(
        conn, tmp_path / "audits", now=now, report_code_version="quality-code"
    )
    assert len(written) == 1
    report = written[0]
    assert report.operational_complete is True
    assert report.evidence_eligible is False
    assert report.issue_codes == ("BELOW_MINIMUM_SAMPLE",)
    path = tmp_path / "audits" / "postmarket_quality_marketwide_2026-08-27_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["checkpoint_reports"]) == 7
    assert write_completed_quality_reports(
        conn,
        tmp_path / "audits",
        now=now + timedelta(minutes=1),
        report_code_version="different-code",
    ) == ()
    assert json.loads(path.read_text(encoding="utf-8"))["report_code_version"] == "quality-code"


def test_later_provider_correction_appends_marks_and_report_version(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)
    audits = tmp_path / "audits"
    now = datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC)

    missing = run_due_quality_backfill(
        conn,
        now=now,
        data_feed="sip",
        code_version="quality-code-v1",
        run_id="missing-run",
        bars_fetch=lambda symbols, session: {symbol: [] for symbol in symbols},
    )
    assert missing.marks_written == 7
    first = write_completed_quality_reports(
        conn, audits, now=now, report_code_version="quality-code-v1"
    )
    assert first[0].report_version == 1
    assert "NO_BAR_MARKS" in first[0].issue_codes

    corrected = run_due_quality_backfill(
        conn,
        now=now + timedelta(minutes=10),
        data_feed="sip",
        code_version="quality-code-v2",
        run_id="correction-run",
        bars_fetch=lambda symbols, session: {
            symbol: _bars(symbol, session) for symbol in symbols
        },
        reconcile_no_bar=True,
    )
    assert corrected.marks_written == 7
    second = write_completed_quality_reports(
        conn,
        audits,
        now=now + timedelta(minutes=10),
        report_code_version="quality-code-v2",
    )
    assert second[0].report_version == 2
    assert "NO_BAR_MARKS" not in second[0].issue_codes
    assert (audits / "postmarket_quality_marketwide_2026-08-27_v1.json").exists()
    assert (audits / "postmarket_quality_marketwide_2026-08-27_v2.json").exists()
    assert latest_quality_report_summaries(audits) == (
        {
            "candidate_stream": "marketwide",
            "session": "2026-08-27",
            "report_version": 2,
            "operational_complete": True,
            "evidence_eligible": False,
            "issue_codes": ["BELOW_MINIMUM_SAMPLE"],
        },
    )


def test_quality_report_builder_exposes_incomplete_coverage(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)

    report = build_daily_quality_report(
        conn,
        candidate_stream="marketwide",
        session=SESSION,
        generated_at=datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
        report_code_version="quality-code",
    )

    assert report.operational_complete is False
    assert report.evidence_eligible is False
    assert "INCOMPLETE_MARKS" in report.issue_codes


def test_quality_report_fails_closed_without_code_version(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    _seed_candidate(conn)

    report = build_daily_quality_report(
        conn,
        candidate_stream="marketwide",
        session=SESSION,
        generated_at=datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
        report_code_version=None,
    )

    assert report.evidence_eligible is False
    assert "CODE_VERSION_MISSING" in report.issue_codes


def test_backfill_module_has_no_delivery_or_trading_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_quality_backfill.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = (
        "tradebot.alerts",
        "tradebot.telegram_bot",
        "tradebot.order",
        "tradebot.broker",
    )
    assert not any(module.startswith(forbidden) for module in imports)


def test_service_heartbeat_surfaces_quality_maintenance(monkeypatch, tmp_path):
    result = QualityBackfillResult(1, 2, 2, 7, 7, 0, 0, (), 123)
    monkeypatch.setattr(
        discovery_shadow,
        "run_due_quality_backfill",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        discovery_shadow,
        "write_completed_quality_reports",
        lambda *args, **kwargs: (object(),),
    )
    monkeypatch.setattr(discovery_shadow, "AUDIT_DIR", tmp_path)

    fields = discovery_shadow.quality_backfill_heartbeat_fields(
        datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
        object(),
        data_feed="sip",
        version="quality-code",
        run_id="quality-run",
    )

    assert fields == {
        "quality_backfill_status": "current",
        "quality_candidates_planned": 1,
        "quality_marks_written": 7,
        "quality_unresolved_checkpoints": 0,
        "quality_fetch_errors": 0,
        "quality_fetch_error_details": [],
        "quality_reports_written": 1,
        "latest_quality_reports": [],
        "quality_latency_ms": 123,
    }


def test_service_heartbeat_exposes_quality_failure(monkeypatch):
    class Connection:
        rolled_back = False

        def rollback(self):
            self.rolled_back = True

    conn = Connection()

    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(discovery_shadow, "run_due_quality_backfill", fail)
    fields = discovery_shadow.quality_backfill_heartbeat_fields(
        datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
        conn,
        data_feed="sip",
        version="quality-code",
        run_id="quality-run",
    )

    assert conn.rolled_back is True
    assert fields["quality_backfill_status"] == "error"
    assert "provider unavailable" in fields["quality_backfill_error"]
