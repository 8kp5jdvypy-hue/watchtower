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

import json
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

-- Stage 1 observability. What the cheap screen (tradebot.broad_scan) did
-- with every symbol, so "why did Perch miss this mover?" has an answer
-- for the widest part of the funnel -- thousands of symbols down to a
-- couple of dozen, which is where a miss is most likely to happen and
-- where, until now, nothing was recorded at all.
--
-- Here rather than in journal.db on purpose. journal.db is "what Perch
-- detected"; these rows are about symbols that mostly produced no
-- detection whatever, at universe cardinality, and would swamp the file
-- the track record lives in. They also sit next to `assets`, the table
-- they join to. A consequence accepted deliberately: correlating these
-- with detections means an ATTACH, the same as the existing
-- journal.db/users.db split.
--
-- Deliberately NOT decision_events: that ledger is keyed on
-- detection_id, and a symbol screened out at Stage 1 has no detection to
-- key on. Minting a synthetic id would destroy the one property that
-- table has -- every row refers to a real detection.

-- One row per scan tick. Home for the funnel counts, the thresholds
-- actually applied, and the conservation invariant.
CREATE TABLE IF NOT EXISTS screening_ticks (
    tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    tick_utc TEXT NOT NULL,
    run_id TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    -- Bumped when the screen's MEANING changes (broad_scan.SCREEN_VERSION).
    -- Rows with different values are not comparable; a cross-session query
    -- should filter on it.
    screen_version INTEGER NOT NULL,
    code_version TEXT,
    -- Whether per-symbol QUIET rows were written for this tick. Without
    -- it, a reader cannot tell a 200-row session from a 185,000-row one
    -- except by guessing.
    audit_mode INTEGER NOT NULL DEFAULT 0,
    universe_count INTEGER NOT NULL,
    thresholds_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    invariant_ok INTEGER NOT NULL,
    promotion_limit INTEGER NOT NULL,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_screening_ticks_session ON screening_ticks(session, tick_utc);

-- One row per interesting per-symbol outcome. QUIET -- roughly 99% of
-- the universe -- is counted in the tick's counts_json rather than
-- written here, unless that tick ran in verbose audit mode.
CREATE TABLE IF NOT EXISTS screening_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    -- screening_ticks.tick_id. Loose reference, no FK constraint --
    -- same style as marks.detection_id / decision_events.detection_id.
    tick_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    -- STAGE 1 UNITS ONLY. Named screen_score, not score, because the one
    -- thing a reader must never do is compare it with detections.score:
    -- that is an ATR-based detector score, this is a ratio-of-thresholds
    -- rank used only to order candidates within a single pass, and it is
    -- never shown to a user. See broad_scan's module docstring.
    screen_score REAL,
    rank INTEGER,
    reasons_json TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_screening_events_symbol ON screening_events(symbol);
CREATE INDEX IF NOT EXISTS idx_screening_events_tick ON screening_events(tick_id, outcome);
"""

# Retention, DOCUMENTED BUT NOT ENFORCED -- nothing in this module
# deletes. Shipping a deleter in the same change that first creates the
# data would mean the first bug in it destroys the only copy.
#
#   interesting outcomes  ~hundreds/session   keep ~90 days
#   aggregated quiet      1 row/tick          keep with the tick
#   verbose audit         ~185k rows/session  bounded investigation only,
#                                             roughly 25-30 MB/session
#
# A pruning job is a separate change. Until it exists, verbose audit mode
# is the only setting that grows this file quickly, and it is off by
# default.
MAX_SCREENING_JSON_LEN = 2000


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


# --------------------------------------------------------------------------
# Stage 1 observability — persistence.
#
# The classification itself is pure and lives in
# broad_scan.classify_screen_outcomes; this only writes what it decided.
# --------------------------------------------------------------------------


def _encode(value: dict | tuple | list | None) -> str | None:
    """Same all-or-nothing discipline as journal.record_decision_event:
    an oversized document is dropped rather than truncated, because half
    a JSON document is not a smaller fact, it is an unparseable one."""
    if value is None or value == () or value == [] or value == {}:
        return None
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded) > MAX_SCREENING_JSON_LEN:
        logger.warning("screening JSON is %d bytes (limit %d) -- recording without it",
                       len(encoded), MAX_SCREENING_JSON_LEN)
        return None
    return encoded


def record_screening_tick(
    conn: sqlite3.Connection,
    tick,
    events,
    *,
    session: str,
    tick_utc: str,
    run_id: str,
    run_mode: str,
    screen_version: int,
    code_version: str | None = None,
    audit_mode: bool = False,
    latency_ms: int | None = None,
) -> int:
    """Append one Stage 1 tick and its per-symbol outcomes. Returns
    tick_id.

    tick/events are exactly what broad_scan.classify_screen_outcomes
    returned — this function decides nothing about what happened, it only
    stores it. One transaction: a tick's counts and its rows are true
    together or not at all, since the counts are what makes the
    aggregated-quiet subtraction valid.

    run_mode/run_id carry the same meaning as in journal.decision_events:
    which execution produced these rows. Stage 1 is live-only today
    (--broad-scan requires --live), so replay attribution is
    forward-looking rather than load-bearing — recorded anyway because a
    row that cannot say which run wrote it is the problem those columns
    exist to prevent."""
    cursor = conn.execute(
        """
        INSERT INTO screening_ticks
            (session, tick_utc, run_id, run_mode, screen_version, code_version,
             audit_mode, universe_count, thresholds_json, counts_json,
             invariant_ok, promotion_limit, latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session, tick_utc, run_id, run_mode, screen_version, code_version,
            int(audit_mode), tick.universe_count,
            json.dumps(tick.thresholds, separators=(",", ":"), sort_keys=True),
            json.dumps(tick.counts, separators=(",", ":"), sort_keys=True),
            int(tick.invariant_ok), tick.promotion_limit, latency_ms,
        ),
    )
    tick_id = cursor.lastrowid
    conn.executemany(
        """
        INSERT INTO screening_events
            (tick_id, symbol, outcome, screen_score, rank, reasons_json, detail_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (tick_id, e.symbol, e.outcome, e.screen_score, e.rank,
             _encode(list(e.reasons)), _encode(e.detail))
            for e in events
        ],
    )
    conn.commit()
    return tick_id


def screening_history_for_symbol(conn: sqlite3.Connection, symbol: str, session: str) -> list[dict]:
    """Every Stage 1 outcome recorded for one symbol in one session, in
    tick order — the "why was this missed?" query.

    Returns the tick's context alongside each row, because an outcome is
    not interpretable without it: CANDIDATE_NOT_PROMOTED means nothing
    without the promotion_limit it lost to, and no screen_score is
    comparable across a screen_version change."""
    rows = conn.execute(
        """
        SELECT t.tick_utc, e.outcome, e.screen_score, e.rank, e.reasons_json,
               e.detail_json, t.promotion_limit, t.screen_version, t.counts_json,
               t.invariant_ok, t.audit_mode, t.run_id, t.run_mode
        FROM screening_events e
        JOIN screening_ticks t ON t.tick_id = e.tick_id
        WHERE e.symbol = ? AND t.session = ?
        ORDER BY t.tick_utc, e.seq
        """,
        (symbol, session),
    ).fetchall()
    return [
        {
            "tick_utc": tick_utc, "outcome": outcome, "screen_score": screen_score,
            "rank": rank, "reasons": json.loads(reasons) if reasons else [],
            "detail": json.loads(detail) if detail else None,
            "promotion_limit": promotion_limit, "screen_version": screen_version,
            "counts": json.loads(counts), "invariant_ok": bool(invariant_ok),
            "audit_mode": bool(audit_mode), "run_id": run_id, "run_mode": run_mode,
        }
        for (tick_utc, outcome, screen_score, rank, reasons, detail, promotion_limit,
             screen_version, counts, invariant_ok, audit_mode, run_id, run_mode) in rows
    ]

