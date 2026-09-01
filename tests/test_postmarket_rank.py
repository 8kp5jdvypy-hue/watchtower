"""Versioned postmarket rank is deterministic, decomposed, and append-only."""
from __future__ import annotations

import ast
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_discovery_shadow as discovery_shadow
from tradebot.detectors import Bar
from tradebot.postmarket import OUTCOME_CANDIDATE, ReactionEvaluation
from tradebot.postmarket_context import ContextEvidence, record_context
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_lifecycle import (
    LifecycleCandidate,
    LifecycleTransition,
    STATE_CONFIRMED,
    STATE_FADING,
    record_observation,
    record_transition,
)
from tradebot.postmarket_rank import (
    RankEvidence,
    ensure_rank_schema,
    latest_rank_summary,
    rank_contract_payload,
    rank_contract_sha256,
    run_rank_snapshot,
    score_candidate,
)


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
AS_OF = CLOSE + timedelta(minutes=20)


def _rank_evidence(**changes):
    values = dict(
        candidate_id=1,
        session=SESSION.isoformat(),
        symbol="AAA",
        direction="up",
        context_id=1,
        context_status="complete",
        context_observed_at=AS_OF,
        context_lifecycle_observation_seq=1,
        data_confidence_status="HIGH",
        volatility_status="AVAILABLE",
        move_atr_units=3.0,
        market_relative_status="AVAILABLE",
        directional_market_excess_pct=9.0,
        quote_status="AVAILABLE",
        quote_ts=AS_OF,
        spread_bps=20.0,
        quoted_depth_notional=500_000.0,
        liquidity_status="AVAILABLE",
        rth_dollar_volume=100_000_000.0,
        postmarket_notional=10_000_000.0,
        asset_status="AVAILABLE",
        tradable=True,
        catalyst_status="VERIFIED",
        transition_id=1,
        lifecycle_state=STATE_CONFIRMED,
        actionability="QUALIFIED",
        observation_seq=1,
        observation_recorded_at=AS_OF,
        evidence_bar_open_ts=AS_OF - timedelta(minutes=5),
        observation_outcome=OUTCOME_CANDIDATE,
    )
    values.update(changes)
    return RankEvidence(**values)


def test_score_is_fully_decomposed_and_not_a_probability():
    score = score_candidate(_rank_evidence(), as_of=AS_OF)

    assert score.rankable is True
    assert set(score.components) == {
        "volatility_normalized_move",
        "market_relative_excess",
        "rth_dollar_liquidity",
        "postmarket_notional",
        "quote_spread",
        "quoted_depth",
        "verified_catalyst",
        "lifecycle",
    }
    assert score.raw_component_score == pytest.approx(sum(score.components.values()))
    assert score.penalty_total == pytest.approx(sum(score.penalties.values()))
    assert score.evidence_score == pytest.approx(
        max(0, min(100, score.raw_component_score + score.penalty_total))
    )
    assert score.evidence_coverage_pct == 100
    assert score.exclusion_reasons == ()


def test_rank_schema_migrates_legacy_runs_without_rewriting_attribution():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE postmarket_rank_runs (
          rank_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          session TEXT NOT NULL, rank_version INTEGER NOT NULL,
          as_of_utc TEXT NOT NULL, recorded_at_utc TEXT NOT NULL,
          code_version TEXT, run_id TEXT NOT NULL,
          input_digest_sha256 TEXT NOT NULL, input_candidates INTEGER NOT NULL,
          rankable_candidates INTEGER NOT NULL, status TEXT NOT NULL,
          weights_json TEXT NOT NULL, thresholds_json TEXT NOT NULL
        );
        INSERT INTO postmarket_rank_runs
          (session,rank_version,as_of_utc,recorded_at_utc,code_version,run_id,
           input_digest_sha256,input_candidates,rankable_candidates,status,
           weights_json,thresholds_json)
        VALUES
          ('2026-08-27',1,'2026-08-27T20:10:00+00:00',
           '2026-08-27T20:10:01+00:00','abc1234','legacy','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
           1,1,'complete','{}','{}');
        """
    )

    ensure_rank_schema(conn)

    assert "rank_contract_sha256" in {
        row[1] for row in conn.execute("PRAGMA table_info(postmarket_rank_runs)")
    }
    assert conn.execute(
        "SELECT rank_contract_sha256 FROM postmarket_rank_runs"
    ).fetchone()[0] is None


def test_stale_fading_temporally_bad_quote_is_unrankable_with_named_penalties():
    score = score_candidate(
        _rank_evidence(
            lifecycle_state=STATE_FADING,
            actionability="WATCH",
            quote_status="TEMPORALLY_MISMATCHED",
            evidence_bar_open_ts=AS_OF - timedelta(minutes=20),
            catalyst_status="NO_VERIFIED_CATALYST",
        ),
        as_of=AS_OF,
    )

    assert score.rankable is False
    assert "STATE_FADING_NOT_RANKABLE" in score.exclusion_reasons
    assert "QUOTE_TEMPORALLY_MISMATCHED" in score.exclusion_reasons
    assert "OBSERVATION_STALE" in score.exclusion_reasons
    assert score.penalties["fading"] == -15
    assert score.penalties["unexplained_catalyst"] == -5
    assert score.penalties["stale_observation"] == -25


def test_rank_rejects_mismatched_context_future_quote_and_unusable_data():
    score = score_candidate(
        _rank_evidence(
            context_lifecycle_observation_seq=2,
            data_confidence_status="UNUSABLE",
            quote_ts=AS_OF + timedelta(seconds=2),
        ),
        as_of=AS_OF,
    )

    assert score.rankable is False
    assert "CONTEXT_LIFECYCLE_MISMATCH" in score.exclusion_reasons
    assert "DATA_CONFIDENCE_UNUSABLE" in score.exclusion_reasons
    assert "QUOTE_FROM_FUTURE" in score.exclusion_reasons


def _seed_candidate(conn, candidate_id, symbol):
    conn.execute(
        """
        INSERT INTO postmarket_discovery_candidates
            (candidate_id,session,symbol,event_date,direction,discovery_version,
             first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
             cumulative_volume,cumulative_notional,sources_json,data_feed,
             market_data_provider,bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            candidate_id, SESSION.isoformat(), symbol, SESSION.isoformat(), "up", 1,
            (CLOSE + timedelta(minutes=10)).isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(), 100, 110, 10,
            100_000, 10_000_000, '["market_gainer"]', "sip", "alpaca",
            "5Min", "candidate-code", "candidate-run",
        ),
    )
    conn.commit()


def _context(
    candidate_id, symbol, *, spread, excess, atr_units,
    observation_seq=None, observed_at=AS_OF,
):
    observation_seq = candidate_id if observation_seq is None else observation_seq
    return ContextEvidence(
        candidate_id=candidate_id,
        session=SESSION.isoformat(),
        symbol=symbol,
        direction="up",
        candidate_detected_at=(CLOSE + timedelta(minutes=10)).isoformat(),
        observed_at_utc=observed_at.isoformat(),
        lifecycle_observation_seq=observation_seq,
        lifecycle_evidence_bar_open_ts_utc=(
            observed_at - timedelta(minutes=5)
        ).isoformat(),
        code_version="context-code",
        bar_data_feed="sip",
        bar_data_provider="alpaca",
        bar_timeframe="5Min",
        quote_data_provider="alpaca",
        quote_data_feed="sip",
        status="complete",
        volatility_status="AVAILABLE",
        prior_daily_bars=20,
        atr14=3,
        atr_pct_of_rth_close=3,
        move_atr_units=atr_units,
        implied_expected_move_status="UNAVAILABLE_NO_OPTIONS_EXPECTED_MOVE_SOURCE",
        market_relative_status="AVAILABLE",
        benchmark_symbol="SPY",
        benchmark_move_pct=1,
        market_relative_move_pct=excess,
        directional_market_excess_pct=excess,
        sector_relative_status="UNAVAILABLE_NO_SECTOR_CLASSIFICATION_SOURCE",
        sector_symbol=None,
        sector_move_pct=None,
        sector_relative_move_pct=None,
        sector_reference_manifest_id=None,
        sector_reference_sha256=None,
        sector_reference_observed_at_utc=None,
        quote_status="AVAILABLE",
        quote_ts_utc=observed_at.isoformat(),
        quote_distance_seconds=0,
        bid=109.9,
        ask=110.1,
        bid_size=500,
        ask_size=400,
        spread_bps=spread,
        quoted_depth_notional=100_000,
        liquidity_status="AVAILABLE",
        rth_volume=1_000_000,
        rth_dollar_volume=100_000_000,
        postmarket_notional=10_000_000,
        asset_status="AVAILABLE",
        asset_observed_at_utc=CLOSE.isoformat(),
        exchange="NASDAQ",
        tradable=True,
        options_enabled=True,
        overnight_eligible=True,
        float_status="UNAVAILABLE_NO_SOURCE",
        market_cap_status="UNAVAILABLE_NO_SOURCE",
        halt_status="UNAVAILABLE_NO_POINT_IN_TIME_SOURCE",
        catalyst_status="VERIFIED",
        catalyst_category="SCHEDULED_EARNINGS",
        catalyst_sources=("nasdaq_earnings",),
        catalyst_details=(),
        catalyst_coverage={"earnings": "CONFIGURED"},
        bar_quality_status="PASSED_CANDIDATE_COMPLETED_BAR_GATES",
        data_confidence_status="HIGH",
        data_confidence_coverage_pct=100,
        data_confidence_components={"technical_evidence": True},
        issues=(),
    )


def _lifecycle(conn, candidate_id, symbol, *, observation_bar=None):
    bar_open = observation_bar or CLOSE + timedelta(minutes=15)
    candidate = LifecycleCandidate(
        candidate_id, SESSION, symbol, "up",
        CLOSE + timedelta(minutes=10), CLOSE + timedelta(minutes=5),
        10, 10_000_000, "sip", "alpaca", "5Min",
    )
    record_transition(
        conn,
        LifecycleTransition(
            candidate_id, SESSION.isoformat(), symbol, "up", "NEWLY_QUALIFYING",
            STATE_CONFIRMED, "QUALIFIED", (bar_open + timedelta(minutes=5)).isoformat(),
            (bar_open + timedelta(minutes=5)).isoformat(), bar_open.isoformat(),
            OUTCOME_CANDIDATE, "confirmed", 11, 11, 12_000_000, "sip", "alpaca",
            "5Min", "lifecycle-code", "lifecycle-run",
        ),
    )
    record_observation(
        conn,
        candidate,
        ReactionEvaluation(
            symbol, OUTCOME_CANDIDATE, "confirmed", SESSION,
            bar=Bar(symbol, bar_open, 111, 111, 111, 111, 100_000),
            rth_close=100, cumulative_volume=200_000,
            cumulative_notional=12_000_000, move_pct=11, direction="up",
            persistence_bars=2, persistence_span_seconds=300, data_age_seconds=0,
        ),
        observed_at=bar_open + timedelta(minutes=5),
        code_version="lifecycle-code",
        run_id="lifecycle-run",
    )


def test_rank_snapshot_orders_deterministically_and_is_digest_idempotent(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_candidate(conn, 1, "AAA")
    _seed_candidate(conn, 2, "BBB")
    record_context(conn, _context(1, "AAA", spread=20, excess=9, atr_units=4))
    record_context(conn, _context(2, "BBB", spread=80, excess=4, atr_units=2))
    _lifecycle(conn, 1, "AAA")
    _lifecycle(conn, 2, "BBB")

    first = run_rank_snapshot(
        conn,
        session=SESSION.isoformat(),
        as_of=CLOSE + timedelta(minutes=20),
        code_version="rank-code",
        run_id="rank-run",
    )
    second = run_rank_snapshot(
        conn,
        session=SESSION.isoformat(),
        as_of=CLOSE + timedelta(minutes=21),
        code_version="rank-code",
        run_id="rank-run",
    )

    assert first.created is True
    assert first.rankable_candidates == 2
    assert [row[1] for row in first.top_candidates] == ["AAA", "BBB"]
    assert second.created is False
    assert second.rank_run_id == first.rank_run_id
    assert conn.execute("SELECT COUNT(*) FROM postmarket_rank_runs").fetchone()[0] == 1
    contract, weights, thresholds = conn.execute(
        "SELECT rank_contract_sha256,weights_json,thresholds_json "
        "FROM postmarket_rank_runs WHERE rank_run_id=?",
        (first.rank_run_id,),
    ).fetchone()
    assert contract == rank_contract_sha256()
    assert ast.literal_eval(weights) == rank_contract_payload()["weights"]
    assert ast.literal_eval(thresholds) == rank_contract_payload()["thresholds"]
    summary = latest_rank_summary(conn)
    assert summary["semantics"] == (
        "heuristic_evidence_ordering_not_probability"
    )
    assert summary["session_runs"] == summary["session_rankable_runs"] == 1
    assert summary["session_peak_rankable_candidates"] == 2
    assert summary["latest_exclusion_counts"] == {}
    assert summary["latest_rankable_snapshot"]["top"][0]["symbol"] == "AAA"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_candidate_ranks SET evidence_score=100")


def test_new_completed_bar_changes_digest_and_creates_new_snapshot(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_candidate(conn, 1, "AAA")
    record_context(conn, _context(1, "AAA", spread=20, excess=9, atr_units=4))
    _lifecycle(conn, 1, "AAA")
    first = run_rank_snapshot(
        conn, session=SESSION.isoformat(), as_of=AS_OF,
        code_version="rank-code", run_id="rank-run",
    )
    _lifecycle(
        conn, 1, "AAA", observation_bar=CLOSE + timedelta(minutes=20)
    )
    record_context(
        conn,
        _context(
            1,
            "AAA",
            spread=20,
            excess=9,
            atr_units=4,
            observation_seq=2,
            observed_at=CLOSE + timedelta(minutes=25),
        ),
    )
    second = run_rank_snapshot(
        conn, session=SESSION.isoformat(), as_of=CLOSE + timedelta(minutes=25),
        code_version="rank-code", run_id="rank-run",
    )

    assert first.created is True
    assert second.created is True
    assert second.rank_run_id != first.rank_run_id
    assert conn.execute("SELECT COUNT(*) FROM postmarket_rank_runs").fetchone()[0] == 2


def test_freshness_boundary_changes_digest_without_new_market_evidence(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_candidate(conn, 1, "AAA")
    record_context(conn, _context(1, "AAA", spread=20, excess=9, atr_units=4))
    _lifecycle(conn, 1, "AAA")

    fresh = run_rank_snapshot(
        conn, session=SESSION.isoformat(), as_of=AS_OF,
        code_version="rank-code", run_id="rank-run",
    )
    stale = run_rank_snapshot(
        conn, session=SESSION.isoformat(),
        as_of=AS_OF + timedelta(minutes=8),
        code_version="rank-code", run_id="rank-run",
    )

    assert fresh.created is True
    assert fresh.rankable_candidates == 1
    assert stale.created is True
    assert stale.rankable_candidates == 0
    reasons = conn.execute(
        """
        SELECT exclusion_reasons_json FROM postmarket_candidate_ranks
        WHERE rank_run_id=?
        """,
        (stale.rank_run_id,),
    ).fetchone()[0]
    assert "OBSERVATION_STALE" in reasons
    summary = latest_rank_summary(conn)
    assert summary["rankable_candidates"] == 0
    assert summary["unrankable_candidates"] == 1
    assert summary["session_runs"] == 2
    assert summary["session_rankable_runs"] == 1
    assert summary["session_peak_rankable_candidates"] == 1
    assert summary["latest_exclusion_counts"] == {
        "CONTEXT_STALE": 1,
        "OBSERVATION_STALE": 1,
        "QUOTE_STALE": 1,
    }
    assert summary["latest_rankable_snapshot"]["rank_run_id"] == fresh.rank_run_id
    assert summary["latest_rankable_snapshot"]["top"][0]["symbol"] == "AAA"


def test_service_heartbeat_exposes_rank_semantics_and_top_candidates(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_candidate(conn, 1, "AAA")
    record_context(conn, _context(1, "AAA", spread=20, excess=9, atr_units=4))
    _lifecycle(conn, 1, "AAA")

    fields = discovery_shadow.rank_heartbeat_fields(
        AS_OF,
        conn,
        version="rank-code",
        run_id="rank-run",
    )

    assert fields["rank_status"] == "complete"
    assert fields["rank_snapshot_created"] is True
    assert fields["rankable_candidates"] == 1
    assert fields["rank_top"][0][1] == "AAA"
    assert fields["rank_session_peak_rankable_candidates"] == 1
    assert fields["rank_session_rankable_runs"] == 1
    assert fields["rank_latest_exclusion_counts"] == {}
    assert fields["rank_latest_rankable_snapshot"]["top"][0]["symbol"] == "AAA"
    assert fields["latest_rank"]["semantics"] == (
        "heuristic_evidence_ordering_not_probability"
    )


def test_rank_module_has_no_provider_alert_delivery_or_trading_dependency():
    path = Path(__file__).parents[1] / "tradebot" / "postmarket_rank.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = (
        "tradebot.vendors", "tradebot.alerts", "tradebot.telegram_bot",
        "tradebot.order", "tradebot.broker",
    )
    assert not any(module.startswith(forbidden) for module in imports)
