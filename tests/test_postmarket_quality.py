"""Outcome-truth coverage for postmarket candidates."""
from __future__ import annotations

import ast
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Bar
from tradebot.postmarket_discovery import connect as connect_discovery
from tradebot.postmarket_quality import (
    MARK_STATUS_AVAILABLE,
    MARK_STATUS_NO_BAR,
    NEXT_SESSION_CLOSE,
    NEXT_SESSION_OPEN,
    POSTMARKET_CLOSE,
    CandidateReference,
    candidate_quality_report,
    compute_outcome_marks,
    ensure_quality_schema,
    mark_targets,
    record_outcome_marks,
)


SESSION = date(2026, 8, 27)
UTC = timezone.utc


def _bar(ts: datetime, *, symbol="TEST", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000):
    return Bar(symbol, ts, open, high, low, close, volume)


def _candidate(candidate_id=1, *, direction="up", baseline=100.0):
    return CandidateReference(
        candidate_stream="marketwide",
        candidate_id=candidate_id,
        session=SESSION,
        symbol="TEST",
        direction=direction,
        detection_bar_open_ts_utc=datetime(2026, 8, 27, 20, 10, tzinfo=UTC),
        baseline_price=baseline,
    )


def _postmarket_bars():
    return [
        _bar(datetime(2026, 8, 27, 20, 10, tzinfo=UTC), close=100.0),
        _bar(
            datetime(2026, 8, 27, 20, 15, tzinfo=UTC),
            open=100.0,
            high=105.0,
            low=99.0,
            close=104.0,
        ),
        _bar(
            datetime(2026, 8, 27, 20, 25, tzinfo=UTC),
            open=104.0,
            high=111.0,
            low=103.0,
            close=110.0,
        ),
        _bar(
            datetime(2026, 8, 27, 20, 55, tzinfo=UTC),
            open=110.0,
            high=112.0,
            low=108.0,
            close=111.0,
        ),
        _bar(
            datetime(2026, 8, 27, 23, 55, tzinfo=UTC),
            open=111.0,
            high=113.0,
            low=109.0,
            close=112.0,
        ),
    ]


def _next_rth_bars():
    return [
        _bar(
            datetime(2026, 8, 28, 13, 30, tzinfo=UTC),
            open=115.0,
            high=117.0,
            low=114.0,
            close=116.0,
        ),
        _bar(
            datetime(2026, 8, 28, 19, 55, tzinfo=UTC),
            open=117.0,
            high=119.0,
            low=116.0,
            close=118.0,
        ),
    ]


def _seed_marketwide_candidate(conn, symbol, direction, first_detected_at):
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
            direction,
            1,
            first_detected_at.isoformat(),
            (first_detected_at - timedelta(minutes=5)).isoformat(),
            92.0,
            100.0,
            8.7,
            1000,
            100_000.0,
            '["market_gainer"]',
            "sip",
            "alpaca",
            "5Min",
            "abc123",
            "candidate-run",
        ),
    )
    conn.commit()
    return cursor.lastrowid


def test_targets_use_knowable_detection_close_and_exchange_calendar():
    targets = {target.checkpoint: target.target_ts_utc for target in mark_targets(_candidate())}

    assert targets["+5m"] == datetime(2026, 8, 27, 20, 20, tzinfo=UTC)
    assert targets[POSTMARKET_CLOSE] == datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    assert targets[NEXT_SESSION_OPEN] == datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
    assert targets[NEXT_SESSION_CLOSE] == datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


def test_outcomes_use_completed_bars_and_compute_directional_excursion():
    marks = compute_outcome_marks(
        _candidate(),
        _postmarket_bars(),
        _next_rth_bars(),
        as_of=datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
    )
    by_checkpoint = {mark.checkpoint: mark for mark in marks}

    assert set(by_checkpoint) == {
        "+5m",
        "+15m",
        "+30m",
        "+60m",
        POSTMARKET_CLOSE,
        NEXT_SESSION_OPEN,
        NEXT_SESSION_CLOSE,
    }
    five = by_checkpoint["+5m"]
    assert five.status == MARK_STATUS_AVAILABLE
    assert five.price == 104.0
    assert five.observed_at_utc == "2026-08-27T20:20:00+00:00"
    assert five.directional_return_pct == pytest.approx(4.0)
    assert five.mfe_pct == pytest.approx(5.0)
    assert five.mae_pct == pytest.approx(1.0)
    assert five.time_to_mfe_minutes == 5.0
    assert five.detail["target_distance_seconds"] == 0
    assert by_checkpoint[POSTMARKET_CLOSE].price == 112.0
    assert by_checkpoint[POSTMARKET_CLOSE].detail["target_distance_seconds"] == 0
    assert by_checkpoint[NEXT_SESSION_OPEN].price == 115.0
    assert by_checkpoint[NEXT_SESSION_OPEN].detail == {
        "price_field": "open",
        "target_distance_seconds": 0,
    }
    assert by_checkpoint[NEXT_SESSION_OPEN].mfe_pct == pytest.approx(15.0)
    assert by_checkpoint[NEXT_SESSION_CLOSE].price == 118.0


def test_down_direction_returns_and_excursions_are_signed_correctly():
    bars = [
        _bar(datetime(2026, 8, 27, 20, 10, tzinfo=UTC), close=100.0),
        _bar(
            datetime(2026, 8, 27, 20, 15, tzinfo=UTC),
            open=100.0,
            high=102.0,
            low=94.0,
            close=95.0,
        ),
    ]
    marks = compute_outcome_marks(
        _candidate(direction="down"),
        bars,
        (),
        as_of=datetime(2026, 8, 27, 20, 20, tzinfo=UTC),
    )

    mark = marks[0]
    assert mark.checkpoint == "+5m"
    assert mark.directional_return_pct == pytest.approx(5.0)
    assert mark.mfe_pct == pytest.approx(6.0)
    assert mark.mae_pct == pytest.approx(2.0)


def test_missing_prices_are_explicit_only_after_sessions_finalize():
    before_final = compute_outcome_marks(
        _candidate(),
        (),
        (),
        as_of=datetime(2026, 8, 27, 20, 30, tzinfo=UTC),
    )
    after_final = compute_outcome_marks(
        _candidate(),
        (),
        (),
        as_of=datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
    )

    assert before_final == ()
    assert len(after_final) == 7
    assert {mark.status for mark in after_final} == {MARK_STATUS_NO_BAR}
    assert all(mark.price is None for mark in after_final)


def test_incomplete_provider_responses_do_not_become_no_bar_truth():
    after_final = compute_outcome_marks(
        _candidate(),
        (),
        (),
        as_of=datetime(2026, 8, 28, 20, 5, 1, tzinfo=UTC),
        postmarket_data_complete=False,
        next_session_data_complete=False,
    )

    assert after_final == ()


def test_recording_is_append_only_idempotent_and_allows_later_correction(tmp_path):
    conn = sqlite3.connect(tmp_path / "quality.db")
    ensure_quality_schema(conn)
    missing = compute_outcome_marks(
        _candidate(),
        (),
        (),
        as_of=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
    )
    available = compute_outcome_marks(
        _candidate(),
        _postmarket_bars(),
        (),
        as_of=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
    )

    kwargs = dict(
        data_feed="sip",
        market_data_provider="alpaca",
        bar_timeframe="5Min",
        code_version="abc123",
        run_id="quality-run",
        recorded_at_utc=datetime(2026, 8, 28, 0, 6, tzinfo=UTC),
    )
    assert record_outcome_marks(conn, missing, **kwargs) == 5
    assert record_outcome_marks(conn, missing, **kwargs) == 0
    replay_kwargs = {
        **kwargs,
        "run_id": "another-run",
        "recorded_at_utc": datetime(2026, 8, 28, 0, 7, tzinfo=UTC),
    }
    assert record_outcome_marks(conn, missing, **replay_kwargs) == 0
    assert record_outcome_marks(conn, available, **kwargs) == 5
    assert conn.execute("SELECT COUNT(*) FROM postmarket_candidate_mark_events").fetchone()[0] == 10

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_candidate_mark_events SET status='NO_BAR'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM postmarket_candidate_mark_events")


def test_quality_report_fails_closed_below_sample_and_resolves_latest_events(tmp_path):
    conn = connect_discovery(tmp_path / "shadow.db")
    detection = datetime(2026, 8, 27, 20, 15, tzinfo=UTC)
    first_id = _seed_marketwide_candidate(conn, "UPONE", "up", detection)
    second_id = _seed_marketwide_candidate(conn, "UPTWO", "up", detection)
    ensure_quality_schema(conn)

    first = replace(_candidate(first_id), symbol="UPONE")
    second = replace(_candidate(second_id), symbol="UPTWO")
    bars_one = [replace(bar, symbol="UPONE") for bar in _postmarket_bars()]
    bars_two = [replace(bar, symbol="UPTWO") for bar in _postmarket_bars()]
    marks = [
        compute_outcome_marks(
            first,
            bars_one,
            (),
            as_of=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
        )[0],
        compute_outcome_marks(
            second,
            bars_two,
            (),
            as_of=datetime(2026, 8, 28, 0, 5, 1, tzinfo=UTC),
        )[0],
    ]
    record_outcome_marks(
        conn,
        marks,
        data_feed="sip",
        market_data_provider="alpaca",
        bar_timeframe="5Min",
        code_version="abc123",
        run_id="quality-run",
        recorded_at_utc=datetime(2026, 8, 28, 0, 6, tzinfo=UTC),
    )

    guarded = candidate_quality_report(
        conn,
        candidate_stream="marketwide",
        session=SESSION,
        checkpoint="+5m",
    )
    eligible = candidate_quality_report(
        conn,
        candidate_stream="marketwide",
        session=SESSION,
        checkpoint="+5m",
        minimum_sample=2,
    )

    assert guarded.evidence_eligible is False
    assert guarded.continuation_rate is None
    assert eligible.evidence_eligible is True
    assert eligible.total_candidates == 2
    assert eligible.available_marks == 2
    assert eligible.missing_marks == 0
    assert eligible.continuation_rate == 1.0


def test_malformed_or_cross_symbol_bars_fail_instead_of_becoming_marks():
    with pytest.raises(ValueError, match="another symbol"):
        compute_outcome_marks(
            _candidate(),
            [_bar(datetime(2026, 8, 27, 20, 15, tzinfo=UTC), symbol="OTHER")],
            (),
            as_of=datetime(2026, 8, 27, 20, 20, tzinfo=UTC),
        )


def test_quality_module_has_no_provider_delivery_or_trading_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_quality.py"
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
