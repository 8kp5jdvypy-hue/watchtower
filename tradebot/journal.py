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

from tradebot.detectors import Detection, bar_close_ts, tier_for_score
from tradebot.marketdata import ReplayMarketData

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
    offsets_min: tuple[int, ...] = (15, 30, 60),
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
