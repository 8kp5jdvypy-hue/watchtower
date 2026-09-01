"""Postmarket candidate context is attributable, append-only, and fail-visible."""
from __future__ import annotations

import ast
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Bar
from tradebot.journal import connect as connect_journal
from tradebot.marketdata import Quote
from tradebot.postmarket_context import (
    AssetFact,
    CandidateFact,
    CatalystFact,
    CONTEXT_SCHEMA,
    build_context_evidence,
    ensure_context_schema,
    latest_context_summary,
    run_context_backfill,
)
from tradebot.postmarket_discovery import connect as connect_shadow
from tradebot.postmarket_lifecycle import ensure_lifecycle_schema
from tradebot.postmarket_reference_manifest import (
    CandidateReference,
    ensure_reference_schema,
)
from tradebot.universe import connect as connect_universe


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
DETECTED = CLOSE + timedelta(minutes=10)


def _bar(symbol, ts, open_, high, low, close, volume=10_000):
    return Bar(symbol, ts, open_, high, low, close, volume)


def _daily(symbol="ABC"):
    return [
        _bar(
            symbol,
            datetime(2026, 8, day, tzinfo=timezone.utc),
            100,
            102,
            99,
            101,
            1_000_000,
        )
        for day in range(1, 17)
    ]


def _intraday(symbol="ABC", post_close=110):
    return [
        _bar(symbol, CLOSE - timedelta(minutes=5), 100, 101, 99, 100, 1_000_000),
        _bar(symbol, CLOSE, post_close, post_close, post_close, post_close, 50_000),
        _bar(
            symbol,
            CLOSE + timedelta(minutes=5),
            post_close,
            post_close,
            post_close,
            post_close,
            50_000,
        ),
    ]


def _candidate(candidate_id=1):
    return CandidateFact(
        candidate_id=candidate_id,
        session=SESSION,
        symbol="ABC",
        direction="up",
        detected_at=DETECTED,
        bar_open_ts=CLOSE + timedelta(minutes=5),
        rth_close=100,
        close=110,
        move_pct=10,
        postmarket_notional=1_000_000,
        bar_data_feed="sip",
        bar_data_provider="alpaca",
        bar_timeframe="5Min",
    )


def _reference(
    *,
    published_at=CLOSE - timedelta(hours=2),
    observed_at=CLOSE - timedelta(minutes=30),
):
    return CandidateReference(
        1,
        "licensed-vendor",
        "daily-sector-and-float-v1",
        "contract-2026-001",
        SESSION.isoformat(),
        published_at.isoformat(),
        observed_at.isoformat(),
        "GICS",
        "a" * 64,
        "ABC",
        "45",
        "Information Technology",
        "XLK",
        1_000_000,
        (SESSION - timedelta(days=1)).isoformat(),
    )


def test_context_computes_volatility_relative_strength_spread_depth_and_catalyst():
    evidence = build_context_evidence(
        _candidate(),
        observed_at=DETECTED + timedelta(seconds=1),
        code_version="abc1234",
        daily_bars=_daily(),
        intraday_bars=_intraday(),
        benchmark_bars=_intraday("SPY", post_close=101),
        quote=Quote(
            "ABC", DETECTED, bid=109.90, ask=110.10, last=110,
            bid_size=500, ask_size=400,
        ),
        asset=AssetFact(
            "AVAILABLE", DETECTED.isoformat(), "NASDAQ", True, True, True,
        ),
        catalysts=(
            CatalystFact(
                "SCHEDULED_EARNINGS", "nasdaq_earnings", "after-hours earnings",
                (CLOSE - timedelta(hours=8)).isoformat(),
            ),
        ),
    )

    assert evidence.status == "complete"
    assert evidence.volatility_status == "AVAILABLE"
    assert evidence.atr14 == pytest.approx(3)
    assert evidence.move_atr_units == pytest.approx(10 / 3)
    assert evidence.market_relative_status == "AVAILABLE"
    assert evidence.benchmark_move_pct == pytest.approx(1)
    assert evidence.directional_market_excess_pct == pytest.approx(9)
    assert evidence.quote_status == "AVAILABLE"
    assert evidence.spread_bps == pytest.approx(18.1818, rel=1e-4)
    assert evidence.quoted_depth_notional == pytest.approx(98_990)
    assert evidence.liquidity_status == "AVAILABLE"
    assert evidence.catalyst_category == "SCHEDULED_EARNINGS"
    assert evidence.catalyst_sources == ("nasdaq_earnings",)
    assert evidence.sector_relative_status.startswith("UNAVAILABLE")
    assert "FLOAT_UNAVAILABLE" in evidence.issues
    assert evidence.bar_data_feed == "sip"
    assert evidence.quote_data_feed == "sip"
    assert evidence.asset_observed_at_utc == DETECTED.isoformat()
    assert evidence.data_confidence_status == "HIGH"
    assert evidence.data_confidence_coverage_pct == 100
    assert all(evidence.data_confidence_components.values())


def test_temporally_mismatched_quote_is_not_treated_as_snapshot_time_spread():
    evidence = build_context_evidence(
        _candidate(),
        observed_at=DETECTED + timedelta(hours=2),
        code_version="abc1234",
        daily_bars=_daily(),
        intraday_bars=_intraday(),
        benchmark_bars=_intraday("SPY", post_close=101),
        quote=Quote(
            "ABC", DETECTED, bid=109.9, ask=110.1, last=110,
            bid_size=10, ask_size=10,
        ),
        asset=AssetFact("AVAILABLE", tradable=True),
        catalysts=(),
    )

    assert evidence.quote_status == "TEMPORALLY_MISMATCHED"
    assert evidence.spread_bps is not None
    assert "QUOTE_TEMPORALLY_MISMATCHED" in evidence.issues
    assert evidence.catalyst_category == "UNEXPLAINED"


def test_quote_after_snapshot_boundary_is_explicitly_future():
    evidence = build_context_evidence(
        _candidate(),
        observed_at=DETECTED + timedelta(seconds=1),
        code_version="abc1234",
        daily_bars=_daily(),
        intraday_bars=_intraday(),
        benchmark_bars=_intraday("SPY", post_close=101),
        quote=Quote(
            "ABC", DETECTED + timedelta(seconds=3), bid=109.9, ask=110.1,
            last=110, bid_size=10, ask_size=10,
        ),
        asset=AssetFact("AVAILABLE", DETECTED.isoformat(), tradable=True),
        catalysts=(),
    )

    assert evidence.quote_status == "FUTURE"
    assert "QUOTE_FROM_FUTURE" in evidence.issues
    assert evidence.data_confidence_components["quote_temporal_integrity"] is False


def test_context_computes_causal_licensed_sector_relative_feature():
    evidence = build_context_evidence(
        _candidate(),
        observed_at=DETECTED + timedelta(seconds=1),
        code_version="abc1234",
        daily_bars=_daily(),
        intraday_bars=_intraday(),
        benchmark_bars=_intraday("SPY", post_close=101),
        quote=None,
        asset=AssetFact("AVAILABLE", tradable=True),
        catalysts=(),
        sector_reference=_reference(),
        sector_benchmark_bars=_intraday("XLK", post_close=102),
    )

    assert evidence.sector_relative_status == "AVAILABLE"
    assert evidence.sector_symbol == "XLK"
    assert evidence.sector_move_pct == pytest.approx(2)
    assert evidence.sector_relative_move_pct == pytest.approx(8)
    assert evidence.sector_reference_manifest_id == 1
    assert evidence.sector_reference_sha256 == "a" * 64
    assert evidence.sector_reference_observed_at_utc == (
        CLOSE - timedelta(minutes=30)
    ).isoformat()
    assert evidence.float_status == "AVAILABLE_LICENSED_REFERENCE"
    assert not any(issue.startswith("SECTOR_RELATIVE_") for issue in evidence.issues)
    assert "FLOAT_UNAVAILABLE" not in evidence.issues


def test_sector_reference_must_have_been_knowable_at_candidate_detection():
    with pytest.raises(ValueError, match="not knowable"):
        build_context_evidence(
            _candidate(),
            observed_at=DETECTED + timedelta(seconds=1),
            code_version="abc1234",
            daily_bars=_daily(),
            intraday_bars=_intraday(),
            benchmark_bars=_intraday("SPY", post_close=101),
            quote=None,
            asset=AssetFact("AVAILABLE", tradable=True),
            catalysts=(),
            sector_reference=_reference(published_at=DETECTED + timedelta(seconds=1)),
            sector_benchmark_bars=_intraday("XLK", post_close=102),
        )


def _seed_candidate(shadow):
    shadow.execute(
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
            100, 110, 10, 100_000, 1_000_000, '["market_gainer"]', "sip",
            "alpaca", "5Min", "candidate-code", "candidate-run",
        ),
    )
    shadow.commit()


def _seed_reference(shadow):
    ensure_reference_schema(shadow)
    reference = _reference()
    cursor = shadow.execute(
        """
        INSERT INTO postmarket_reference_manifests
            (manifest_version,provider,dataset,license_reference,effective_date,
             published_at_utc,created_at_utc,observed_at_utc,
             classification_system,manifest_sha256,row_count,code_version,
             run_id,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'locked')
        """,
        (
            1, reference.provider, reference.dataset, reference.license_reference,
            reference.effective_date, reference.published_at_utc,
            reference.published_at_utc, reference.source_observed_at_utc,
            reference.classification_system, reference.manifest_sha256, 1,
            "reference-code", "reference-run",
        ),
    )
    shadow.execute(
        """
        INSERT INTO postmarket_reference_rows
            (reference_manifest_id,symbol,sector_code,sector_name,
             benchmark_symbol,float_shares,float_as_of_date)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            cursor.lastrowid, reference.symbol, reference.sector_code,
            reference.sector_name, reference.benchmark_symbol,
            reference.float_shares, reference.float_as_of_date,
        ),
    )
    shadow.commit()


def _seed_lifecycle_observation(shadow, bar_open):
    ensure_lifecycle_schema(shadow)
    shadow.execute(
        """
        INSERT INTO postmarket_candidate_lifecycle_observations
            (candidate_id,lifecycle_version,session,symbol,observed_at_utc,
             evidence_bar_open_ts_utc,evaluation_outcome,reason,move_pct,
             observed_direction,cumulative_notional,data_age_seconds,data_feed,
             market_data_provider,bar_timeframe,code_version,run_id)
        VALUES (1,1,?,'ABC',?,?,'CANDIDATE','qualified',10,'up',1000000,0,
                'sip','alpaca','5Min','lifecycle-code','lifecycle-run')
        """,
        (
            SESSION.isoformat(),
            (bar_open + timedelta(minutes=5)).isoformat(),
            bar_open.isoformat(),
        ),
    )
    shadow.commit()


def _databases(tmp_path):
    shadow = connect_shadow(tmp_path / "shadow.db")
    journal = connect_journal(tmp_path / "journal.db")
    universe = connect_universe(tmp_path / "universe.db")
    _seed_candidate(shadow)
    universe.execute(
        """
        INSERT INTO assets
            (symbol,exchange,name,tradable,options_enabled,overnight_eligible,
             attributes_json,is_active,first_seen_at,last_seen_at,delisted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "ABC", "NASDAQ", "ABC Inc", 1, 1, 1, "[]", 1,
            CLOSE.isoformat(), CLOSE.isoformat(), None,
        ),
    )
    universe.commit()
    journal.execute(
        """
        INSERT INTO event_windows
            (symbol,kind,start_utc,end_utc,severity,source,detail,event_date,
             event_timing,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "ABC", "earnings", CLOSE.isoformat(), CLOSE.isoformat(), "context",
            "nasdaq_earnings", "ABC earnings", SESSION.isoformat(),
            "after-hours", (CLOSE - timedelta(hours=8)).isoformat(),
        ),
    )
    journal.commit()
    return shadow, journal, universe


def test_backfill_is_bounded_append_only_and_idempotent_after_complete(tmp_path):
    shadow, journal, universe = _databases(tmp_path)
    _seed_reference(shadow)
    quote = Quote("ABC", DETECTED, 109.9, 110.1, 110, 500, 400)

    def intraday_fetch(symbols, session):
        assert symbols == ["ABC", "SPY", "XLK"]
        return {
            "ABC": _intraday(),
            "SPY": _intraday("SPY", 101),
            "XLK": _intraday("XLK", 102),
        }

    first = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED + timedelta(seconds=1),
        code_version="context-code",
        intraday_fetch=intraday_fetch,
        daily_fetch=lambda symbols: {"ABC": _daily()},
        quote_fetch=lambda symbols: {"ABC": quote},
    )
    second = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED + timedelta(minutes=1),
        code_version="context-code",
        intraday_fetch=lambda symbols, session: pytest.fail("must not refetch"),
        daily_fetch=lambda symbols: pytest.fail("must not refetch"),
        quote_fetch=lambda symbols: pytest.fail("must not refetch"),
    )

    assert (first.candidates_planned, first.contexts_written) == (1, 1)
    assert first.degraded_contexts == 0
    assert second.candidates_planned == 0
    row = shadow.execute(
        """
        SELECT status,volatility_status,market_relative_status,quote_status,
               catalyst_category,bar_data_feed,bar_data_provider,
               quote_data_feed,asset_observed_at_utc,sector_relative_status,
               sector_symbol,sector_relative_move_pct,
               sector_reference_manifest_id,sector_reference_sha256,
               sector_reference_observed_at_utc,float_status
        FROM postmarket_candidate_context
        """
    ).fetchone()
    assert row[:11] == (
        "complete", "AVAILABLE", "AVAILABLE", "AVAILABLE",
        "SCHEDULED_EARNINGS", "sip", "alpaca", "sip", CLOSE.isoformat(),
        "AVAILABLE", "XLK",
    )
    assert row[11] == pytest.approx(8)
    assert row[12:] == (
        1, "a" * 64,
        (CLOSE - timedelta(minutes=30)).isoformat(),
        "AVAILABLE_LICENSED_REFERENCE",
    )
    assert latest_context_summary(shadow) == {
        "session": SESSION.isoformat(),
        "candidates": 1,
        "contexts": 1,
        "missing_contexts": 0,
        "complete": 1,
        "degraded": 0,
        "volatility_available": 1,
        "market_relative_available": 1,
        "sector_relative_available": 1,
        "quote_available": 1,
        "liquidity_available": 1,
        "verified_catalysts": 1,
        "usable_data_confidence": 1,
    }
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        shadow.execute("UPDATE postmarket_candidate_context SET status='degraded'")


def test_provider_failure_writes_degraded_attempt_and_successful_retry(tmp_path):
    shadow, journal, universe = _databases(tmp_path)

    first = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED + timedelta(seconds=1),
        code_version="context-code",
        intraday_fetch=lambda symbols, session: (_ for _ in ()).throw(
            RuntimeError("bars down")
        ),
        daily_fetch=lambda symbols: (_ for _ in ()).throw(RuntimeError("daily down")),
        quote_fetch=lambda symbols: (_ for _ in ()).throw(RuntimeError("quotes down")),
    )
    second = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED + timedelta(minutes=1),
        code_version="context-code",
        intraday_fetch=lambda symbols, session: {
            "ABC": _intraday(), "SPY": _intraday("SPY", 101)
        },
        daily_fetch=lambda symbols: {"ABC": _daily()},
        quote_fetch=lambda symbols: {
            "ABC": Quote("ABC", DETECTED, 109.9, 110.1, 110, 10, 10)
        },
    )

    assert first.degraded_contexts == 1
    assert first.fetch_errors == 3
    assert second.degraded_contexts == 0
    assert shadow.execute(
        "SELECT GROUP_CONCAT(status, ',') FROM postmarket_candidate_context"
    ).fetchone()[0] == "degraded,complete"


def test_missing_mapped_sector_bars_are_fail_visible_and_retryable(tmp_path):
    shadow, journal, universe = _databases(tmp_path)
    _seed_reference(shadow)

    result = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED + timedelta(seconds=1),
        code_version="context-code",
        intraday_fetch=lambda symbols, session: {
            "ABC": _intraday(), "SPY": _intraday("SPY", 101)
        },
        daily_fetch=lambda symbols: {"ABC": _daily()},
        quote_fetch=lambda symbols: {
            "ABC": Quote("ABC", DETECTED, 109.9, 110.1, 110, 10, 10)
        },
    )

    status, sector_status, issues = shadow.execute(
        """
        SELECT status,sector_relative_status,issues_json
        FROM postmarket_candidate_context
        """
    ).fetchone()
    assert result.degraded_contexts == 1
    assert status == "degraded"
    assert sector_status == "NO_RTH_CLOSE"
    assert "SECTOR_BENCHMARK_BARS_UNAVAILABLE" in issues


def test_complete_context_refreshes_for_each_new_lifecycle_bar(tmp_path):
    shadow, journal, universe = _databases(tmp_path)
    first_bar = CLOSE + timedelta(minutes=5)
    second_bar = CLOSE + timedelta(minutes=10)
    _seed_lifecycle_observation(shadow, first_bar)
    quote_times = iter((DETECTED, CLOSE + timedelta(minutes=15)))

    def run(now):
        quote_time = next(quote_times)
        return run_context_backfill(
            shadow,
            journal,
            universe,
            now=now,
            code_version="context-code",
            intraday_fetch=lambda symbols, session: {
                "ABC": _intraday(), "SPY": _intraday("SPY", 101)
            },
            daily_fetch=lambda symbols: {"ABC": _daily()},
            quote_fetch=lambda symbols: {
                "ABC": Quote("ABC", quote_time, 109.9, 110.1, 110, 10, 10)
            },
        )

    first = run(DETECTED + timedelta(seconds=1))
    _seed_lifecycle_observation(shadow, second_bar)
    second = run(CLOSE + timedelta(minutes=15, seconds=1))

    assert first.contexts_written == second.contexts_written == 1
    rows = shadow.execute(
        """
        SELECT attempt,lifecycle_observation_seq,
               lifecycle_evidence_bar_open_ts_utc,quote_ts_utc
        FROM postmarket_candidate_context ORDER BY attempt
        """
    ).fetchall()
    assert rows == [
        (1, 1, first_bar.isoformat(), DETECTED.isoformat()),
        (
            2,
            2,
            second_bar.isoformat(),
            (CLOSE + timedelta(minutes=15)).isoformat(),
        ),
    ]


def test_degraded_context_retries_are_bounded_per_lifecycle_bar(tmp_path):
    shadow, journal, universe = _databases(tmp_path)
    _seed_lifecycle_observation(shadow, CLOSE + timedelta(minutes=5))

    def failed_run(minute):
        return run_context_backfill(
            shadow,
            journal,
            universe,
            now=DETECTED + timedelta(minutes=minute),
            code_version="context-code",
            intraday_fetch=lambda symbols, session: (_ for _ in ()).throw(
                RuntimeError("bars down")
            ),
            daily_fetch=lambda symbols: (_ for _ in ()).throw(
                RuntimeError("daily down")
            ),
            quote_fetch=lambda symbols: (_ for _ in ()).throw(
                RuntimeError("quotes down")
            ),
        )

    results = [failed_run(minute) for minute in range(4)]

    assert [result.contexts_written for result in results] == [1, 1, 1, 0]
    assert shadow.execute(
        "SELECT COUNT(*) FROM postmarket_candidate_context"
    ).fetchone()[0] == 3


def test_backfill_snapshot_boundary_is_recorded_after_provider_fetches(tmp_path):
    shadow, journal, universe = _databases(tmp_path)
    completed_at = DETECTED + timedelta(seconds=3)

    result = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED,
        code_version="context-code",
        intraday_fetch=lambda symbols, session: {
            "ABC": _intraday(), "SPY": _intraday("SPY", 101)
        },
        daily_fetch=lambda symbols: {"ABC": _daily()},
        quote_fetch=lambda symbols: {
            "ABC": Quote(
                "ABC", DETECTED + timedelta(seconds=2),
                109.9, 110.1, 110, 10, 10,
            )
        },
        observation_clock=lambda: completed_at,
    )

    assert result.contexts_written == 1
    assert shadow.execute(
        """
        SELECT observed_at_utc,quote_status,quote_distance_seconds
        FROM postmarket_candidate_context
        """
    ).fetchone() == (completed_at.isoformat(), "AVAILABLE", 1.0)


def test_context_v1_schema_migrates_append_only_without_rewriting_rows():
    legacy_schema = CONTEXT_SCHEMA
    for column in (
        "    lifecycle_observation_seq INTEGER,\n",
        "    lifecycle_evidence_bar_open_ts_utc TEXT,\n",
        "    sector_reference_manifest_id INTEGER,\n",
        "    sector_reference_sha256 TEXT,\n",
        "    sector_reference_observed_at_utc TEXT,\n",
        "    data_confidence_status TEXT NOT NULL,\n",
        "    data_confidence_coverage_pct REAL NOT NULL,\n",
        "    data_confidence_components_json TEXT NOT NULL,\n",
    ):
        legacy_schema = legacy_schema.replace(column, "")
    conn = sqlite3.connect(":memory:")
    conn.executescript(legacy_schema)
    conn.execute(
        """
        INSERT INTO postmarket_candidate_context
            (candidate_id,context_version,attempt,session,symbol,direction,
             candidate_detected_at,observed_at_utc,bar_data_feed,
             bar_data_provider,bar_timeframe,quote_data_provider,
             quote_data_feed,status,volatility_status,prior_daily_bars,
             implied_expected_move_status,market_relative_status,
             benchmark_symbol,sector_relative_status,quote_status,
             liquidity_status,postmarket_notional,asset_status,float_status,
             market_cap_status,halt_status,catalyst_status,catalyst_category,
             catalyst_sources_json,catalyst_details_json,
             catalyst_coverage_json,bar_quality_status,issues_json)
        VALUES (1,1,1,?,'ABC','up',?,?,'sip','alpaca','5Min','alpaca','sip',
                'complete','AVAILABLE',20,'UNAVAILABLE','AVAILABLE','SPY',
                'UNAVAILABLE','AVAILABLE','AVAILABLE',1000000,'AVAILABLE',
                'UNAVAILABLE','UNAVAILABLE','UNAVAILABLE','VERIFIED',
                'SCHEDULED_EARNINGS','[]','[]','{}','PASSED','[]')
        """,
        (SESSION.isoformat(), DETECTED.isoformat(), DETECTED.isoformat()),
    )
    conn.commit()
    before = conn.total_changes

    ensure_context_schema(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(postmarket_candidate_context)")
    }
    assert {
        "sector_reference_manifest_id",
        "sector_reference_sha256",
        "sector_reference_observed_at_utc",
        "lifecycle_observation_seq",
        "lifecycle_evidence_bar_open_ts_utc",
        "data_confidence_status",
        "data_confidence_coverage_pct",
        "data_confidence_components_json",
    } <= columns
    assert conn.total_changes == before
    assert conn.execute(
        """
        SELECT context_version,sector_reference_manifest_id,
               sector_reference_sha256,sector_reference_observed_at_utc,
               lifecycle_observation_seq,lifecycle_evidence_bar_open_ts_utc,
               data_confidence_status,data_confidence_coverage_pct,
               data_confidence_components_json
        FROM postmarket_candidate_context WHERE candidate_id=1
        """
    ).fetchone() == (1, None, None, None, None, None, None, None, None)


def test_context_module_has_no_alert_delivery_or_trading_dependency():
    path = Path(__file__).parents[1] / "tradebot" / "postmarket_context.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = ("tradebot.alerts", "tradebot.telegram_bot", "tradebot.order", "tradebot.broker")
    assert not any(module.startswith(forbidden) for module in imports)
