"""Tests for tradebot.universe — discovering, diffing, and storing the
scan-eligible market universe from a (faked) asset catalog fetch."""
from __future__ import annotations

import logging
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


# -------------------------------------------------------------------- #
# Implausible-fetch guard on the delisting half (code review finding #6)
# -------------------------------------------------------------------- #

LATER = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


def _seed(conn, *symbols):
    refresh_universe(conn, lambda: [_asset(s) for s in symbols], NOW)


def test_an_implausibly_small_fetch_refuses_to_delist_but_still_applies_additions(caplog):
    """A 200 OK carrying a truncated list (rate limit, pagination bug,
    transient vendor issue) used to mark every absent symbol delisted in
    one call, silently wiping the scan universe. The delisting half is now
    skipped; the additions half is unaffected, since a short fetch just
    upserts fewer rows and loses nothing."""
    conn = connect(":memory:")
    _seed(conn, "AAPL", "NVDA", "MSFT", "AMD", "TSLA")  # 5 active

    with caplog.at_level(logging.ERROR, logger="watchtower.universe"):
        result = refresh_universe(conn, lambda: [_asset("AAPL"), _asset("NEWCO")], LATER)  # 2/5 = 0.4

    assert result.delisted == ()
    # nothing was demoted -- the four absent symbols are still scannable
    assert active_symbols(conn) == ["AAPL", "AMD", "MSFT", "NEWCO", "NVDA", "TSLA"]
    # ...and the additions half still ran
    assert result.added == ("NEWCO",)

    assert "vendor fetch returned 2 assets vs 5 currently active" in caplog.text
    assert "refusing to delist" in caplog.text


def test_a_legitimate_shrink_just_above_the_floor_still_delists(caplog):
    """The guard must not become a blanket refusal to ever delist. At
    exactly the floor the fetch is trusted and reconciliation proceeds
    normally."""
    conn = connect(":memory:")
    _seed(conn, "AAPL", "NVDA", "MSFT", "AMD")  # 4 active

    with caplog.at_level(logging.ERROR, logger="watchtower.universe"):
        result = refresh_universe(conn, lambda: [_asset("AAPL"), _asset("NVDA")], LATER)  # 2/4 = 0.5

    assert result.delisted == ("AMD", "MSFT")
    assert active_symbols(conn) == ["AAPL", "NVDA"]
    assert caplog.text == ""  # a real, plausible shrink is not an error


def test_an_empty_fetch_never_delists_the_entire_universe(caplog):
    """The original incident shape: fetch_fn returns [] with no exception
    (it reports success by returning a list), which is indistinguishable
    from 'every US equity delisted at once' to reconciliation-by-absence."""
    conn = connect(":memory:")
    _seed(conn, "AAPL", "NVDA", "MSFT")

    with caplog.at_level(logging.ERROR, logger="watchtower.universe"):
        result = refresh_universe(conn, lambda: [], LATER)

    assert result.delisted == ()
    assert active_symbols(conn) == ["AAPL", "MSFT", "NVDA"]
    assert result.total_active == 3
    assert "vendor fetch returned 0 assets vs 3 currently active" in caplog.text


def test_the_guard_does_not_fire_on_a_first_refresh_into_an_empty_database(caplog):
    """Bootstrap: nothing is active yet, so there is no baseline to be
    implausible against and nothing to delist. A fresh install must not
    log an ERROR on its very first refresh."""
    conn = connect(":memory:")

    with caplog.at_level(logging.ERROR, logger="watchtower.universe"):
        result = refresh_universe(conn, lambda: [], NOW)

    assert result.delisted == ()
    assert result.total_active == 0
    assert caplog.text == ""
