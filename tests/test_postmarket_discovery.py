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
from tradebot.postmarket_discovery import connect, select_discovery_symbols
from tradebot.postmarket_discovery_shadow import discovery_enabled, run_discovery_tick


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
    } <= tables


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
                ("market_movers", NOW + timedelta(seconds=1)),
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


def test_compose_wires_independent_default_off_discovery_service():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "command: python -m tradebot.postmarket_discovery_shadow" in compose
    assert "POSTMARKET_DISCOVERY_ENABLED: ${POSTMARKET_DISCOVERY_ENABLED:-0}" in compose
    assert "tradebot.postmarket_discovery_health" in compose
