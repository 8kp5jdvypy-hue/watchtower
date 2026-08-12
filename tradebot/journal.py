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
import sqlite3
import statistics
import subprocess
from datetime import date, datetime, timedelta
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
    origin TEXT
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
    _add_column_if_missing(conn, "contract_selections", "mid_close", "REAL")
    _add_column_if_missing(conn, "contract_selections", "day_low", "REAL")
    _add_column_if_missing(conn, "contract_selections", "day_high", "REAL")
    return conn


def code_version() -> str:
    """Short git hash at write time, or 'unknown' outside a git repo."""
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
    in, not derived here. See docs/broad-scan-honesty-proposal.md."""
    tier = tier_for_score(score).value
    detection_id = cluster_id(symbol, session, ts_utc, kinds)
    context_json = json.dumps([d.context for d in detections])
    symbol_class = liquidity_class(symbol)
    conn.execute(
        """
        INSERT INTO detections
            (id, ts_utc, session, symbol, kinds, headlines, score, tier,
             close, atr14, trend, context_json, code_version, alerted, suppress_reason,
             primary_kind, symbol_class, data_feed, origin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ts_utc=excluded.ts_utc, kinds=excluded.kinds, headlines=excluded.headlines,
            score=excluded.score, tier=excluded.tier, close=excluded.close,
            atr14=excluded.atr14, trend=excluded.trend, context_json=excluded.context_json,
            code_version=excluded.code_version, alerted=excluded.alerted,
            suppress_reason=excluded.suppress_reason, primary_kind=excluded.primary_kind,
            symbol_class=excluded.symbol_class, data_feed=excluded.data_feed,
            origin=excluded.origin
        """,
        (
            detection_id, ts_utc, session, symbol, kinds, headlines, score, tier,
            close, atr14, trend, context_json, code_version_str, int(alerted), suppress_reason,
            primary_kind, symbol_class, data_feed, origin,
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

    atr_normalized = [abs(price - close) / atr14 for close, price, atr14 in rows if atr14]
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
