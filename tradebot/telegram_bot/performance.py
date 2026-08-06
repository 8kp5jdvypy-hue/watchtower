"""Read-only performance analysis over tradebot.journal's detections/marks
tables. Used by /performance and by /start's pre-onboarding track record,
so both ever say exactly the same numbers — one query path, not two
copies that could drift.

Never writes to the journal. Never reports a stat built on fewer than
MIN_HISTORY_SAMPLE data points — same discipline as tradebot.journal.

The "news-driven vs clean-technical" split uses the real detections.
news_driven column — set by runner.py from tradebot.events' actual EDGAR
filing, earnings, and FOMC/CPI/NFP/EIA event windows (see that module's
docstring), not a kind-name heuristic. If the edge lives in only one
bucket, this is how that finding surfaces: a cluster overlapping a known
event went through this same detector/scoring pipeline, but its
continuation stats don't share the same mechanism as an intraday
technical signal, so they're never blended into one number.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from tradebot.journal import MIN_HISTORY_SAMPLE


@dataclass(frozen=True)
class Slice:
    label: str
    sample_size: int
    hit_rate: float
    avg_return_pct: float


@dataclass(frozen=True)
class TrackRecord:
    tier: str
    offset_min: int
    sample_size: int
    hit_rate: float
    avg_return_pct: float
    max_drawdown_pct: float
    longest_losing_streak: int
    news_driven: Slice | None
    clean_technical: Slice | None
    total_alerts: int
    total_no_trade: int
    no_trade_tracked_count: int


def _signed_returns(conn: sqlite3.Connection, tier: str, offset_min: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.ts_utc, d.close, d.trend, d.news_driven, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.tier = ?
        ORDER BY d.ts_utc
        """,
        (offset_min, tier),
    ).fetchall()
    out = []
    for ts_utc, close, trend, news_driven, price in rows:
        r = (price - close) / close * 100
        signed = r if trend == "up" else -r
        out.append({"ts_utc": ts_utc, "return_pct": signed, "news_driven": bool(news_driven)})
    return out


def _slice_stats(label: str, returns: list[dict]) -> Slice | None:
    if len(returns) < MIN_HISTORY_SAMPLE:
        return None
    hits = sum(1 for r in returns if r["return_pct"] > 0)
    return Slice(
        label=label,
        sample_size=len(returns),
        hit_rate=hits / len(returns),
        avg_return_pct=sum(r["return_pct"] for r in returns) / len(returns),
    )


def _max_drawdown_pct(returns: list[dict]) -> float:
    """Peak-to-trough on a hypothetical equal-weighted, back-to-back
    cumulative curve of these returns taken in chronological order — NOT
    real compounded P&L (these are overlapping 30m windows on different
    symbols, not sequential trades of one account). Stated as
    "hypothetical" everywhere it's surfaced."""
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r["return_pct"]
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return max_dd


def _longest_losing_streak(returns: list[dict]) -> int:
    longest = current = 0
    for r in returns:
        if r["return_pct"] <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def track_record(conn: sqlite3.Connection, tier: str = "high", offset_min: int = 30) -> TrackRecord | None:
    returns = _signed_returns(conn, tier, offset_min)
    if len(returns) < MIN_HISTORY_SAMPLE:
        return None

    news = [r for r in returns if r["news_driven"]]
    technical = [r for r in returns if not r["news_driven"]]

    total_alerts = conn.execute(
        "SELECT COUNT(*) FROM detections WHERE tier = ? AND alerted = 1", (tier,)
    ).fetchone()[0]
    no_trade_row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN no_trade = 1 THEN 1 ELSE 0 END) "
        "FROM detections WHERE tier = ? AND alerted = 1 AND no_trade IS NOT NULL",
        (tier,),
    ).fetchone()
    tracked_count, no_trade_count = no_trade_row
    no_trade_count = no_trade_count or 0

    return TrackRecord(
        tier=tier,
        offset_min=offset_min,
        sample_size=len(returns),
        hit_rate=sum(1 for r in returns if r["return_pct"] > 0) / len(returns),
        avg_return_pct=sum(r["return_pct"] for r in returns) / len(returns),
        max_drawdown_pct=_max_drawdown_pct(returns),
        longest_losing_streak=_longest_losing_streak(returns),
        news_driven=_slice_stats("news-driven", news),
        clean_technical=_slice_stats("clean technical", technical),
        total_alerts=total_alerts,
        total_no_trade=no_trade_count,
        no_trade_tracked_count=tracked_count,
    )
