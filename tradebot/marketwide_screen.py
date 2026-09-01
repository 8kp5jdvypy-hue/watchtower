"""Shared validation contract for Alpaca's bounded market-wide screen."""
from __future__ import annotations

import math
from datetime import datetime

from tradebot.marketdata import MarketWideScreen


MAX_SCREEN_AGE_SECONDS = 180
EXPECTED_ENDPOINTS = {
    "market_movers",
    "most_actives_volume",
    "most_actives_trades",
}
SOURCE_ENDPOINT = {
    "market_gainer": "market_movers",
    "market_loser": "market_movers",
    "most_active_volume": "most_actives_volume",
    "most_active_trades": "most_actives_trades",
}


def validate_marketwide_screen(
    screen: MarketWideScreen,
    *,
    now: datetime,
    data_feed: str,
    top_n: int,
    max_age_seconds: int = MAX_SCREEN_AGE_SECONDS,
) -> None:
    """Fail closed unless every bounded-screen provenance invariant holds."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market-wide screen validation time must be timezone-aware")
    if max_age_seconds <= 0:
        raise ValueError("market-wide screen maximum age must be positive")
    if screen.provider != "alpaca":
        raise ValueError(f"unexpected market-wide screen provider: {screen.provider!r}")
    if screen.feed != "sip":
        raise ValueError(f"market-wide screener must use SIP, got {screen.feed!r}")
    if data_feed != screen.feed:
        raise ValueError(
            f"market-wide screen/bar feed mismatch: {screen.feed!r} vs {data_feed!r}"
        )
    if screen.requested_top_n != top_n or not 1 <= top_n <= 50:
        raise ValueError("market-wide screen requested bound does not match the tick")
    if (
        len(screen.endpoints) != len(EXPECTED_ENDPOINTS)
        or set(screen.endpoints) != EXPECTED_ENDPOINTS
    ):
        raise ValueError(
            "market-wide screen endpoint set is incomplete, duplicated, or unexpected"
        )
    updates = dict(screen.source_updates)
    if (
        len(screen.source_updates) != len(EXPECTED_ENDPOINTS)
        or set(updates) != EXPECTED_ENDPOINTS
    ):
        raise ValueError(
            "market-wide screen source timestamps are incomplete or duplicated"
        )
    for source, updated in updates.items():
        if updated.tzinfo is None or updated.utcoffset() is None:
            raise ValueError(f"market-wide screen timestamp is naive for {source}")
        age = (now - updated).total_seconds()
        if age < 0:
            raise ValueError(f"market-wide screen timestamp is in the future for {source}")
        if age > max_age_seconds:
            raise ValueError(
                f"market-wide screen timestamp is stale for {source}: {age:.0f}s"
            )
    by_source: dict[str, list[int]] = {source: [] for source in SOURCE_ENDPOINT}
    seen_symbol_source: set[tuple[str, str]] = set()
    for entry in screen.entries:
        if entry.source not in SOURCE_ENDPOINT:
            raise ValueError(f"unknown market-wide screen row source: {entry.source!r}")
        if entry.symbol != entry.symbol.strip().upper() or not entry.symbol:
            raise ValueError("market-wide screen symbol is not canonical")
        if not 1 <= entry.rank <= top_n:
            raise ValueError("market-wide screen rank is outside the requested bound")
        key = (entry.symbol, entry.source)
        if key in seen_symbol_source:
            raise ValueError("market-wide screen duplicated a symbol within one source")
        seen_symbol_source.add(key)
        endpoint = SOURCE_ENDPOINT[entry.source]
        if entry.source_updated_at != updates[endpoint]:
            raise ValueError("market-wide screen row/source timestamps disagree")
        numeric = (entry.move_pct, entry.price, entry.volume, entry.trade_count)
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("market-wide screen row contains a non-finite metric")
        if entry.source in {"market_gainer", "market_loser"}:
            if entry.move_pct is None or entry.price is None or entry.price <= 0:
                raise ValueError("market mover row is missing price/change provenance")
            if entry.source == "market_gainer" and entry.move_pct <= 0:
                raise ValueError("market gainer row has a non-positive move")
            if entry.source == "market_loser" and entry.move_pct >= 0:
                raise ValueError("market loser row has a non-negative move")
        elif entry.volume is None or entry.trade_count is None:
            raise ValueError("most-active row is missing activity provenance")
        elif entry.volume < 0 or entry.trade_count < 0:
            raise ValueError("most-active row contains a negative metric")
        by_source[entry.source].append(entry.rank)
    for ranks in by_source.values():
        if ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("market-wide screen ranks are duplicated or non-contiguous")
