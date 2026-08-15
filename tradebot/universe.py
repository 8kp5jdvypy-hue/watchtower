"""The market universe — every active US equity/ETF Perch is allowed to
scan, discovered from Alpaca's asset catalog rather than a hand-curated
list. See tradebot.vendors.alpaca.fetch_us_equity_assets for the real
data this is built from (live-verified 2026-08-08: 14,195 active
us_equity assets, ~1,100 of them OTC).

Deliberately a separate store from data/journal.db (what Perch detected)
and data/users.db (what a user did) — this is a third, distinct kind of
truth: what Perch is even ALLOWED to look at. Same reasoning as keeping
those two apart in the first place.

This module only discovers and stores the universe. It does not fetch
bar data and does not run any detector — see tradebot.broad_scan for the
cheap Stage 1 screen that actually looks at price/volume for symbols in
this universe, and detectors.py (unchanged) for the existing Stage 2
deep analysis applied only to whatever Stage 1 promotes.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from tradebot.marketdata import AssetInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "universe.db"

logger = logging.getLogger("watchtower.universe")

# Below this share of the currently-active count, a fetch is treated as
# vendor trouble rather than a real market event, and the delisting half
# of refresh_universe is skipped. The catalog is ~14,200 symbols and
# moves by single digits on a normal day (see this module's docstring),
# so a fetch returning less than half of what's active has no legitimate
# reading -- the entire US market did not delist overnight. 0.5 leaves
# enormous headroom over any real day while still catching the failure
# that matters: a 200 OK carrying a truncated or empty list.
MIN_FETCH_RATIO_TO_DELIST = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    symbol TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    name TEXT NOT NULL,
    tradable INTEGER NOT NULL,
    options_enabled INTEGER NOT NULL,
    overnight_eligible INTEGER,
    attributes_json TEXT NOT NULL,
    is_active INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    delisted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_active ON assets(is_active);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


@dataclass(frozen=True)
class RefreshResult:
    """What changed in this one refresh — the whole point of tracking a
    universe over time rather than just re-fetching it live every use:
    'newly listed' and 'removed' are real, observable events, not
    something a flat live query could ever answer on its own."""

    added: tuple[str, ...]  # never seen before
    reactivated: tuple[str, ...]  # previously delisted/removed, now active again
    delisted: tuple[str, ...]  # was active, missing from this fetch
    total_active: int


def refresh_universe(
    conn: sqlite3.Connection,
    fetch_fn: Callable[[], Sequence[AssetInfo]],
    now: datetime,
    include_otc: bool = False,
) -> RefreshResult:
    """Fetches the current catalog (fetch_fn — real callers pass
    tradebot.vendors.alpaca.fetch_us_equity_assets; tests pass a fake),
    and reconciles it against what's stored: every symbol in the fresh
    fetch is upserted (added if new, reactivated if it had been marked
    delisted, refreshed in place otherwise); every symbol stored as
    active but ABSENT from this fetch is marked delisted rather than
    deleted — the history of "this used to be tradable" is itself real
    information, never discarded to keep the table tidy.

    include_otc=False (the default) excludes OTC-exchange assets from
    what counts as 'active' here — per tradebot.vendors.alpaca's
    live-observed data, that's ~1,100 of the ~14,200 active us_equity
    assets Alpaca reports; they're still stored (never silently dropped
    from the fetch), just not counted active unless explicitly enabled.

    Delisting is guarded: a fetch returning fewer than
    MIN_FETCH_RATIO_TO_DELIST of the currently-active count is treated as
    vendor trouble, logged as an ERROR naming both counts, and the
    delisting pass is skipped entirely (additions/refreshes still apply).
    An empty or truncated 200 OK would otherwise delist the whole scan
    universe in one call — see the comment at that branch."""
    now_iso = now.isoformat()
    fetched = {a.symbol: a for a in fetch_fn()}

    existing = dict(conn.execute("SELECT symbol, is_active FROM assets").fetchall())

    added, reactivated = [], []
    for symbol, asset in fetched.items():
        is_active = int(asset.tradable and (include_otc or asset.exchange != "OTC"))
        if symbol not in existing:
            added.append(symbol)
        elif is_active and not existing[symbol]:
            reactivated.append(symbol)
        conn.execute(
            """
            INSERT INTO assets
                (symbol, exchange, name, tradable, options_enabled, overnight_eligible,
                 attributes_json, is_active, first_seen_at, last_seen_at, delisted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(symbol) DO UPDATE SET
                exchange=excluded.exchange, name=excluded.name, tradable=excluded.tradable,
                options_enabled=excluded.options_enabled, overnight_eligible=excluded.overnight_eligible,
                attributes_json=excluded.attributes_json, is_active=excluded.is_active,
                last_seen_at=excluded.last_seen_at,
                delisted_at=CASE WHEN excluded.is_active THEN NULL ELSE assets.delisted_at END
            """,
            (
                symbol, asset.exchange, asset.name, int(asset.tradable), int(asset.options_enabled),
                None if asset.overnight_eligible is None else int(asset.overnight_eligible),
                _dump_attrs(asset.attributes), is_active, now_iso, now_iso,
            ),
        )

    # Delisting is reconciliation by ABSENCE — the one operation here that
    # a bad fetch turns into mass destruction. fetch_fn() reports success
    # by returning a list, so a 200 OK carrying zero or a truncated set of
    # rows (rate limit, pagination bug, transient vendor issue) is
    # indistinguishable from "the market really shrank" to the loop below,
    # and would mark every real symbol is_active=0 in a single call. The
    # additions half above is safe either way: a short fetch just upserts
    # fewer rows, and nothing is lost.
    active_before = sum(1 for was_active in existing.values() if was_active)
    fetch_is_plausible = (
        not active_before or len(fetched) >= active_before * MIN_FETCH_RATIO_TO_DELIST
    )

    delisted = []
    if not fetch_is_plausible:
        logger.error(
            "vendor fetch returned %d assets vs %d currently active — refusing to delist "
            "(below the %.0f%% floor). Additions/refreshes from this fetch were applied "
            "normally; no symbol was marked delisted. If the market really did shrink this "
            "much, re-run once the vendor is healthy and this will reconcile itself.",
            len(fetched), active_before, MIN_FETCH_RATIO_TO_DELIST * 100,
        )
    else:
        for symbol, was_active in existing.items():
            if was_active and symbol not in fetched:
                conn.execute(
                    "UPDATE assets SET is_active = 0, delisted_at = ? WHERE symbol = ?", (now_iso, symbol)
                )
                delisted.append(symbol)

    conn.commit()
    total_active = conn.execute("SELECT COUNT(*) FROM assets WHERE is_active = 1").fetchone()[0]
    return RefreshResult(
        added=tuple(sorted(added)), reactivated=tuple(sorted(reactivated)),
        delisted=tuple(sorted(delisted)), total_active=total_active,
    )


def _dump_attrs(attrs: tuple[str, ...]) -> str:
    import json

    return json.dumps(list(attrs))


def active_symbols(conn: sqlite3.Connection, require_options: bool = False) -> list[str]:
    """The current scan-eligible universe — what tradebot.broad_scan's
    Stage 1 screen runs against. require_options=True narrows to symbols
    Alpaca reports as having a listed options chain (relevant once a
    promoted candidate needs a real contract idea, same as costs.py
    already requires for the existing fixed watchlist)."""
    query = "SELECT symbol FROM assets WHERE is_active = 1"
    if require_options:
        query += " AND options_enabled = 1"
    query += " ORDER BY symbol"
    return [r[0] for r in conn.execute(query).fetchall()]


def asset_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM assets WHERE is_active = 1").fetchone()[0]
