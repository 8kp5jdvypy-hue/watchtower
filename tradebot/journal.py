"""SQLite journal for every detection cluster.

Every cluster gets written here, including sub-threshold ('log' tier)
ones — see CLAUDE.md: every detection is journaled before any alert is
sent, and sub-threshold detections are how we find out the thresholds are
wrong, so they're never dropped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import statistics
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dataclasses import dataclass

from tradebot.config import liquidity_class
from tradebot.detectors import Detection, bar_close_ts, tier_for_score
from tradebot.marketdata import ReplayMarketData

logger = logging.getLogger("watchtower.journal")
ET = ZoneInfo("America/New_York")

MIN_HISTORY_SAMPLE = 5

# Decision B (docs/sip-migration-proposal.md, Option 1 -- post-cutover-only):
# historical_performance()/tier_performance()/kind_performance() never blend
# rows written under different data feeds into one continuation-rate number
# -- a feed change means different prices and volume baselines, the same
# "don't fabricate a stat from an incompatible population" reasoning as the
# existing news_driven exclusion. "Current feed" is read from the journal's
# own most recent row rather than importing vendors.alpaca's
# DETECTOR_DATA_FEED here (this module has no vendor-SDK dependency and
# shouldn't gain one just for this) -- ground truth of what was actually
# written, not what a config value currently claims. Every row written
# before this column existed has data_feed IS NULL, which matches neither
# 'iex' nor 'sip', so all pre-migration history is excluded from these
# three functions by construction -- the "resets to n=0 for a while"
# tradeoff the proposal already named as Option 1's known cost.
#
# The same clause also excludes broad_scan-promoted ("screening") symbols
# -- see docs/broad-scan-honesty-proposal.md's finding (b). Bundled here
# since both filters touch the same three queries and both are about "don't
# let a different measurement population masquerade as the historical
# technical base rate."
CURRENT_FEED_FILTER_SQL = """
    d.data_feed = (SELECT data_feed FROM detections WHERE data_feed IS NOT NULL ORDER BY ts_utc DESC LIMIT 1)
    AND d.origin = 'watchlist'
"""

# Sentinel offset_min for "the session close" — not a fixed number of
# minutes after the detection (that varies with when in the day it
# fired), so it can't share a positive-minutes value with the 15/30/60
# checkpoints. Used in both `marks` (underlying close price) and
# `contract_selections` (mid_close) so both share one query pattern.
CLOSE_MARK_OFFSET_MIN = -1
OUTCOME_OFFSETS_MIN = (15, 30, 60)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "journal.db"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"

SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id TEXT PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kinds TEXT NOT NULL,
    headlines TEXT NOT NULL,
    score REAL NOT NULL,
    tier TEXT NOT NULL,
    close REAL,
    atr14 REAL,
    trend TEXT,
    context_json TEXT,
    code_version TEXT,
    alerted INTEGER DEFAULT 0,
    suppress_reason TEXT,
    no_trade INTEGER,
    news_driven INTEGER,
    primary_kind TEXT,
    symbol_class TEXT,
    event_kind TEXT,
    event_severity TEXT,
    suppress_category TEXT,
    lifecycle_state TEXT,
    related_detection_id TEXT,
    data_feed TEXT,
    origin TEXT,
    extreme_mover INTEGER,
    extreme_mover_gap_pct REAL,
    extreme_mover_volume INTEGER,
    pct_from_prior_close REAL,
    pct_from_prior_close_status TEXT
);

CREATE TABLE IF NOT EXISTS marks (
    detection_id TEXT NOT NULL,
    offset_min INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (detection_id, offset_min)
);

CREATE TABLE IF NOT EXISTS iv_history (
    symbol TEXT NOT NULL,
    session TEXT NOT NULL,
    iv REAL NOT NULL,
    PRIMARY KEY (symbol, session)
);

CREATE TABLE IF NOT EXISTS contract_selections (
    detection_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    right TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    dte INTEGER NOT NULL,
    delta REAL,
    is_vertical INTEGER NOT NULL DEFAULT 0,
    short_strike REAL,
    short_delta REAL,
    entry_mid REAL NOT NULL,
    entry_ts_utc TEXT NOT NULL,
    mid_15m REAL,
    mid_30m REAL,
    mid_60m REAL,
    mid_close REAL
);

CREATE TABLE IF NOT EXISTS event_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    kind TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_windows_symbol_time ON event_windows(symbol, start_utc, end_utc);
-- COALESCE so a market-wide row (symbol IS NULL) still dedupes against
-- itself on repeated ingestion runs — SQL UNIQUE treats NULL != NULL,
-- which would otherwise let every macro-calendar refresh insert a copy.
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_windows_dedup
    ON event_windows(COALESCE(symbol, ''), kind, start_utc, source);

-- Append-only ledger of the individual decisions taken about a detection
-- on its way through the pipeline. Deliberately NOT columns on
-- `detections`: that row is a mutable snapshot of the cluster's latest
-- known state (write_cluster upserts it, set_no_trade/set_news_driven/
-- set_extreme_mover overwrite fields on it), which by construction cannot
-- answer "what did we decide, in what order, and on what basis, at the
-- time." One detection has many decision_events; a decision_event is
-- never revised, only superseded by a later one appended after it.
--
-- runner.py's process_new_bar is the writer -- see record_decision_event
-- and the call sites there.
CREATE TABLE IF NOT EXISTS decision_events (
    -- AUTOINCREMENT (not bare rowid): guarantees a strictly increasing
    -- seq that is never reused, so ordering by seq is a real append
    -- order for the life of the file. Matches event_windows above.
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    -- detections.id. Loose reference, no SQLite FK constraint -- same
    -- style as marks.detection_id / contract_selections.detection_id.
    detection_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    detail_json TEXT,
    code_version TEXT,
    -- Which invocation of the scanner produced this event, and whether
    -- that invocation was the live loop or a replay of a cached session.
    -- The ledger is append-only, so a replay's decisions sit permanently
    -- alongside live ones in the same table and there is no later pass
    -- that could go back and label them; without these two columns a
    -- reader some months from now has no way to tell "the system decided
    -- this during Tuesday's session" from "someone re-ran Tuesday from
    -- cache on Friday to test a detector change." run_id also groups one
    -- run's decisions together across every symbol it touched, which
    -- neither ts_utc (two runs can overlap in time) nor code_version
    -- (the same build runs many times) can do.
    --
    -- Both NULL on rows written by a caller that didn't identify its run
    -- -- never back-filled or inferred, same discipline as code_version.
    run_id TEXT,
    run_mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_events_detection ON decision_events(detection_id, seq);
CREATE INDEX IF NOT EXISTS idx_decision_events_ts ON decision_events(ts_utc);

-- Append-only enforced by the database, not by convention. A ledger whose
-- immutability rests on "no helper in this module issues an UPDATE" is
-- only as good as the next person who opens a sqlite3 shell; these make a
-- rewrite fail loudly instead. Consequence, accepted deliberately: rows
-- here can never be edited or purged in place. Correcting the record means
-- appending a superseding event, which is the property the ledger exists
-- to have.
CREATE TRIGGER IF NOT EXISTS decision_events_no_update
BEFORE UPDATE ON decision_events
BEGIN
    SELECT RAISE(ABORT, 'decision_events is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS decision_events_no_delete
BEFORE DELETE ON decision_events
BEGIN
    SELECT RAISE(ABORT, 'decision_events is append-only: DELETE is not permitted');
END;
"""


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
    """CREATE TABLE IF NOT EXISTS won't retroactively add a column to a
    pre-existing table — every column added after a table's first release
    needs one of these so an existing data/journal.db picks it up."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def connect(db_path: Path | str = DEFAULT_DB_PATH, check_same_thread: bool = True) -> sqlite3.Connection:
    """check_same_thread=False is for callers (the Telegram command
    dispatcher) that hand this connection to a worker-thread pool and
    serialize access themselves with their own lock — see
    tradebot.telegram_bot.dispatcher. Every other caller (the scanner,
    replay, scripts) is single-threaded and should leave the default."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.executescript(SCHEMA)
    _add_column_if_missing(conn, "detections", "no_trade", "INTEGER")
    _add_column_if_missing(conn, "detections", "news_driven", "INTEGER")
    _add_column_if_missing(conn, "detections", "primary_kind", "TEXT")
    _add_column_if_missing(conn, "detections", "symbol_class", "TEXT")
    _add_column_if_missing(conn, "detections", "event_kind", "TEXT")
    _add_column_if_missing(conn, "detections", "event_severity", "TEXT")
    # Additive, parallel to the free-text suppress_reason above — never
    # replaces it. tradebot.telegram_bot.handlers has an exact-string
    # dependency on suppress_reason='cooldown_active', so existing values
    # must never change shape. suppress_category is the structured,
    # enumerable counterpart (see alerts.SuppressionCategory). NULL on
    # every row written before this shipped, same as primary_kind/
    # symbol_class on pre-existing rows — never back-filled.
    _add_column_if_missing(conn, "detections", "suppress_category", "TEXT")
    # "watch" (first cluster on a symbol in the dedup window) or
    # "confirmed" (a later one, see tradebot.dedup). NULL pre-migration.
    _add_column_if_missing(conn, "detections", "lifecycle_state", "TEXT")
    # The anchor detections.id a "confirmed" row was deduped against.
    # Loose reference, no SQLite FK constraint, same style as
    # marks.detection_id / contract_selections.detection_id below.
    _add_column_if_missing(conn, "detections", "related_detection_id", "TEXT")
    # Both NULL on every row written before this shipped (docs/sip-migration-
    # proposal.md's Decision B, docs/broad-scan-honesty-proposal.md's finding
    # (b)) -- never back-filled, same as symbol_class/primary_kind above.
    # See CURRENT_FEED_FILTER_SQL for how that NULL is handled by the
    # performance-stats functions.
    _add_column_if_missing(conn, "detections", "data_feed", "TEXT")
    _add_column_if_missing(conn, "detections", "origin", "TEXT")
    # NULL on every row before Proposal 3 (docs/open-awareness-proposals-
    # 2026-08.md) and on every row since where the move never persisted
    # the guard's 25% line -- never back-filled, never set to 0, same
    # NULL-means-not-applicable discipline as no_trade/data_feed above.
    # See tradebot.guard.extreme_mover_evidence and set_extreme_mover().
    _add_column_if_missing(conn, "detections", "extreme_mover", "INTEGER")
    _add_column_if_missing(conn, "detections", "extreme_mover_gap_pct", "REAL")
    _add_column_if_missing(conn, "detections", "extreme_mover_volume", "INTEGER")
    # A1 (docs/open-awareness-proposals-2026-08.md, Phase 1 perception
    # decomposition): prior-close displacement recorded as an audit
    # feature only -- not a detector, not a scoring input, not a
    # threshold. NULL on every row before this shipped and on any row
    # where tradebot.features.pct_from_prior_close() came back
    # UNAVAILABLE -- same NULL-means-not-applicable discipline as
    # extreme_mover_gap_pct above. pct_from_prior_close_status is never
    # NULL for a row written by a caller that ran the primitive (it's
    # always 'AVAILABLE' or 'UNAVAILABLE:<reason>' -- see
    # tradebot.features.PctFromPriorClose.status) -- the status column
    # is what lets a query distinguish "this session had no signal" from
    # "this feature couldn't be computed," which a bare NULL on the
    # value column alone cannot.
    _add_column_if_missing(conn, "detections", "pct_from_prior_close", "REAL")
    _add_column_if_missing(conn, "detections", "pct_from_prior_close_status", "TEXT")
    _add_column_if_missing(conn, "contract_selections", "mid_close", "REAL")
    _add_column_if_missing(conn, "contract_selections", "day_low", "REAL")
    _add_column_if_missing(conn, "contract_selections", "day_high", "REAL")
    # NULL on any row appended before these shipped -- see the columns'
    # own comment in SCHEMA. ALTER TABLE ADD COLUMN is not an UPDATE or a
    # DELETE, so the ledger's append-only triggers don't (and shouldn't)
    # block a migration; what they protect is the content of rows already
    # written, and adding a column leaves every one of those rows saying
    # exactly what it said before.
    _add_column_if_missing(conn, "decision_events", "run_id", "TEXT")
    _add_column_if_missing(conn, "decision_events", "run_mode", "TEXT")
    # Deliberately NOT in SCHEMA with the table's other indexes: an
    # existing journal.db has a decision_events table without run_id, and
    # executescript(SCHEMA) runs above these ALTERs -- a CREATE INDEX on
    # run_id up there would raise "no such column" and abort the whole
    # script, taking every later statement in SCHEMA down with it. It has
    # to come after the column it indexes exists.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_events_run ON decision_events(run_id, seq)")
    conn.commit()
    return conn


def code_version() -> str:
    """Short git hash at write time.

    2026-08-12: the deployed container has no .git directory (the
    Dockerfile only COPYs tradebot/), so the git subprocess below always
    failed in production and every row silently got 'unknown' -- not
    just a missing value, a load-bearing one: this is the only per-row
    record of which code produced a detection. GIT_SHA is now baked into
    the image at build time (see Dockerfile's ARG/ENV and
    docker-compose.yml's build.args) and checked first; the git
    subprocess is the fallback for local dev, where GIT_SHA is normally
    unset but a real .git directory is."""
    env_sha = os.environ.get("GIT_SHA")
    if env_sha and env_sha != "unknown":
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def cluster_id(symbol: str, session: str, ts_utc: str, kinds: str) -> str:
    """Deterministic id from cluster identity, so re-running the same
    replay upserts the row instead of duplicating it."""
    raw = f"{symbol}|{session}|{ts_utc}|{kinds}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_cluster(
    conn: sqlite3.Connection,
    *,
    session: str,
    symbol: str,
    ts_utc: str,
    kinds: str,
    headlines: str,
    score: float,
    close: float | None,
    atr14: float | None,
    trend: str | None,
    detections: list[Detection],
    code_version_str: str,
    alerted: bool = False,
    suppress_reason: str | None = None,
    primary_kind: str | None = None,
    data_feed: str | None = None,
    origin: str = "watchlist",
    pct_from_prior_close: float | None = None,
    pct_from_prior_close_status: str | None = None,
) -> str:
    """symbol_class (deep/thin liquidity, see tradebot.config) is derived
    and frozen here at write time, not looked up later — the watchlist's
    classification could change in the future, and a historical row
    should keep reporting what was true when the alert actually fired,
    the same discipline historical_performance() already applies to each
    row's own atr14 rather than borrowing a current value.

    data_feed: the caller's DETECTOR_DATA_FEED value at write time ('iex'/
    'sip'), same frozen-at-write-time discipline as symbol_class — None
    (the default) matches every pre-Decision-B caller/test, same as the
    other columns added after this function's first release.

    origin: 'watchlist' (default) or 'screening' — whether `symbol` was in
    the fixed watchlist or promoted in by broad_scan for this session. The
    only place this fact is knowable is the caller's own merge point
    (tradebot.runner's `scan_symbols` construction), so it must be passed
    in, not derived here. See docs/broad-scan-honesty-proposal.md.

    pct_from_prior_close/pct_from_prior_close_status: the caller's own
    tradebot.features.pct_from_prior_close(close, anchors.prior_close)
    result, already resolved before this call the same way data_feed/
    origin are — this module has no reason to import tradebot.features
    just to recompute what the caller already has. value is None exactly
    when status is 'UNAVAILABLE:<reason>' rather than 'AVAILABLE'; both
    default to None (pre-A1 callers/tests and any test double that never
    ran the real primitive)."""
    tier = tier_for_score(score).value
    detection_id = cluster_id(symbol, session, ts_utc, kinds)
    context_json = json.dumps([d.context for d in detections])
    symbol_class = liquidity_class(symbol)
    conn.execute(
        """
        INSERT INTO detections
            (id, ts_utc, session, symbol, kinds, headlines, score, tier,
             close, atr14, trend, context_json, code_version, alerted, suppress_reason,
             primary_kind, symbol_class, data_feed, origin,
             pct_from_prior_close, pct_from_prior_close_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ts_utc=excluded.ts_utc, kinds=excluded.kinds, headlines=excluded.headlines,
            score=excluded.score, tier=excluded.tier, close=excluded.close,
            atr14=excluded.atr14, trend=excluded.trend, context_json=excluded.context_json,
            code_version=excluded.code_version, alerted=excluded.alerted,
            suppress_reason=excluded.suppress_reason, primary_kind=excluded.primary_kind,
            symbol_class=excluded.symbol_class, data_feed=excluded.data_feed,
            origin=excluded.origin, pct_from_prior_close=excluded.pct_from_prior_close,
            pct_from_prior_close_status=excluded.pct_from_prior_close_status
        """,
        (
            detection_id, ts_utc, session, symbol, kinds, headlines, score, tier,
            close, atr14, trend, context_json, code_version_str, int(alerted), suppress_reason,
            primary_kind, symbol_class, data_feed, origin,
            pct_from_prior_close, pct_from_prior_close_status,
        ),
    )
    return detection_id


def set_no_trade(conn: sqlite3.Connection, detection_id: str, no_trade: bool) -> None:
    """Records whether a dispatched HIGH alert ended up with a tradable
    contract — set once costs.select_contract() has actually been computed,
    since write_cluster() happens before that. NULL (never called) means 'not
    applicable or not yet computed', not 'had a contract' — see
    /performance, which reports that distinction rather than assuming."""
    conn.execute("UPDATE detections SET no_trade = ? WHERE id = ?", (int(no_trade), detection_id))


def set_news_driven(
    conn: sqlite3.Connection, detection_id: str, news_driven: bool,
    kind: str | None = None, severity: str | None = None,
) -> None:
    """Records whether this cluster overlaps a known event window (EDGAR
    filing, earnings, FOMC/CPI/NFP/EIA — see tradebot.events), set by
    runner.py right after journal_write_cluster() for every tier, not
    just HIGH. NULL (never called) means 'no events data to check
    against', not 'confirmed clean technical' — same NULL-means-unknown
    discipline as set_no_trade(). Used by historical_performance() to
    exclude event-driven history from the technical continuation sample,
    and by /performance to report news-driven vs clean-technical
    separately.

    kind/severity snapshot which event window applied (e.g. "earnings",
    "suppress"), frozen at decision time rather than left to a later join
    against event_windows — the same "recompute from a frozen fact, not a
    live re-lookup" discipline as symbol_class in write_cluster(). Only
    the caller that already resolved active_event_window() knows these;
    left None when there's no window (news_driven=False)."""
    conn.execute(
        "UPDATE detections SET news_driven = ?, event_kind = ?, event_severity = ? WHERE id = ?",
        (int(news_driven), kind, severity, detection_id),
    )


def set_extreme_mover(conn: sqlite3.Connection, detection_id: str, gap_pct: float, verified_volume: int) -> None:
    """Records that this cluster's alert cleared guard.py's extreme-mover
    persistence check (Proposal 3) -- set by runner.py right after a HIGH
    alert with tradebot.guard.ExtremeMoverEvidence actually sends, never
    called otherwise, so the column stays NULL (not 0) on every other
    row -- same discipline as set_news_driven's caller only invoking it
    from inside `if news_driven:`. gap_pct/verified_volume are the exact
    numbers the alert's own card showed, frozen at decision time rather
    than recomputed later, so a query against this table can never
    disagree with what a subscriber actually read."""
    conn.execute(
        "UPDATE detections SET extreme_mover = 1, extreme_mover_gap_pct = ?, extreme_mover_volume = ? WHERE id = ?",
        (gap_pct, verified_volume, detection_id),
    )


def _all_bars_for_session(cache_dir: Path, symbol: str, session_date: date):
    md = ReplayMarketData(cache_dir, symbol, session_date)
    while md.advance():
        pass
    bars = list(md.premarket_bars(symbol, session_date)) + list(md.session_bars(symbol, session_date))
    bars.sort(key=lambda b: b.ts)
    return bars


def _price_at_or_after(bars, target_ts: datetime) -> float | None:
    for b in bars:
        if bar_close_ts(b) >= target_ts:
            return b.close
    return None


def detected_symbols_for_session(conn: sqlite3.Connection, session: date) -> list[str]:
    """Every distinct symbol with at least one journaled detection this
    session -- watchlist AND screening alike, unlike the old ad-hoc
    manual backfill which only ever covered WATCHLIST. The single source
    of truth for "which symbols does backfill_marks() need bars for," so
    runner.py's close-time cache fetch and backfill_marks() itself never
    drift apart on scope."""
    rows = conn.execute("SELECT DISTINCT symbol FROM detections WHERE session = ?", (session.isoformat(),))
    return sorted(row[0] for row in rows)


def backfill_marks(
    conn: sqlite3.Connection,
    session: date,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    offsets_min: tuple[int, ...] = OUTCOME_OFFSETS_MIN,
) -> int:
    """Fill the marks table at +offsets_min from every journaled
    detection in `session`, reading forward prices from cached bars, plus
    one CLOSE_MARK_OFFSET_MIN row per detection — the session's actual
    final bar close, not a fixed-minutes offset, so every alert gets a
    real end-of-day outcome regardless of when in the session it fired.
    Skips an offset silently if the session ended before reaching it, and
    skips the close mark if there are no bars at all — never fabricates a
    price. Automatic and unconditional: called once at the end of every
    replay/live session (see runner.py), never gated on how the alert
    performed — a loss is exactly as recordable as a win.

    2026-08-12 incident: an absent cache file and "cached, but nothing
    new to add" both silently produce zero bars here (see
    marketdata._read_bars's `if not path.exists(): return []`), so a
    genuinely missing intraday file for the session being backfilled was
    indistinguishable from an ordinary quiet outcome. Logs an ERROR line
    per symbol whose expected intraday cache file doesn't exist, so that
    specific failure shape is loud in the logs even though the return
    value (a bare int) has no room to carry it — see runner.py's caller,
    which alerts on the aggregate mark count regardless of which symbols
    caused it."""
    cache_dir = Path(cache_dir)
    rows = conn.execute(
        "SELECT id, symbol, ts_utc FROM detections WHERE session = ?", (session.isoformat(),)
    ).fetchall()

    bars_by_symbol: dict[str, list] = {}
    missing_cache_symbols: set[str] = set()
    written = 0
    for detection_id, symbol, ts_utc in rows:
        if symbol not in bars_by_symbol:
            intraday_path = cache_dir / symbol / f"intraday_{session.isoformat()}.csv"
            if not intraday_path.exists():
                missing_cache_symbols.add(symbol)
            bars_by_symbol[symbol] = _all_bars_for_session(cache_dir, symbol, session)
        bars = bars_by_symbol[symbol]
        detection_ts = datetime.fromisoformat(ts_utc)
        for offset in offsets_min:
            target = detection_ts + timedelta(minutes=offset)
            price = _price_at_or_after(bars, target)
            if price is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO marks (detection_id, offset_min, price) VALUES (?, ?, ?)",
                (detection_id, offset, price),
            )
            written += 1
        if bars:
            conn.execute(
                "INSERT OR REPLACE INTO marks (detection_id, offset_min, price) VALUES (?, ?, ?)",
                (detection_id, CLOSE_MARK_OFFSET_MIN, bars[-1].close),
            )
            written += 1
    if missing_cache_symbols:
        logger.error(
            "backfill_marks(session=%s): no cached intraday file for %s -- outcomes for "
            "these symbols' detections cannot be computed until it exists",
            session.isoformat(), ", ".join(sorted(missing_cache_symbols)),
        )
    conn.commit()
    return written


@dataclass(frozen=True)
class HistoricalPerformance:
    sample_size: int
    continuation_rate: float  # fraction that moved further in the alert's direction by offset_min
    avg_return_pct: float  # signed average return at offset_min
    offset_min: int
    # Same average move, but normalized by each historical row's OWN
    # atr14 (real per-instance data from the detections table) rather
    # than the current decision's atr14 — a %-move average can't be
    # converted to "typical ATR units" after the fact without each
    # instance's own scale. None only when every row in the sample is
    # missing atr14 (never fabricated by borrowing today's ATR instead).
    avg_return_atr: float | None = None


def historical_performance(
    conn: sqlite3.Connection,
    kind: str,
    trend: str,
    exclude_id: str,
    lookback: int = 20,
    offset_min: int = 30,
) -> HistoricalPerformance | None:
    """How past clusters with this same primary detector kind and trend
    direction actually played out, using real backfilled forward prices —
    a base rate from the journal's own history, not a prediction. Returns
    None if there isn't at least MIN_HISTORY_SAMPLE of them yet; never
    reports a stat built on too few data points to mean anything.

    Matches on the real primary_kind column (exact match), not a LIKE
    scan over the full multi-detector `kinds` string — the old fuzzy
    match would count a row where `kind` fired as a SECONDARY detector,
    not the primary/headline one, as if it were a same-kind setup. Rows
    written before primary_kind existed have it NULL and so never match
    here — excluded rather than fuzzy-matched, the same "never fabricate"
    call as everywhere else in this module.

    Excludes news-driven history (news_driven=1) from the sample: these
    stats are continuation rates for TECHNICAL setups, and an event-driven
    move (earnings, an 8-K, a macro print) doesn't share that mechanism —
    mixing it into the sample would let event noise masquerade as a
    technical base rate. See tradebot.events module docstring.

    Also excludes pre-cutover-feed history and broad_scan-promoted
    ("screening") symbols — see CURRENT_FEED_FILTER_SQL's docstring."""
    rows = conn.execute(
        f"""
        SELECT d.close, m.price, d.atr14
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.primary_kind = ? AND d.trend = ? AND d.id != ?
              AND (d.news_driven IS NULL OR d.news_driven = 0)
              AND {CURRENT_FEED_FILTER_SQL}
        ORDER BY d.ts_utc DESC
        LIMIT ?
        """,
        (offset_min, kind, trend, exclude_id, lookback),
    ).fetchall()
    if len(rows) < MIN_HISTORY_SAMPLE:
        return None

    # Signed to the DETECTION's own trend, same convention tier_performance()/
    # kind_performance() already use -- a continuation always reports positive
    # here, regardless of whether the detection itself called "up" or "down".
    # Previously left un-flipped: continuation_rate correctly interpreted
    # direction via the if/else below, but avg_return_pct summed the raw,
    # un-flipped returns, so a down-trend detection that continued down
    # reported a NEGATIVE avg_return_pct here while the same row would
    # report POSITIVE in tier_performance()/kind_performance() -- the same
    # 5 historical rows disagreeing on their own sign depending which
    # endpoint asked.
    returns = [
        ((price - close) / close) if trend == "up" else -((price - close) / close)
        for close, price, _atr14 in rows
    ]
    continued = sum(1 for r in returns if r > 0)

    # Signed and trend-flipped, the SAME per-entry convention as `returns`
    # above -- previously abs(), which made this an unsigned magnitude
    # that could contradict avg_return_pct's sign in the UI ("0.07%
    # (~-0.15x ATR)", design review H5). One convention, both figures.
    atr_normalized = [
        (((price - close) / atr14) if trend == "up" else -((price - close) / atr14))
        for close, price, atr14 in rows
        if atr14
    ]
    avg_return_atr = sum(atr_normalized) / len(atr_normalized) if atr_normalized else None

    return HistoricalPerformance(
        sample_size=len(returns),
        continuation_rate=continued / len(returns),
        avg_return_pct=sum(returns) / len(returns) * 100,
        offset_min=offset_min,
        avg_return_atr=avg_return_atr,
    )


@dataclass(frozen=True)
class TierPerformance:
    tier: str
    sample_size: int
    continuation_rate: float
    avg_return_pct: float
    offset_min: int


def tier_performance(conn: sqlite3.Connection, offset_min: int = 30) -> dict[str, TierPerformance]:
    """Real continuation rate and average directional return per tier,
    across the whole journal, using backfilled forward prices — the same
    'is this tier actually predictive' check as historical_performance(),
    aggregated by tier instead of by kind. Tiers with fewer than
    MIN_HISTORY_SAMPLE data points are omitted rather than reported on
    too little data.

    Also excludes pre-cutover-feed history and broad_scan-promoted
    ("screening") symbols — see CURRENT_FEED_FILTER_SQL's docstring."""
    rows = conn.execute(
        f"""
        SELECT d.tier, d.close, d.trend, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE {CURRENT_FEED_FILTER_SQL}
        """,
        (offset_min,),
    ).fetchall()

    by_tier: dict[str, list[float]] = {}
    for tier, close, trend, price in rows:
        r = (price - close) / close
        signed = r if trend == "up" else -r
        by_tier.setdefault(tier, []).append(signed)

    result: dict[str, TierPerformance] = {}
    for tier, returns in by_tier.items():
        if len(returns) < MIN_HISTORY_SAMPLE:
            continue
        result[tier] = TierPerformance(
            tier=tier,
            sample_size=len(returns),
            continuation_rate=sum(1 for r in returns if r > 0) / len(returns),
            avg_return_pct=sum(returns) / len(returns) * 100,
            offset_min=offset_min,
        )
    return result


@dataclass(frozen=True)
class KindPerformance:
    kind: str
    sample_size: int
    continuation_rate: float
    avg_return_pct: float
    median_return_pct: float
    offset_min: int
    excluded_news_driven: int


def kind_performance(conn: sqlite3.Connection, offset_min: int = 30) -> dict[str, KindPerformance]:
    """Real continuation rate and average/median directional return per
    primary detector kind, across the whole journal -- the same 'is this
    actually predictive' check as tier_performance(), grouped by kind
    instead of tier.

    Deliberately diverges from tier_performance() in one way: news-driven
    rows (news_driven=1) are excluded, the same filter
    historical_performance() already applies to the per-signal "same
    setup" stats shown in the modal -- this function and that one need to
    report on the same population. The Performance tab claims to answer
    "is this technical setup predictive," and an event-driven move
    (earnings, an 8-K, a macro print) doesn't share that mechanism; mixing
    it in would let event noise masquerade as a technical base rate, same
    reasoning as historical_performance()'s own docstring.

    Rows with primary_kind IS NULL (written before that column existed)
    are excluded rather than grouped into a fake 'None' kind -- same
    discipline historical_performance() already applies to kind matching.
    Kinds with fewer than MIN_HISTORY_SAMPLE clean (non-news-driven) data
    points are omitted, same as tier_performance() -- excluded_news_driven
    only appears for kinds that clear that bar on their remaining clean
    sample; a kind with no reportable clean sample doesn't appear at all,
    same as today.

    Also excludes pre-cutover-feed history and broad_scan-promoted
    ("screening") symbols — see CURRENT_FEED_FILTER_SQL's docstring."""
    rows = conn.execute(
        f"""
        SELECT d.primary_kind, d.close, d.trend, m.price, d.news_driven
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.primary_kind IS NOT NULL
              AND {CURRENT_FEED_FILTER_SQL}
        """,
        (offset_min,),
    ).fetchall()

    by_kind: dict[str, list[float]] = {}
    excluded_news_driven: dict[str, int] = {}
    for kind, close, trend, price, news_driven in rows:
        if news_driven:
            excluded_news_driven[kind] = excluded_news_driven.get(kind, 0) + 1
            continue
        r = (price - close) / close
        signed = r if trend == "up" else -r
        by_kind.setdefault(kind, []).append(signed)

    result: dict[str, KindPerformance] = {}
    for kind, returns in by_kind.items():
        if len(returns) < MIN_HISTORY_SAMPLE:
            continue
        result[kind] = KindPerformance(
            kind=kind,
            sample_size=len(returns),
            continuation_rate=sum(1 for r in returns if r > 0) / len(returns),
            avg_return_pct=sum(returns) / len(returns) * 100,
            median_return_pct=statistics.median(returns) * 100,
            offset_min=offset_min,
            excluded_news_driven=excluded_news_driven.get(kind, 0),
        )
    return result


@dataclass(frozen=True)
class HourPerformance:
    hour_et: int
    sample_size: int
    continuation_rate: float
    avg_return_pct: float
    offset_min: int


def hour_performance(
    conn: sqlite3.Connection, tier: str | None = "high", offset_min: int = 30
) -> dict[int, HourPerformance]:
    """Real continuation rate and average directional return grouped by
    the ET hour the cluster fired in, across the whole journal — a pure
    reporting tool, never used to gate or suppress alerts.

    IMPORTANT: a train/test split on this project's data (see
    SCANNER_PLAN.md) showed hour-of-day patterns that looked real on one
    half of the data completely inverted on the other half — a signature
    of noise, not a stable effect, at the sample sizes available when
    that check was run. Do not treat any single hour's numbers here as
    a real edge without re-validating on a proper held-out split first;
    this function exists so that check gets easier to redo as more
    sessions accumulate, not as a ready-to-trust signal today.

    tier=None includes every non-log tier; pass a specific tier (e.g.
    "high") to scope to just that one. Hours with fewer than
    MIN_HISTORY_SAMPLE data points are omitted rather than reported on
    too little data.
    """
    if tier is None:
        rows = conn.execute(
            """
            SELECT d.ts_utc, d.close, d.trend, m.price
            FROM detections d
            JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
            WHERE d.tier != 'log'
            """,
            (offset_min,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT d.ts_utc, d.close, d.trend, m.price
            FROM detections d
            JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
            WHERE d.tier = ?
            """,
            (offset_min, tier),
        ).fetchall()

    by_hour: dict[int, list[float]] = {}
    for ts_utc, close, trend, price in rows:
        hour = datetime.fromisoformat(ts_utc).astimezone(ET).hour
        r = (price - close) / close
        signed = r if trend == "up" else -r
        by_hour.setdefault(hour, []).append(signed)

    result: dict[int, HourPerformance] = {}
    for hour, returns in by_hour.items():
        if len(returns) < MIN_HISTORY_SAMPLE:
            continue
        result[hour] = HourPerformance(
            hour_et=hour,
            sample_size=len(returns),
            continuation_rate=sum(1 for r in returns if r > 0) / len(returns),
            avg_return_pct=sum(returns) / len(returns) * 100,
            offset_min=offset_min,
        )
    return result


# --------------------------------------------------------------------------
# IV history — a local, real-data cache so IV rank becomes computable over
# time without ever fabricating one. One sample per symbol per session
# (upserted, so re-running a session is idempotent, same as write_cluster).
# --------------------------------------------------------------------------


def record_iv_sample(conn: sqlite3.Connection, symbol: str, session: date, iv: float) -> None:
    conn.execute(
        "INSERT INTO iv_history (symbol, session, iv) VALUES (?, ?, ?) "
        "ON CONFLICT(symbol, session) DO UPDATE SET iv = excluded.iv",
        (symbol, session.isoformat(), iv),
    )
    conn.commit()


def iv_rank(conn: sqlite3.Connection, symbol: str, current_iv: float, lookback_sessions: int = 252) -> tuple[float | None, int]:
    """Standard IV Rank: where current_iv sits between the lowest and
    highest IV over the trailing lookback_sessions, as a 0-100 percentile
    of that RANGE (not a percentile of days below it — that's IV
    Percentile, a different, commonly-confused metric). Returns
    (None, sample_size) rather than a rank computed on a degenerate
    (min == max) or empty window — never fabricated, never divided by
    zero."""
    rows = conn.execute(
        "SELECT iv FROM iv_history WHERE symbol = ? ORDER BY session DESC LIMIT ?",
        (symbol, lookback_sessions),
    ).fetchall()
    sample = len(rows)
    if sample == 0:
        return None, 0
    ivs = [r[0] for r in rows]
    lo, hi = min(ivs), max(ivs)
    if hi == lo:
        return None, sample
    rank = (current_iv - lo) / (hi - lo) * 100
    return max(0.0, min(100.0, rank)), sample


# --------------------------------------------------------------------------
# Contract selections — what select_contract() actually chose, so it can
# be checked against reality later. strike/DTE/delta/entry mid up front;
# forward mids at +15/30/60m are backfilled once they're knowable (live
# only — see costs.select_contract and marketdata.ReplayMarketData.chain,
# there's no cached historical options data to replay against).
# --------------------------------------------------------------------------


def record_contract_selection(
    conn: sqlite3.Connection,
    detection_id: str,
    *,
    symbol: str,
    right: str,
    strike: float,
    expiry: date,
    dte: int,
    delta: float | None,
    entry_mid: float,
    entry_ts: datetime,
    is_vertical: bool = False,
    short_strike: float | None = None,
    short_delta: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO contract_selections
            (detection_id, symbol, right, strike, expiry, dte, delta, is_vertical,
             short_strike, short_delta, entry_mid, entry_ts_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(detection_id) DO UPDATE SET
            symbol=excluded.symbol, right=excluded.right, strike=excluded.strike,
            expiry=excluded.expiry, dte=excluded.dte, delta=excluded.delta,
            is_vertical=excluded.is_vertical, short_strike=excluded.short_strike,
            short_delta=excluded.short_delta, entry_mid=excluded.entry_mid,
            entry_ts_utc=excluded.entry_ts_utc
        """,
        (
            detection_id, symbol, right, strike, expiry.isoformat(), dte, delta, int(is_vertical),
            short_strike, short_delta, entry_mid, entry_ts.isoformat(),
        ),
    )
    conn.commit()


_CONTRACT_MID_COLUMNS = {15: "mid_15m", 30: "mid_30m", 60: "mid_60m", CLOSE_MARK_OFFSET_MIN: "mid_close"}


def record_contract_forward_mid(conn: sqlite3.Connection, detection_id: str, offset_min: int, mid: float) -> None:
    column = _CONTRACT_MID_COLUMNS.get(offset_min)
    if column is None:
        raise ValueError(f"unsupported contract forward-mid offset: {offset_min} (only 15/30/60/close are journaled)")
    conn.execute(f"UPDATE contract_selections SET {column} = ? WHERE detection_id = ?", (mid, detection_id))
    conn.commit()


def pending_contract_backfills(conn: sqlite3.Connection, older_than: datetime, offset_min: int) -> list[tuple]:
    """Selections whose entry is at least offset_min old but don't have
    that forward mid recorded yet — what a live backfill loop should
    still go fetch every iteration for the 15/30/60m checkpoints (see
    runner.py's backfill_pending_contract_mids). Returns (detection_id,
    symbol, right, strike, expiry, is_vertical, short_strike) rows —
    is_vertical/short_strike are needed to compute the SAME long-minus-
    short mid formula used for entry_mid, not just the long leg's price.

    CLOSE_MARK_OFFSET_MIN isn't valid here: "close" isn't due at a fixed
    number of minutes after entry, it's due once the session itself has
    ended — see pending_contract_close_backfills for that case."""
    column = _CONTRACT_MID_COLUMNS.get(offset_min)
    if column is None or offset_min == CLOSE_MARK_OFFSET_MIN:
        raise ValueError(f"unsupported contract forward-mid offset: {offset_min} (use pending_contract_close_backfills for close)")
    cutoff = (older_than - timedelta(minutes=offset_min)).isoformat()
    rows = conn.execute(
        f"SELECT detection_id, symbol, right, strike, expiry, is_vertical, short_strike FROM contract_selections "
        f"WHERE entry_ts_utc <= ? AND {column} IS NULL",
        (cutoff,),
    ).fetchall()
    return rows


def pending_contract_close_backfills(conn: sqlite3.Connection, session: date) -> list[tuple]:
    """Every contract_selections row from `session` still missing its
    close mid — what a live end-of-session backfill pass should fetch
    (see runner.py's backfill_pending_contract_close_mids). Unlike
    pending_contract_backfills (elapsed-time gated, for the 15/30/60m
    checkpoints), 'close' is due once the session has ended, which the
    caller already knows by the time it calls this — no cutoff math."""
    rows = conn.execute(
        """
        SELECT cs.detection_id, cs.symbol, cs.right, cs.strike, cs.expiry, cs.is_vertical, cs.short_strike
        FROM contract_selections cs
        JOIN detections d ON d.id = cs.detection_id
        WHERE d.session = ? AND cs.mid_close IS NULL
        """,
        (session.isoformat(),),
    ).fetchall()
    return rows


def record_contract_day_range(conn: sqlite3.Connection, detection_id: str, day_low: float, day_high: float) -> None:
    """The contract's own real intraday trade-price range for the day —
    distinct from the mid_* columns (bid/ask midpoints at fixed
    checkpoints): this is the actual low/high across every real trade in
    the contract that session, i.e. the full range anyone trading it that
    day could have captured, independent of our specific entry timing."""
    conn.execute(
        "UPDATE contract_selections SET day_low = ?, day_high = ? WHERE detection_id = ?",
        (day_low, day_high, detection_id),
    )
    conn.commit()


def pending_contract_day_range_backfills(conn: sqlite3.Connection, session: date) -> list[tuple]:
    """Every SINGLE-LEG contract_selections row from `session` still
    missing its day range — same due-once-the-session-ends timing as
    pending_contract_close_backfills, fetched via a separate vendor call
    (a full intraday bar series, not a point-in-time chain snapshot).
    Verticals are excluded on purpose: a spread's real day range isn't
    the sum of its two legs' independent ranges (they don't hit their
    extremes at the same moment), so there's no honest single number to
    report for one."""
    rows = conn.execute(
        """
        SELECT cs.detection_id, cs.symbol, cs.right, cs.strike, cs.expiry
        FROM contract_selections cs
        JOIN detections d ON d.id = cs.detection_id
        WHERE d.session = ? AND cs.day_low IS NULL AND cs.is_vertical = 0
        """,
        (session.isoformat(),),
    ).fetchall()
    return rows


@dataclass(frozen=True)
class ContractOutcome:
    """Everything /took's "how'd it play out" follow-up needs — one
    query, joined against the one contract_selections row for this
    detection. Every field is None until the corresponding backfill has
    actually run; never fabricated to fill a gap."""

    symbol: str
    right: str
    strike: float
    expiry: str
    entry_mid: float
    mid_30m: float | None
    mid_60m: float | None
    mid_close: float | None
    day_low: float | None
    day_high: float | None


def get_contract_outcome(conn: sqlite3.Connection, detection_id: str) -> ContractOutcome | None:
    row = conn.execute(
        "SELECT symbol, right, strike, expiry, entry_mid, mid_30m, mid_60m, mid_close, day_low, day_high "
        "FROM contract_selections WHERE detection_id = ?",
        (detection_id,),
    ).fetchone()
    return ContractOutcome(*row) if row else None


# --------------------------------------------------------------------------
# Decision ledger — an append-only record of the individual decisions taken
# about a detection, in the order they were taken. Every other write path in
# this module overwrites: write_cluster upserts the detections row,
# set_no_trade/set_news_driven/set_extreme_mover UPDATE columns on it. That
# makes `detections` a snapshot of latest-known state and leaves it unable,
# by construction, to answer "what did we decide, when, and on what basis" —
# which is the only question that matters when an alert that should have
# fired didn't. This table answers that, and answers it the same way
# afterwards as it did at the time, because nothing here is ever rewritten.
#
# runner.py's process_new_bar writes to this — see its call sites for which
# decision points are recorded and which deliberately are not.
# --------------------------------------------------------------------------

MAX_DECISION_DETAIL_JSON_LEN = 2000

# The two values run_mode takes. Spelled here rather than in runner.py so
# that a reader of the ledger and a writer of it share one vocabulary: a
# query filtering out replays is only correct if it knows the exact string
# the writer used, and a second spelling of "replay" somewhere else would
# silently return the wrong rows rather than fail.
RUN_MODE_LIVE = "live"
RUN_MODE_REPLAY = "replay"


@dataclass(frozen=True)
class DecisionEvent:
    """One appended row. `seq` is the ledger's own append order, not
    anything derived from ts_utc — two events can share a timestamp (the
    same pass through the pipeline resolving several decisions off one
    clock read), and seq still totally orders them."""

    seq: int
    detection_id: str
    ts_utc: str
    stage: str
    decision: str
    reason: str | None
    detail: dict | None
    code_version: str | None
    run_id: str | None
    run_mode: str | None


def record_decision_event(
    conn: sqlite3.Connection,
    detection_id: str,
    *,
    stage: str,
    decision: str,
    reason: str | None = None,
    detail: dict | None = None,
    ts_utc: datetime | None = None,
    code_version_str: str | None = None,
    run_id: str | None = None,
    run_mode: str | None = None,
    commit: bool = True,
) -> int:
    """Append one decision event. Returns its `seq`.

    Appends unconditionally: there is no upsert key and no de-duplication.
    Recording the same stage/decision pair twice for one detection is a
    real fact about what happened (the pipeline reached that point twice),
    not a mistake to collapse — the same reasoning that keeps sub-threshold
    'log' tier clusters in the detections table rather than dropping them.

    ts_utc: when the decision was actually taken. Defaults to wall clock
    for the ordinary live caller, but is a parameter so a caller that
    already knows the decision's real timestamp passes it rather than
    letting it drift to "whenever the write happened", and so tests are
    deterministic — same injectable-clock discipline as runner.py's
    validation_now_fn. Stored as ISO-8601; naive datetimes are assumed UTC
    and stamped as such rather than silently recorded without an offset.

    detail: structured context for this one decision (thresholds compared,
    values seen). Serialized to JSON; dropped rather than truncated if it
    exceeds MAX_DECISION_DETAIL_JSON_LEN, since half a JSON document is
    not a smaller fact, it's an unparseable one — same all-or-nothing
    choice funnel_events.record_event makes with props.

    code_version_str: the caller's code_version() at decision time, same
    frozen-at-write-time discipline as write_cluster's. None (the default)
    means the caller didn't record one — never back-filled from a later
    code_version() call, which would attribute a decision to code that
    didn't make it.

    run_id/run_mode: which invocation of the scanner took this decision,
    and whether it was RUN_MODE_LIVE or RUN_MODE_REPLAY. See the columns'
    comment in SCHEMA for why an append-only ledger needs them at write
    time: nothing can label these rows later. Both default to None, which
    records honestly that the caller didn't say — a script or a test
    appending an event is not a run.

    commit: True (the default) commits immediately, for a caller whose
    event is the whole of its transaction. Pass False when this append is
    part of a larger unit of work the caller commits itself — which is
    what runner.py's process_new_bar does, because committing here would
    flush that function's still-pending detection writes at a moment it
    did not choose. See _commit_then_send there: journal.db's commit
    points are ordered against an irreversible send on another database,
    and a helper that commits whenever it feels like it silently takes
    that ordering away from the code responsible for it. The row is
    written to the connection either way; only durability waits."""
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    elif ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)

    detail_json = None
    if detail:
        encoded = json.dumps(detail, separators=(",", ":"), sort_keys=True, default=str)
        if len(encoded) <= MAX_DECISION_DETAIL_JSON_LEN:
            detail_json = encoded
        else:
            logger.warning(
                "record_decision_event(detection_id=%s, stage=%s): detail JSON is %d bytes "
                "(limit %d) -- recording the event without it",
                detection_id, stage, len(encoded), MAX_DECISION_DETAIL_JSON_LEN,
            )

    cursor = conn.execute(
        """
        INSERT INTO decision_events
            (detection_id, ts_utc, stage, decision, reason, detail_json, code_version, run_id, run_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            detection_id, ts_utc.isoformat(), stage, decision, reason, detail_json,
            code_version_str, run_id, run_mode,
        ),
    )
    if commit:
        conn.commit()
    return cursor.lastrowid


def decision_events_for_detection(conn: sqlite3.Connection, detection_id: str) -> list[DecisionEvent]:
    """Every event appended for this detection, in append order. Read-only
    counterpart to record_decision_event — no HTTP endpoint, no UI; this
    is what a shell, a test, or a future consumer reads the ledger with."""
    rows = conn.execute(
        """
        SELECT seq, detection_id, ts_utc, stage, decision, reason, detail_json, code_version,
               run_id, run_mode
        FROM decision_events WHERE detection_id = ? ORDER BY seq
        """,
        (detection_id,),
    ).fetchall()
    return [
        DecisionEvent(
            seq=seq, detection_id=det_id, ts_utc=ts, stage=stage, decision=decision,
            reason=reason, detail=json.loads(detail_json) if detail_json else None,
            code_version=code_ver, run_id=run_id, run_mode=run_mode,
        )
        for seq, det_id, ts, stage, decision, reason, detail_json, code_ver, run_id, run_mode in rows
    ]
