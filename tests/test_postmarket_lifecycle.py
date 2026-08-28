"""Candidate lifecycle remains completed-bar, append-only, and screen-independent."""
from __future__ import annotations

import ast
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_discovery_shadow as discovery_shadow
from tradebot.detectors import Bar
from tradebot.postmarket import (
    OUTCOME_BELOW_MOVE,
    OUTCOME_CANDIDATE,
    OUTCOME_FETCH_ERROR,
    ReactionEvaluation,
)
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_lifecycle import (
    STATE_CLOSED,
    STATE_CONFIRMED,
    STATE_DEQUALIFIED,
    STATE_FADING,
    STATE_NEW,
    STATE_REQUALIFIED,
    STATE_STRENGTHENING,
    lifecycle_summary,
    lifecycle_window,
    run_lifecycle_pass,
)


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 0, 5, tzinfo=timezone.utc)


def _bar(ts, close, symbol="ABC", volume=50_000):
    return Bar(symbol, ts, close, close, close, close, volume)


def _evaluation(bar_open, move, *, outcome=OUTCOME_CANDIDATE, direction="up"):
    return ReactionEvaluation(
        symbol="ABC",
        outcome=outcome,
        reason=f"fixture {outcome}",
        event_date=SESSION,
        bar=_bar(bar_open, 100 * (1 + move / 100)),
        rth_close=100,
        cumulative_volume=100_000,
        cumulative_notional=10_000_000,
        move_pct=move,
        direction=direction,
        persistence_bars=2,
        persistence_span_seconds=300,
        data_age_seconds=0,
    )


def _seed(conn):
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
            SESSION.isoformat(), "ABC", SESSION.isoformat(), "up", 1,
            (CLOSE + timedelta(minutes=10)).isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(), 100, 110, 10,
            100_000, 10_000_000, '["market_gainer"]', "sip", "alpaca",
            "5Min", "candidate-code", "candidate-run",
        ),
    )
    conn.commit()


def _run(conn, now, evaluation=None):
    return run_lifecycle_pass(
        conn,
        session=SESSION,
        session_close=CLOSE,
        window_end=END,
        now=now,
        code_version="lifecycle-code",
        run_id="lifecycle-run",
        data_feed="sip",
        bars_fetch=lambda symbols, session: pytest.fail("unexpected fetch"),
        existing_evaluations=(() if evaluation is None else (evaluation,)),
    )


def test_lifecycle_distinguishes_every_required_state_and_closes(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed(conn)

    _run(conn, CLOSE + timedelta(minutes=10), _evaluation(CLOSE + timedelta(minutes=5), 10))
    _run(conn, CLOSE + timedelta(minutes=15), _evaluation(CLOSE + timedelta(minutes=10), 10.5))
    _run(conn, CLOSE + timedelta(minutes=20), _evaluation(CLOSE + timedelta(minutes=15), 12))
    _run(conn, CLOSE + timedelta(minutes=25), _evaluation(CLOSE + timedelta(minutes=20), 9.5))
    _run(
        conn,
        CLOSE + timedelta(minutes=30),
        _evaluation(CLOSE + timedelta(minutes=25), 7, outcome=OUTCOME_BELOW_MOVE),
    )
    _run(conn, CLOSE + timedelta(minutes=35), _evaluation(CLOSE + timedelta(minutes=30), 9))
    _run(conn, END, _evaluation(CLOSE + timedelta(hours=3, minutes=55), 11.5))

    rows = conn.execute(
        """
        SELECT state,actionability,transition_at_utc,recorded_at_utc
        FROM postmarket_candidate_lifecycle ORDER BY transition_id
        """
    ).fetchall()
    assert [row[0] for row in rows] == [
        STATE_NEW,
        STATE_CONFIRMED,
        STATE_STRENGTHENING,
        STATE_FADING,
        STATE_DEQUALIFIED,
        STATE_REQUALIFIED,
        STATE_CLOSED,
    ]
    assert [row[1] for row in rows] == [
        "WATCH", "QUALIFIED", "QUALIFIED", "WATCH",
        "NOT_ACTIONABLE", "QUALIFIED", "CLOSED",
    ]
    assert rows[1][2] == (CLOSE + timedelta(minutes=15)).isoformat()
    assert rows[1][3] == (CLOSE + timedelta(minutes=15)).isoformat()
    assert lifecycle_summary(conn) == {
        "session": SESSION.isoformat(),
        "candidates": 1,
        "states": {"CLOSED": 1},
        "currently_qualified": 0,
        "closed": 1,
        "missing": 0,
        "observations": 7,
        "latest_observed_at_utc": END.isoformat(),
        "latest_evidence_bar_open_ts_utc": (
            CLOSE + timedelta(hours=3, minutes=55)
        ).isoformat(),
    }
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_candidate_lifecycle SET state='CONFIRMED'")


def test_offscreen_candidate_is_fetched_and_confirmed(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed(conn)
    first = _run(
        conn,
        CLOSE + timedelta(minutes=10),
        _evaluation(CLOSE + timedelta(minutes=5), 10),
    )
    calls = []

    def fetch(symbols, session):
        calls.append((symbols, session))
        return {
            "ABC": [
                _bar(CLOSE - timedelta(minutes=5), 100),
                _bar(CLOSE, 110),
                _bar(CLOSE + timedelta(minutes=5), 110.5),
                _bar(CLOSE + timedelta(minutes=10), 111),
            ]
        }

    second = run_lifecycle_pass(
        conn,
        session=SESSION,
        session_close=CLOSE,
        window_end=END,
        now=CLOSE + timedelta(minutes=15),
        code_version="lifecycle-code",
        run_id="lifecycle-run",
        data_feed="sip",
        bars_fetch=fetch,
    )

    assert first.states_written == ((STATE_NEW, 1),)
    assert calls == [(["ABC"], SESSION)]
    assert second.symbols_fetched == 1
    assert second.states_written == ((STATE_CONFIRMED, 1),)


def test_data_failure_does_not_fabricate_dequalification(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed(conn)
    _run(
        conn,
        CLOSE + timedelta(minutes=10),
        _evaluation(CLOSE + timedelta(minutes=5), 10),
    )
    result = _run(
        conn,
        CLOSE + timedelta(minutes=15),
        ReactionEvaluation(
            "ABC", OUTCOME_FETCH_ERROR, "provider down", SESSION,
        ),
    )

    assert result.transitions_written == 0
    assert conn.execute(
        "SELECT state FROM postmarket_candidate_lifecycle ORDER BY transition_id DESC LIMIT 1"
    ).fetchone()[0] == STATE_NEW


def test_same_completed_bar_is_idempotent(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed(conn)
    evaluation = _evaluation(CLOSE + timedelta(minutes=5), 10)

    first = _run(conn, CLOSE + timedelta(minutes=10), evaluation)
    second = _run(conn, CLOSE + timedelta(minutes=11), evaluation)

    assert first.transitions_written == 1
    assert second.transitions_written == 0
    assert first.observations_written == 1
    assert second.observations_written == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_candidate_lifecycle"
    ).fetchone()[0] == 1


def test_service_heartbeat_surfaces_lifecycle_and_never_needs_stage1_membership(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed(conn)

    fields = discovery_shadow.lifecycle_heartbeat_fields(
        CLOSE + timedelta(minutes=10),
        conn,
        data_feed="sip",
        version="lifecycle-code",
        run_id="lifecycle-run",
        existing_evaluations=(
            _evaluation(CLOSE + timedelta(minutes=5), 10),
        ),
        bars_fetch=lambda symbols, session: pytest.fail("unexpected fetch"),
    )

    assert fields["lifecycle_status"] == "current"
    assert fields["lifecycle_transitions_written"] == 1
    assert fields["lifecycle_observations_written"] == 1
    assert fields["lifecycle_states_written"] == {STATE_NEW: 1}
    assert fields["latest_lifecycle"]["states"] == {STATE_NEW: 1}


def test_lifecycle_window_uses_real_early_close_and_final_bar_grace():
    close, end = lifecycle_window(date(2026, 11, 27))

    assert close == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 28, 1, 5, tzinfo=timezone.utc)


def test_lifecycle_module_has_no_alert_delivery_or_trading_dependency():
    path = Path(__file__).parents[1] / "tradebot" / "postmarket_lifecycle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = ("tradebot.alerts", "tradebot.telegram_bot", "tradebot.order", "tradebot.broker")
    assert not any(module.startswith(forbidden) for module in imports)
