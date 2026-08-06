"""SQLite journal for every detection cluster.

Every cluster gets written here, including sub-threshold ('log' tier)
ones — see CLAUDE.md: every detection is journaled before any alert is
sent, and sub-threshold detections are how we find out the thresholds are
wrong, so they're never dropped.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dataclasses import dataclass

from tradebot.detectors import Detection, bar_close_ts, tier_for_score
from tradebot.marketdata import ReplayMarketData

ET = ZoneInfo("America/New_York")

MIN_HISTORY_SAMPLE = 5

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
    suppress_reason TEXT
);

CREATE TABLE IF NOT EXISTS marks (
    detection_id TEXT NOT NULL,
    offset_min INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (detection_id, offset_min)
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
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
) -> str:
    tier = tier_for_score(score).value
    detection_id = cluster_id(symbol, session, ts_utc, kinds)
    context_json = json.dumps([d.context for d in detections])
    conn.execute(
        """
        INSERT INTO detections
            (id, ts_utc, session, symbol, kinds, headlines, score, tier,
             close, atr14, trend, context_json, code_version, alerted, suppress_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ts_utc=excluded.ts_utc, kinds=excluded.kinds, headlines=excluded.headlines,
            score=excluded.score, tier=excluded.tier, close=excluded.close,
            atr14=excluded.atr14, trend=excluded.trend, context_json=excluded.context_json,
            code_version=excluded.code_version, alerted=excluded.alerted,
            suppress_reason=excluded.suppress_reason
        """,
        (
            detection_id, ts_utc, session, symbol, kinds, headlines, score, tier,
            close, atr14, trend, context_json, code_version_str, int(alerted), suppress_reason,
        ),
    )
    return detection_id


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
    offsets_min: tuple[int, ...] = (5, 15, 30, 60),
) -> int:
    """Fill the marks table at +offsets_min from every journaled
    detection in `session`, reading forward prices from cached bars.
    Skips an offset silently if the session ended before reaching it —
    never fabricates a price."""
    cache_dir = Path(cache_dir)
    rows = conn.execute(
        "SELECT id, symbol, ts_utc FROM detections WHERE session = ?", (session.isoformat(),)
    ).fetchall()

    bars_by_symbol: dict[str, list] = {}
    written = 0
    for detection_id, symbol, ts_utc in rows:
        if symbol not in bars_by_symbol:
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
    conn.commit()
    return written


@dataclass(frozen=True)
class HistoricalPerformance:
    sample_size: int
    continuation_rate: float  # fraction that moved further in the alert's direction by offset_min
    avg_return_pct: float  # signed average return at offset_min
    offset_min: int


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
    reports a stat built on too few data points to mean anything."""
    rows = conn.execute(
        """
        SELECT d.close, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.kinds LIKE ? AND d.trend = ? AND d.id != ?
        ORDER BY d.ts_utc DESC
        LIMIT ?
        """,
        (offset_min, f"%{kind}%", trend, exclude_id, lookback),
    ).fetchall()
    if len(rows) < MIN_HISTORY_SAMPLE:
        return None

    returns = [(price - close) / close for close, price in rows]
    if trend == "up":
        continued = sum(1 for r in returns if r > 0)
    else:
        continued = sum(1 for r in returns if r < 0)
    return HistoricalPerformance(
        sample_size=len(returns),
        continuation_rate=continued / len(returns),
        avg_return_pct=sum(returns) / len(returns) * 100,
        offset_min=offset_min,
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
    too little data."""
    rows = conn.execute(
        """
        SELECT d.tier, d.close, d.trend, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
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
