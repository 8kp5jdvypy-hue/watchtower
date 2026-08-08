"""Tests for tradebot.universe — discovering, diffing, and storing the
scan-eligible market universe from a (faked) asset catalog fetch."""
from __future__ import annotations

from datetime import datetime, timezone

from tradebot.marketdata import AssetInfo
from tradebot.universe import active_symbols, asset_count, connect, refresh_universe

NOW = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)


def _asset(symbol, exchange="NASDAQ", tradable=True, options_enabled=True, overnight=None) -> AssetInfo:
    return AssetInfo(
        symbol=symbol, exchange=exchange, name=f"{symbol} Inc.", tradable=tradable,
        options_enabled=options_enabled, overnight_eligible=overnight, attributes=(),
    )


def test_first_refresh_adds_every_fetched_symbol():
    conn = connect(":memory:")
    result = refresh_universe(conn, lambda: [_asset("AAPL"), _asset("NVDA")], NOW)

    assert result.added == ("AAPL", "NVDA")
    assert result.reactivated == ()
    assert result.delisted == ()
    assert result.total_active == 2
    assert active_symbols(conn) == ["AAPL", "NVDA"]


def test_otc_symbols_are_stored_but_excluded_from_active_by_default():
    conn = connect(":memory:")
    refresh_universe(conn, lambda: [_asset("AAPL"), _asset("QVCAQ", exchange="OTC")], NOW)

    assert active_symbols(conn) == ["AAPL"]
    assert asset_count(conn) == 1
    # still stored, just not active -- never silently dropped from the fetch
    row = conn.execute("SELECT is_active FROM assets WHERE symbol = 'QVCAQ'").fetchone()
    assert row == (0,)


def test_include_otc_true_counts_otc_assets_as_active():
    conn = connect(":memory:")
    refresh_universe(conn, lambda: [_asset("AAPL"), _asset("QVCAQ", exchange="OTC")], NOW, include_otc=True)
    assert active_symbols(conn) == ["AAPL", "QVCAQ"]


def test_a_symbol_missing_from_the_next_fetch_is_marked_delisted_not_deleted():
    conn = connect(":memory:")
    refresh_universe(conn, lambda: [_asset("AAPL"), _asset("NVDA")], NOW)

    later = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)
    result = refresh_universe(conn, lambda: [_asset("AAPL")], later)

    assert result.delisted == ("NVDA",)
    assert active_symbols(conn) == ["AAPL"]
    row = conn.execute("SELECT is_active, delisted_at FROM assets WHERE symbol = 'NVDA'").fetchone()
    assert row == (0, later.isoformat())


def test_a_delisted_symbol_reappearing_is_reported_as_reactivated():
    conn = connect(":memory:")
    refresh_universe(conn, lambda: [_asset("AAPL"), _asset("NVDA")], NOW)
    day2 = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)
    refresh_universe(conn, lambda: [_asset("AAPL")], day2)  # NVDA delisted

    day3 = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    result = refresh_universe(conn, lambda: [_asset("AAPL"), _asset("NVDA")], day3)

    assert result.added == ()
    assert result.reactivated == ("NVDA",)
    row = conn.execute("SELECT is_active, delisted_at FROM assets WHERE symbol = 'NVDA'").fetchone()
    assert row == (1, None)


def test_a_symbol_that_becomes_non_tradable_is_excluded_from_active_without_a_refetch_gap():
    """tradable can flip to False in the SAME fetch (e.g. a halt) — this
    doesn't need the 'missing from the fetch' delisting path at all."""
    conn = connect(":memory:")
    refresh_universe(conn, lambda: [_asset("AAPL", tradable=True)], NOW)
    later = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)
    refresh_universe(conn, lambda: [_asset("AAPL", tradable=False)], later)

    assert active_symbols(conn) == []


def test_active_symbols_can_require_options_availability():
    conn = connect(":memory:")
    refresh_universe(
        conn, lambda: [_asset("AAPL", options_enabled=True), _asset("PENNY", options_enabled=False)], NOW,
    )
    assert active_symbols(conn) == ["AAPL", "PENNY"]
    assert active_symbols(conn, require_options=True) == ["AAPL"]


def test_refresh_is_idempotent_when_nothing_changed():
    conn = connect(":memory:")
    refresh_universe(conn, lambda: [_asset("AAPL")], NOW)
    result = refresh_universe(conn, lambda: [_asset("AAPL")], NOW)
    assert result.added == ()
    assert result.reactivated == ()
    assert result.delisted == ()
    assert result.total_active == 1
