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
    build_context_evidence,
    latest_context_summary,
    run_context_backfill,
)
from tradebot.postmarket_discovery import connect as connect_shadow
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


def test_temporally_mismatched_quote_is_not_treated_as_signal_time_spread():
    evidence = build_context_evidence(
        _candidate(),
        observed_at=DETECTED + timedelta(hours=2),
        code_version="abc1234",
        daily_bars=_daily(),
        intraday_bars=_intraday(),
        benchmark_bars=_intraday("SPY", post_close=101),
        quote=Quote(
            "ABC", DETECTED + timedelta(hours=2), bid=109.9, ask=110.1, last=110,
            bid_size=10, ask_size=10,
        ),
        asset=AssetFact("AVAILABLE", tradable=True),
        catalysts=(),
    )

    assert evidence.quote_status == "TEMPORALLY_MISMATCHED"
    assert evidence.spread_bps is not None
    assert "QUOTE_TEMPORALLY_MISMATCHED" in evidence.issues
    assert evidence.catalyst_category == "UNEXPLAINED"


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
    quote = Quote("ABC", DETECTED, 109.9, 110.1, 110, 500, 400)

    first = run_context_backfill(
        shadow,
        journal,
        universe,
        now=DETECTED + timedelta(seconds=1),
        code_version="context-code",
        intraday_fetch=lambda symbols, session: {
            "ABC": _intraday(), "SPY": _intraday("SPY", 101)
        },
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
               quote_data_feed,asset_observed_at_utc
        FROM postmarket_candidate_context
        """
    ).fetchone()
    assert row == (
        "complete", "AVAILABLE", "AVAILABLE", "AVAILABLE",
        "SCHEDULED_EARNINGS", "sip", "alpaca", "sip", CLOSE.isoformat(),
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
        "quote_available": 1,
        "liquidity_available": 1,
        "verified_catalysts": 1,
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
