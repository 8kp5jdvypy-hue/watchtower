"""Pure postmarket earnings-reaction evaluation and shadow persistence.

This module cannot send an alert. It turns already-fetched RTH/postmarket
bars into attributable shadow observations and stores them in a database
separate from the production detection journal. Candidate activation is a
future, separately approved routing change.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from tradebot.detectors import Bar, bar_close_ts


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"

OBSERVER_VERSION = 1
MOVE_THRESHOLD_PCT = 8.0
MIN_CUMULATIVE_NOTIONAL = 100_000.0
PERSISTENCE_BARS = 2
MAX_CLOSE_DIVERGENCE_PCT = 10.0
MAX_DATA_AGE_SECONDS = 420
EXPECTED_BAR_INTERVAL = timedelta(minutes=5)
MARKET_DATA_PROVIDER = "alpaca"
BAR_TIMEFRAME = "5Min"
CATALYST_SOURCE = "nasdaq_earnings"

OUTCOME_CANDIDATE = "CANDIDATE"
OUTCOME_BELOW_MOVE = "BELOW_MOVE"
OUTCOME_AWAITING_PERSISTENCE = "AWAITING_PERSISTENCE"
OUTCOME_BELOW_NOTIONAL = "BELOW_NOTIONAL"
OUTCOME_NO_RTH_CLOSE = "NO_RTH_CLOSE"
OUTCOME_NO_COMPLETED_POSTMARKET_BAR = "NO_COMPLETED_POSTMARKET_BAR"
OUTCOME_STALE = "STALE"
OUTCOME_ZERO_VOLUME = "ZERO_VOLUME"
OUTCOME_BAR_GAP = "BAR_GAP"
OUTCOME_DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
OUTCOME_OUT_OF_ORDER = "OUT_OF_ORDER"
OUTCOME_MALFORMED_BAR = "MALFORMED_BAR"
OUTCOME_UNSTABLE_PRINT = "UNSTABLE_PRINT"
OUTCOME_FETCH_ERROR = "FETCH_ERROR"


SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_ticks (
    tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    tick_utc TEXT NOT NULL,
    completed_utc TEXT NOT NULL,
    run_id TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    observer_version INTEGER NOT NULL,
    code_version TEXT,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    catalyst_source TEXT NOT NULL,
    scheduled_symbols INTEGER NOT NULL,
    evaluated_symbols INTEGER NOT NULL,
    candidate_observations INTEGER NOT NULL,
    new_candidates INTEGER NOT NULL,
    invariant_ok INTEGER NOT NULL,
    thresholds_json TEXT NOT NULL,
    latency_ms INTEGER,
    error_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_postmarket_ticks_session
    ON postmarket_ticks(session, tick_utc);

CREATE TABLE IF NOT EXISTS postmarket_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    event_date TEXT NOT NULL,
    bar_open_ts_utc TEXT,
    rth_close REAL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    cumulative_volume INTEGER,
    cumulative_notional REAL,
    move_pct REAL,
    direction TEXT,
    persistence_bars INTEGER NOT NULL,
    data_age_seconds REAL,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    catalyst_source TEXT NOT NULL,
    UNIQUE(tick_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_observations_symbol
    ON postmarket_observations(symbol, event_date, seq);
CREATE INDEX IF NOT EXISTS idx_postmarket_observations_outcome
    ON postmarket_observations(outcome, event_date);

CREATE TABLE IF NOT EXISTS postmarket_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_date TEXT NOT NULL,
    direction TEXT NOT NULL,
    observer_version INTEGER NOT NULL,
    first_detected_at TEXT NOT NULL,
    bar_open_ts_utc TEXT NOT NULL,
    rth_close REAL NOT NULL,
    close REAL NOT NULL,
    move_pct REAL NOT NULL,
    cumulative_volume INTEGER NOT NULL,
    cumulative_notional REAL NOT NULL,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    catalyst_source TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    UNIQUE(session, symbol, event_date, direction, observer_version)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidates_session
    ON postmarket_candidates(session, first_detected_at);

CREATE TRIGGER IF NOT EXISTS postmarket_ticks_no_update
BEFORE UPDATE ON postmarket_ticks BEGIN
    SELECT RAISE(ABORT, 'postmarket_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_ticks_no_delete
BEFORE DELETE ON postmarket_ticks BEGIN
    SELECT RAISE(ABORT, 'postmarket_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_observations_no_update
BEFORE UPDATE ON postmarket_observations BEGIN
    SELECT RAISE(ABORT, 'postmarket_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_observations_no_delete
BEFORE DELETE ON postmarket_observations BEGIN
    SELECT RAISE(ABORT, 'postmarket_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidates_no_update
BEFORE UPDATE ON postmarket_candidates BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidates_no_delete
BEFORE DELETE ON postmarket_candidates BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidates is append-only');
END;
"""


@dataclass(frozen=True)
class ReactionEvaluation:
    symbol: str
    outcome: str
    reason: str
    event_date: date
    bar: Bar | None = None
    rth_close: float | None = None
    cumulative_volume: int | None = None
    cumulative_notional: float | None = None
    move_pct: float | None = None
    direction: str | None = None
    persistence_bars: int = 0
    data_age_seconds: float | None = None


def thresholds() -> dict:
    return {
        "move_pct": MOVE_THRESHOLD_PCT,
        "min_cumulative_notional": MIN_CUMULATIVE_NOTIONAL,
        "persistence_bars": PERSISTENCE_BARS,
        "max_close_divergence_pct": MAX_CLOSE_DIVERGENCE_PCT,
        "max_data_age_seconds": MAX_DATA_AGE_SECONDS,
    }


def _valid_bar(bar: Bar) -> bool:
    return (
        bar.open > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0
        and bar.high >= max(bar.open, bar.close, bar.low)
        and bar.low <= min(bar.open, bar.close, bar.high)
    )


def _notional(bar: Bar) -> float:
    typical_price = (bar.open + bar.high + bar.low + bar.close) / 4
    return typical_price * bar.volume


def fetch_error_evaluation(symbol: str, event_date: date, error: Exception) -> ReactionEvaluation:
    return ReactionEvaluation(
        symbol=symbol,
        event_date=event_date,
        outcome=OUTCOME_FETCH_ERROR,
        reason=f"{type(error).__name__}: {error}"[:1000],
    )


def evaluate_earnings_reaction(
    symbol: str,
    event_date: date,
    rth_bars: Sequence[Bar],
    postmarket_bars: Sequence[Bar],
    *,
    session_close: datetime,
    now: datetime,
) -> ReactionEvaluation:
    """Evaluate one scheduled reporter from completed bars only.

    The RTH baseline must be the bar that actually closes at the calendar
    session close. Two consecutive, same-direction completed postmarket
    bars must hold beyond the move threshold on real volume/notional. This
    rejects stale, missing, malformed, duplicated, out-of-order, gapped,
    zero-volume, and single-print moves before they can become candidates.
    """
    if now.tzinfo is None or session_close.tzinfo is None:
        raise ValueError("now and session_close must be timezone-aware")

    if not rth_bars or bar_close_ts(rth_bars[-1]) != session_close:
        return ReactionEvaluation(
            symbol, OUTCOME_NO_RTH_CLOSE,
            "last RTH bar does not close at the calendar session close",
            event_date,
        )

    rth_close = rth_bars[-1].close
    if rth_close <= 0 or not _valid_bar(rth_bars[-1]):
        return ReactionEvaluation(
            symbol, OUTCOME_MALFORMED_BAR, "invalid RTH close bar", event_date,
            bar=rth_bars[-1], rth_close=rth_close,
        )

    bars = list(postmarket_bars)
    timestamps = [bar.ts for bar in bars]
    if timestamps != sorted(timestamps):
        return ReactionEvaluation(
            symbol, OUTCOME_OUT_OF_ORDER, "postmarket timestamps are out of order",
            event_date, rth_close=rth_close,
        )
    if len(timestamps) != len(set(timestamps)):
        return ReactionEvaluation(
            symbol, OUTCOME_DUPLICATE_TIMESTAMP, "duplicate postmarket timestamp",
            event_date, rth_close=rth_close,
        )

    completed = [bar for bar in bars if bar_close_ts(bar) <= now]
    if not completed:
        return ReactionEvaluation(
            symbol, OUTCOME_NO_COMPLETED_POSTMARKET_BAR,
            "no completed postmarket bar is available", event_date,
            rth_close=rth_close,
        )

    latest = completed[-1]
    age = (now - bar_close_ts(latest)).total_seconds()
    if age < 0 or age > MAX_DATA_AGE_SECONDS:
        return ReactionEvaluation(
            symbol, OUTCOME_STALE,
            f"latest completed postmarket bar is {age:.0f}s old",
            event_date, bar=latest, rth_close=rth_close,
            data_age_seconds=age,
        )

    if any(bar.ts < session_close for bar in completed):
        return ReactionEvaluation(
            symbol, OUTCOME_MALFORMED_BAR,
            "postmarket input contains a bar before the calendar close",
            event_date, bar=latest, rth_close=rth_close,
            data_age_seconds=age,
        )
    if any(not _valid_bar(bar) for bar in completed):
        return ReactionEvaluation(
            symbol, OUTCOME_MALFORMED_BAR, "invalid postmarket OHLC",
            event_date, bar=latest, rth_close=rth_close,
            data_age_seconds=age,
        )
    if any(bar.volume <= 0 for bar in completed):
        return ReactionEvaluation(
            symbol, OUTCOME_ZERO_VOLUME, "completed postmarket series contains zero volume",
            event_date, bar=latest, rth_close=rth_close,
            data_age_seconds=age,
        )
    lookback = completed[-PERSISTENCE_BARS:]

    cumulative_volume = sum(bar.volume for bar in completed)
    cumulative_notional = sum(_notional(bar) for bar in completed)
    move_pct = (latest.close / rth_close - 1) * 100
    direction = "up" if move_pct > 0 else "down" if move_pct < 0 else None

    common = dict(
        symbol=symbol, event_date=event_date, bar=latest, rth_close=rth_close,
        cumulative_volume=cumulative_volume,
        cumulative_notional=cumulative_notional, move_pct=move_pct,
        direction=direction, data_age_seconds=age,
    )
    if abs(move_pct) < MOVE_THRESHOLD_PCT:
        return ReactionEvaluation(
            outcome=OUTCOME_BELOW_MOVE,
            reason=f"{move_pct:+.2f}% is below the {MOVE_THRESHOLD_PCT:.1f}% threshold",
            **common,
        )
    if len(lookback) < PERSISTENCE_BARS:
        return ReactionEvaluation(
            outcome=OUTCOME_AWAITING_PERSISTENCE,
            reason=f"needs {PERSISTENCE_BARS} completed bars beyond threshold",
            persistence_bars=len(lookback), **common,
        )
    if lookback[-1].ts - lookback[-2].ts != EXPECTED_BAR_INTERVAL:
        return ReactionEvaluation(
            outcome=OUTCOME_BAR_GAP,
            reason="persistence bars are not consecutive",
            persistence_bars=len(lookback),
            **common,
        )
    moves = [(bar.close / rth_close - 1) * 100 for bar in lookback]
    same_direction = all(move >= MOVE_THRESHOLD_PCT for move in moves) or all(
        move <= -MOVE_THRESHOLD_PCT for move in moves
    )
    if not same_direction:
        qualifies = (
            (lambda move: move >= MOVE_THRESHOLD_PCT)
            if direction == "up"
            else (lambda move: move <= -MOVE_THRESHOLD_PCT)
        )
        persisted = 0
        for move in reversed(moves):
            if not qualifies(move):
                break
            persisted += 1
        return ReactionEvaluation(
            outcome=OUTCOME_AWAITING_PERSISTENCE,
            reason="move has not persisted beyond threshold on consecutive bars",
            persistence_bars=persisted,
            **common,
        )

    close_divergence = abs(lookback[-1].close / lookback[-2].close - 1) * 100
    if close_divergence > MAX_CLOSE_DIVERGENCE_PCT:
        return ReactionEvaluation(
            outcome=OUTCOME_UNSTABLE_PRINT,
            reason=f"consecutive closes differ by {close_divergence:.2f}%",
            persistence_bars=PERSISTENCE_BARS, **common,
        )
    if cumulative_notional < MIN_CUMULATIVE_NOTIONAL:
        return ReactionEvaluation(
            outcome=OUTCOME_BELOW_NOTIONAL,
            reason=(
                f"${cumulative_notional:,.0f} cumulative postmarket notional is below "
                f"${MIN_CUMULATIVE_NOTIONAL:,.0f}"
            ),
            persistence_bars=PERSISTENCE_BARS, **common,
        )
    return ReactionEvaluation(
        outcome=OUTCOME_CANDIDATE,
        reason=(
            f"{move_pct:+.2f}% from RTH close persisted across {PERSISTENCE_BARS} bars "
            f"on ${cumulative_notional:,.0f} notional"
        ),
        persistence_bars=PERSISTENCE_BARS, **common,
    )


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path if db_path is not None else DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def record_shadow_tick(
    conn: sqlite3.Connection,
    evaluations: Sequence[ReactionEvaluation],
    *,
    session: date,
    tick_utc: datetime,
    completed_utc: datetime,
    run_id: str,
    run_mode: str,
    code_version: str | None,
    data_feed: str,
    scheduled_symbols: int,
    latency_ms: int | None = None,
) -> tuple[int, int]:
    """Persist one tick atomically, or leave no partial candidates behind."""
    if scheduled_symbols < 0:
        raise ValueError("scheduled_symbols must not be negative")
    if tick_utc.tzinfo is None or tick_utc.utcoffset() is None:
        raise ValueError("tick_utc must be timezone-aware")
    if completed_utc.tzinfo is None or completed_utc.utcoffset() is None:
        raise ValueError("completed_utc must be timezone-aware")
    if completed_utc < tick_utc:
        raise ValueError("completed_utc must not precede tick_utc")
    if not data_feed.strip():
        raise ValueError("data_feed must not be empty")
    symbols = [evaluation.symbol for evaluation in evaluations]
    if len(symbols) != len(set(symbols)):
        raise ValueError("evaluations must contain at most one row per symbol")
    for evaluation in evaluations:
        if evaluation.event_date != session:
            raise ValueError("every evaluation event_date must match the tick session")
        if evaluation.outcome == OUTCOME_CANDIDATE and (
            evaluation.bar is None
            or evaluation.rth_close is None
            or evaluation.cumulative_volume is None
            or evaluation.cumulative_notional is None
            or evaluation.move_pct is None
            or evaluation.direction not in {"up", "down"}
        ):
            raise ValueError(f"candidate evaluation is incomplete for {evaluation.symbol}")
    with conn:
        return _record_shadow_tick(
            conn, evaluations, session=session, tick_utc=tick_utc,
            completed_utc=completed_utc,
            run_id=run_id, run_mode=run_mode, code_version=code_version,
            data_feed=data_feed, scheduled_symbols=scheduled_symbols,
            latency_ms=latency_ms,
        )


def _record_shadow_tick(
    conn: sqlite3.Connection,
    evaluations: Sequence[ReactionEvaluation],
    *,
    session: date,
    tick_utc: datetime,
    completed_utc: datetime,
    run_id: str,
    run_mode: str,
    code_version: str | None,
    data_feed: str,
    scheduled_symbols: int,
    latency_ms: int | None = None,
) -> tuple[int, int]:
    """Persist a complete shadow tick and newly deduplicated candidates."""
    candidate_observations = sum(e.outcome == OUTCOME_CANDIDATE for e in evaluations)
    invariant_ok = len(evaluations) == scheduled_symbols
    error_count = sum(e.outcome == OUTCOME_FETCH_ERROR for e in evaluations)

    # Insert deduplicated candidates first inside the same transaction so
    # the tick can be born with its final new_candidates count. No UPDATE
    # exception to the append-only contract is needed.
    new_candidates = 0
    for evaluation in evaluations:
        if evaluation.outcome != OUTCOME_CANDIDATE or evaluation.bar is None:
            continue
        result = conn.execute(
            """
            INSERT OR IGNORE INTO postmarket_candidates
                (session, symbol, event_date, direction, observer_version,
                 first_detected_at, bar_open_ts_utc, rth_close, close, move_pct,
                 cumulative_volume, cumulative_notional, data_feed,
                 market_data_provider, bar_timeframe, catalyst_source,
                 code_version, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.isoformat(), evaluation.symbol,
                evaluation.event_date.isoformat(), evaluation.direction,
                OBSERVER_VERSION, completed_utc.isoformat(),
                evaluation.bar.ts.isoformat(), evaluation.rth_close,
                evaluation.bar.close, evaluation.move_pct,
                evaluation.cumulative_volume, evaluation.cumulative_notional,
                data_feed, MARKET_DATA_PROVIDER, BAR_TIMEFRAME,
                CATALYST_SOURCE, code_version, run_id,
            ),
        )
        new_candidates += int(result.rowcount > 0)

    cursor = conn.execute(
        """
        INSERT INTO postmarket_ticks
            (session, tick_utc, completed_utc, run_id, run_mode, observer_version,
             code_version, data_feed, market_data_provider, bar_timeframe,
             catalyst_source, scheduled_symbols, evaluated_symbols,
             candidate_observations, new_candidates, invariant_ok,
             thresholds_json, latency_ms, error_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session.isoformat(), tick_utc.isoformat(), completed_utc.isoformat(),
            run_id, run_mode,
            OBSERVER_VERSION, code_version, data_feed, MARKET_DATA_PROVIDER,
            BAR_TIMEFRAME, CATALYST_SOURCE, scheduled_symbols, len(evaluations),
            candidate_observations, new_candidates,
            int(invariant_ok),
            json.dumps(thresholds(), separators=(",", ":"), sort_keys=True),
            latency_ms, error_count,
        ),
    )
    tick_id = cursor.lastrowid
    conn.executemany(
        """
        INSERT INTO postmarket_observations
            (tick_id, symbol, outcome, reason, event_date, bar_open_ts_utc,
             rth_close, open, high, low, close, volume, cumulative_volume,
             cumulative_notional, move_pct, direction, persistence_bars,
             data_age_seconds, data_feed, market_data_provider, bar_timeframe,
             catalyst_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                tick_id, e.symbol, e.outcome, e.reason, e.event_date.isoformat(),
                e.bar.ts.isoformat() if e.bar else None, e.rth_close,
                e.bar.open if e.bar else None, e.bar.high if e.bar else None,
                e.bar.low if e.bar else None, e.bar.close if e.bar else None,
                e.bar.volume if e.bar else None, e.cumulative_volume,
                e.cumulative_notional, e.move_pct, e.direction,
                e.persistence_bars, e.data_age_seconds, data_feed,
                MARKET_DATA_PROVIDER, BAR_TIMEFRAME, CATALYST_SOURCE,
            )
            for e in evaluations
        ],
    )

    return tick_id, new_candidates
