"""After-the-fact full-universe census for major regular-session movers.

The final-RTH fast lane is deliberately bounded.  This module runs only after
the postmarket evidence window, evaluates finalized daily OHLCV for the entire
active universe, and attributes every qualifying close the fast lane missed.
It cannot create candidates, alter thresholds, deliver alerts, or place orders.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.detectors import Bar


CENSUS_VERSION = 1
CENSUS_CHUNK_SIZE = 500
MAX_CENSUS_ATTEMPTS = 3
FINALIZATION_GRACE = timedelta(minutes=5)
MOVE_THRESHOLD_PCT = 8.0
MIN_DAILY_NOTIONAL = 1_000_000.0
EXPECTED_FEED = "sip"
MARKET_DATA_PROVIDER = "alpaca"
BAR_TIMEFRAME = "1Day"
PROVIDER_COMPARISON_STATUS = "NOT_CONFIGURED"
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")

OUTCOME_MAJOR_CLOSE_MOVER = "MAJOR_CLOSE_MOVER"
OUTCOME_EXCURSION_ONLY = "EXCURSION_ONLY"
OUTCOME_BELOW_MOVE = "BELOW_MOVE"
OUTCOME_BELOW_NOTIONAL = "BELOW_NOTIONAL"
OUTCOME_INVALID_DATA = "INVALID_DATA"
OUTCOME_NO_SESSION_BAR = "NO_SESSION_BAR"
OUTCOME_NO_PRIOR_CLOSE = "NO_PRIOR_CLOSE"
OUTCOME_FETCH_ERROR = "FETCH_ERROR"
OUTCOME_NO_DATA_RETURNED = "NO_DATA_RETURNED"


CENSUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS rth_missed_mover_census_runs (
    census_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    census_version INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    code_version TEXT,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    provider_comparison_status TEXT NOT NULL,
    universe_snapshot_sha256 TEXT NOT NULL,
    universe_symbols INTEGER NOT NULL,
    requested_chunks INTEGER NOT NULL,
    fetched_symbols INTEGER NOT NULL,
    evaluated_symbols INTEGER NOT NULL,
    unavailable_symbols INTEGER NOT NULL,
    fast_lane_ticks INTEGER NOT NULL,
    fast_lane_seen_symbols INTEGER NOT NULL,
    fast_lane_candidate_pairs INTEGER NOT NULL,
    major_close_pairs INTEGER NOT NULL,
    caught_pairs INTEGER NOT NULL,
    missed_pairs INTEGER NOT NULL,
    close_recall REAL,
    excursion_only_symbols INTEGER NOT NULL,
    status TEXT NOT NULL,
    invariant_ok INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    thresholds_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(session,census_version,attempt),
    CHECK (status IN ('success','degraded'))
);
CREATE INDEX IF NOT EXISTS idx_rth_missed_mover_census_runs_session
    ON rth_missed_mover_census_runs(session,attempt);

CREATE TABLE IF NOT EXISTS rth_missed_mover_census_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    census_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    data_status TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    prior_close REAL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    daily_notional REAL,
    close_move_pct REAL,
    high_move_pct REAL,
    low_move_pct REAL,
    qualifying_directions_json TEXT NOT NULL,
    excursion_directions_json TEXT NOT NULL,
    fast_lane_seen INTEGER NOT NULL,
    fast_lane_directions_json TEXT NOT NULL,
    fast_lane_outcomes_json TEXT NOT NULL,
    missed_directions_json TEXT NOT NULL,
    miss_reasons_json TEXT NOT NULL,
    UNIQUE(census_id,symbol)
);
CREATE INDEX IF NOT EXISTS idx_rth_missed_mover_census_events_symbol
    ON rth_missed_mover_census_events(symbol,census_id);

CREATE TRIGGER IF NOT EXISTS rth_missed_mover_census_runs_no_update
BEFORE UPDATE ON rth_missed_mover_census_runs BEGIN
    SELECT RAISE(ABORT, 'rth_missed_mover_census_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_missed_mover_census_runs_no_delete
BEFORE DELETE ON rth_missed_mover_census_runs BEGIN
    SELECT RAISE(ABORT, 'rth_missed_mover_census_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_missed_mover_census_events_no_update
BEFORE UPDATE ON rth_missed_mover_census_events BEGIN
    SELECT RAISE(ABORT, 'rth_missed_mover_census_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_missed_mover_census_events_no_delete
BEFORE DELETE ON rth_missed_mover_census_events BEGIN
    SELECT RAISE(ABORT, 'rth_missed_mover_census_events is append-only');
END;
"""


@dataclass(frozen=True)
class RthMissedMoverSymbolResult:
    symbol: str
    data_status: str
    outcome: str
    reason: str
    prior_close: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    daily_notional: float | None
    close_move_pct: float | None
    high_move_pct: float | None
    low_move_pct: float | None
    qualifying_directions: tuple[str, ...]
    excursion_directions: tuple[str, ...]
    fast_lane_seen: bool
    fast_lane_directions: tuple[str, ...]
    fast_lane_outcomes: tuple[str, ...]
    missed_directions: tuple[str, ...]
    miss_reasons: dict[str, str]


@dataclass(frozen=True)
class RthMissedMoverCensusResult:
    census_id: int
    session: str
    attempt: int
    status: str
    universe_symbols: int
    requested_chunks: int
    fetched_symbols: int
    evaluated_symbols: int
    unavailable_symbols: int
    fast_lane_ticks: int
    fast_lane_seen_symbols: int
    fast_lane_candidate_pairs: int
    major_close_pairs: int
    caught_pairs: int
    missed_pairs: int
    close_recall: float | None
    excursion_only_symbols: int
    invariant_ok: bool
    error_count: int
    latency_ms: int


@dataclass(frozen=True)
class RthMissedMoverCensusReport:
    report_version: int
    census_id: int
    session: str
    attempt: int
    code_version: str | None
    operational_complete: bool
    quality_evidence_eligible: bool
    metrics: dict
    missed_major_closes: tuple[dict, ...]
    excursion_only: tuple[dict, ...]
    unavailable: tuple[dict, ...]
    issue_codes: tuple[str, ...]


def ensure_rth_missed_mover_census_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CENSUS_SCHEMA)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _universe_digest(symbols: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def census_window(session: date) -> tuple[datetime, datetime]:
    if not CALENDAR.is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    close = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    postmarket_end = datetime.combine(
        session, datetime.min.time().replace(hour=20), tzinfo=ET
    ).astimezone(timezone.utc)
    return close, postmarket_end


def next_due_rth_missed_mover_census_session(
    conn: sqlite3.Connection,
    *,
    now: datetime,
) -> tuple[date, datetime, datetime] | None:
    current = _aware_utc(now, "now")
    ensure_rth_missed_mover_census_schema(conn)
    sessions = {
        date.fromisoformat(row[0])
        for table in ("rth_momentum_ticks", "postmarket_discovery_ticks")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        for row in conn.execute(
            f"SELECT DISTINCT session FROM {table} ORDER BY session DESC"
        ).fetchall()
    }
    local_session = current.astimezone(ET).date()
    if CALENDAR.is_session(local_session):
        _, local_postmarket_end = census_window(local_session)
        if current >= local_postmarket_end + FINALIZATION_GRACE:
            sessions.add(local_session)
    for session in sorted(sessions, reverse=True):
        close, postmarket_end = census_window(session)
        if current < postmarket_end + FINALIZATION_GRACE:
            continue
        latest = conn.execute(
            """
            SELECT status,attempt FROM rth_missed_mover_census_runs
            WHERE session=? AND census_version=? ORDER BY attempt DESC LIMIT 1
            """,
            (session.isoformat(), CENSUS_VERSION),
        ).fetchone()
        if latest is None or (
            latest[0] == "degraded" and int(latest[1]) < MAX_CENSUS_ATTEMPTS
        ):
            return session, close, postmarket_end
    return None


def _fast_lane_evidence(
    conn: sqlite3.Connection,
    session: date,
) -> tuple[
    int,
    bool,
    set[str],
    dict[str, set[str]],
    dict[str, tuple[str, ...]],
]:
    has_ticks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rth_momentum_ticks'"
    ).fetchone()
    if not has_ticks:
        return 0, False, set(), {}, {}
    tick_count = int(conn.execute(
        "SELECT COUNT(*) FROM rth_momentum_ticks WHERE session=?",
        (session.isoformat(),),
    ).fetchone()[0])
    tick_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(rth_momentum_ticks)")
    }
    full_universe_sweep_active = False
    if "sweep_universe_sha256" in tick_columns:
        full_universe_sweep_active = bool(conn.execute(
            """
            SELECT COUNT(*) FROM rth_momentum_ticks
            WHERE session=? AND sweep_universe_sha256 IS NOT NULL
              AND COALESCE(sweep_shard_symbols,0)>0
            """,
            (session.isoformat(),),
        ).fetchone()[0])
    seen: set[str] = set()
    outcomes: dict[str, set[str]] = {}
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='rth_momentum_observations'"
    ).fetchone():
        for symbol, outcome in conn.execute(
            """
            SELECT symbol,outcome FROM rth_momentum_observations
            WHERE session=? ORDER BY symbol,seq
            """,
            (session.isoformat(),),
        ).fetchall():
            seen.add(symbol)
            outcomes.setdefault(symbol, set()).add(outcome)
    candidates: dict[str, set[str]] = {}
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='rth_momentum_candidates'"
    ).fetchone():
        for symbol, direction in conn.execute(
            """
            SELECT symbol,direction FROM rth_momentum_candidates
            WHERE session=? ORDER BY symbol,direction
            """,
            (session.isoformat(),),
        ).fetchall():
            candidates.setdefault(symbol, set()).add(direction)
    return (
        tick_count,
        full_universe_sweep_active,
        seen,
        candidates,
        {symbol: tuple(sorted(values)) for symbol, values in outcomes.items()},
    )


def _valid_daily_bar(bar: Bar) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close)
    return (
        bar.ts.tzinfo is not None
        and bar.ts.utcoffset() is not None
        and all(math.isfinite(value) and value > 0 for value in values)
        and bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        and bar.volume > 0
    )


def _unavailable_result(
    symbol: str,
    *,
    status: str,
    reason: str,
    fast_lane_seen: bool,
    fast_lane_directions: set[str],
    fast_lane_outcomes: tuple[str, ...],
) -> RthMissedMoverSymbolResult:
    return RthMissedMoverSymbolResult(
        symbol=symbol,
        data_status=status,
        outcome=status,
        reason=reason,
        prior_close=None,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        daily_notional=None,
        close_move_pct=None,
        high_move_pct=None,
        low_move_pct=None,
        qualifying_directions=(),
        excursion_directions=(),
        fast_lane_seen=fast_lane_seen,
        fast_lane_directions=tuple(sorted(fast_lane_directions)),
        fast_lane_outcomes=fast_lane_outcomes,
        missed_directions=(),
        miss_reasons={},
    )


def evaluate_rth_missed_mover_symbol(
    symbol: str,
    session: date,
    daily_bars: Sequence[Bar],
    *,
    fast_lane_ticks: int,
    full_universe_sweep_active: bool = False,
    fast_lane_seen: bool,
    fast_lane_directions: set[str],
    fast_lane_outcomes: tuple[str, ...],
) -> RthMissedMoverSymbolResult:
    ordered = list(daily_bars)
    if any(bar.symbol != symbol for bar in ordered):
        return _unavailable_result(
            symbol,
            status=OUTCOME_INVALID_DATA,
            reason="daily-bar response contains the wrong symbol identity",
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    if any(
        bar.ts.tzinfo is None or bar.ts.utcoffset() is None for bar in ordered
    ):
        return _unavailable_result(
            symbol,
            status=OUTCOME_INVALID_DATA,
            reason="daily-bar response contains a naive timestamp",
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    ordered.sort(key=lambda bar: bar.ts)
    if any(bar.ts.astimezone(ET).date() > session for bar in ordered):
        return _unavailable_result(
            symbol,
            status=OUTCOME_INVALID_DATA,
            reason="daily-bar response contains a future session",
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    current_rows = [bar for bar in ordered if bar.ts.astimezone(ET).date() == session]
    prior_rows = [bar for bar in ordered if bar.ts.astimezone(ET).date() < session]
    if len(current_rows) != 1:
        status = OUTCOME_NO_SESSION_BAR if not current_rows else OUTCOME_INVALID_DATA
        return _unavailable_result(
            symbol,
            status=status,
            reason=(
                "no finalized daily bar for census session"
                if not current_rows
                else "duplicate daily bars for census session"
            ),
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    if not prior_rows:
        return _unavailable_result(
            symbol,
            status=OUTCOME_NO_PRIOR_CLOSE,
            reason="no daily bar before census session",
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    current = current_rows[0]
    prior_date = prior_rows[-1].ts.astimezone(ET).date()
    latest_prior_rows = [
        bar for bar in prior_rows if bar.ts.astimezone(ET).date() == prior_date
    ]
    if len(latest_prior_rows) != 1:
        return _unavailable_result(
            symbol,
            status=OUTCOME_INVALID_DATA,
            reason="duplicate daily bars for latest prior session",
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    prior = latest_prior_rows[0]
    if not _valid_daily_bar(current) or not _valid_daily_bar(prior):
        return _unavailable_result(
            symbol,
            status=OUTCOME_INVALID_DATA,
            reason="invalid current or prior daily OHLCV",
            fast_lane_seen=fast_lane_seen,
            fast_lane_directions=fast_lane_directions,
            fast_lane_outcomes=fast_lane_outcomes,
        )
    close_move = (current.close / prior.close - 1.0) * 100.0
    high_move = (current.high / prior.close - 1.0) * 100.0
    low_move = (current.low / prior.close - 1.0) * 100.0
    notional = current.close * current.volume
    liquid = notional >= MIN_DAILY_NOTIONAL
    qualifying = (
        (("up",) if close_move >= MOVE_THRESHOLD_PCT else ())
        + (("down",) if close_move <= -MOVE_THRESHOLD_PCT else ())
        if liquid else ()
    )
    excursions = (
        (("up",) if high_move >= MOVE_THRESHOLD_PCT else ())
        + (("down",) if low_move <= -MOVE_THRESHOLD_PCT else ())
        if liquid else ()
    )
    missed = tuple(sorted(set(qualifying) - fast_lane_directions))
    miss_reasons = {}
    for direction in missed:
        if fast_lane_ticks == 0:
            reason = "RTH_LANE_NOT_RUNNING"
        elif not fast_lane_seen:
            reason = (
                "NOT_OBSERVED_BY_FULL_UNIVERSE_RTH_SWEEP"
                if full_universe_sweep_active
                else "NOT_SELECTED_BY_BOUNDED_RTH_LANE"
            )
        else:
            suffix = ",".join(fast_lane_outcomes) or "UNKNOWN"
            reason = f"SELECTED_NOT_QUALIFIED:{suffix}"
        miss_reasons[direction] = reason
    if qualifying:
        outcome = OUTCOME_MAJOR_CLOSE_MOVER
        reason = "final close exceeded the major-move and daily-notional thresholds"
    elif excursions:
        outcome = OUTCOME_EXCURSION_ONLY
        reason = "intraday high/low crossed the move threshold but final close did not"
    elif not liquid:
        outcome = OUTCOME_BELOW_NOTIONAL
        reason = "finalized daily notional was below the census floor"
    else:
        outcome = OUTCOME_BELOW_MOVE
        reason = "final close and intraday range did not cross the move threshold"
    return RthMissedMoverSymbolResult(
        symbol=symbol,
        data_status="AVAILABLE",
        outcome=outcome,
        reason=reason,
        prior_close=prior.close,
        open=current.open,
        high=current.high,
        low=current.low,
        close=current.close,
        volume=current.volume,
        daily_notional=notional,
        close_move_pct=close_move,
        high_move_pct=high_move,
        low_move_pct=low_move,
        qualifying_directions=tuple(sorted(qualifying)),
        excursion_directions=tuple(sorted(excursions)),
        fast_lane_seen=fast_lane_seen,
        fast_lane_directions=tuple(sorted(fast_lane_directions)),
        fast_lane_outcomes=fast_lane_outcomes,
        missed_directions=missed,
        miss_reasons=miss_reasons,
    )


def _next_attempt(conn: sqlite3.Connection, session: date) -> int:
    return int(conn.execute(
        """
        SELECT COALESCE(MAX(attempt),0)+1 FROM rth_missed_mover_census_runs
        WHERE session=? AND census_version=?
        """,
        (session.isoformat(), CENSUS_VERSION),
    ).fetchone()[0])


def run_rth_missed_mover_census(
    conn: sqlite3.Connection,
    *,
    universe_symbols: Sequence[str],
    session: date,
    postmarket_end: datetime,
    now: datetime,
    run_id: str,
    code_version: str | None,
    data_feed: str,
    daily_fetch: Callable[[list[str]], dict[str, Sequence[Bar]]],
    chunk_size: int = CENSUS_CHUNK_SIZE,
) -> tuple[RthMissedMoverCensusResult, tuple[RthMissedMoverSymbolResult, ...]]:
    current = _aware_utc(now, "now")
    end_utc = _aware_utc(postmarket_end, "postmarket_end")
    _, expected_end = census_window(session)
    if end_utc != expected_end:
        raise ValueError("postmarket_end does not match the XNYS census session")
    if current < end_utc + FINALIZATION_GRACE:
        raise ValueError("RTH missed-mover census requires the finalized postmarket window")
    if data_feed != EXPECTED_FEED:
        raise ValueError("RTH missed-mover census requires SIP daily bars")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    symbols = tuple(sorted(set(universe_symbols)))
    if not symbols or any(
        not symbol or symbol != symbol.strip().upper() for symbol in symbols
    ):
        raise ValueError("universe_symbols must contain canonical symbols")
    ensure_rth_missed_mover_census_schema(conn)
    attempt = _next_attempt(conn, session)
    if attempt > MAX_CENSUS_ATTEMPTS:
        raise ValueError("maximum RTH missed-mover census attempts reached")
    started = time.perf_counter()
    (
        fast_ticks,
        full_universe_sweep_active,
        fast_seen,
        fast_candidates,
        fast_outcomes,
    ) = _fast_lane_evidence(conn, session)
    universe_set = set(symbols)
    fast_seen &= universe_set
    fast_candidates = {
        symbol: directions
        for symbol, directions in fast_candidates.items()
        if symbol in universe_set
    }
    fast_outcomes = {
        symbol: outcomes
        for symbol, outcomes in fast_outcomes.items()
        if symbol in universe_set
    }
    rows: list[RthMissedMoverSymbolResult] = []
    chunk_errors: list[dict] = []
    evaluation_errors: list[dict] = []
    unexpected_response_symbols: list[dict] = []
    fetched_symbols = 0
    for index in range(0, len(symbols), chunk_size):
        chunk = list(symbols[index:index + chunk_size])
        try:
            response = daily_fetch(chunk)
        except Exception as exc:
            response = {}
            failed_chunk = True
            chunk_errors.append({
                "chunk": index // chunk_size + 1,
                "symbols": len(chunk),
                "error_type": type(exc).__name__,
            })
        else:
            failed_chunk = False
            unexpected = sorted(set(response) - set(chunk))
            unexpected_response_symbols.extend(
                {
                    "chunk": index // chunk_size + 1,
                    "symbol": symbol,
                }
                for symbol in unexpected
            )
        for symbol in chunk:
            directions = fast_candidates.get(symbol, set())
            outcomes = fast_outcomes.get(symbol, ())
            if failed_chunk:
                rows.append(_unavailable_result(
                    symbol,
                    status=OUTCOME_FETCH_ERROR,
                    reason="daily census chunk fetch failed",
                    fast_lane_seen=symbol in fast_seen,
                    fast_lane_directions=directions,
                    fast_lane_outcomes=outcomes,
                ))
                continue
            if symbol not in response:
                rows.append(_unavailable_result(
                    symbol,
                    status=OUTCOME_NO_DATA_RETURNED,
                    reason="symbol absent from successful daily-bar response",
                    fast_lane_seen=symbol in fast_seen,
                    fast_lane_directions=directions,
                    fast_lane_outcomes=outcomes,
                ))
                continue
            fetched_symbols += 1
            try:
                rows.append(evaluate_rth_missed_mover_symbol(
                    symbol,
                    session,
                    response[symbol],
                    fast_lane_ticks=fast_ticks,
                    full_universe_sweep_active=full_universe_sweep_active,
                    fast_lane_seen=symbol in fast_seen,
                    fast_lane_directions=directions,
                    fast_lane_outcomes=outcomes,
                ))
            except Exception as exc:
                evaluation_errors.append({
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                })
                rows.append(_unavailable_result(
                    symbol,
                    status="EVALUATION_ERROR",
                    reason=f"{type(exc).__name__}: {exc}"[:1000],
                    fast_lane_seen=symbol in fast_seen,
                    fast_lane_directions=directions,
                    fast_lane_outcomes=outcomes,
                ))
    available = [row for row in rows if row.data_status == "AVAILABLE"]
    major_pairs = sum(len(row.qualifying_directions) for row in available)
    missed_pairs = sum(len(row.missed_directions) for row in available)
    caught_pairs = major_pairs - missed_pairs
    unavailable = len(rows) - len(available)
    provider_unavailable = sum(
        row.data_status in {OUTCOME_FETCH_ERROR, OUTCOME_NO_DATA_RETURNED}
        for row in rows
    )
    requested_chunks = math.ceil(len(symbols) / chunk_size)
    invariant_ok = (
        len(rows) == len(symbols)
        and fetched_symbols + provider_unavailable == len(rows)
        and caught_pairs + missed_pairs == major_pairs
    )
    error_count = (
        len(chunk_errors)
        + len(evaluation_errors)
        + len(unexpected_response_symbols)
    )
    status = "success" if invariant_ok and not error_count else "degraded"
    recall = caught_pairs / major_pairs if major_pairs else None
    completed = current + timedelta(seconds=time.perf_counter() - started)
    thresholds = {
        "move_pct": MOVE_THRESHOLD_PCT,
        "minimum_daily_notional": MIN_DAILY_NOTIONAL,
        "truth_scope": "finalized_daily_close",
        "excursion_scope": "daily_high_low_review_only",
    }
    detail = {
        "chunk_errors": chunk_errors,
        "evaluation_errors": evaluation_errors,
        "unexpected_response_symbols": unexpected_response_symbols,
        "postmarket_end_utc": end_utc.isoformat(),
        "full_universe_sweep_active": full_universe_sweep_active,
        "limitation": (
            "daily OHLCV cannot establish intraday persistence or exact eligibility time; "
            "high/low excursions are review-only"
        ),
    }
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO rth_missed_mover_census_runs
              (session,census_version,attempt,run_id,started_at_utc,
               completed_at_utc,code_version,data_feed,market_data_provider,
               bar_timeframe,provider_comparison_status,universe_snapshot_sha256,
               universe_symbols,requested_chunks,fetched_symbols,evaluated_symbols,
               unavailable_symbols,fast_lane_ticks,fast_lane_seen_symbols,
               fast_lane_candidate_pairs,major_close_pairs,caught_pairs,missed_pairs,
               close_recall,excursion_only_symbols,status,invariant_ok,error_count,
               thresholds_json,detail_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.isoformat(), CENSUS_VERSION, attempt, run_id,
                current.isoformat(), completed.isoformat(), code_version, data_feed,
                MARKET_DATA_PROVIDER, BAR_TIMEFRAME, PROVIDER_COMPARISON_STATUS,
                _universe_digest(symbols), len(symbols), requested_chunks,
                fetched_symbols, fetched_symbols, unavailable, fast_ticks,
                len(fast_seen), sum(len(value) for value in fast_candidates.values()),
                major_pairs, caught_pairs, missed_pairs, recall,
                sum(row.outcome == OUTCOME_EXCURSION_ONLY for row in available),
                status, int(invariant_ok), error_count, _json(thresholds), _json(detail),
            ),
        )
        census_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO rth_missed_mover_census_events
              (census_id,symbol,data_status,outcome,reason,prior_close,open,high,
               low,close,volume,daily_notional,close_move_pct,high_move_pct,
               low_move_pct,qualifying_directions_json,excursion_directions_json,
               fast_lane_seen,fast_lane_directions_json,fast_lane_outcomes_json,
               missed_directions_json,miss_reasons_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    census_id, row.symbol, row.data_status, row.outcome, row.reason,
                    row.prior_close, row.open, row.high, row.low, row.close, row.volume,
                    row.daily_notional, row.close_move_pct, row.high_move_pct,
                    row.low_move_pct, _json(row.qualifying_directions),
                    _json(row.excursion_directions), int(row.fast_lane_seen),
                    _json(row.fast_lane_directions), _json(row.fast_lane_outcomes),
                    _json(row.missed_directions), _json(row.miss_reasons),
                )
                for row in rows
            ],
        )
    result = RthMissedMoverCensusResult(
        census_id=census_id,
        session=session.isoformat(),
        attempt=attempt,
        status=status,
        universe_symbols=len(symbols),
        requested_chunks=requested_chunks,
        fetched_symbols=fetched_symbols,
        evaluated_symbols=fetched_symbols,
        unavailable_symbols=unavailable,
        fast_lane_ticks=fast_ticks,
        fast_lane_seen_symbols=len(fast_seen),
        fast_lane_candidate_pairs=sum(len(value) for value in fast_candidates.values()),
        major_close_pairs=major_pairs,
        caught_pairs=caught_pairs,
        missed_pairs=missed_pairs,
        close_recall=recall,
        excursion_only_symbols=sum(
            row.outcome == OUTCOME_EXCURSION_ONLY for row in available
        ),
        invariant_ok=invariant_ok,
        error_count=error_count,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
    return result, tuple(rows)


def build_rth_missed_mover_census_report(
    conn: sqlite3.Connection,
    census_id: int,
) -> RthMissedMoverCensusReport:
    ensure_rth_missed_mover_census_schema(conn)
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT * FROM rth_missed_mover_census_runs WHERE census_id=?",
            (census_id,),
        ).fetchone()
        if run is None:
            raise ValueError("RTH missed-mover census run does not exist")
        events = conn.execute(
            """
            SELECT * FROM rth_missed_mover_census_events
            WHERE census_id=? ORDER BY symbol
            """,
            (census_id,),
        ).fetchall()
    finally:
        conn.row_factory = original
    missed = tuple(
        {
            "symbol": row["symbol"],
            "directions": json.loads(row["missed_directions_json"]),
            "reasons": json.loads(row["miss_reasons_json"]),
            "close_move_pct": row["close_move_pct"],
            "daily_notional": row["daily_notional"],
            "fast_lane_seen": bool(row["fast_lane_seen"]),
            "fast_lane_outcomes": json.loads(row["fast_lane_outcomes_json"]),
        }
        for row in events
        if json.loads(row["missed_directions_json"])
    )
    excursions = tuple(
        {
            "symbol": row["symbol"],
            "directions": json.loads(row["excursion_directions_json"]),
            "high_move_pct": row["high_move_pct"],
            "low_move_pct": row["low_move_pct"],
            "close_move_pct": row["close_move_pct"],
        }
        for row in events
        if row["outcome"] == OUTCOME_EXCURSION_ONLY
    )
    unavailable = tuple(
        {"symbol": row["symbol"], "data_status": row["data_status"]}
        for row in events
        if row["data_status"] != "AVAILABLE"
    )
    issues: list[str] = []
    if run["status"] != "success" or not run["invariant_ok"]:
        issues.append("CENSUS_OPERATIONAL_FAILURE")
    if run["unavailable_symbols"]:
        issues.append("UNAVAILABLE_SYMBOLS")
    if run["missed_pairs"]:
        issues.append("MISSED_MAJOR_CLOSE_MOVERS")
    if run["provider_comparison_status"] != "AVAILABLE":
        issues.append("PROVIDER_COMPARISON_NOT_CONFIGURED")
    if run["code_version"] in {None, "", "unknown"}:
        issues.append("CODE_VERSION_MISSING")
    operational = run["status"] == "success" and bool(run["invariant_ok"])
    metrics = {
        key: run[key]
        for key in (
            "universe_symbols", "requested_chunks", "fetched_symbols",
            "evaluated_symbols", "unavailable_symbols", "fast_lane_ticks",
            "fast_lane_seen_symbols", "fast_lane_candidate_pairs",
            "major_close_pairs", "caught_pairs", "missed_pairs", "close_recall",
            "excursion_only_symbols", "provider_comparison_status",
        )
    }
    return RthMissedMoverCensusReport(
        report_version=int(run["attempt"]),
        census_id=int(run["census_id"]),
        session=run["session"],
        attempt=int(run["attempt"]),
        code_version=run["code_version"],
        operational_complete=operational,
        quality_evidence_eligible=operational and not issues,
        metrics=metrics,
        missed_major_closes=missed,
        excursion_only=excursions,
        unavailable=unavailable,
        issue_codes=tuple(issues),
    )


def _write_atomic_exclusive(path: Path, payload: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    descriptor, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        os.chmod(path, 0o444)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def write_rth_missed_mover_census_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    census_id: int,
) -> tuple[RthMissedMoverCensusReport, bool]:
    report = build_rth_missed_mover_census_report(conn, census_id)
    destination = output_dir / (
        f"rth_missed_mover_census_{report.session}_v{report.report_version}.json"
    )
    return report, _write_atomic_exclusive(destination, asdict(report))


def next_unreported_rth_missed_mover_census(
    conn: sqlite3.Connection,
    output_dir: Path,
) -> int | None:
    """Return the newest durable run whose immutable report is absent."""
    ensure_rth_missed_mover_census_schema(conn)
    for census_id, session, attempt in conn.execute(
        """
        SELECT census_id,session,attempt FROM rth_missed_mover_census_runs
        ORDER BY session DESC,attempt DESC,census_id DESC
        """
    ).fetchall():
        path = output_dir / f"rth_missed_mover_census_{session}_v{attempt}.json"
        if not path.exists():
            return int(census_id)
    return None


def latest_rth_missed_mover_census_summary(output_dir: Path) -> dict | None:
    latest: tuple[tuple[str, int], dict] | None = None
    for path in output_dir.glob("rth_missed_mover_census_*_v*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = (str(payload["session"]), int(payload["report_version"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid RTH missed-mover census report: {path}") from exc
        if latest is None or key > latest[0]:
            latest = (key, payload)
    if latest is None:
        return None
    payload = latest[1]
    return {
        "session": payload["session"],
        "report_version": payload["report_version"],
        "operational_complete": payload["operational_complete"],
        "quality_evidence_eligible": payload["quality_evidence_eligible"],
        "close_recall": payload["metrics"]["close_recall"],
        "missed_pairs": payload["metrics"]["missed_pairs"],
        "unavailable_symbols": payload["metrics"]["unavailable_symbols"],
        "issue_codes": payload["issue_codes"],
    }
