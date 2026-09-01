from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

import tradebot.postmarket_discovery_shadow as discovery_shadow
from tradebot.detectors import Bar
from tradebot.rth_missed_mover_census import (
    OUTCOME_EXCURSION_ONLY,
    OUTCOME_INVALID_DATA,
    OUTCOME_MAJOR_CLOSE_MOVER,
    build_rth_missed_mover_census_report,
    ensure_rth_missed_mover_census_schema,
    evaluate_rth_missed_mover_symbol,
    latest_rth_missed_mover_census_summary,
    next_due_rth_missed_mover_census_session,
    next_unreported_rth_missed_mover_census,
    run_rth_missed_mover_census,
    write_rth_missed_mover_census_report,
)


SESSION = date(2026, 8, 31)
CLOSE = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)
POSTMARKET_END = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
NOW = POSTMARKET_END + timedelta(minutes=5)


def _bar(
    symbol: str,
    session: date,
    *,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: int,
) -> Bar:
    return Bar(
        symbol,
        datetime(session.year, session.month, session.day, 4, tzinfo=timezone.utc),
        open,
        high,
        low,
        close,
        volume,
    )


def _daily(symbol: str, *, close: float = 1.5, high: float = 1.6, low: float = 0.9):
    return [
        _bar(
            symbol,
            date(2026, 8, 28),
            open=1.0,
            high=1.05,
            low=0.95,
            close=1.0,
            volume=1_000_000,
        ),
        _bar(
            symbol,
            SESSION,
            open=1.0,
            high=high,
            low=low,
            close=close,
            volume=220_000_000,
        ),
    ]


def _lane_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE rth_momentum_ticks (
          tick_id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL
        );
        CREATE TABLE rth_momentum_observations (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
          symbol TEXT NOT NULL, outcome TEXT NOT NULL
        );
        CREATE TABLE rth_momentum_candidates (
          candidate_id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
          symbol TEXT NOT NULL, direction TEXT NOT NULL
        );
        """
    )


def test_gpro_shaped_major_close_is_an_explicit_bounded_lane_miss():
    row = evaluate_rth_missed_mover_symbol(
        "GPRO",
        SESSION,
        _daily("GPRO"),
        fast_lane_ticks=31,
        fast_lane_seen=False,
        fast_lane_directions=set(),
        fast_lane_outcomes=(),
    )

    assert row.outcome == OUTCOME_MAJOR_CLOSE_MOVER
    assert row.close_move_pct == pytest.approx(50.0)
    assert row.daily_notional == 330_000_000
    assert row.qualifying_directions == ("up",)
    assert row.missed_directions == ("up",)
    assert row.miss_reasons == {"up": "NOT_SELECTED_BY_BOUNDED_RTH_LANE"}


def test_miss_reason_distinguishes_lane_outage_and_live_rejection():
    outage = evaluate_rth_missed_mover_symbol(
        "GPRO",
        SESSION,
        _daily("GPRO"),
        fast_lane_ticks=0,
        fast_lane_seen=False,
        fast_lane_directions=set(),
        fast_lane_outcomes=(),
    )
    rejected = evaluate_rth_missed_mover_symbol(
        "GPRO",
        SESSION,
        _daily("GPRO"),
        fast_lane_ticks=31,
        fast_lane_seen=True,
        fast_lane_directions=set(),
        fast_lane_outcomes=("AWAITING_PERSISTENCE", "BELOW_NOTIONAL"),
    )

    assert outage.miss_reasons == {"up": "RTH_LANE_NOT_RUNNING"}
    assert rejected.miss_reasons == {
        "up": "SELECTED_NOT_QUALIFIED:AWAITING_PERSISTENCE,BELOW_NOTIONAL"
    }


def test_intraday_excursion_is_review_only_not_close_truth():
    row = evaluate_rth_missed_mover_symbol(
        "FADE",
        SESSION,
        _daily("FADE", close=1.02, high=1.30, low=0.70),
        fast_lane_ticks=31,
        fast_lane_seen=False,
        fast_lane_directions=set(),
        fast_lane_outcomes=(),
    )

    assert row.outcome == OUTCOME_EXCURSION_ONLY
    assert row.qualifying_directions == ()
    assert row.excursion_directions == ("down", "up")
    assert row.missed_directions == ()


def test_duplicate_or_wrong_symbol_daily_identity_fails_closed():
    duplicate = _daily("GPRO") + [_daily("GPRO")[-1]]
    wrong = _daily("WRONG")
    naive = _daily("GPRO") + [
        Bar("GPRO", datetime(2026, 8, 27), 1, 1, 1, 1, 1_000)
    ]
    future = _daily("GPRO") + [
        _bar(
            "GPRO",
            date(2026, 9, 1),
            open=1.5,
            high=1.5,
            low=1.5,
            close=1.5,
            volume=1_000,
        )
    ]

    assert evaluate_rth_missed_mover_symbol(
        "GPRO",
        SESSION,
        duplicate,
        fast_lane_ticks=31,
        fast_lane_seen=False,
        fast_lane_directions=set(),
        fast_lane_outcomes=(),
    ).outcome == OUTCOME_INVALID_DATA
    for bars in (naive, future):
        assert evaluate_rth_missed_mover_symbol(
            "GPRO",
            SESSION,
            bars,
            fast_lane_ticks=31,
            fast_lane_seen=False,
            fast_lane_directions=set(),
            fast_lane_outcomes=(),
        ).outcome == OUTCOME_INVALID_DATA
    assert evaluate_rth_missed_mover_symbol(
        "GPRO",
        SESSION,
        wrong,
        fast_lane_ticks=31,
        fast_lane_seen=False,
        fast_lane_directions=set(),
        fast_lane_outcomes=(),
    ).outcome == OUTCOME_INVALID_DATA


def test_full_universe_run_attributes_caught_and_missed_pairs(tmp_path):
    conn = sqlite3.connect(tmp_path / "shadow.db")
    _lane_schema(conn)
    conn.executemany(
        "INSERT INTO rth_momentum_ticks (session) VALUES (?)",
        [(SESSION.isoformat(),)] * 31,
    )
    conn.execute(
        "INSERT INTO rth_momentum_observations (session,symbol,outcome) "
        "VALUES (?,?,?)",
        (SESSION.isoformat(), "CAUGHT", "CANDIDATE"),
    )
    conn.execute(
        "INSERT INTO rth_momentum_candidates (session,symbol,direction) "
        "VALUES (?,?,?)",
        (SESSION.isoformat(), "CAUGHT", "up"),
    )
    conn.commit()
    bars = {
        "CAUGHT": _daily("CAUGHT", close=1.20),
        "GPRO": _daily("GPRO"),
        "QUIET": _daily("QUIET", close=1.02, high=1.03, low=0.99),
    }

    result, rows = run_rth_missed_mover_census(
        conn,
        universe_symbols=("QUIET", "GPRO", "CAUGHT"),
        session=SESSION,
        postmarket_end=POSTMARKET_END,
        now=NOW,
        run_id="census-1",
        code_version="abc1234",
        data_feed="sip",
        daily_fetch=lambda symbols: {symbol: bars[symbol] for symbol in symbols},
        chunk_size=2,
    )

    by_symbol = {row.symbol: row for row in rows}
    assert result.status == "success"
    assert result.universe_symbols == result.fetched_symbols == 3
    assert result.evaluated_symbols == 3
    assert result.major_close_pairs == 2
    assert result.caught_pairs == result.missed_pairs == 1
    assert result.close_recall == 0.5
    assert result.invariant_ok is True
    assert by_symbol["CAUGHT"].missed_directions == ()
    assert by_symbol["GPRO"].miss_reasons == {
        "up": "NOT_SELECTED_BY_BOUNDED_RTH_LANE"
    }

    report = build_rth_missed_mover_census_report(conn, result.census_id)
    assert report.operational_complete is True
    assert report.quality_evidence_eligible is False
    assert {"MISSED_MAJOR_CLOSE_MOVERS", "PROVIDER_COMPARISON_NOT_CONFIGURED"} <= set(
        report.issue_codes
    )
    assert report.missed_major_closes[0]["symbol"] == "GPRO"

    assert next_unreported_rth_missed_mover_census(
        conn, tmp_path / "audits"
    ) == result.census_id
    written, created = write_rth_missed_mover_census_report(
        conn, tmp_path / "audits", result.census_id
    )
    assert created is True
    assert written == report
    assert write_rth_missed_mover_census_report(
        conn, tmp_path / "audits", result.census_id
    )[1] is False
    assert next_unreported_rth_missed_mover_census(
        conn, tmp_path / "audits"
    ) is None
    summary = latest_rth_missed_mover_census_summary(tmp_path / "audits")
    assert summary["session"] == SESSION.isoformat()
    assert summary["missed_pairs"] == 1
    path = tmp_path / "audits" / "rth_missed_mover_census_2026-08-31_v1.json"
    assert json.loads(path.read_text())["metrics"]["close_recall"] == 0.5
    assert path.stat().st_mode & 0o222 == 0


def test_provider_chunk_failure_is_degraded_and_conserved():
    conn = sqlite3.connect(":memory:")
    _lane_schema(conn)
    conn.execute(
        "INSERT INTO rth_momentum_ticks (session) VALUES (?)",
        (SESSION.isoformat(),),
    )

    result, rows = run_rth_missed_mover_census(
        conn,
        universe_symbols=("A", "B"),
        session=SESSION,
        postmarket_end=POSTMARKET_END,
        now=NOW,
        run_id="failure",
        code_version="abc1234",
        data_feed="sip",
        daily_fetch=lambda symbols: (_ for _ in ()).throw(RuntimeError("outage")),
        chunk_size=1,
    )

    assert result.status == "degraded"
    assert result.error_count == 2
    assert result.fetched_symbols == result.evaluated_symbols == 0
    assert result.unavailable_symbols == 2
    assert result.invariant_ok is True
    assert {row.data_status for row in rows} == {"FETCH_ERROR"}


def test_unexpected_provider_symbol_is_loud_but_not_evaluated():
    conn = sqlite3.connect(":memory:")
    _lane_schema(conn)
    result, rows = run_rth_missed_mover_census(
        conn,
        universe_symbols=("GPRO",),
        session=SESSION,
        postmarket_end=POSTMARKET_END,
        now=NOW,
        run_id="unexpected",
        code_version="abc1234",
        data_feed="sip",
        daily_fetch=lambda symbols: {
            "GPRO": _daily("GPRO"),
            "NOT_REQUESTED": _daily("NOT_REQUESTED"),
        },
    )

    assert result.status == "degraded"
    assert result.error_count == 1
    assert result.universe_symbols == result.fetched_symbols == len(rows) == 1
    assert rows[0].symbol == "GPRO"


def test_due_session_waits_for_full_postmarket_finalization():
    conn = sqlite3.connect(":memory:")
    _lane_schema(conn)
    conn.execute(
        "INSERT INTO rth_momentum_ticks (session) VALUES (?)",
        (SESSION.isoformat(),),
    )
    conn.commit()

    assert next_due_rth_missed_mover_census_session(
        conn, now=NOW - timedelta(seconds=1)
    ) is None
    due = next_due_rth_missed_mover_census_session(conn, now=NOW)
    assert due == (SESSION, CLOSE, POSTMARKET_END)

    empty = sqlite3.connect(":memory:")
    assert next_due_rth_missed_mover_census_session(empty, now=NOW) == (
        SESSION,
        CLOSE,
        POSTMARKET_END,
    )


def test_census_tables_are_append_only():
    conn = sqlite3.connect(":memory:")
    _lane_schema(conn)
    conn.execute(
        "INSERT INTO rth_momentum_ticks (session) VALUES (?)",
        (SESSION.isoformat(),),
    )
    result, _ = run_rth_missed_mover_census(
        conn,
        universe_symbols=("GPRO",),
        session=SESSION,
        postmarket_end=POSTMARKET_END,
        now=NOW,
        run_id="append-only",
        code_version="abc1234",
        data_feed="sip",
        daily_fetch=lambda symbols: {"GPRO": _daily("GPRO")},
    )
    assert result.census_id == 1

    for table in (
        "rth_missed_mover_census_runs",
        "rth_missed_mover_census_events",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {table} SET rowid=rowid")
        conn.rollback()
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_idle_heartbeat_surfaces_latest_census(monkeypatch):
    latest = {
        "session": SESSION.isoformat(),
        "report_version": 1,
        "operational_complete": True,
        "quality_evidence_eligible": False,
        "close_recall": 0.5,
        "missed_pairs": 1,
        "unavailable_symbols": 0,
        "issue_codes": ["MISSED_MAJOR_CLOSE_MOVERS"],
    }
    monkeypatch.setattr(
        discovery_shadow,
        "next_due_rth_missed_mover_census_session",
        lambda conn, now: None,
    )
    monkeypatch.setattr(
        discovery_shadow,
        "latest_rth_missed_mover_census_summary",
        lambda path: latest,
    )

    fields = discovery_shadow.rth_missed_mover_census_heartbeat_fields(
        NOW,
        sqlite3.connect(":memory:"),
        sqlite3.connect(":memory:"),
        data_feed="sip",
        version="abc1234",
    )

    assert fields == {
        "rth_missed_mover_census_status": "current",
        "rth_missed_mover_census_report_written": False,
        "latest_rth_missed_mover_census": latest,
    }


def test_idle_heartbeat_makes_census_failure_visible(monkeypatch):
    def fail(conn, now):
        raise ValueError("malformed census evidence")

    monkeypatch.setattr(
        discovery_shadow,
        "next_due_rth_missed_mover_census_session",
        fail,
    )
    monkeypatch.setattr(
        discovery_shadow,
        "latest_rth_missed_mover_census_summary",
        lambda path: None,
    )
    fields = discovery_shadow.rth_missed_mover_census_heartbeat_fields(
        NOW,
        sqlite3.connect(":memory:"),
        sqlite3.connect(":memory:"),
        data_feed="sip",
        version="abc1234",
    )

    assert fields["rth_missed_mover_census_status"] == "error"
    assert "malformed census evidence" in fields[
        "rth_missed_mover_census_error"
    ]
