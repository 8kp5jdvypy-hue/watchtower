"""Market-wide postmarket discovery conservation and isolation tests."""
from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Bar
from tradebot.marketdata import MarketScreenEntry, MarketWideScreen
from tradebot.postmarket_discovery import (
    FULL_UNIVERSE_SWEEP_SOURCE,
    connect,
    plan_tick_schedule,
    plan_universe_sweep,
    select_discovery_symbols,
)
from tradebot.postmarket_discovery_shadow import (
    active_poll_sleep_seconds,
    discovery_enabled,
    run_discovery_tick,
)


SESSION = date(2026, 8, 27)
CLOSE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
NOW = CLOSE + timedelta(minutes=10)
UPDATED = CLOSE + timedelta(minutes=9)


def _entry(symbol, source, rank, *, move=None):
    mover = source in {"market_gainer", "market_loser"}
    return MarketScreenEntry(
        symbol=symbol,
        source=source,
        rank=rank,
        source_updated_at=UPDATED,
        move_pct=move,
        price=100.0 if mover else None,
        volume=None if mover else 1_000_000.0,
        trade_count=None if mover else 10_000.0,
    )


def _screen():
    return MarketWideScreen(
        entries=(
            _entry("MOVER", "market_gainer", 1, move=10.0),
            _entry("MOVER", "most_active_volume", 1),
            _entry("QUIET", "most_active_trades", 1),
            _entry("BROKEN", "market_loser", 1, move=-9.0),
            _entry("OUTSIDE", "market_gainer", 2, move=8.5),
        ),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", UPDATED),
            ("most_actives_volume", UPDATED),
            ("most_actives_trades", UPDATED),
        ),
    )


def _bar(symbol, ts, close, volume=10_000):
    return Bar(symbol, ts, close, close, close, close, volume)


def _bars(symbol, closes):
    return [
        _bar(symbol, CLOSE - timedelta(minutes=5), 100),
        *[
            _bar(symbol, CLOSE + timedelta(minutes=5 * index), close)
            for index, close in enumerate(closes)
        ],
    ]


def test_selection_deduplicates_sources_filters_universe_and_marks_earnings():
    selection = select_discovery_symbols(
        _screen(), {"MOVER", "QUIET", "BROKEN", "UNSEEN"}, {"MOVER"}
    )

    assert selection.universe_symbols == 4
    assert selection.screen_rows == 5
    assert selection.screen_unique_symbols == 4
    assert selection.excluded_symbols == 1
    assert selection.not_returned_symbols == 1
    assert [row.symbol for row in selection.symbols] == ["BROKEN", "MOVER", "QUIET"]
    mover = next(row for row in selection.symbols if row.symbol == "MOVER")
    assert mover.sources == ("market_gainer", "most_active_volume", "scheduled_earnings")
    assert mover.screen_move_pct == 10.0


def test_full_universe_sweep_covers_every_symbol_once_per_five_tick_cycle():
    universe = {f"S{i:05d}" for i in range(13_102)}
    shards = [
        plan_universe_sweep(
            universe,
            scheduled_tick_utc=CLOSE + timedelta(minutes=minute),
            session_close=CLOSE,
        )
        for minute in range(5)
    ]

    assert [shard.shard_index for shard in shards] == list(range(5))
    assert {shard.shard_count for shard in shards} == {5}
    assert {shard.shard_size for shard in shards} == {2621}
    assert max(len(shard.symbols) for shard in shards) == 2621
    assert sum(len(shard.symbols) for shard in shards) == len(universe)
    assert set().union(*(set(shard.symbols) for shard in shards)) == universe
    assert sum(
        len(set(left.symbols) & set(right.symbols))
        for left in shards
        for right in shards
        if left.shard_index < right.shard_index
    ) == 0
    assert len({shard.universe_sha256 for shard in shards}) == 1


def test_selection_unions_sweep_provenance_and_conserves_overlap():
    active = {"BROKEN", "MOVER", "QUIET", "SWEEP", "UNSEEN"}
    sweep = plan_universe_sweep(
        active,
        scheduled_tick_utc=CLOSE + timedelta(minutes=3),
        session_close=CLOSE,
    )
    assert sweep.symbols == ("SWEEP",)

    selection = select_discovery_symbols(_screen(), active, set(), sweep)

    assert selection.provider_screen_rows == 5
    assert selection.provider_screen_unique_symbols == 4
    assert selection.screen_rows == 6
    assert selection.screen_unique_symbols == 5
    assert selection.sweep_overlap_symbols == 0
    assert selection.excluded_symbols == 1
    assert selection.not_returned_symbols == 1
    row = next(item for item in selection.symbols if item.symbol == "SWEEP")
    assert row.sources == (FULL_UNIVERSE_SWEEP_SOURCE,)
    assert row.ranks == ()
    assert row.screen_move_pct is None
    assert row.screen_evidence == (
        {
            "source": FULL_UNIVERSE_SWEEP_SOURCE,
            "scheduled_tick_utc": sweep.scheduled_tick_utc.isoformat(),
            "universe_sha256": sweep.universe_sha256,
            "universe_position": 3,
            "cycle_ticks": 5,
            "shard_index": 3,
            "shard_count": 5,
            "shard_size": 1,
        },
    )


def test_tick_fetches_sweep_only_window_and_persists_candidate(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    active = {"BROKEN", "MOVER", "QUIET", "SWEEP", "UNSEEN"}
    tick_now = CLOSE + timedelta(minutes=13)
    screen_updated = tick_now - timedelta(minutes=1)
    base_screen = _screen()
    screen = replace(
        base_screen,
        entries=tuple(
            replace(entry, source_updated_at=screen_updated)
            for entry in base_screen.entries
        ),
        source_updates=tuple(
            (source, screen_updated) for source, _ in base_screen.source_updates
        ),
    )
    bounded_requested = []
    sweep_requested = []

    def bounded_fetch(symbols, session):
        bounded_requested.extend(symbols)
        return {
            symbol: _bars(symbol, [101, 102])
            for symbol in symbols
        }

    def sweep_fetch(symbols, start, end):
        sweep_requested.extend(symbols)
        assert start == CLOSE - timedelta(minutes=5)
        assert end == tick_now
        return {"SWEEP": _bars("SWEEP", [109, 110])}

    result, selection, _ = run_discovery_tick(
        conn,
        active_universe=active,
        scheduled_earnings=set(),
        now=tick_now,
        run_id="sweep-run",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        bars_fetch=bounded_fetch,
        sweep_bars_fetch=sweep_fetch,
        sweep_cycle_ticks=5,
    )

    assert set(bounded_requested) == {"BROKEN", "MOVER", "QUIET"}
    assert sweep_requested == ["SWEEP"]
    assert result.sweep_shard_index == 3
    assert result.sweep_shard_count == 5
    assert result.sweep_shard_symbols == 1
    assert result.discovered_symbols == result.evaluated_symbols == 4
    assert result.candidate_observations == result.new_candidates == 1
    assert result.error_count == 0
    assert selection.not_returned_symbols == 1
    assert conn.execute(
        "SELECT sources_json FROM postmarket_discovery_candidates WHERE symbol='SWEEP'"
    ).fetchone()[0] == '["full_universe_sweep"]'
    tick = conn.execute(
        """
        SELECT discovery_version,discovery_scope,provider_screen_rows,
               provider_screen_unique_symbols,sweep_cycle_ticks,
               sweep_shard_index,sweep_shard_count,sweep_shard_size,
               sweep_shard_symbols,sweep_overlap_symbols,invariant_ok
        FROM postmarket_discovery_ticks WHERE tick_id=?
        """,
        (result.tick_id,),
    ).fetchone()
    assert tick == (
        2,
        "alpaca_top_movers_actives_plus_full_universe_sweep",
        5,
        4,
        5,
        3,
        5,
        1,
        1,
        0,
        1,
    )


def test_sweep_fetch_outage_is_explicit_and_does_not_hide_bounded_candidate(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    active = {"BROKEN", "MOVER", "QUIET", "SWEEP", "UNSEEN"}
    tick_now = CLOSE + timedelta(minutes=13)
    screen_updated = tick_now - timedelta(minutes=1)
    base_screen = _screen()
    screen = replace(
        base_screen,
        entries=tuple(
            replace(entry, source_updated_at=screen_updated)
            for entry in base_screen.entries
        ),
        source_updates=tuple(
            (source, screen_updated) for source, _ in base_screen.source_updates
        ),
    )

    def fail_sweep(symbols, start, end):
        assert symbols == ["SWEEP"]
        raise RuntimeError("injected sweep provider outage")

    result, _, evaluations = run_discovery_tick(
        conn,
        active_universe=active,
        scheduled_earnings=set(),
        now=tick_now,
        run_id="sweep-outage",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        bars_fetch=lambda symbols, session: {
            symbol: _bars(symbol, [109, 110] if symbol == "MOVER" else [101, 102])
            for symbol in symbols
        },
        sweep_bars_fetch=fail_sweep,
        sweep_cycle_ticks=5,
    )

    outcomes = {row.symbol: row.outcome for row in evaluations}
    assert outcomes["MOVER"] == "CANDIDATE"
    assert outcomes["SWEEP"] == "FETCH_ERROR"
    assert result.fetched_symbols == 3
    assert result.evaluated_symbols == 4
    assert result.candidate_observations == result.new_candidates == 1
    assert result.error_count == 1
    assert conn.execute(
        "SELECT symbol FROM postmarket_discovery_candidates ORDER BY symbol"
    ).fetchall() == [("MOVER",)]
    sweep_error = conn.execute(
        "SELECT outcome,reason FROM postmarket_discovery_observations "
        "WHERE tick_id=? AND symbol='SWEEP'",
        (result.tick_id,),
    ).fetchone()
    assert sweep_error[0] == "FETCH_ERROR"
    assert "injected sweep provider outage" in sweep_error[1]


def test_sweep_omitted_symbol_is_no_bars_not_fetch_error(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    active = {"BROKEN", "MOVER", "QUIET", "SWEEP", "UNSEEN"}
    tick_now = CLOSE + timedelta(minutes=13)
    screen_updated = tick_now - timedelta(minutes=1)
    base_screen = _screen()
    screen = replace(
        base_screen,
        entries=tuple(
            replace(entry, source_updated_at=screen_updated)
            for entry in base_screen.entries
        ),
        source_updates=tuple(
            (source, screen_updated) for source, _ in base_screen.source_updates
        ),
    )

    result, _, evaluations = run_discovery_tick(
        conn,
        active_universe=active,
        scheduled_earnings=set(),
        now=tick_now,
        run_id="sweep-no-bars",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        bars_fetch=lambda symbols, session: {
            symbol: _bars(symbol, [101, 102]) for symbol in symbols
        },
        sweep_bars_fetch=lambda symbols, start, end: {},
        sweep_cycle_ticks=5,
    )

    outcomes = {row.symbol: row.outcome for row in evaluations}
    assert outcomes["SWEEP"] == "NO_BARS_RETURNED"
    assert result.discovered_symbols == result.evaluated_symbols == 4
    assert result.fetched_symbols == 3
    assert result.error_count == 0
    assert conn.execute(
        "SELECT reason FROM postmarket_discovery_observations "
        "WHERE tick_id=? AND symbol='SWEEP'",
        (result.tick_id,),
    ).fetchone()[0] == "no bars returned for full-universe sweep window"


def test_tick_bulk_fetches_bounded_union_and_conserves_missing_symbol(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    requested = []

    def bars_fetch(symbols, session):
        requested.extend(symbols)
        assert session == SESSION
        return {
            "MOVER": _bars("MOVER", [109, 110]),
            "QUIET": _bars("QUIET", [101, 102]),
        }

    result, selection, evaluations = run_discovery_tick(
        conn,
        active_universe={"MOVER", "QUIET", "BROKEN", "UNSEEN"},
        scheduled_earnings={"MOVER"},
        now=NOW,
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: _screen(),
        bars_fetch=bars_fetch,
    )

    assert set(requested) == {"MOVER", "QUIET", "BROKEN"}
    assert result.universe_symbols == 4
    assert result.screen_rows == 5
    assert result.discovered_symbols == result.evaluated_symbols == 3
    assert result.fetched_symbols == 2
    assert result.candidate_observations == result.new_candidates == 1
    assert result.error_count == 1
    assert len(selection.symbols) == len(evaluations) == 3
    tick = conn.execute(
        """
        SELECT universe_symbols,screen_rows,screen_unique_symbols,excluded_symbols,
               discovered_symbols,not_returned_symbols,fetched_symbols,
               evaluated_symbols,candidate_observations,new_candidates,
               invariant_ok,error_count,data_feed
        FROM postmarket_discovery_ticks WHERE tick_id=?
        """,
        (result.tick_id,),
    ).fetchone()
    assert tick == (4, 5, 4, 1, 3, 1, 2, 3, 1, 1, 1, 1, "sip")
    outcomes = dict(
        conn.execute(
            "SELECT symbol,outcome FROM postmarket_discovery_observations WHERE tick_id=?",
            (result.tick_id,),
        ).fetchall()
    )
    assert outcomes == {
        "BROKEN": "FETCH_ERROR",
        "MOVER": "CANDIDATE",
        "QUIET": "BELOW_MOVE",
    }
    sources = conn.execute(
        "SELECT sources_json,screen_evidence_json,open,high,low,close FROM "
        "postmarket_discovery_observations "
        "WHERE tick_id=? AND symbol='MOVER'",
        (result.tick_id,),
    ).fetchone()
    assert json.loads(sources[0]) == [
        "market_gainer",
        "most_active_volume",
        "scheduled_earnings",
    ]
    evidence = json.loads(sources[1])
    assert evidence[0] == {
        "move_pct": 10.0,
        "price": 100.0,
        "rank": 1,
        "source": "market_gainer",
        "source_updated_at": UPDATED.isoformat(),
        "trade_count": None,
        "volume": None,
    }
    assert sources[2:] == (110.0, 110.0, 110.0, 110.0)


def test_tick_records_schedule_stage_and_persistence_timing(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    values = iter((0.0, 0.100, 0.150, 0.400, 0.800))

    result, _, _ = run_discovery_tick(
        conn,
        active_universe={"MOVER", "QUIET", "BROKEN", "UNSEEN"},
        scheduled_earnings={"MOVER"},
        now=NOW,
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: _screen(),
        bars_fetch=lambda symbols, session: {
            "MOVER": _bars("MOVER", [109, 110]),
            "QUIET": _bars("QUIET", [101, 102]),
        },
        clock=lambda: next(values),
    )

    assert result.scheduled_lag_ms == 0
    assert result.missed_cycles == 10
    assert result.screen_latency_ms == 100
    assert result.selection_latency_ms == 50
    assert result.bar_fetch_latency_ms == 250
    assert result.evaluation_latency_ms == 400
    assert result.persistence_span_max_seconds == 300
    assert result.latency_ms == 800
    assert conn.execute(
        """
        SELECT scheduled_tick_utc,scheduled_lag_ms,missed_cycles,
               screen_latency_ms,selection_latency_ms,bar_fetch_latency_ms,
               evaluation_latency_ms,persistence_observations,
               persistence_span_avg_seconds,persistence_span_max_seconds,
               total_latency_ms
        FROM postmarket_discovery_timing WHERE tick_id=?
        """,
        (result.tick_id,),
    ).fetchone() == (
        NOW.isoformat(),
        0,
        10,
        100,
        50,
        250,
        400,
        1,
        300.0,
        300.0,
        800,
    )


def test_schedule_planner_attributes_startup_and_between_tick_misses(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    first = plan_tick_schedule(
        conn,
        session=SESSION,
        session_close=CLOSE,
        actual_start=CLOSE + timedelta(minutes=10, milliseconds=2500),
        interval_seconds=60,
    )
    assert first.scheduled_tick_utc == CLOSE + timedelta(minutes=10)
    assert first.scheduled_lag_ms == 2500
    assert first.missed_cycles == 10

    conn.execute(
        """
        INSERT INTO postmarket_discovery_timing
            (tick_id,session,scheduled_tick_utc,actual_start_utc,completed_utc,
             scheduled_lag_ms,missed_cycles,screen_latency_ms,
             selection_latency_ms,bar_fetch_latency_ms,evaluation_latency_ms,
             persistence_observations,persistence_span_avg_seconds,
             persistence_span_max_seconds,total_latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            99,
            SESSION.isoformat(),
            first.scheduled_tick_utc.isoformat(),
            (CLOSE + timedelta(minutes=10, milliseconds=2500)).isoformat(),
            (CLOSE + timedelta(minutes=10, milliseconds=2600)).isoformat(),
            2500,
            10,
            20,
            5,
            50,
            25,
            0,
            None,
            None,
            100,
        ),
    )
    second = plan_tick_schedule(
        conn,
        session=SESSION,
        session_close=CLOSE,
        actual_start=CLOSE + timedelta(minutes=13, milliseconds=500),
        interval_seconds=60,
    )
    assert second.scheduled_tick_utc == CLOSE + timedelta(minutes=13)
    assert second.scheduled_lag_ms == 500
    assert second.missed_cycles == 2


def test_active_poll_sleep_stays_on_exchange_close_anchored_grid():
    assert active_poll_sleep_seconds(
        CLOSE + timedelta(seconds=2.5), session_close=CLOSE
    ) == 57.5
    assert active_poll_sleep_seconds(
        CLOSE + timedelta(minutes=17, seconds=59.9), session_close=CLOSE
    ) == pytest.approx(0.1)
    assert active_poll_sleep_seconds(
        CLOSE + timedelta(minutes=18, seconds=2), session_close=CLOSE
    ) == 58


def test_candidate_is_deduplicated_across_ticks(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    kwargs = dict(
        active_universe={"MOVER"},
        scheduled_earnings=set(),
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: MarketWideScreen(
            entries=(_entry("MOVER", "market_gainer", 1, move=10),),
            requested_top_n=50,
            provider="alpaca",
            feed="sip",
            endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
            source_updates=(
                ("market_movers", UPDATED),
                ("most_actives_volume", UPDATED),
                ("most_actives_trades", UPDATED),
            ),
        ),
        bars_fetch=lambda symbols, session: {"MOVER": _bars("MOVER", [109, 110])},
    )
    first, _, _ = run_discovery_tick(conn, now=NOW, **kwargs)
    second, _, _ = run_discovery_tick(conn, now=NOW + timedelta(minutes=1), **kwargs)

    assert first.new_candidates == 1
    assert second.new_candidates == 0
    assert conn.execute("SELECT COUNT(*) FROM postmarket_discovery_candidates").fetchone()[0] == 1


def test_discovery_tables_are_append_only(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    run_discovery_tick(
        conn,
        active_universe={"MOVER"},
        scheduled_earnings=set(),
        now=NOW,
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: MarketWideScreen(
            entries=(_entry("MOVER", "market_gainer", 1, move=10),),
            requested_top_n=50,
            provider="alpaca",
            feed="sip",
            endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
            source_updates=(
                ("market_movers", UPDATED),
                ("most_actives_volume", UPDATED),
                ("most_actives_trades", UPDATED),
            ),
        ),
        bars_fetch=lambda symbols, session: {"MOVER": _bars("MOVER", [109, 110])},
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_discovery_ticks SET error_count=99")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM postmarket_discovery_observations")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM postmarket_discovery_candidates")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_discovery_timing SET missed_cycles=99")


def test_discovery_schema_coexists_with_scheduled_shadow_schema(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert {
        "postmarket_ticks",
        "postmarket_observations",
        "postmarket_candidates",
        "postmarket_discovery_ticks",
        "postmarket_discovery_observations",
        "postmarket_discovery_candidates",
        "postmarket_discovery_timing",
    } <= tables


def test_connect_adds_sweep_columns_to_existing_discovery_tick_ledger(tmp_path):
    db_path = tmp_path / "legacy-shadow.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE postmarket_discovery_ticks (
            tick_id INTEGER PRIMARY KEY,
            session TEXT NOT NULL,
            tick_utc TEXT NOT NULL
        )
        """
    )
    legacy.close()

    migrated = connect(db_path)
    columns = {
        row[1] for row in migrated.execute(
            "PRAGMA table_info(postmarket_discovery_ticks)"
        )
    }

    assert {
        "provider_screen_rows",
        "provider_screen_unique_symbols",
        "sweep_universe_sha256",
        "sweep_cycle_ticks",
        "sweep_shard_index",
        "sweep_shard_count",
        "sweep_shard_size",
        "sweep_shard_symbols",
        "sweep_overlap_symbols",
    } <= columns


def test_non_sip_screen_is_rejected_before_persistence(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    bad = MarketWideScreen(
        entries=(), requested_top_n=50, provider="alpaca", feed="iex", endpoints=(),
        source_updates=(),
    )
    with pytest.raises(ValueError, match="must use SIP"):
        run_discovery_tick(
            conn,
            active_universe=set(),
            scheduled_earnings=set(),
            now=NOW,
            run_id="run-1",
            version="abc123",
            data_feed="sip",
            screen_fetch=lambda top: bad,
            bars_fetch=lambda symbols, session: {},
        )
    assert conn.execute("SELECT COUNT(*) FROM postmarket_discovery_ticks").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("source_updates", "message"),
    [
        (
            (
                ("market_movers", UPDATED - timedelta(minutes=5)),
                ("most_actives_volume", UPDATED),
                ("most_actives_trades", UPDATED),
            ),
            "stale",
        ),
        (
            (
                ("market_movers", NOW + timedelta(seconds=2)),
                ("most_actives_volume", UPDATED),
                ("most_actives_trades", UPDATED),
            ),
            "future",
        ),
    ],
)
def test_stale_or_future_screener_timestamp_fails_before_bar_fetch(
    tmp_path, source_updates, message,
):
    conn = connect(tmp_path / "shadow.db")
    screen = MarketWideScreen(
        entries=(_entry("MOVER", "market_gainer", 1, move=10),),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=source_updates,
    )
    called = False

    def bars_fetch(symbols, session):
        nonlocal called
        called = True
        return {}

    with pytest.raises(ValueError, match=message):
        run_discovery_tick(
            conn,
            active_universe={"MOVER"},
            scheduled_earnings=set(),
            now=NOW,
            run_id="run-1",
            version="abc123",
            data_feed="sip",
            screen_fetch=lambda top: screen,
            bars_fetch=bars_fetch,
        )
    assert called is False
    assert conn.execute("SELECT COUNT(*) FROM postmarket_discovery_ticks").fetchone()[0] == 0


def test_screener_timestamp_uses_clock_captured_after_fetch(tmp_path):
    """A response produced during the request is not future-dated.

    The tick/session clock is intentionally earlier than the provider's source
    timestamp. The validation clock advances while ``screen_fetch`` runs, which
    models the live request that exposed this race in production.
    """
    conn = connect(tmp_path / "shadow.db")
    response_updated = NOW + timedelta(milliseconds=400)
    screen = MarketWideScreen(
        entries=(),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", response_updated),
            ("most_actives_volume", response_updated),
            ("most_actives_trades", response_updated),
        ),
    )
    request_complete = False

    def screen_fetch(top):
        nonlocal request_complete
        request_complete = True
        return screen

    def validation_now():
        assert request_complete is True
        return NOW + timedelta(milliseconds=500)

    result, _, _ = run_discovery_tick(
        conn,
        active_universe=set(),
        scheduled_earnings=set(),
        now=NOW,
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        screen_fetch=screen_fetch,
        bars_fetch=lambda symbols, session: {},
        validation_now_fn=validation_now,
    )

    assert result.error_count == 0
    assert conn.execute("SELECT COUNT(*) FROM postmarket_discovery_ticks").fetchone()[0] == 1


def test_bounded_provider_clock_skew_does_not_drop_a_live_tick(tmp_path):
    """Sub-second provider/host skew is not future market evidence.

    The screener is only a bounded selector; completed-bar evaluation remains
    the price-truth boundary.  This reproduces the 2026-09-01 production miss
    where one source timestamp landed just ahead of the post-fetch host clock.
    """
    conn = connect(tmp_path / "shadow.db")
    response_updated = NOW + timedelta(milliseconds=750)
    screen = MarketWideScreen(
        entries=(),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", response_updated),
            ("most_actives_volume", response_updated),
            ("most_actives_trades", response_updated),
        ),
    )

    result, _, _ = run_discovery_tick(
        conn,
        active_universe=set(),
        scheduled_earnings=set(),
        now=NOW,
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        bars_fetch=lambda symbols, session: {},
        validation_now_fn=lambda: NOW + timedelta(milliseconds=250),
    )

    assert result.error_count == 0
    assert conn.execute("SELECT COUNT(*) FROM postmarket_discovery_ticks").fetchone()[0] == 1


def test_genuinely_future_screener_timestamp_still_fails_post_fetch(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    future = NOW + timedelta(seconds=3)
    screen = MarketWideScreen(
        entries=(),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", future),
            ("most_actives_volume", UPDATED),
            ("most_actives_trades", UPDATED),
        ),
    )

    with pytest.raises(ValueError, match="future"):
        run_discovery_tick(
            conn,
            active_universe=set(),
            scheduled_earnings=set(),
            now=NOW,
            run_id="run-1",
            version="abc123",
            data_feed="sip",
            screen_fetch=lambda top: screen,
            bars_fetch=lambda symbols, session: {},
            validation_now_fn=lambda: NOW + timedelta(seconds=1),
        )

    assert conn.execute("SELECT COUNT(*) FROM postmarket_discovery_ticks").fetchone()[0] == 0


def test_same_session_new_asset_is_observed_but_not_promoted(tmp_path):
    """A new listing/ticker/ADS basis stays visible without becoming a signal."""
    conn = connect(tmp_path / "shadow.db")
    screen = MarketWideScreen(
        entries=(_entry("MOVER", "market_gainer", 1, move=900),),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=("market_movers", "most_actives_volume", "most_actives_trades"),
        source_updates=(
            ("market_movers", UPDATED),
            ("most_actives_volume", UPDATED),
            ("most_actives_trades", UPDATED),
        ),
    )

    result, _, evaluations = run_discovery_tick(
        conn,
        active_universe={"MOVER"},
        scheduled_earnings=set(),
        now=NOW,
        run_id="new-listing",
        version="abc123",
        data_feed="sip",
        screen_fetch=lambda top: screen,
        bars_fetch=lambda symbols, session: {"MOVER": _bars("MOVER", [900, 900])},
        identity_quarantine_symbols={"MOVER"},
    )

    assert evaluations[0].outcome == "IDENTITY_UNVERIFIED"
    assert "first observed during this session" in evaluations[0].reason
    assert result.identity_quarantined == 1
    assert result.candidate_observations == result.new_candidates == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_discovery_candidates"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT outcome FROM postmarket_discovery_observations WHERE symbol='MOVER'"
    ).fetchone()[0] == "IDENTITY_UNVERIFIED"


def test_screen_and_bar_feed_must_match(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    with pytest.raises(ValueError, match="feed mismatch"):
        run_discovery_tick(
            conn,
            active_universe={"MOVER"},
            scheduled_earnings=set(),
            now=NOW,
            run_id="run-1",
            version="abc123",
            data_feed="iex",
            screen_fetch=lambda top: _screen(),
            bars_fetch=lambda symbols, session: {},
        )


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (replace(_entry("MOVER", "market_gainer", 1, move=10), symbol=" mover "), "canonical"),
        (replace(_entry("MOVER", "market_gainer", 1, move=10), move_pct=float("nan")), "non-finite"),
        (replace(_entry("MOVER", "market_gainer", 1, move=10), price=None), "missing.*price"),
    ],
)
def test_malformed_screener_rows_fail_before_bar_fetch(tmp_path, entry, message):
    conn = connect(tmp_path / "shadow.db")
    base = _screen()
    screen = replace(base, entries=(entry,))
    with pytest.raises(ValueError, match=message):
        run_discovery_tick(
            conn,
            active_universe={"MOVER"},
            scheduled_earnings=set(),
            now=NOW,
            run_id="run-1",
            version="abc123",
            data_feed="sip",
            screen_fetch=lambda top: screen,
            bars_fetch=lambda symbols, session: pytest.fail("bar fetch must not run"),
        )


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_discovery_kill_switch_true_values(raw):
    assert discovery_enabled(raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off", ""])
def test_discovery_kill_switch_false_values(raw):
    assert discovery_enabled(raw) is False


def test_discovery_kill_switch_rejects_ambiguous_value():
    with pytest.raises(ValueError, match="POSTMARKET_DISCOVERY_ENABLED"):
        discovery_enabled("maybe")


def test_service_has_no_delivery_or_order_dependency():
    source = (
        Path(__file__).parents[1] / "tradebot" / "postmarket_discovery_shadow.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = ("tradebot.alerts", "tradebot.telegram_bot", "tradebot.order", "tradebot.broker")
    assert not any(module.startswith(forbidden) for module in imports)


def test_live_service_injects_post_fetch_validation_clock():
    source = (
        Path(__file__).parents[1] / "tradebot" / "postmarket_discovery_shadow.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    tick_call = next(
        node for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_discovery_tick"
    )
    validation_clock = next(
        keyword.value
        for keyword in tick_call.keywords
        if keyword.arg == "validation_now_fn"
    )
    assert isinstance(validation_clock, ast.Name)
    assert validation_clock.id == "_utc_now"


def test_compose_wires_independent_default_off_discovery_service():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "command: python -m tradebot.postmarket_discovery_shadow" in compose
    assert "POSTMARKET_DISCOVERY_ENABLED: ${POSTMARKET_DISCOVERY_ENABLED:-0}" in compose
    assert "tradebot.postmarket_discovery_health" in compose
