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
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dataclasses import dataclass

from tradebot.config import liquidity_class
from tradebot.detectors import Detection, bar_close_ts, tier_for_score
from tradebot.marketdata import ReplayMarketData, XNYS

logger = logging.getLogger("watchtower.journal")
ET = ZoneInfo("America/New_York")

MIN_HISTORY_SAMPLE = 5

# Decision B (docs/sip-migration-proposal.md, Option 1 -- post-cutover-only):
# historical_performance()/tier_performance()/kind_performance()/
# hour_performance() never blend rows written under different data feeds into
# one continuation-rate number
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
# since both filters touch the same four queries and both are about "don't
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
MARK_STATUS_AVAILABLE = "AVAILABLE"
MARK_STATUS_NOT_REACHED = "NOT_REACHED_BEFORE_CLOSE"
MARK_STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
MARK_STATUS_PENDING = "PENDING"
MARK_STATUS_WAITING = "WAITING_FOR_CLOSE_BATCH"
MARK_STATUS_DELAYED = "DELAYED"
OUTCOME_BACKFILL_GRACE_MINUTES = 15

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "journal.db"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"

# Where a replay writes when it wasn't told where to write. A sibling of
# the production journal rather than a temp dir: replay output is meant to
# be kept and queried (the SIP Phase 1 backtest and compare_replay both
# read theirs back), it just isn't meant to land on top of the live
# record. data/ is gitignored wholesale, so this needs no new ignore rule.
REPLAY_DB_PATH = REPO_ROOT / "data" / "journal_replay.db"


class ProductionJournalRefused(ValueError):
    """Raised when a replay asks to write to the production journal
    without saying so explicitly.

    A ValueError subclass so a caller that only knows it passed a bad
    path still catches it, and a distinct type so the two CLIs can turn
    exactly this into a parser.error() without swallowing unrelated
    ValueErrors from the same call."""


def resolve_replay_db_path(db_path=None, *, allow_production_db: bool = False) -> Path:
    """Where this replay is allowed to write. Pure: resolves, validates,
    returns a Path or raises. No printing, no exiting, no I/O — the CLIs
    own how a refusal is presented (see runner.main / scripts/replay.py).

    Replay reproduces live detection ids exactly (cluster_id hashes
    symbol/session/ts/kinds), so a replay pointed at the production
    journal upserts onto the live rows and then keeps mutating them
    through the setters that run after write_cluster — set_no_trade in
    particular ALWAYS writes no_trade=1 in replay, because
    ReplayMarketData has no options chain. Rather than teaching every one
    of those writes to recognise a replay, this settles WHICH JOURNAL a
    replay writes, one level up: it writes somewhere else unless a human
    insists otherwise.

    Scoped to the journal, and to nothing else. A replay still has other
    production side effects that this function neither sees nor claims to
    address — most concretely metrics.increment(), which process_new_bar
    calls throughout and which writes data/metrics.json by default.

    db_path=None means "the caller didn't choose" and resolves to
    REPLAY_DB_PATH — never DEFAULT_DB_PATH. An explicit path is honoured
    as-is, which keeps every existing --db-path workflow (compare_replay's
    journal_a/journal_b, the SIP cache comparison) working untouched.

    allow_production_db is the deliberate escape hatch, and it is the only
    way to reach DEFAULT_DB_PATH. Comparison is on resolved paths, so
    'data/../data/journal.db', a relative 'data/journal.db' from the repo
    root, and a symlink to it are all the same path as far as the guard is
    concerned — a guard that only string-matched would be a guard in name
    only."""
    candidate = REPLAY_DB_PATH if db_path is None else Path(db_path)
    if allow_production_db:
        return candidate.resolve()

    resolved = candidate.resolve()
    if resolved == DEFAULT_DB_PATH.resolve():
        raise ProductionJournalRefused(
            f"refusing to replay into the production journal ({resolved}). A replay "
            f"reproduces live detection ids and would overwrite the live record's own "
            f"decision state. Leave the path unset to use {REPLAY_DB_PATH}, pass a "
            f"different --db-path, or pass --allow-production-replay-db if overwriting "
            f"the production journal is genuinely what you want."
        )
    return resolved

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

-- Append-only resolution truth for every regular-session outcome checkpoint.
-- A missing marks row alone cannot distinguish "not due", "late detection",
-- and "the close batch ran but data was unavailable". Each backfill attempt
-- records that distinction without rewriting earlier attempts; marks remains
-- the compact latest-price table consumed by historical analytics.
CREATE TABLE IF NOT EXISTS mark_resolution_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    detection_id TEXT NOT NULL,
    session TEXT NOT NULL,
    offset_min INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('AVAILABLE', 'NOT_REACHED_BEFORE_CLOSE', 'DATA_UNAVAILABLE')
    ),
    reason TEXT,
    price REAL,
    created_at TEXT NOT NULL,
    code_version TEXT NOT NULL,
    CHECK (
        (status = 'AVAILABLE' AND price IS NOT NULL)
        OR (status != 'AVAILABLE' AND price IS NULL)
    ),
    UNIQUE (attempt_id, detection_id, offset_min)
);
CREATE INDEX IF NOT EXISTS idx_mark_resolution_events_latest
    ON mark_resolution_events(detection_id, offset_min, event_id DESC);
CREATE TRIGGER IF NOT EXISTS mark_resolution_events_no_update
BEFORE UPDATE ON mark_resolution_events
BEGIN
    SELECT RAISE(ABORT, 'mark_resolution_events is append-only: UPDATE is not permitted');
END;
CREATE TRIGGER IF NOT EXISTS mark_resolution_events_no_delete
BEFORE DELETE ON mark_resolution_events
BEGIN
    SELECT RAISE(ABORT, 'mark_resolution_events is append-only: DELETE is not permitted');
END;

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
    event_date TEXT,
    event_timing TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_windows_symbol_time ON event_windows(symbol, start_utc, end_utc);
-- COALESCE so a market-wide row (symbol IS NULL) still dedupes against
-- itself on repeated ingestion runs — SQL UNIQUE treats NULL != NULL,
-- which would otherwise let every macro-calendar refresh insert a copy.
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_windows_dedup
    ON event_windows(COALESCE(symbol, ''), kind, start_utc, source);

-- Append-only provenance for scheduled-event ingestion. An empty
-- event_windows result is otherwise ambiguous: it can mean "the provider
-- reported no events" or "the provider failed and the adapter returned
-- nothing." These rows preserve that distinction permanently and make
-- every session's claimed catalyst coverage auditable.
CREATE TABLE IF NOT EXISTS event_ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    report_date TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    universe_scope TEXT NOT NULL,
    requested_symbols INTEGER NOT NULL,
    fetched_events INTEGER,
    matched_events INTEGER,
    windows_created INTEGER,
    error TEXT,
    code_version TEXT,
    run_mode TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ingestion_runs_lookup
    ON event_ingestion_runs(kind, report_date, universe_scope, status, completed_at);
CREATE TRIGGER IF NOT EXISTS event_ingestion_runs_no_update
BEFORE UPDATE ON event_ingestion_runs
BEGIN
    SELECT RAISE(ABORT, 'event_ingestion_runs is append-only: UPDATE is not permitted');
END;
CREATE TRIGGER IF NOT EXISTS event_ingestion_runs_no_delete
BEFORE DELETE ON event_ingestion_runs
BEGIN
    SELECT RAISE(ABORT, 'event_ingestion_runs is append-only: DELETE is not permitted');
END;

-- Append-only ledger of the individual decisions taken about a detection
-- on its way through the pipeline. Deliberately NOT columns on
-- `detections`: that row is a mutable snapshot of the cluster's latest
-- known state (write_cluster upserts it, set_no_trade/set_news_driven/
-- set_extreme_mover overwrite fields on it), which by construction cannot
-- answer "what did we decide, in what order, and on what basis, at the
-- time." One detection has many decision_events; a decision_event is
-- never revised, only superseded by a later one appended after it.
--
-- runner.process_new_bar writes the five decisions it takes that the
-- `detections` snapshot cannot express on its own: a dedup lookup that
-- failed and forced WATCH, a HIGH routed down to MEDIUM by an event
-- window, the resolved alert-routing outcome, a data-guard rejection,
-- and the contract-selection outcome. Deliberately NOT a record of
-- everything that happened: no row for a guard that passed, a dedup that
-- agreed, a bar with no detection, or a window that didn't apply. A
-- ledger of non-events buries the ones that are.
CREATE TABLE IF NOT EXISTS decision_events (
    -- AUTOINCREMENT (not bare rowid): guarantees a strictly increasing
    -- seq that is never reused, so ordering by seq is a real append
    -- order for the life of the file. Matches event_windows above.
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    -- detections.id. Loose reference, no SQLite FK constraint -- same
    -- style as marks.detection_id / contract_selections.detection_id.
    detection_id TEXT NOT NULL,
    -- EXECUTION time, not market time: when this run took and recorded
    -- the decision. Deliberately a different clock from
    -- detections.ts_utc, which is the market/bar time the detection is
    -- about. The two answer different questions and must not be read as
    -- the same one:
    --
    --   detections.ts_utc     -- WHEN IN THE MARKET. The bar's own
    --                            timestamp (CLAUDE.md: the OPEN of the
    --                            bar, UTC). Identical across every run
    --                            that ever evaluates that bar.
    --   decision_events.ts_utc -- WHEN THIS EXECUTION RAN. Wall clock at
    --                            the moment the decision was taken.
    --
    -- During replay this is therefore real 'now' -- the moment the
    -- replay was executed -- NOT the historical session time being
    -- replayed. That is the intended reading: it is a record of when
    -- this process decided something, and a replay run in 2026 really
    -- did decide it in 2026. Anyone asking 'what market moment was this
    -- about' joins to detections.ts_utc, and anyone asking 'which
    -- execution was this' reads run_mode/run_id below -- so nothing is
    -- lost by this column meaning exactly one thing.
    ts_utc TEXT NOT NULL,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    detail_json TEXT,
    code_version TEXT,
    -- Which execution wrote this row. Without them a replay of an old
    -- session appends events that read exactly like the live ones from
    -- that day: same detection_id (cluster_id is a hash of
    -- symbol/session/ts/kinds, so it is stable across runs), same
    -- stages, same decisions, appended after them and therefore looking
    -- like the later, superseding truth. Replaying a session is a normal
    -- thing to do repeatedly, and the ledger is append-only by design, so
    -- there is no cleanup afterwards -- the only defence is that every
    -- row states which run it came from. NOT NULL with a default rather
    -- than nullable: 'this row did not say' is a fact the ledger should
    -- state out loud ('unknown'/'unattributed'), never a NULL that a
    -- reader is free to assume means live.
    run_mode TEXT NOT NULL DEFAULT 'unknown',
    run_id TEXT NOT NULL DEFAULT 'unattributed'
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
    _add_column_if_missing(conn, "event_windows", "event_date", "TEXT")
    _add_column_if_missing(conn, "event_windows", "event_timing", "TEXT")
    # Owner decision 2026-08-17 superseded the original earnings policy:
    # earnings are context and signal, never suppression/downgrade. Update
    # any windows written by an older build so a pre-existing row cannot
    # keep enforcing the retired policy after the new classifier ships.
    conn.execute(
        "UPDATE event_windows SET severity = 'context' "
        "WHERE kind = 'earnings' AND source = 'nasdaq_earnings' AND severity != 'context'"
    )
    # Rows written by the catalyst-ledger release before these structured
    # columns existed used one source-owned, deterministic detail shape.
    # Backfill only that source and only NULLs; manual/other-provider text
    # is not parsed or guessed.
    conn.execute(
        """
        UPDATE event_windows
        SET event_date = substr(detail, -10),
            event_timing = CASE
                WHEN instr(detail, '(pre-market)') > 0 THEN 'pre-market'
                WHEN instr(detail, '(after-hours)') > 0 THEN 'after-hours'
                WHEN instr(detail, '(unspecified)') > 0 THEN 'unspecified'
                ELSE NULL
            END
        WHERE kind = 'earnings' AND source = 'nasdaq_earnings'
          AND detail IS NOT NULL
          AND instr(detail, '), reported ') > 0
          AND (event_date IS NULL OR event_timing IS NULL)
        """
    )
    conn.commit()
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
    # decision_events shipped one release before anything wrote to it, so
    # a journal.db created in that window has the table but not these two
    # columns. NOT NULL is safe on ADD COLUMN here precisely because a
    # non-null DEFAULT is supplied, and no existing row can conflict with
    # it: nothing has ever written to this table.
    _add_column_if_missing(conn, "decision_events", "run_mode", "TEXT NOT NULL DEFAULT 'unknown'")
    _add_column_if_missing(conn, "decision_events", "run_id", "TEXT NOT NULL DEFAULT 'unattributed'")
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
    origin: str | None = None,
    pct_from_prior_close: float | None = None,
    pct_from_prior_close_status: str | None = None,
) -> str:
    """Upserts by cluster identity: re-writing the same
    (symbol, session, ts_utc, kinds) refreshes the row rather than
    duplicating it — see cluster_id(). What that refresh may touch is
    deliberately narrow, because the same identity is reproduced exactly
    by any replay of a session that already ran live (scripts/replay.py
    defaults to the production journal and re-runs every cached session):

      REFRESHED — detector-derived facts this call recomputed from bars
      (ts_utc, kinds, headlines, score, tier, close, atr14, trend,
      context_json, pct_from_prior_close*), plus the provenance of that
      recomputation (code_version, symbol_class). Refreshing these is the
      point of the upsert; scripts/compare_replay.py's header documents
      A/B replay relying on it.

      PRESERVED — everything the pipeline decided or delivered, which this
      function is never the author of: alerted, suppress_reason,
      suppress_category, lifecycle_state, related_detection_id, no_trade,
      news_driven, event_kind, event_severity, extreme_mover*. Those are
      written by their own setters (set_no_trade, set_news_driven, and
      runner.py's own UPDATEs) and must survive a later replay of the same
      bar. `alerted` in particular gates the entire public track record
      (/performance, the weekly recap, api/app.py), so letting a replay
      reset it to 0 would silently delete the record that an alert really
      fired. alerted/suppress_reason stay settable on the INSERT path,
      where there is no prior state to destroy.

      PRESERVED-IF-OMITTED — primary_kind, data_feed, origin. Omitting an
      argument is not the same as recomputing it to nothing, and a caller
      that never computed one (scripts/replay.py passes none of the three)
      must not null out what the row already knows.

    Scope note: this covers corruption through THIS function only. A
    run_replay() pass also reaches the same row via set_no_trade,
    set_news_driven and runner.py's direct UPDATEs, none of which are
    affected by anything here.

    symbol_class (deep/thin liquidity, see tradebot.config) is derived
    and frozen here at write time, not looked up later — the watchlist's
    classification could change in the future, and a historical row
    should keep reporting what was true when the alert actually fired,
    the same discipline historical_performance() already applies to each
    row's own atr14 rather than borrowing a current value.

    data_feed: the caller's DETECTOR_DATA_FEED value at write time ('iex'/
    'sip'), same frozen-at-write-time discipline as symbol_class — None
    (the default) matches every pre-Decision-B caller/test, same as the
    other columns added after this function's first release.

    origin: 'watchlist' or 'screening' — whether `symbol` was in the
    fixed watchlist or promoted in by broad_scan for this session. The
    only place this fact is knowable is the caller's own merge point
    (tradebot.runner's `scan_symbols` construction), so it must be passed
    in, not derived here. See docs/broad-scan-honesty-proposal.md.
    Defaults to None, meaning "the caller didn't say" — which a fresh
    INSERT still stores as 'watchlist' (unchanged from before), but which
    an upsert now treats as "leave the existing value alone" rather than
    as an assertion that this row is a watchlist row. The default is None
    rather than 'watchlist' precisely so those two cases are still
    distinguishable by the time the SQL runs.

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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE(?, 'watchlist'), ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ts_utc=excluded.ts_utc, kinds=excluded.kinds, headlines=excluded.headlines,
            score=excluded.score, tier=excluded.tier, close=excluded.close,
            atr14=excluded.atr14, trend=excluded.trend, context_json=excluded.context_json,
            code_version=excluded.code_version,
            symbol_class=excluded.symbol_class,
            pct_from_prior_close=excluded.pct_from_prior_close,
            pct_from_prior_close_status=excluded.pct_from_prior_close_status,
            -- Omission is not recomputation. A caller that never computed
            -- one of these (scripts/replay.py passes neither) would
            -- otherwise overwrite a known value with its parameter
            -- default, erasing it. COALESCE keeps what the row already
            -- knows when the incoming value is NULL, and still refreshes
            -- it whenever the caller actually supplied one.
            primary_kind=COALESCE(excluded.primary_kind, detections.primary_kind),
            data_feed=COALESCE(excluded.data_feed, detections.data_feed),
            -- origin can't use excluded.origin: its INSERT expression
            -- above already collapsed "omitted" to 'watchlist' for
            -- backwards compatibility, so by the time it reaches
            -- `excluded` the two are indistinguishable. The raw argument
            -- is bound a second time here, still NULL when omitted, so
            -- the conflict path can tell them apart -- an explicit
            -- 'watchlist' updates, an omitted one preserves whatever the
            -- row already had (e.g. 'screening').
            origin=COALESCE(?, detections.origin)
        """,
        (
            detection_id, ts_utc, session, symbol, kinds, headlines, score, tier,
            close, atr14, trend, context_json, code_version_str, int(alerted), suppress_reason,
            primary_kind, symbol_class, data_feed, origin,
            pct_from_prior_close, pct_from_prior_close_status,
            origin,  # again, for the DO UPDATE SET's own COALESCE above
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
    """Completed RTH bars used for regular-session detection outcomes.

    Premarket bars are deliberately excluded: if an RTH cache is missing
    but premarket prints exist, the last premarket price is not a session
    close and must never be persisted as one.
    """
    md = ReplayMarketData(cache_dir, symbol, session_date)
    while md.advance():
        pass
    bars = list(md.session_bars(symbol, session_date))
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
    Never fabricates a price. An offset the session could not reach and a
    checkpoint whose data is unavailable receive different append-only
    resolution events, even though neither receives a compact marks row.
    Automatic and unconditional: called once at the end of every replay/live
    session (see runner.py), never gated on how the alert performed — a loss
    is exactly as recordable as a win.

    2026-08-12 incident: an absent cache file and "cached, but nothing
    new to add" both silently produce zero bars here (see
    marketdata._read_bars's `if not path.exists(): return []`), so a
    genuinely missing intraday file for the session being backfilled was
    indistinguishable from an ordinary quiet outcome. The durable
    DATA_UNAVAILABLE event now carries that failure into the API/UI in
    addition to the ERROR log; the bare integer return remains the count of
    real price marks written for backward compatibility."""
    cache_dir = Path(cache_dir)
    rows = conn.execute(
        "SELECT id, symbol, ts_utc FROM detections WHERE session = ?", (session.isoformat(),)
    ).fetchall()

    attempt_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    revision = code_version()
    session_close = XNYS.session_close(session).to_pydatetime().astimezone(timezone.utc)
    bars_by_symbol: dict[str, list] = {}
    data_error_by_symbol: dict[str, str | None] = {}
    missing_cache_symbols: set[str] = set()
    written = 0
    for detection_id, symbol, ts_utc in rows:
        if symbol not in bars_by_symbol:
            intraday_path = cache_dir / symbol / f"intraday_{session.isoformat()}.csv"
            if not intraday_path.exists():
                missing_cache_symbols.add(symbol)
                data_error_by_symbol[symbol] = "missing_cache_file"
            else:
                data_error_by_symbol[symbol] = None
            try:
                bars_by_symbol[symbol] = _all_bars_for_session(cache_dir, symbol, session)
            except Exception as exc:
                logger.error(
                    "backfill_marks(session=%s): cached bars unreadable for %s exception=%s",
                    session.isoformat(), symbol, type(exc).__name__,
                )
                bars_by_symbol[symbol] = []
                data_error_by_symbol[symbol] = f"cache_read_failed:{type(exc).__name__}"
        bars = bars_by_symbol[symbol]
        detection_ts = datetime.fromisoformat(ts_utc)
        for offset in offsets_min:
            target = detection_ts + timedelta(minutes=offset)
            if target > session_close:
                status, reason, price = MARK_STATUS_NOT_REACHED, "target_after_session_close", None
            else:
                price = _price_at_or_after(bars, target)
                if price is None:
                    status = MARK_STATUS_DATA_UNAVAILABLE
                    reason = data_error_by_symbol[symbol] or "no_bar_at_or_after_target"
                else:
                    status, reason = MARK_STATUS_AVAILABLE, "cached_session_bar"
                    conn.execute(
                        "INSERT OR REPLACE INTO marks (detection_id, offset_min, price) VALUES (?, ?, ?)",
                        (detection_id, offset, price),
                    )
                    written += 1
            _record_mark_resolution_event(
                conn, attempt_id=attempt_id, detection_id=detection_id,
                session=session, offset_min=offset, status=status, reason=reason,
                price=price, created_at=created_at, revision=revision,
            )
        if bars:
            close_price = bars[-1].close
            conn.execute(
                "INSERT OR REPLACE INTO marks (detection_id, offset_min, price) VALUES (?, ?, ?)",
                (detection_id, CLOSE_MARK_OFFSET_MIN, close_price),
            )
            written += 1
            close_status, close_reason = MARK_STATUS_AVAILABLE, "session_close_bar"
        else:
            close_price = None
            close_status = MARK_STATUS_DATA_UNAVAILABLE
            close_reason = data_error_by_symbol[symbol] or "no_session_bars"
        _record_mark_resolution_event(
            conn, attempt_id=attempt_id, detection_id=detection_id,
            session=session, offset_min=CLOSE_MARK_OFFSET_MIN,
            status=close_status, reason=close_reason, price=close_price,
            created_at=created_at, revision=revision,
        )
    if missing_cache_symbols:
        logger.error(
            "backfill_marks(session=%s): no cached intraday file for %s -- outcomes for "
            "these symbols' detections cannot be computed until it exists",
            session.isoformat(), ", ".join(sorted(missing_cache_symbols)),
        )
    conn.commit()
    return written


_MARK_FINAL_STATUSES = {
    MARK_STATUS_AVAILABLE,
    MARK_STATUS_NOT_REACHED,
    MARK_STATUS_DATA_UNAVAILABLE,
}


def _record_mark_resolution_event(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    detection_id: str,
    session: date,
    offset_min: int,
    status: str,
    reason: str | None,
    price: float | None,
    created_at: str,
    revision: str,
) -> None:
    if status not in _MARK_FINAL_STATUSES:
        raise ValueError(f"invalid final mark resolution status: {status}")
    if (status == MARK_STATUS_AVAILABLE) != (price is not None):
        raise ValueError("AVAILABLE requires price and non-AVAILABLE forbids price")
    conn.execute(
        """
        INSERT INTO mark_resolution_events
            (attempt_id,detection_id,session,offset_min,status,reason,price,created_at,code_version)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id, detection_id, session.isoformat(), offset_min, status,
            reason, price, created_at, revision,
        ),
    )


@dataclass(frozen=True)
class OutcomeCheckpoint:
    offset_min: int
    status: str
    price: float | None
    reason: str | None
    resolved_at: str | None


def outcome_checkpoints(
    conn: sqlite3.Connection,
    detection_id: str,
    detection_ts: datetime,
    session: date,
    *,
    now: datetime | None = None,
) -> list[OutcomeCheckpoint]:
    """Return every fixed/close checkpoint with an explicit state.

    Durable close-batch events win. Historical marks written before the
    ledger shipped remain AVAILABLE. Only checkpoints with neither are
    derived from the clock/calendar: pending before their target, waiting
    during the bounded close-batch grace period, and DELAYED afterward.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or detection_ts.tzinfo is None:
        raise ValueError("now and detection_ts must be timezone-aware")
    marks = dict(
        conn.execute(
            "SELECT offset_min,price FROM marks WHERE detection_id=?",
            (detection_id,),
        ).fetchall()
    )
    latest_events: dict[int, tuple[str, str | None, float | None, str]] = {}
    latest_available: dict[int, tuple[str | None, str]] = {}
    for offset, status, reason, price, created_at in conn.execute(
        """
        SELECT offset_min,status,reason,price,created_at
        FROM mark_resolution_events
        WHERE detection_id=?
        ORDER BY event_id
        """,
        (detection_id,),
    ):
        latest_events[offset] = (status, reason, price, created_at)
        if status == MARK_STATUS_AVAILABLE:
            latest_available[offset] = (reason, created_at)

    session_close: datetime | None = None
    if XNYS.is_session(session):
        session_close = XNYS.session_close(session).to_pydatetime().astimezone(timezone.utc)
    grace_end = (
        session_close + timedelta(minutes=OUTCOME_BACKFILL_GRACE_MINUTES)
        if session_close is not None else None
    )

    result: list[OutcomeCheckpoint] = []
    for offset in (*OUTCOME_OFFSETS_MIN, CLOSE_MARK_OFFSET_MIN):
        # A real persisted price is monotonic customer outcome truth. A later
        # retry failure remains visible in the append-only event ledger, but
        # cannot downgrade or contradict an already resolved checkpoint.
        if offset in marks:
            if offset in latest_available:
                reason, resolved_at = latest_available[offset]
            else:
                reason, resolved_at = "legacy_mark", None
            status, price = MARK_STATUS_AVAILABLE, marks[offset]
        elif offset in latest_events:
            status, reason, event_price, resolved_at = latest_events[offset]
            price = event_price
        else:
            target = session_close if offset == CLOSE_MARK_OFFSET_MIN else detection_ts + timedelta(minutes=offset)
            if offset != CLOSE_MARK_OFFSET_MIN and session_close is not None and target > session_close:
                status, reason = MARK_STATUS_NOT_REACHED, "target_after_session_close"
            elif target is not None and now < target:
                status, reason = MARK_STATUS_PENDING, "target_not_reached"
            elif grace_end is not None and now <= grace_end:
                status, reason = MARK_STATUS_WAITING, "close_batch_grace"
            else:
                status = MARK_STATUS_DELAYED
                reason = "no_resolution_event_after_grace" if session_close is not None else "invalid_session_calendar"
            price, resolved_at = None, None
        result.append(OutcomeCheckpoint(offset, status, price, reason, resolved_at))
    return result


def outcome_resolution_status(checkpoints: list[OutcomeCheckpoint]) -> str:
    statuses = {checkpoint.status for checkpoint in checkpoints}
    if MARK_STATUS_DATA_UNAVAILABLE in statuses or MARK_STATUS_DELAYED in statuses:
        return "DEGRADED"
    if statuses <= {MARK_STATUS_AVAILABLE, MARK_STATUS_NOT_REACHED}:
        return "RESOLVED"
    if MARK_STATUS_WAITING in statuses:
        return MARK_STATUS_WAITING
    return MARK_STATUS_PENDING


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
    excluded_news_driven: int = 0


def tier_performance(conn: sqlite3.Connection, offset_min: int = 30) -> dict[str, TierPerformance]:
    """Real continuation rate and average directional return per tier,
    across the whole journal, using backfilled forward prices — the same
    'is this tier actually predictive' check as historical_performance(),
    aggregated by tier instead of by kind. Tiers with fewer than
    MIN_HISTORY_SAMPLE data points are omitted rather than reported on
    too little data.

    Uses the same technical-performance population as
    historical_performance()/kind_performance(): news-driven rows are
    excluded, as are pre-cutover-feed history and broad_scan-promoted
    ("screening") symbols. excluded_news_driven counts event-driven rows
    that otherwise belong to the same feed/origin population, so the
    omission remains visible rather than silently shrinking n. See
    CURRENT_FEED_FILTER_SQL's docstring."""
    rows = conn.execute(
        f"""
        SELECT d.tier, d.close, d.trend, m.price, d.news_driven
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE {CURRENT_FEED_FILTER_SQL}
        """,
        (offset_min,),
    ).fetchall()

    by_tier: dict[str, list[float]] = {}
    excluded_news_driven: dict[str, int] = {}
    for tier, close, trend, price, news_driven in rows:
        if news_driven:
            excluded_news_driven[tier] = excluded_news_driven.get(tier, 0) + 1
            continue
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
            excluded_news_driven=excluded_news_driven.get(tier, 0),
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
    excluded_news_driven: int = 0


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
    too little data. Uses the same current-feed, watchlist-origin,
    non-news-driven technical population as historical_performance(),
    tier_performance(), and kind_performance(); excluded_news_driven keeps
    the event-driven omissions visible per hour.
    """
    if tier is None:
        rows = conn.execute(
            f"""
            SELECT d.ts_utc, d.close, d.trend, m.price, d.news_driven
            FROM detections d
            JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
            WHERE d.tier != 'log'
              AND {CURRENT_FEED_FILTER_SQL}
            """,
            (offset_min,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT d.ts_utc, d.close, d.trend, m.price, d.news_driven
            FROM detections d
            JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
            WHERE d.tier = ?
              AND {CURRENT_FEED_FILTER_SQL}
            """,
            (offset_min, tier),
        ).fetchall()

    by_hour: dict[int, list[float]] = {}
    excluded_news_driven: dict[int, int] = {}
    for ts_utc, close, trend, price, news_driven in rows:
        hour = datetime.fromisoformat(ts_utc).astimezone(ET).hour
        if news_driven:
            excluded_news_driven[hour] = excluded_news_driven.get(hour, 0) + 1
            continue
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
            excluded_news_driven=excluded_news_driven.get(hour, 0),
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
# The pipeline's call sites live in runner.process_new_bar, behind
# runner._record_decision -- which pins commit=False (this function's
# default commit would break process_new_bar's journal-before-send
# ordering) and swallows write failures (instrumentation must never
# change the decision it is recording).
# --------------------------------------------------------------------------

MAX_DECISION_DETAIL_JSON_LEN = 2000

# Which kind of execution appended a row. "at least live vs replay" is the
# requirement; these three are the complete set of answers the writer can
# actually give, and UNKNOWN is one of them on purpose — see
# UNATTRIBUTED_RUN_ID.
RUN_MODE_LIVE = "live"
RUN_MODE_REPLAY = "replay"
RUN_MODE_UNKNOWN = "unknown"

# The run_id for a caller that didn't state one. A literal, not a NULL and
# not a fresh uuid: a NULL invites a reader to assume live, and a fresh
# uuid per unattributed write would manufacture the appearance of many
# distinct runs. "unattributed" says the one true thing — this row cannot
# be tied to a run — and says it identically every time.
UNATTRIBUTED_RUN_ID = "unattributed"


def new_run_id() -> str:
    """One opaque id per execution of run_live()/run_replay().

    Random, not derived from session_date/start time: two replays of the
    same session started in the same second must not collide, and that is
    the exact case this exists to separate. Nothing parses it — run_mode
    is its own column, and the session is reachable through
    detections.session — so it carries no encoded fields to go stale."""
    return uuid.uuid4().hex


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
    run_mode: str
    run_id: str


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
    run_mode: str | None = None,
    run_id: str | None = None,
    commit: bool = True,
) -> int:
    """Append one decision event. Returns its `seq`.

    Appends unconditionally: there is no upsert key and no de-duplication.
    Recording the same stage/decision pair twice for one detection is a
    real fact about what happened (the pipeline reached that point twice),
    not a mistake to collapse — the same reasoning that keeps sub-threshold
    'log' tier clusters in the detections table rather than dropping them.

    ts_utc: EXECUTION time — when this run took and recorded the
    decision — not the market time the decision was about. Defaults to
    wall clock, which is what every current caller uses, replay included:
    a replay executed today really did take its decisions today, and
    stamping them with the historical session's clock would claim
    otherwise. The market side of the question is answered by
    detections.ts_utc (the bar's own timestamp), and 'which execution was
    this' by run_mode/run_id — so this column is left meaning exactly one
    thing rather than two. It stays a parameter so a caller that knows a
    decision's real recording time passes it rather than letting it drift
    to "whenever the write happened", and so tests are deterministic —
    same injectable-clock discipline as runner.py's validation_now_fn.
    Stored as ISO-8601; naive datetimes are assumed UTC and stamped as
    such rather than silently recorded without an offset.

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

    run_mode/run_id: which execution is appending this row (RUN_MODE_LIVE
    / RUN_MODE_REPLAY, and a new_run_id() held for the life of that one
    execution). Neither is ever stored as NULL or empty: omitted or falsy
    collapses to RUN_MODE_UNKNOWN / UNATTRIBUTED_RUN_ID, so an
    unattributed row says so in the same column a reader is already
    checking, rather than being silently readable as live. See the
    columns' own comment in SCHEMA for why the ledger needs this at all.

    commit: whether to commit before returning. True (the default) keeps
    the standalone-caller behavior — same as record_iv_sample /
    record_contract_selection — so a decision taken outside any wider
    transaction can't be lost. False is for a caller that is already
    inside one and owns its boundary: process_new_bar orders its writes
    so that everything about a detection is committed before any alert
    referencing it is sent (see runner._commit_then_send), and a commit
    fired from in here would flush that pending state early, at a point
    the caller deliberately hasn't reached. The event still lands in the
    caller's transaction and is durable at its next commit — which for
    every alerting path is _commit_then_send's, i.e. still before the
    alert goes out."""
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
            (detection_id, ts_utc, stage, decision, reason, detail_json, code_version,
             run_mode, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            detection_id, ts_utc.isoformat(), stage, decision, reason, detail_json, code_version_str,
            run_mode or RUN_MODE_UNKNOWN, run_id or UNATTRIBUTED_RUN_ID,
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
               run_mode, run_id
        FROM decision_events WHERE detection_id = ? ORDER BY seq
        """,
        (detection_id,),
    ).fetchall()
    return [
        DecisionEvent(
            seq=seq, detection_id=det_id, ts_utc=ts, stage=stage, decision=decision,
            reason=reason, detail=json.loads(detail_json) if detail_json else None,
            code_version=code_ver, run_mode=run_mode, run_id=run_id,
        )
        for seq, det_id, ts, stage, decision, reason, detail_json, code_ver, run_mode, run_id in rows
    ]
