"""Full-universe second-provider proof remains append-only and shadow-only."""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_discovery_shadow as discovery_shadow
from tradebot.detectors import Bar
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_recall_census import ensure_census_schema
from tradebot.postmarket_recall_provider import (
    build_provider_proof_report,
    latest_provider_proof_summary,
    next_due_provider_proof,
    run_provider_proof,
    write_provider_proof_report,
)
from tradebot.vendors.massive_flatfiles import FlatFileSnapshot, object_key
from tradebot.vendors.historical_reference import (
    HistoricalReferenceCapabilities,
    HistoricalReferenceConfigurationError,
    HistoricalReferenceSource,
)


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 0, tzinfo=timezone.utc)
PROOF_NOW = datetime(2026, 8, 28, 17, 6, tzinfo=timezone.utc)


def _bar(symbol, ts, close, volume=10_000):
    return Bar(symbol, ts, close, close, close, close, volume)


def _candidate_bars(symbol, *, candidate_close=111):
    return (
        _bar(symbol, CLOSE - timedelta(minutes=5), 100),
        _bar(symbol, CLOSE, 110),
        _bar(symbol, CLOSE + timedelta(minutes=5), candidate_close),
    )


def _seed(conn):
    ensure_census_schema(conn)
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
            SESSION.isoformat(), "A", SESSION.isoformat(), "up", 1,
            (CLOSE + timedelta(minutes=10)).isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(), 100, 111, 11,
            20_000, 2_200_000, '["market_gainer"]', "sip", "alpaca",
            "5Min", "stage1-code", "stage1-run",
        ),
    )
    cursor = conn.execute(
        """
        INSERT INTO postmarket_recall_census_runs
            (session,census_version,attempt,run_id,started_at_utc,
             completed_at_utc,code_version,data_feed,market_data_provider,
             bar_timeframe,provider_comparison_status,universe_snapshot_sha256,
             universe_symbols,requested_chunks,fetched_symbols,evaluated_symbols,
             unavailable_symbols,stage1_seen_symbols,stage1_candidate_pairs,
             eligible_pairs,true_positive_pairs,false_negative_pairs,
             false_positive_pairs,recall,average_detection_delay_seconds,
             max_detection_delay_seconds,status,invariant_ok,error_count,
             thresholds_json,detail_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), 1, 1, "census-run", END.isoformat(),
            (END + timedelta(minutes=1)).isoformat(), "census-code", "sip",
            "alpaca", "5Min", "NOT_CONFIGURED", "digest", 2, 1, 2, 2, 0,
            1, 1, 2, 1, 1, 0, 0.5, 0, 0, "success", 1, 0, "{}", "{}",
        ),
    )
    census_id = int(cursor.lastrowid)
    for symbol in ("A", "B"):
        conn.execute(
            """
            INSERT INTO postmarket_recall_census_events
                (census_id,symbol,data_status,final_outcome,final_reason,
                 qualifying_directions_json,first_qualified_at_json,stage1_seen,
                 stage1_directions_json,false_negative_directions_json,
                 false_positive_directions_json,miss_reasons_json,
                 detection_delays_json,rth_close,postmarket_bars,
                 first_postmarket_bar_utc,final_postmarket_bar_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                census_id, symbol, "AVAILABLE", "CANDIDATE", "test", '["up"]',
                json.dumps({"up": (CLOSE + timedelta(minutes=10)).isoformat()}),
                int(symbol == "A"), '["up"]' if symbol == "A" else "[]",
                "[]" if symbol == "A" else '["up"]', "[]", "{}", "{}",
                100, 2, CLOSE.isoformat(), (CLOSE + timedelta(minutes=5)).isoformat(),
            ),
        )
    conn.commit()
    return census_id


def _snapshot(bars):
    return FlatFileSnapshot(
        SESSION, object_key(SESSION), "etag", PROOF_NOW.isoformat(), 123,
        hashlib.sha256(b"selected").hexdigest(), 100, 6, len(bars), bars,
    )


def _custom_source(bars):
    return HistoricalReferenceSource(
        provider="licensed-test-provider",
        feed="consolidated",
        dataset="us-equities-minute-v2",
        capabilities=HistoricalReferenceCapabilities(
            completed_intraday_bars=True,
            full_universe_snapshot=True,
            postmarket_coverage=True,
            immutable_object_provenance=True,
            production_qualified=True,
        ),
        configured=lambda: True,
        expected_available_at=lambda session: PROOF_NOW - timedelta(minutes=1),
        object_key=lambda session: f"licensed/{session.isoformat()}.parquet",
        fetch=lambda session, symbols, start, end: FlatFileSnapshot(
            session,
            f"licensed/{session.isoformat()}.parquet",
            "licensed-etag",
            PROOF_NOW.isoformat(),
            456,
            hashlib.sha256(b"licensed-selected").hexdigest(),
            100,
            6,
            len(bars),
            bars,
        ),
    )


def _ineligible_source():
    return HistoricalReferenceSource(
        provider="eod-only-provider",
        feed="eod",
        dataset="daily-prices",
        capabilities=HistoricalReferenceCapabilities(
            completed_intraday_bars=False,
            full_universe_snapshot=True,
            postmarket_coverage=False,
            immutable_object_provenance=True,
            production_qualified=True,
        ),
        configured=lambda: True,
        expected_available_at=lambda session: PROOF_NOW - timedelta(minutes=1),
        object_key=lambda session: f"daily/{session.isoformat()}.csv.gz",
        fetch=lambda *args: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )


def test_provider_proof_waits_for_next_day_file_and_is_due_once(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    assert next_due_provider_proof(
        conn, now=datetime(2026, 8, 28, 17, 4, tzinfo=timezone.utc)
    ) is None
    assert next_due_provider_proof(conn, now=PROOF_NOW) == (census_id, SESSION)


def test_eod_only_provider_cannot_enter_intraday_recall_proof(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    reference = _ineligible_source()

    with pytest.raises(
        HistoricalReferenceConfigurationError,
        match="cannot serve the intraday full-universe recall proof",
    ):
        next_due_provider_proof(
            conn, now=PROOF_NOW, independent_source=reference
        )
    with pytest.raises(
        HistoricalReferenceConfigurationError,
        match="completed_intraday_bars, postmarket_coverage",
    ):
        run_provider_proof(
            conn,
            census_id=census_id,
            session=SESSION,
            now=PROOF_NOW,
            run_id="must-not-run",
            code_version="proof-code",
            primary_fetch=lambda *args: {},
            independent_fetch=reference.fetch,
            independent_source=reference,
        )
    assert conn.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='table' AND name='postmarket_recall_provider_runs'
        """
    ).fetchone()[0] == 0


def test_provider_proof_replays_same_universe_and_can_pass_cleanly(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}
    independent_calls = []

    def independent_fetch(session, symbols, start, end):
        independent_calls.append((session, tuple(symbols), start, end))
        return _snapshot(bars)

    result, rows = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-run", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {
            symbol: bars[symbol] for symbol in symbols
        },
        independent_fetch=independent_fetch, chunk_size=1,
    )
    assert result.status == "success"
    assert result.comparable_coverage == 1
    assert result.eligible_pair_agreement == 1
    assert result.independent_recall == 0.5
    assert result.compared_bars == 6
    assert result.price_disagreement_bars == 0
    assert len(rows) == 2
    assert len(independent_calls) == 1
    assert next_due_provider_proof(conn, now=PROOF_NOW + timedelta(minutes=1)) is None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_recall_provider_runs SET status='degraded'")

    report = build_provider_proof_report(conn, result.comparison_id)
    assert report.operational_complete is True
    assert report.evidence_eligible is False
    assert report.issue_codes == ("INDEPENDENT_RECALL_BELOW_95_PERCENT",)


def test_provider_identity_and_object_contract_come_from_selected_adapter(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}
    reference = _custom_source(bars)

    assert next_due_provider_proof(
        conn, now=PROOF_NOW, independent_source=reference
    ) == (census_id, SESSION)
    result, _ = run_provider_proof(
        conn,
        census_id=census_id,
        session=SESSION,
        now=PROOF_NOW,
        run_id="custom-provider-proof",
        code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {
            symbol: bars[symbol] for symbol in symbols
        },
        independent_fetch=reference.fetch,
        independent_source=reference,
    )

    assert result.status == "success"
    source_row = conn.execute(
        """
        SELECT independent_provider,independent_feed,independent_dataset,object_key
        FROM postmarket_recall_provider_runs WHERE comparison_id=?
        """,
        (result.comparison_id,),
    ).fetchone()
    assert source_row == (
        "licensed-test-provider",
        "consolidated",
        "us-equities-minute-v2",
        f"licensed/{SESSION.isoformat()}.parquet",
    )


def test_provider_proof_exposes_eligibility_and_price_disagreement(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    primary = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}
    independent = {
        "A": _candidate_bars("A"),
        "B": _candidate_bars("B", candidate_close=90),
    }
    result, rows = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-run", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {
            symbol: primary[symbol] for symbol in symbols
        },
        independent_fetch=lambda session, symbols, start, end: _snapshot(independent),
    )
    by_symbol = {row.symbol: row for row in rows}
    assert result.eligible_pair_agreement == 0.5
    assert result.price_disagreement_bars == 1
    assert by_symbol["B"].primary_only_directions == ("up",)
    assert by_symbol["B"].max_abs_close_difference_bps > 50
    report = build_provider_proof_report(conn, result.comparison_id)
    assert "PROVIDER_ELIGIBLE_PAIR_AGREEMENT_BELOW_95_PERCENT" in report.issue_codes
    assert "PROVIDER_PRICE_DISAGREEMENT" in report.issue_codes


def test_coverage_denominator_is_the_frozen_universe_and_bar_overlap_is_gated(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}
    result, _ = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-partial", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {
            symbol: bars[symbol] for symbol in symbols if symbol == "A"
        },
        independent_fetch=lambda session, symbols, start, end: _snapshot(bars),
    )
    report = build_provider_proof_report(conn, result.comparison_id)
    assert result.primary_evaluated_symbols == 1
    assert result.comparable_coverage == 0.5
    assert result.bar_overlap_coverage == 0.5
    assert "PROVIDER_SYMBOL_COVERAGE_BELOW_99_PERCENT" in report.issue_codes
    assert "PROVIDER_BAR_OVERLAP_BELOW_95_PERCENT" in report.issue_codes


def test_independent_file_failure_is_a_durable_degraded_attempt(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}

    def unavailable(*args):
        raise RuntimeError("not published")

    result, rows = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-failed", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {symbol: bars[symbol] for symbol in symbols},
        independent_fetch=unavailable,
    )
    assert result.status == "degraded"
    assert result.error_count == 1
    assert {row.independent_data_status for row in rows} == {"FETCH_ERROR"}
    assert conn.execute(
        "SELECT status,error_count FROM postmarket_recall_provider_runs"
    ).fetchone() == ("degraded", 1)
    assert next_due_provider_proof(conn, now=PROOF_NOW + timedelta(minutes=1)) == (
        census_id, SESSION,
    )


def test_wrong_independent_object_is_a_durable_degraded_attempt(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}
    wrong = FlatFileSnapshot(
        SESSION, "wrong/object.csv.gz", "etag", PROOF_NOW.isoformat(), 123,
        hashlib.sha256(b"selected").hexdigest(), 100, 6, 2, bars,
    )
    result, rows = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-wrong-object", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {symbol: bars[symbol] for symbol in symbols},
        independent_fetch=lambda *args: wrong,
    )
    assert result.status == "degraded"
    assert result.error_count == 1
    assert {row.independent_data_status for row in rows} == {"FETCH_ERROR"}


def test_provider_proof_report_is_immutable_and_summarized(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}
    result, _ = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-run", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {symbol: bars[symbol] for symbol in symbols},
        independent_fetch=lambda session, symbols, start, end: _snapshot(bars),
    )
    report, written = write_provider_proof_report(
        conn, tmp_path / "audits", result.comparison_id,
    )
    _, written_again = write_provider_proof_report(
        conn, tmp_path / "audits", result.comparison_id,
    )
    assert written is True and written_again is False
    assert latest_provider_proof_summary(tmp_path / "audits")["session"] == SESSION.isoformat()
    assert report.source["selected_rows_sha256"] == _snapshot(bars).selected_rows_sha256


def test_report_version_is_not_retry_attempt_and_latest_summary_is_numeric(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    bars = {symbol: _candidate_bars(symbol) for symbol in ("A", "B")}

    failed, _ = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-failed", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {symbol: bars[symbol] for symbol in symbols},
        independent_fetch=lambda *args: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    passed, _ = run_provider_proof(
        conn, census_id=census_id, session=SESSION,
        now=PROOF_NOW + timedelta(minutes=1), run_id="proof-retry",
        code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {symbol: bars[symbol] for symbol in symbols},
        independent_fetch=lambda session, symbols, start, end: _snapshot(bars),
    )
    first, _ = write_provider_proof_report(conn, tmp_path / "audits", failed.comparison_id)
    second, _ = write_provider_proof_report(conn, tmp_path / "audits", passed.comparison_id)

    assert first.report_version == second.report_version == 1
    assert first.attempt == 1 and second.attempt == 2
    assert latest_provider_proof_summary(tmp_path / "audits")["evidence_eligible"] is False


def test_no_independent_eligible_pairs_is_explicitly_undefined(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    census_id = _seed(conn)
    quiet = {
        symbol: (
            _bar(symbol, CLOSE - timedelta(minutes=5), 100),
            _bar(symbol, CLOSE, 100),
            _bar(symbol, CLOSE + timedelta(minutes=5), 101),
        )
        for symbol in ("A", "B")
    }
    result, _ = run_provider_proof(
        conn, census_id=census_id, session=SESSION, now=PROOF_NOW,
        run_id="proof-quiet", code_version="proof-code",
        primary_fetch=lambda symbols, start, end: {symbol: quiet[symbol] for symbol in symbols},
        independent_fetch=lambda session, symbols, start, end: _snapshot(quiet),
    )
    report = build_provider_proof_report(conn, result.comparison_id)
    assert report.metrics["independent_recall"] is None
    assert "INDEPENDENT_RECALL_UNDEFINED" in report.issue_codes


def test_provider_proof_module_has_no_delivery_or_trading_dependency():
    source = Path(__file__).parents[1] / "tradebot" / "postmarket_recall_provider.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {
        node.module or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(
        forbidden in name
        for name in imports
        for forbidden in ("telegram", "outbox", "broker", "order", "alert")
    )


def test_service_heartbeat_reports_unconfigured_without_fetching(monkeypatch, tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed(conn)
    monkeypatch.setattr(discovery_shadow, "AUDIT_DIR", tmp_path / "audits")
    fields = discovery_shadow.provider_proof_heartbeat_fields(
        PROOF_NOW, conn, version="proof-code",
        provider_configured=lambda: False,
        independent_fetch=lambda *args: pytest.fail("must not fetch"),
    )
    assert fields == {
        "provider_proof_status": "unconfigured",
        "latest_provider_proof": None,
    }
