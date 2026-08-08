"""Signal-quality analytics — internal-only measurement of what the
detector pipeline actually produces, beyond the per-kind/per-tier stats
tradebot.journal and tradebot.telegram_bot.performance already report.

This module NEVER replaces journal.historical_performance/tier_performance
or performance.track_record — those remain the source of truth /start,
/performance, and every subscriber-facing number read from. Everything
here is for internal signal-quality review (which detectors/symbols are
actually working), gated on the same MIN_HISTORY_SAMPLE never-report-on-
too-little-data discipline as the rest of the journal, and never surfaced
to a subscriber as a claim.

Two forward-price checkpoints are added here, both written into the
existing `marks` table (schema: detection_id, offset_min, price — not
limited to a fixed set of columns, unlike contract_selections' mid_15m/
mid_30m/mid_60m), so neither requires touching journal.py:

- +5m, via journal.backfill_marks() itself, just called with one extra
  offset. journal.OUTCOME_OFFSETS_MIN (15/30/60) is left untouched
  because contract_selections' mid_* columns are keyed to that exact
  tuple (see journal._CONTRACT_MID_COLUMNS) — adding 5 there would break
  the options-contract backfill path for no benefit to this module.
- +1 trading day close, a new sentinel offset (NEXT_DAY_CLOSE_OFFSET_MIN)
  requiring a cross-session bar lookup journal.backfill_marks doesn't do
  (it's scoped to one session's cache file) — see
  backfill_next_day_marks().
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from tradebot.detectors import bar_close_ts
from tradebot.journal import DEFAULT_CACHE_DIR, MIN_HISTORY_SAMPLE, backfill_marks
from tradebot.marketdata import ReplayMarketData

# Additive forward-price checkpoint, alongside journal.OUTCOME_OFFSETS_MIN
# (15/30/60) — a finer-grained early read on how a signal is playing out.
FIVE_MIN_OFFSET = 5

# Sentinel for "the next trading session's close", mirroring
# journal.CLOSE_MARK_OFFSET_MIN's "this session's close" sentinel —
# negative and distinct from it (-1) so the two can never collide.
NEXT_DAY_CLOSE_OFFSET_MIN = -2


# --------------------------------------------------------------------------
# Additive backfills — see module docstring for why journal.py isn't
# touched to add these.
# --------------------------------------------------------------------------


def backfill_five_minute_marks(conn: sqlite3.Connection, session: date, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> int:
    """The existing journal.backfill_marks(), called with just the extra
    +5m checkpoint. Never fabricates: skipped silently (same as the
    15/30/60 checkpoints) if the session doesn't extend that far."""
    return backfill_marks(conn, session, cache_dir=cache_dir, offsets_min=(FIVE_MIN_OFFSET,))


def _full_session_bars(cache_dir: Path, symbol: str, session_date: date) -> list:
    """Every bar (premarket + RTH) cached for one session, oldest first —
    mirrors journal._all_bars_for_session exactly; duplicated here rather
    than imported since that helper is private to journal.py."""
    md = ReplayMarketData(cache_dir, symbol, session_date)
    while md.advance():
        pass
    bars = list(md.premarket_bars(symbol, session_date)) + list(md.session_bars(symbol, session_date))
    bars.sort(key=lambda b: b.ts)
    return bars


def _next_cached_session(cache_dir: Path, symbol: str, session_date: date) -> date | None:
    from tradebot.runner import cached_session_dates  # deferred: heavy import graph

    later = [d for d in cached_session_dates(cache_dir, [symbol]) if d > session_date]
    return min(later) if later else None


def backfill_next_day_marks(conn: sqlite3.Connection, session: date, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> int:
    """+1 trading day follow-through for every detection in `session`,
    written under NEXT_DAY_CLOSE_OFFSET_MIN — the NEXT session's own real
    final close, not a fixed-minutes guess, the same discipline as
    journal.CLOSE_MARK_OFFSET_MIN. Skipped (never fabricated) for a
    detection when no later session is cached yet."""
    cache_dir = Path(cache_dir)
    rows = conn.execute("SELECT id, symbol FROM detections WHERE session = ?", (session.isoformat(),)).fetchall()
    bars_by_key: dict[tuple[str, date], list] = {}
    written = 0
    for detection_id, symbol in rows:
        next_session = _next_cached_session(cache_dir, symbol, session)
        if next_session is None:
            continue
        key = (symbol, next_session)
        if key not in bars_by_key:
            bars_by_key[key] = _full_session_bars(cache_dir, symbol, next_session)
        bars = bars_by_key[key]
        if not bars:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO marks (detection_id, offset_min, price) VALUES (?, ?, ?)",
            (detection_id, NEXT_DAY_CLOSE_OFFSET_MIN, bars[-1].close),
        )
        written += 1
    conn.commit()
    return written


# --------------------------------------------------------------------------
# Maximum favorable / adverse excursion — the best and worst the market
# actually offered before a horizon, independent of where price ended up
# at that horizon's single point-in-time mark.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcursionStats:
    detection_id: str
    symbol: str
    trend: str
    horizon_minutes: int
    mfe_pct: float  # best move in the predicted direction, >= 0, never fabricated as negative
    mae_pct: float  # worst move against the predicted direction, >= 0
    time_to_mfe_minutes: float  # minutes from detection to the bar that reached the MFE
    bars_examined: int


def compute_excursion(
    conn: sqlite3.Connection, detection_id: str, cache_dir: Path | str = DEFAULT_CACHE_DIR, horizon_minutes: int = 60,
) -> ExcursionStats | None:
    """Walks every cached bar between the detection and horizon_minutes
    later, using each bar's real high/low (not just its close) — MFE/MAE
    are about the best and worst the market actually offered along the
    way, which a single point-in-time mark can't show. None if the
    detection can't be found, has no close/trend recorded, or no cached
    bar falls in that window — never a fabricated number."""
    row = conn.execute(
        "SELECT symbol, session, ts_utc, close, trend FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()
    if row is None:
        return None
    symbol, session, ts_utc, close, trend = row
    if close is None or trend is None:
        return None

    session_date = date.fromisoformat(session)
    detection_ts = datetime.fromisoformat(ts_utc)
    horizon_end = detection_ts + timedelta(minutes=horizon_minutes)

    bars = _full_session_bars(Path(cache_dir), symbol, session_date)
    window = [b for b in bars if detection_ts < bar_close_ts(b) <= horizon_end]
    if not window:
        return None

    if trend == "up":
        best_bar = max(window, key=lambda b: b.high)
        worst_bar = min(window, key=lambda b: b.low)
        mfe = (best_bar.high - close) / close
        mae = (close - worst_bar.low) / close
    else:
        best_bar = min(window, key=lambda b: b.low)
        worst_bar = max(window, key=lambda b: b.high)
        mfe = (close - best_bar.low) / close
        mae = (worst_bar.high - close) / close

    time_to_mfe = (bar_close_ts(best_bar) - detection_ts).total_seconds() / 60

    return ExcursionStats(
        detection_id=detection_id, symbol=symbol, trend=trend, horizon_minutes=horizon_minutes,
        mfe_pct=max(0.0, mfe * 100), mae_pct=max(0.0, mae * 100),
        time_to_mfe_minutes=time_to_mfe, bars_examined=len(window),
    )


# --------------------------------------------------------------------------
# Aggregate signal-quality report — the internal "is this actually
# working" view, sliceable by detector kind, tier, symbol, direction, and
# news-driven/clean-technical. Every filter is optional; omitting one
# means "don't slice on this dimension," not "match nothing."
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalQualityReport:
    kind: str | None
    tier: str | None
    symbol: str | None
    trend: str | None
    news_driven: bool | None
    offset_min: int
    horizon_minutes: int
    sample_size: int
    hit_rate: float
    false_positive_rate: float  # == 1 - hit_rate, named separately so ops sees "how often wrong" directly
    avg_return_pct: float
    median_return_pct: float
    excursion_sample_size: int  # how many of `sample_size` had cached bars to compute MFE/MAE from
    avg_mfe_pct: float | None
    avg_mae_pct: float | None
    time_to_mfe_median_minutes: float | None


def signal_quality_report(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    tier: str | None = None,
    symbol: str | None = None,
    trend: str | None = None,
    news_driven: bool | None = None,
    offset_min: int = 30,
    horizon_minutes: int = 60,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    excursion_limit: int = 200,
) -> SignalQualityReport | None:
    """None if fewer than MIN_HISTORY_SAMPLE detections match every
    provided filter at `offset_min` — same floor as
    journal.historical_performance/tier_performance. MFE/MAE/time-to-MFE
    are computed over up to `excursion_limit` of the matching detections
    (bounded because each one re-reads a cached CSV — see
    compute_excursion) and are None if none of them had cached bars
    available, never averaged over zero samples."""
    query = """
        SELECT d.id, d.close, d.trend, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE 1 = 1
    """
    params: list = [offset_min]
    if kind is not None:
        query += " AND d.primary_kind = ?"
        params.append(kind)
    if tier is not None:
        query += " AND d.tier = ?"
        params.append(tier)
    if symbol is not None:
        query += " AND d.symbol = ?"
        params.append(symbol)
    if trend is not None:
        query += " AND d.trend = ?"
        params.append(trend)
    if news_driven is not None:
        query += " AND d.news_driven = ?" if news_driven else " AND (d.news_driven IS NULL OR d.news_driven = 0)"
        if news_driven:
            params.append(1)

    rows = conn.execute(query, params).fetchall()
    if len(rows) < MIN_HISTORY_SAMPLE:
        return None

    returns = []
    detection_ids = []
    for detection_id, close, row_trend, price in rows:
        r = (price - close) / close * 100
        returns.append(r if row_trend == "up" else -r)
        detection_ids.append(detection_id)

    hits = sum(1 for r in returns if r > 0)
    hit_rate = hits / len(returns)

    mfes, maes, times_to_mfe = [], [], []
    for detection_id in detection_ids[:excursion_limit]:
        excursion = compute_excursion(conn, detection_id, cache_dir=cache_dir, horizon_minutes=horizon_minutes)
        if excursion is None:
            continue
        mfes.append(excursion.mfe_pct)
        maes.append(excursion.mae_pct)
        times_to_mfe.append(excursion.time_to_mfe_minutes)

    return SignalQualityReport(
        kind=kind, tier=tier, symbol=symbol, trend=trend, news_driven=news_driven,
        offset_min=offset_min, horizon_minutes=horizon_minutes,
        sample_size=len(returns), hit_rate=hit_rate, false_positive_rate=1 - hit_rate,
        avg_return_pct=sum(returns) / len(returns), median_return_pct=statistics.median(returns),
        excursion_sample_size=len(mfes),
        avg_mfe_pct=sum(mfes) / len(mfes) if mfes else None,
        avg_mae_pct=sum(maes) / len(maes) if maes else None,
        time_to_mfe_median_minutes=statistics.median(times_to_mfe) if times_to_mfe else None,
    )


def signal_frequency(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    tier: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, int]:
    """Count of journaled detections per session date, optionally
    filtered by detector kind and/or tier — how often this fires, not how
    well it performs. A session with zero matches is simply absent from
    the result, never padded with a fabricated zero."""
    query = "SELECT session, COUNT(*) FROM detections WHERE 1 = 1"
    params: list = []
    if kind is not None:
        query += " AND primary_kind = ?"
        params.append(kind)
    if tier is not None:
        query += " AND tier = ?"
        params.append(tier)
    if since is not None:
        query += " AND session >= ?"
        params.append(since)
    if until is not None:
        query += " AND session < ?"
        params.append(until)
    query += " GROUP BY session ORDER BY session"
    return dict(conn.execute(query, params).fetchall())
