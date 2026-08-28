"""External context is point-in-time, attributable, and fail-visible."""
from __future__ import annotations

import sqlite3
import ast
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.detectors import Bar
from tradebot.marketdata import NewsItem, OptionChain, OptionContract, Quote
from tradebot.postmarket_context import CandidateFact
from tradebot.postmarket_discovery import DISCOVERY_SCHEMA
from tradebot.postmarket_external_context import (
    build_pre_event_expectation,
    current_option_context_fact,
    filing_snapshot_facts,
    halt_state_fact,
    independent_price_comparison_fact,
    news_fact,
    pre_event_expectation_fact,
    run_external_context_backfill,
    run_pre_event_expectation_capture,
    ticker_reference_facts,
)
from tradebot.postmarket_external_context_shadow import (
    external_context_enabled,
    pre_close_capture_window,
)
from tradebot.vendors.massive import TickerReference
from tradebot.vendors.nasdaq_halts import HaltRecord
from tradebot.vendors.sec_companyfacts import PointInTimeSnapshot, ReportedFact


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, tzinfo=timezone.utc)
DETECTED = CLOSE + timedelta(minutes=10)


def _candidate(candidate_id=1):
    return CandidateFact(
        candidate_id, SESSION, "ABC", "up", DETECTED,
        CLOSE + timedelta(minutes=5), 100, 110, 10, 2_000_000,
        "sip", "alpaca", "5Min",
    )


def _contract(symbol, strike, right, bid, ask, quote_ts=DETECTED):
    return OptionContract(
        symbol, SESSION + timedelta(days=8), strike, right, bid, ask,
        (bid + ask) / 2, None, None, 100, quote_ts=quote_ts,
        quote_feed="indicative",
    )


def _chain(quote_ts=DETECTED):
    expiry = SESSION + timedelta(days=8)
    return OptionChain("ABC", expiry, (
        _contract("ABC-C-100", 100, "call", 12, 14, quote_ts),
        _contract("ABC-P-100", 100, "put", 2, 4, quote_ts),
        _contract("ABC-C-110", 110, "call", 7, 9, quote_ts),
        _contract("ABC-P-110", 110, "put", 6, 8, quote_ts),
        _contract("ABC-C-120", 120, "call", 3, 4, quote_ts),
        _contract("ABC-P-120", 120, "put", 11, 13, quote_ts),
    ))


def test_current_option_context_is_not_mislabeled_as_pre_event_expectation():
    fact = current_option_context_fact(
        _candidate(), chain=_chain(), observed_at=DETECTED + timedelta(seconds=2),
        code_version="abc1234", run_id="run",
    )

    assert fact.status == "AVAILABLE_INDICATIVE"
    assert fact.fact_kind == "CURRENT_OPTION_MARKET_CONTEXT"
    assert fact.provider == "alpaca"
    assert fact.feed == "indicative"
    assert fact.effective_at_utc == DETECTED.isoformat()
    assert fact.payload["strike"] == 110
    assert fact.payload["straddle_mid"] == 15
    assert fact.payload["straddle_pct_of_spot"] == pytest.approx(15 / 110 * 100)
    assert fact.payload["semantic"] == "indicative_atm_straddle_not_probability"
    assert "not_pre_event" in fact.payload["temporal_semantic"]


def test_future_option_quote_is_rejected_instead_of_backdated():
    chain = OptionChain("ABC", SESSION + timedelta(days=8), (
        _contract("C", 110, "call", 7, 9, DETECTED + timedelta(minutes=5)),
        _contract("P", 110, "put", 6, 8, DETECTED + timedelta(minutes=5)),
    ))
    with pytest.raises(ValueError, match="later than observation"):
        current_option_context_fact(
            _candidate(), chain=chain, observed_at=DETECTED,
            code_version="x", run_id="run",
        )


def test_news_filters_future_and_other_symbol_items_without_claiming_causality():
    matching = NewsItem(
        "1", "ABC reports results", "wire", "https://example.test/1",
        DETECTED - timedelta(minutes=1), DETECTED - timedelta(minutes=1), ("ABC",),
    )
    future = NewsItem(
        "2", "Future ABC update", "wire", None,
        DETECTED + timedelta(minutes=5), DETECTED + timedelta(minutes=5), ("ABC",),
    )
    other = NewsItem(
        "3", "XYZ update", "wire", None,
        DETECTED - timedelta(minutes=1), DETECTED - timedelta(minutes=1), ("XYZ",),
    )
    fact = news_fact(
        _candidate(), items=(future, other, matching), observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert fact.status == "AVAILABLE"
    assert fact.payload["match_count"] == 1
    assert fact.payload["items"][0]["provider_id"] == "1"
    assert "not_causal" in fact.payload["semantic"]


def _comparison_bars(rth_close=100, candidate_close=110):
    return (
        Bar("ABC", CLOSE - timedelta(minutes=5), 99, 101, 98, rth_close, 1_000),
        Bar("ABC", CLOSE + timedelta(minutes=5), 108, 111, 107, candidate_close, 2_000),
    )


def test_independent_price_fact_aligns_exact_bars_and_confirms_direction():
    fact = independent_price_comparison_fact(
        _candidate(), bars=_comparison_bars(), observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert fact.status == "AVAILABLE"
    assert fact.provider == "massive"
    assert fact.payload["direction_agrees"] is True
    assert fact.payload["rth_close_difference_bps"] == pytest.approx(0)
    assert fact.payload["candidate_close_difference_bps"] == pytest.approx(0)
    assert fact.payload["comparison_rule"].endswith("_v1")
    assert "not_signal_input" in fact.payload["semantic"]


def test_independent_price_fact_flags_material_disagreement():
    fact = independent_price_comparison_fact(
        _candidate(), bars=_comparison_bars(rth_close=100, candidate_close=90),
        observed_at=DETECTED, code_version="x", run_id="run",
    )
    assert fact.status == "AVAILABLE_DISAGREEMENT"
    assert fact.payload["direction_agrees"] is False


def test_independent_price_fact_never_fills_missing_exact_bar():
    fact = independent_price_comparison_fact(
        _candidate(), bars=_comparison_bars()[:1], observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert fact.status == "UNAVAILABLE_NO_DATA"
    assert fact.payload["reason"] == "EXACT_COMPARISON_BAR_MISSING"
    assert "not_filled" in fact.payload["semantic"]


def test_ticker_reference_facts_are_context_not_replay_safe_rank_inputs():
    reference = TickerReference(
        "ABC", SESSION, True, "stocks", "XNAS", "CS", "usd",
        1_000_000, 100_000, 90_000, "3571", "ELECTRONIC COMPUTERS",
        DETECTED - timedelta(minutes=1),
    )
    sector, fundamentals = ticker_reference_facts(
        _candidate(), reference=reference, observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert sector.status == "AVAILABLE"
    assert sector.payload["classification_system"] == "SEC_SIC"
    assert "not_gics" in sector.payload["semantic"]
    assert fundamentals.status == "AVAILABLE"
    assert fundamentals.payload["float_status"] == "UNAVAILABLE_NOT_PROVIDED_BY_ENDPOINT"
    assert fundamentals.payload["temporal_semantic"].endswith("not_replay_safe")


def _filing_snapshot():
    accepted = DETECTED - timedelta(days=30)
    return PointInTimeSnapshot(
        "ABC", "0000000001", DETECTED, DETECTED - timedelta(minutes=15),
        "0000000001-26-000001", accepted, "7372",
        "SERVICES-PREPACKAGED SOFTWARE",
        (
            ReportedFact(
                "common_shares_outstanding", "dei",
                "EntityCommonStockSharesOutstanding", "shares", 1_000_000,
                None, date(2026, 6, 30), "0000000001-26-000001", "10-Q",
                date(2026, 7, 31), accepted, 2026, "Q2", "CY2026Q2I",
            ),
            ReportedFact(
                "net_income", "us-gaap", "NetIncomeLoss", "USD", -50_000,
                date(2026, 4, 1), date(2026, 6, 30),
                "0000000001-26-000001", "10-Q", date(2026, 7, 31),
                accepted, 2026, "Q2", "CY2026Q2",
            ),
        ),
        40, 38, (),
    )


def test_sec_filing_facts_are_acceptance_bounded_and_semantically_explicit():
    classification, fundamentals = filing_snapshot_facts(
        _candidate(), snapshot=_filing_snapshot(), observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert classification.status == "AVAILABLE"
    assert classification.provider == "sec_edgar"
    assert classification.payload["classification_system"] == "SEC_SIC"
    assert "not_gics" in classification.payload["semantic"]
    assert fundamentals.status == "AVAILABLE"
    assert fundamentals.payload["dissemination_safety_lag_seconds"] == 900
    assert fundamentals.payload["available_fact_names"] == [
        "common_shares_outstanding", "net_income",
    ]
    assert fundamentals.payload["facts"][1]["value"] == -50_000
    assert fundamentals.payload["missing_concepts_are_not_zero"] is True


def test_sec_fact_builder_rejects_post_cutoff_acceptance():
    snapshot = _filing_snapshot()
    future = ReportedFact(
        "assets", "us-gaap", "Assets", "USD", 100,
        None, date(2026, 8, 27), "0000000001-26-000004", "10-Q",
        date(2026, 8, 27), DETECTED, 2026, "Q3", "CY2026Q3I",
    )
    invalid = PointInTimeSnapshot(
        snapshot.symbol, snapshot.cik, snapshot.candidate_cutoff_utc,
        snapshot.eligible_cutoff_utc, snapshot.classification_accession,
        snapshot.classification_accepted_at_utc, snapshot.sic_code,
        snapshot.sic_description, snapshot.facts + (future,),
        snapshot.recent_submission_count, snapshot.eligible_submission_count,
        snapshot.errors,
    )
    with pytest.raises(ValueError, match="accepted after"):
        filing_snapshot_facts(
            _candidate(), snapshot=invalid, observed_at=DETECTED,
            code_version="x", run_id="run",
        )


def test_halt_fact_uses_official_records_not_missing_bar_inference():
    record = HaltRecord(
        "ABC", "ABC Corp", "Q", "LUDP", "10.00",
        DETECTED - timedelta(minutes=2),
        DETECTED + timedelta(minutes=1),
        DETECTED + timedelta(minutes=2),
    )
    fact = halt_state_fact(
        _candidate(), records=(record,), observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert fact.status == "AVAILABLE"
    assert fact.provider == "nasdaq_trader"
    assert fact.payload["halted_at_candidate_detection"] is True
    assert fact.payload["records"][0]["reason_code"] == "LUDP"
    assert "not_missing_bar_inference" in fact.payload["semantic"]


def test_halt_fact_successfully_distinguishes_no_match_from_fetch_failure():
    fact = halt_state_fact(
        _candidate(), records=(), observed_at=DETECTED,
        code_version="x", run_id="run",
    )
    assert fact.status == "AVAILABLE_NO_MATCHES"
    assert fact.payload["match_count"] == 0


def _seed(conn):
    conn.executescript(DISCOVERY_SCHEMA)
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
            SESSION.isoformat(), "ABC", SESSION.isoformat(), "up", 1,
            DETECTED.isoformat(), (CLOSE + timedelta(minutes=5)).isoformat(),
            100, 110, 10, 100_000, 2_000_000, '["market_gainer"]',
            "sip", "alpaca", "5Min", "candidate-code", "candidate-run",
        ),
    )
    conn.commit()
    return cursor.lastrowid


def test_pre_close_capture_is_the_only_source_of_expected_move_baseline():
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    captured = CLOSE - timedelta(minutes=5)
    quote = Quote("ABC", captured - timedelta(seconds=1), 99, 101, 100)
    result = run_pre_event_expectation_capture(
        conn, session=SESSION, session_close=CLOSE, now=captured, symbols=("ABC",),
        code_version="ctx-code", run_id="run",
        quote_fetch=lambda symbols: {"ABC": quote},
        option_fetch=lambda symbol, session, spot: _chain(captured - timedelta(seconds=1)),
        completion_clock=lambda: captured,
    )
    assert result.available_expectations == 1
    candidate = _candidate()
    fact = pre_event_expectation_fact(
        conn, candidate, observed_at=DETECTED, code_version="ctx-code", run_id="run",
    )
    assert fact.fact_kind == "OPTIONS_EXPECTED_MOVE"
    assert fact.effective_at_utc == captured.isoformat()
    assert fact.payload["spot_reference_kind"] == "pre_close_latest_quote_mid"
    assert fact.payload["temporal_semantic"] == "pre_event_expected_move_baseline"
    assert fact.payload["spot_reference"] == 100


def test_pre_event_snapshot_rejects_post_close_capture():
    with pytest.raises(ValueError, match="before session close"):
        build_pre_event_expectation(
            session=SESSION, symbol="ABC", session_close=CLOSE,
            captured_at=CLOSE, spot=100, spot_ts=CLOSE - timedelta(seconds=1),
            chain=_chain(), code_version="x", run_id="run",
        )


def test_pre_close_fetch_that_finishes_after_close_is_not_backdated():
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    captured = CLOSE - timedelta(minutes=1)
    quote = Quote("ABC", captured, 99, 101, 100)
    result = run_pre_event_expectation_capture(
        conn, session=SESSION, session_close=CLOSE, now=captured, symbols=("ABC",),
        code_version="x", run_id="run",
        quote_fetch=lambda symbols: {"ABC": quote},
        option_fetch=lambda symbol, session, spot: _chain(captured),
        completion_clock=lambda: CLOSE + timedelta(seconds=1),
    )
    assert result.available_expectations == 0
    assert result.fetch_errors == 1
    status, failed_at = conn.execute(
        "SELECT status,captured_at_utc FROM postmarket_pre_event_option_expectations"
    ).fetchone()
    assert status == "FETCH_ERROR"
    assert datetime.fromisoformat(failed_at) > CLOSE


def test_backfill_records_available_and_explicit_unconfigured_facts_idempotently():
    conn = sqlite3.connect(":memory:")
    candidate_id = _seed(conn)
    item = NewsItem(
        "1", "ABC results", "wire", None, DETECTED, DETECTED, ("ABC",),
    )
    result = run_external_context_backfill(
        conn, now=DETECTED + timedelta(minutes=1), code_version="ctx-code", run_id="run",
        option_fetch=lambda symbol, session, spot: _chain(),
        news_fetch=lambda symbol, start, end: (item,),
    )
    assert result.candidates_planned == 1
    assert result.facts_written == 9
    assert result.available_facts == 2
    rows = dict(conn.execute(
        "SELECT fact_kind,status FROM postmarket_external_fact_events WHERE candidate_id=?",
        (candidate_id,),
    ).fetchall())
    assert rows["OPTIONS_EXPECTED_MOVE"] == "UNAVAILABLE_NO_DATA"
    assert rows["CURRENT_OPTION_MARKET_CONTEXT"] == "AVAILABLE_INDICATIVE"
    assert rows["NEWS"] == "AVAILABLE"
    assert rows["INDEPENDENT_PRICE_COMPARISON"] == "UNAVAILABLE_UNCONFIGURED"
    again = run_external_context_backfill(
        conn, now=DETECTED + timedelta(minutes=2), code_version="ctx-code", run_id="run",
        option_fetch=lambda symbol, session, spot: _chain(),
        news_fetch=lambda symbol, start, end: (item,),
    )
    assert again.candidates_planned == 0
    assert conn.execute("SELECT COUNT(*) FROM postmarket_external_fact_events").fetchone()[0] == 9
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_external_fact_events SET status='AVAILABLE'")


def test_backfill_wires_independent_reference_and_official_halt_sources():
    conn = sqlite3.connect(":memory:")
    candidate_id = _seed(conn)
    item = NewsItem("1", "ABC results", "wire", None, DETECTED, DETECTED, ("ABC",))
    reference = TickerReference(
        "ABC", SESSION, True, "stocks", "XNAS", "CS", "usd",
        1_000_000, 100_000, 90_000, "3571", "ELECTRONIC COMPUTERS",
        DETECTED - timedelta(minutes=1),
    )
    halt_calls = []

    def fetch_halts(session):
        halt_calls.append(session)
        return ()

    result = run_external_context_backfill(
        conn, now=DETECTED, code_version="ctx-code", run_id="run",
        option_fetch=lambda symbol, session, spot: _chain(),
        news_fetch=lambda symbol, start, end: (item,),
        independent_fetch=lambda symbol, start, end: _comparison_bars(),
        reference_fetch=lambda symbol, as_of: reference,
        filing_context_fetch=lambda symbol, cutoff: _filing_snapshot(),
        completion_clock=lambda: DETECTED + timedelta(seconds=3),
        halt_fetch=fetch_halts,
    )
    rows = dict(conn.execute(
        "SELECT fact_kind,status FROM postmarket_external_fact_events WHERE candidate_id=?",
        (candidate_id,),
    ).fetchall())
    assert result.facts_written == 9
    assert rows["INDEPENDENT_PRICE_COMPARISON"] == "AVAILABLE"
    assert rows["SECTOR_CLASSIFICATION"] == "AVAILABLE"
    assert rows["FUNDAMENTALS"] == "AVAILABLE"
    assert rows["FILING_INDUSTRY_CLASSIFICATION"] == "AVAILABLE"
    assert rows["FILING_FUNDAMENTALS"] == "AVAILABLE"
    assert conn.execute(
        """SELECT observed_at_utc FROM postmarket_external_fact_events
           WHERE candidate_id=? AND fact_kind='FILING_FUNDAMENTALS'""",
        (candidate_id,),
    ).fetchone()[0] == (DETECTED + timedelta(seconds=3)).isoformat()
    assert rows["HALT_STATE"] == "AVAILABLE_NO_MATCHES"
    assert halt_calls == [SESSION]


def test_fetch_errors_retry_three_times_then_stop_without_silent_success():
    conn = sqlite3.connect(":memory:")
    _seed(conn)

    def fail(*args):
        raise TimeoutError("provider timeout")

    for minute in range(3):
        result = run_external_context_backfill(
            conn, now=DETECTED + timedelta(minutes=minute + 1),
            code_version="ctx-code", run_id="run", option_fetch=fail, news_fetch=fail,
        )
        assert result.fetch_errors == 2
    final = run_external_context_backfill(
        conn, now=DETECTED + timedelta(minutes=5), code_version="ctx-code", run_id="run",
        option_fetch=fail, news_fetch=fail,
    )
    assert final.candidates_planned == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_external_fact_events WHERE status='FETCH_ERROR'"
    ).fetchone()[0] == 6


def test_external_worker_is_default_off_strict_and_delivery_incapable():
    assert external_context_enabled("") is False
    assert external_context_enabled("0") is False
    assert external_context_enabled("1") is True
    with pytest.raises(ValueError):
        external_context_enabled("maybe")
    tree = ast.parse(open(
        "tradebot/postmarket_external_context_shadow.py", encoding="utf-8"
    ).read())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        forbidden in name
        for name in imports
        for forbidden in ("alerts", "telegram", "broker", "outbox", "orders")
    )


def test_pre_close_capture_window_uses_actual_exchange_close_including_early_close():
    standard = pre_close_capture_window(datetime(2026, 8, 27, 19, 55, tzinfo=timezone.utc))
    assert standard is not None
    assert standard[1:] == (
        datetime(2026, 8, 27, 19, 50, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc),
    )
    early = pre_close_capture_window(datetime(2026, 11, 27, 17, 55, tzinfo=timezone.utc))
    assert early is not None
    assert early[1:] == (
        datetime(2026, 11, 27, 17, 50, tzinfo=timezone.utc),
        datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
    )
