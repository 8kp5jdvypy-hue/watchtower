"""Independent full-universe postmarket recall census coverage."""
from __future__ import annotations

import ast
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Bar
from tradebot.postmarket import thresholds
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_recall_census import (
    RecallCensusReport,
    RecallCensusResult,
    _stage1_evidence,
    build_census_report,
    ensure_census_schema,
    evaluate_census_symbol,
    latest_census_report_summary,
    next_due_census_session,
    run_recall_census,
    write_census_report,
)
from tradebot import postmarket_discovery_shadow as discovery_shadow


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
NOW = END + timedelta(minutes=5, seconds=1)


def _bar(symbol, ts, close, volume=10_000):
    return Bar(symbol, ts, close, close, close, close, volume)


def _candidate_bars(symbol, *, final_close=None):
    bars = [
        _bar(symbol, CLOSE - timedelta(minutes=5), 100),
        _bar(symbol, CLOSE, 110),
        _bar(symbol, CLOSE + timedelta(minutes=5), 111),
    ]
    if final_close is not None:
        bars.append(_bar(symbol, CLOSE + timedelta(minutes=10), final_close))
    return bars


def _seed_stage1(conn):
    source_updates = {
        "market_movers": (CLOSE + timedelta(minutes=10)).isoformat(),
        "most_actives_volume": (CLOSE + timedelta(minutes=10)).isoformat(),
        "most_actives_trades": (CLOSE + timedelta(minutes=10)).isoformat(),
    }
    cursor = conn.execute(
        """
        INSERT INTO postmarket_discovery_ticks
            (session,tick_utc,completed_utc,run_id,run_mode,discovery_version,
             code_version,data_feed,market_data_provider,bar_timeframe,
             discovery_scope,endpoints_json,source_updates_json,requested_top_n,
             universe_symbols,screen_rows,screen_unique_symbols,excluded_symbols,
             discovered_symbols,not_returned_symbols,fetched_symbols,
             evaluated_symbols,candidate_observations,new_candidates,invariant_ok,
             thresholds_json,latency_ms,error_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(),
            (CLOSE + timedelta(minutes=10)).isoformat(),
            (CLOSE + timedelta(minutes=10, seconds=1)).isoformat(),
            "stage1-run",
            "postmarket-marketwide-shadow",
            1,
            "stage1-code",
            "sip",
            "alpaca",
            "5Min",
            "alpaca_top_movers_and_actives",
            json.dumps(["market_movers", "most_actives_volume", "most_actives_trades"]),
            json.dumps(source_updates),
            50,
            3,
            200,
            2,
            0,
            2,
            1,
            2,
            2,
            1,
            1,
            1,
            json.dumps(thresholds()),
            1000,
            0,
        ),
    )
    tick_id = int(cursor.lastrowid)
    for symbol, outcome in (("A", "CANDIDATE"), ("B", "BELOW_MOVE")):
        conn.execute(
            """
            INSERT INTO postmarket_discovery_observations
                (tick_id,symbol,sources_json,ranks_json,screen_evidence_json,
                 screen_move_pct,outcome,reason,event_date,bar_open_ts_utc,
                 rth_close,open,high,low,close,volume,cumulative_volume,
                 cumulative_notional,move_pct,direction,persistence_bars,
                 data_age_seconds,data_feed,market_data_provider,bar_timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tick_id,
                symbol,
                '["market_gainer"]',
                '[["market_gainer",1]]',
                "[]",
                10,
                outcome,
                "test",
                SESSION.isoformat(),
                (CLOSE + timedelta(minutes=5)).isoformat(),
                100,
                111,
                111,
                111,
                111,
                10_000,
                20_000,
                2_200_000,
                11,
                "up",
                2,
                0,
                "sip",
                "alpaca",
                "5Min",
            ),
        )
    conn.execute(
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
            "A",
            SESSION.isoformat(),
            "up",
            1,
            (CLOSE + timedelta(minutes=10, seconds=1)).isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(),
            100,
            111,
            11,
            20_000,
            2_200_000,
            '["market_gainer"]',
            "sip",
            "alpaca",
            "5Min",
            "stage1-code",
            "stage1-run",
        ),
    )
    conn.commit()


def test_symbol_replay_keeps_transient_qualification_after_final_reversal():
    result = evaluate_census_symbol(
        "A",
        SESSION,
        _candidate_bars("A", final_close=101),
        session_close=CLOSE,
        postmarket_end=END,
        stage1_seen=False,
        stage1_candidates={},
    )

    assert result.qualifying_directions == ("up",)
    assert result.first_qualified_at == {
        "up": (CLOSE + timedelta(minutes=10)).isoformat()
    }
    assert result.false_negative_directions == ("up",)
    assert result.miss_reasons == {"up": "NOT_OBSERVED_BY_LIVE_DISCOVERY"}
    assert result.final_outcome != "CANDIDATE"


def test_recall_stage1_reads_full_universe_sweep_observations_and_candidates(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_stage1(conn)
    tick_id = conn.execute(
        "SELECT tick_id FROM postmarket_discovery_ticks WHERE session=?",
        (SESSION.isoformat(),),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO postmarket_discovery_observations
            (tick_id,symbol,sources_json,ranks_json,screen_evidence_json,
             screen_move_pct,outcome,reason,event_date,bar_open_ts_utc,
             rth_close,open,high,low,close,volume,cumulative_volume,
             cumulative_notional,move_pct,direction,persistence_bars,
             data_age_seconds,data_feed,market_data_provider,bar_timeframe)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            tick_id,
            "SWEEP",
            '["full_universe_sweep"]',
            "[]",
            "[]",
            None,
            "CANDIDATE",
            "qualified",
            SESSION.isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(),
            100,
            111,
            111,
            111,
            111,
            10_000,
            20_000,
            2_200_000,
            11,
            "up",
            2,
            0,
            "sip",
            "alpaca",
            "5Min",
        ),
    )
    detected_at = CLOSE + timedelta(minutes=10, seconds=1)
    conn.execute(
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
            "SWEEP",
            SESSION.isoformat(),
            "up",
            2,
            detected_at.isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(),
            100,
            111,
            11,
            20_000,
            2_200_000,
            '["full_universe_sweep"]',
            "sip",
            "alpaca",
            "5Min",
            "sweep-code",
            "sweep-run",
        ),
    )
    conn.commit()

    seen, candidates = _stage1_evidence(conn, SESSION)

    assert "SWEEP" in seen
    assert candidates["SWEEP"] == {"up": detected_at}


def test_census_measures_recall_misses_delays_and_unavailable_universe(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_stage1(conn)

    result, rows = run_recall_census(
        conn,
        universe_symbols=["C", "B", "A"],
        session=SESSION,
        session_close=CLOSE,
        postmarket_end=END,
        now=NOW,
        run_id="census-run",
        code_version="census-code",
        data_feed="sip",
        bars_fetch=lambda symbols, start, end: {
            "A": _candidate_bars("A"),
            "B": _candidate_bars("B"),
        },
        chunk_size=10,
    )

    assert result.status == "success"
    assert result.universe_symbols == 3
    assert result.fetched_symbols == result.evaluated_symbols == 2
    assert result.unavailable_symbols == 1
    assert result.eligible_pairs == 2
    assert result.true_positive_pairs == 1
    assert result.false_negative_pairs == 1
    assert result.false_positive_pairs == 0
    assert result.recall == 0.5
    assert result.average_detection_delay_seconds == 1
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["B"].false_negative_directions == ("up",)
    assert by_symbol["B"].miss_reasons == {"up": "RETURNED_NOT_CONFIRMED"}
    assert by_symbol["C"].data_status == "NO_DATA_RETURNED"
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_recall_census_events"
    ).fetchone()[0] == 3
    assert next_due_census_session(conn, now=NOW + timedelta(minutes=1)) is None

    report = build_census_report(conn, result.census_id)
    assert report.operational_complete is True
    assert report.evidence_eligible is False
    assert report.false_negatives[0]["symbol"] == "B"
    assert {
        "UNAVAILABLE_SYMBOLS",
        "PROVIDER_COMPARISON_NOT_CONFIGURED",
        "RECALL_BELOW_95_PERCENT",
    } <= set(report.issue_codes)


def test_chunk_failure_is_conserved_and_retry_is_append_only(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_stage1(conn)

    first, _ = run_recall_census(
        conn,
        universe_symbols=["A", "B"],
        session=SESSION,
        session_close=CLOSE,
        postmarket_end=END,
        now=NOW,
        run_id="census-failed",
        code_version="census-code",
        data_feed="sip",
        bars_fetch=lambda symbols, start, end: (_ for _ in ()).throw(
            RuntimeError("provider down")
        ),
        chunk_size=10,
    )
    second, _ = run_recall_census(
        conn,
        universe_symbols=["A", "B"],
        session=SESSION,
        session_close=CLOSE,
        postmarket_end=END,
        now=NOW + timedelta(minutes=5),
        run_id="census-retry",
        code_version="census-code",
        data_feed="sip",
        bars_fetch=lambda symbols, start, end: {
            symbol: _candidate_bars(symbol) for symbol in symbols
        },
        chunk_size=10,
    )

    assert (first.attempt, first.status, first.error_count) == (1, "degraded", 1)
    assert (second.attempt, second.status, second.error_count) == (2, "success", 0)
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_recall_census_runs"
    ).fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_recall_census_runs SET status='success'")


def test_evaluation_failure_is_counted_and_degrades_attempt(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_stage1(conn)

    result, rows = run_recall_census(
        conn,
        universe_symbols=["A"],
        session=SESSION,
        session_close=CLOSE,
        postmarket_end=END,
        now=NOW,
        run_id="census-evaluation-error",
        code_version="census-code",
        data_feed="sip",
        bars_fetch=lambda symbols, start, end: {"A": [object()]},
    )

    assert result.status == "degraded"
    assert result.error_count == 1
    assert result.invariant_ok is False
    assert rows[0].data_status == "EVALUATION_ERROR"


def test_census_report_publication_is_immutable(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_stage1(conn)
    result, _ = run_recall_census(
        conn,
        universe_symbols=["A"],
        session=SESSION,
        session_close=CLOSE,
        postmarket_end=END,
        now=NOW,
        run_id="census-run",
        code_version="census-code",
        data_feed="sip",
        bars_fetch=lambda symbols, start, end: {"A": _candidate_bars("A")},
    )

    report, written = write_census_report(conn, tmp_path / "audits", result.census_id)
    _, replay_written = write_census_report(conn, tmp_path / "audits", result.census_id)

    assert written is True
    assert replay_written is False
    path = tmp_path / "audits" / "postmarket_recall_census_2026-08-27_v1.json"
    assert json.loads(path.read_text(encoding="utf-8"))["census_id"] == result.census_id
    assert report.session == "2026-08-27"
    assert latest_census_report_summary(tmp_path / "audits") == {
        "session": "2026-08-27",
        "report_version": 1,
        "operational_complete": True,
        "evidence_eligible": False,
        "recall": 1.0,
        "false_negative_pairs": 0,
        "unavailable_symbols": 0,
        "issue_codes": ["PROVIDER_COMPARISON_NOT_CONFIGURED"],
    }


def test_census_module_has_no_provider_delivery_or_trading_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_recall_census.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = (
        "tradebot.vendors",
        "tradebot.alerts",
        "tradebot.telegram_bot",
        "tradebot.order",
        "tradebot.broker",
    )
    assert not any(module.startswith(forbidden) for module in imports)


def test_service_heartbeat_surfaces_completed_census(monkeypatch):
    result = RecallCensusResult(
        census_id=7,
        session=SESSION.isoformat(),
        attempt=1,
        status="success",
        universe_symbols=13_000,
        requested_chunks=26,
        fetched_symbols=8_000,
        evaluated_symbols=8_000,
        unavailable_symbols=5_000,
        stage1_seen_symbols=170,
        stage1_candidate_pairs=12,
        eligible_pairs=20,
        true_positive_pairs=12,
        false_negative_pairs=8,
        false_positive_pairs=0,
        recall=0.6,
        average_detection_delay_seconds=20,
        max_detection_delay_seconds=50,
        invariant_ok=True,
        error_count=0,
        latency_ms=12_345,
    )
    report = RecallCensusReport(
        report_version=1,
        census_id=7,
        session=SESSION.isoformat(),
        attempt=1,
        code_version="census-code",
        operational_complete=True,
        evidence_eligible=False,
        metrics={
            "recall": 0.6,
            "false_negative_pairs": 8,
            "unavailable_symbols": 5_000,
        },
        false_negatives=(),
        false_positives=(),
        unavailable=(),
        issue_codes=("UNAVAILABLE_SYMBOLS",),
    )
    monkeypatch.setattr(
        discovery_shadow,
        "next_due_census_session",
        lambda conn, now: (SESSION, CLOSE, END),
    )
    monkeypatch.setattr(discovery_shadow, "active_symbols", lambda conn: ["A"])
    monkeypatch.setattr(
        discovery_shadow,
        "run_recall_census",
        lambda *args, **kwargs: (result, ()),
    )
    monkeypatch.setattr(
        discovery_shadow,
        "write_census_report",
        lambda *args, **kwargs: (report, True),
    )

    fields = discovery_shadow.recall_census_heartbeat_fields(
        NOW,
        object(),
        object(),
        data_feed="sip",
        version="census-code",
    )

    assert fields["recall_census_status"] == "success"
    assert fields["recall_census_universe"] == 13_000
    assert fields["recall_census_false_negatives"] == 8
    assert fields["recall_census_recall"] == 0.6
    assert fields["latest_recall_census"]["issue_codes"] == ["UNAVAILABLE_SYMBOLS"]
