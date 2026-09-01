from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.detectors import Bar
from tradebot.marketdata import MarketScreenEntry, MarketWideScreen
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_discovery_shadow import (
    discovery_idle_sleep_seconds,
    rth_handoff_reconcile_heartbeat_fields,
)
from tradebot.rth_momentum import (
    HANDOFF_POSTMARKET_NOT_QUALIFIED,
    HANDOFF_POSTMARKET_QUALIFIED,
    HANDOFF_RTH_QUALIFIED,
    OUTCOME_BAR_GAP,
    OUTCOME_CANDIDATE,
    OUTCOME_NO_DAILY_BASELINE_RETURNED,
    OUTCOME_NO_INTRADAY_BARS_RETURNED,
    OUTCOME_NO_PRIOR_CLOSE,
    OUTCOME_STALE,
    evaluate_rth_momentum,
    reconcile_rth_postmarket_handoffs,
    rth_handoff_is_active,
    rth_handoff_window,
    run_rth_momentum_tick,
    select_rth_symbols,
)


SESSION = date(2026, 8, 31)
OPEN = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)
NOW = CLOSE - timedelta(minutes=5)
UPDATED = NOW - timedelta(seconds=20)


def _screen(*, updated: datetime = UPDATED) -> MarketWideScreen:
    return MarketWideScreen(
        entries=(
            MarketScreenEntry(
                "GPRO", "market_gainer", 1, updated, move_pct=45.0, price=1.20
            ),
            MarketScreenEntry(
                "GPRO", "most_active_volume", 1, updated,
                volume=220_000_000, trade_count=100_000,
            ),
            MarketScreenEntry(
                "OUTSIDE", "market_loser", 1, updated, move_pct=-20.0, price=2.0
            ),
        ),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", updated),
            ("most_actives_volume", updated),
            ("most_actives_trades", updated),
        ),
    )


def _bar(symbol: str, ts: datetime, close: float, volume: int = 100_000) -> Bar:
    return Bar(symbol, ts, close, close, close, close, volume)


def _rth_bars(symbol: str = "GPRO", *, move: float = 50.0) -> list[Bar]:
    bars = []
    for index in range(77):
        ts = OPEN + timedelta(minutes=5 * index)
        if ts >= NOW:
            break
        close = 1.0 if index < 70 else 1.0 + move / 100.0
        bars.append(_bar(symbol, ts, close))
    return bars


def _daily(symbol: str = "GPRO", close: float = 1.0) -> list[Bar]:
    return [
        _bar(
            symbol,
            datetime(2026, 8, 28, 19, 55, tzinfo=timezone.utc),
            close,
            1_000_000,
        )
    ]


def _run(conn, *, now: datetime = NOW, scheduled=()):
    updated = now - timedelta(seconds=20)
    screen = _screen(updated=updated)
    bars = _rth_bars()
    return run_rth_momentum_tick(
        conn,
        active_universe={"GPRO", "SCHEDULED"},
        scheduled_earnings=set(scheduled),
        now=now,
        run_id="run-1",
        code_version="abc1234",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        intraday_fetch=lambda symbols, session: {
            symbol: bars if symbol == "GPRO" else [] for symbol in symbols
        },
        daily_fetch=lambda symbols: {
            symbol: _daily(symbol) for symbol in symbols if symbol == "GPRO"
        },
    )


def test_window_uses_real_close_and_early_close():
    window = rth_handoff_window(NOW)
    assert window == (SESSION, CLOSE - timedelta(minutes=30), CLOSE)
    assert rth_handoff_is_active(CLOSE - timedelta(minutes=30)) is True
    assert rth_handoff_is_active(CLOSE) is True
    assert rth_handoff_is_active(CLOSE - timedelta(minutes=31)) is False

    early_now = datetime(2026, 11, 27, 17, 45, tzinfo=timezone.utc)
    early = rth_handoff_window(early_now)
    assert early == (
        date(2026, 11, 27),
        datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc),
        datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
    )


def test_supervisor_idle_sleep_wakes_at_handoff_start():
    one_minute_before = CLOSE - timedelta(minutes=31)
    assert discovery_idle_sleep_seconds(one_minute_before) == 60.0


def test_reconciliation_initializes_new_database_without_false_error(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    fields = rth_handoff_reconcile_heartbeat_fields(
        datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        conn,
        version="abc1234",
        run_id="startup",
    )
    assert fields == {"rth_handoff_status": "current", "latest_rth_handoff": None}


def test_selection_unions_scheduled_symbol_and_filters_outside_universe():
    selection = select_rth_symbols(
        _screen(), {"GPRO", "SCHEDULED"}, {"SCHEDULED"}
    )
    assert [row.symbol for row in selection.symbols] == ["GPRO", "SCHEDULED"]
    assert selection.provider_screen_rows == 3
    assert selection.provider_screen_unique_symbols == 2
    assert selection.scheduled_symbols == 1
    assert selection.excluded_symbols == 1
    gpro = selection.symbols[0]
    assert gpro.sources == ("market_gainer", "most_active_volume")
    assert gpro.ranks == (("market_gainer", 1), ("most_active_volume", 1))
    assert selection.symbols[1].sources == ("scheduled_earnings",)


def test_gpro_shaped_move_qualifies_only_on_completed_persistent_bars():
    result = evaluate_rth_momentum(
        "GPRO", SESSION, _rth_bars(), _daily(),
        session_open=OPEN, session_close=CLOSE, now=NOW,
    )
    assert result.outcome == OUTCOME_CANDIDATE
    assert result.prior_close == 1.0
    assert result.move_pct == 50.0
    assert result.direction == "up"
    assert result.persistence_bars == 2
    assert result.cumulative_notional > 1_000_000
    assert result.data_age_seconds == 0


def test_exact_eight_percent_boundary_qualifies():
    result = evaluate_rth_momentum(
        "BOUNDARY", SESSION, _rth_bars("BOUNDARY", move=8.0), _daily("BOUNDARY"),
        session_open=OPEN, session_close=CLOSE, now=NOW,
    )
    assert result.outcome == OUTCOME_CANDIDATE
    assert result.move_pct == pytest.approx(8.0)


def test_gap_stale_and_missing_prior_close_fail_closed():
    gap = _rth_bars()
    gap.pop(-2)
    assert evaluate_rth_momentum(
        "GPRO", SESSION, gap, _daily(),
        session_open=OPEN, session_close=CLOSE, now=NOW,
    ).outcome == OUTCOME_BAR_GAP

    stale_now = NOW
    stale = [bar for bar in _rth_bars() if bar.ts <= NOW - timedelta(minutes=15)]
    assert evaluate_rth_momentum(
        "GPRO", SESSION, stale, _daily(),
        session_open=OPEN, session_close=CLOSE, now=stale_now,
    ).outcome == OUTCOME_STALE

    assert evaluate_rth_momentum(
        "GPRO", SESSION, _rth_bars(), [],
        session_open=OPEN, session_close=CLOSE, now=NOW,
    ).outcome == OUTCOME_NO_PRIOR_CLOSE


def test_out_of_order_and_naive_rth_timestamps_fail_closed():
    out_of_order = _rth_bars()
    out_of_order[-2], out_of_order[-1] = out_of_order[-1], out_of_order[-2]
    naive = _rth_bars()
    naive[-1] = replace(naive[-1], ts=naive[-1].ts.replace(tzinfo=None))

    assert evaluate_rth_momentum(
        "GPRO", SESSION, out_of_order, _daily(),
        session_open=OPEN, session_close=CLOSE, now=NOW,
    ).outcome == "INVALID_DATA"
    assert evaluate_rth_momentum(
        "GPRO", SESSION, naive, _daily(),
        session_open=OPEN, session_close=CLOSE, now=NOW,
    ).outcome == "INVALID_DATA"


def test_tick_conserves_rows_persists_candidate_and_deduplicates(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    first, selection, evaluations = _run(conn, scheduled=("SCHEDULED",))
    second_now = NOW + timedelta(minutes=1)
    second, _, _ = _run(conn, now=second_now, scheduled=("SCHEDULED",))

    assert first.selected_symbols == first.evaluated_symbols == len(selection.symbols) == 2
    assert first.candidate_observations == first.new_candidates == 1
    assert first.invariant_ok is True
    assert second.candidate_observations == 1
    assert second.new_candidates == 0
    assert [row.outcome for row in evaluations] == [
        OUTCOME_CANDIDATE,
        OUTCOME_NO_INTRADAY_BARS_RETURNED,
    ]
    assert conn.execute("SELECT COUNT(*) FROM rth_momentum_ticks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM rth_momentum_observations").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM rth_momentum_candidates").fetchone()[0] == 1
    handoff = conn.execute(
        "SELECT state FROM rth_postmarket_handoffs ORDER BY handoff_id"
    ).fetchall()
    assert handoff == [(HANDOFF_RTH_QUALIFIED,)]
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_successful_provider_omissions_are_explicit_not_fetch_errors(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    updated = NOW - timedelta(seconds=20)
    screen = _screen(updated=updated)
    result, _, evaluations = run_rth_momentum_tick(
        conn,
        active_universe={"GPRO", "SCHEDULED"},
        scheduled_earnings={"SCHEDULED"},
        now=NOW,
        run_id="run",
        code_version="abc1234",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        intraday_fetch=lambda symbols, session: {"GPRO": _rth_bars()},
        daily_fetch=lambda symbols: {},
    )
    assert {row.symbol: row.outcome for row in evaluations} == {
        "GPRO": OUTCOME_NO_DAILY_BASELINE_RETURNED,
        "SCHEDULED": OUTCOME_NO_INTRADAY_BARS_RETURNED,
    }
    assert result.error_count == 0
    assert result.invariant_ok is True


def test_handoff_links_same_direction_postmarket_candidate_idempotently(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _run(conn)
    rth_candidate = conn.execute(
        "SELECT candidate_id FROM rth_momentum_candidates"
    ).fetchone()[0]
    cursor = conn.execute(
        """
        INSERT INTO postmarket_discovery_candidates
          (session,symbol,event_date,direction,discovery_version,first_detected_at,
           bar_open_ts_utc,rth_close,close,move_pct,cumulative_volume,
           cumulative_notional,sources_json,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), "GPRO", SESSION.isoformat(), "up", 2,
            (CLOSE + timedelta(minutes=10)).isoformat(),
            (CLOSE + timedelta(minutes=5)).isoformat(), 1.5, 2.0, 33.33,
            10_000_000, 20_000_000.0, '["market_gainer"]', "sip", "alpaca",
            "5Min", "abc1234", "pm-run",
        ),
    )
    pm_candidate = int(cursor.lastrowid)
    conn.commit()
    first = reconcile_rth_postmarket_handoffs(
        conn, session=SESSION, now=CLOSE + timedelta(minutes=10),
        postmarket_end=datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc),
        code_version="abc1234", run_id="reconcile",
    )
    second = reconcile_rth_postmarket_handoffs(
        conn, session=SESSION, now=CLOSE + timedelta(minutes=11),
        postmarket_end=datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc),
        code_version="abc1234", run_id="rerun",
    )
    assert first.postmarket_links_written == 1
    assert second.postmarket_links_written == 0
    assert conn.execute(
        """
        SELECT rth_candidate_id,postmarket_candidate_id,state
        FROM rth_postmarket_handoffs WHERE state=?
        """,
        (HANDOFF_POSTMARKET_QUALIFIED,),
    ).fetchone() == (rth_candidate, pm_candidate, HANDOFF_POSTMARKET_QUALIFIED)


def test_handoff_closes_without_qualification_only_after_window(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _run(conn)
    end = datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc)
    before = reconcile_rth_postmarket_handoffs(
        conn, session=SESSION, now=end, postmarket_end=end,
        code_version="abc1234", run_id="before",
    )
    after = reconcile_rth_postmarket_handoffs(
        conn, session=SESSION, now=end + timedelta(seconds=1), postmarket_end=end,
        code_version="abc1234", run_id="after",
    )
    assert before.terminal_not_qualified_written == 0
    assert after.terminal_not_qualified_written == 1
    assert conn.execute(
        "SELECT state FROM rth_postmarket_handoffs ORDER BY handoff_id DESC LIMIT 1"
    ).fetchone()[0] == HANDOFF_POSTMARKET_NOT_QUALIFIED


def test_all_rth_tables_are_append_only(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _run(conn)
    for table in (
        "rth_momentum_ticks",
        "rth_momentum_observations",
        "rth_momentum_candidates",
        "rth_postmarket_handoffs",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {table} SET rowid=rowid")
        conn.rollback()
