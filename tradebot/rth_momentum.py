"""Append-only final-RTH market-wide momentum evidence and postmarket handoff.

This is the pre-close fast lane for the existing market-wide discovery
supervisor.  It observes bounded provider movers/actives plus scheduled
after-hours reporters during the final 30 minutes of the real XNYS session,
qualifies only completed five-minute bars against a real prior close, and
persists an explicit handoff identity for postmarket reconciliation.

It has no alert, Telegram, customer, broker, or order dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.detectors import Bar, bar_close_ts
from tradebot.marketdata import MarketWideScreen, partition_intraday_bars
from tradebot.marketwide_screen import validate_marketwide_screen


RTH_MOMENTUM_VERSION = 1
RTH_HANDOFF_LEAD = timedelta(minutes=30)
MOVE_THRESHOLD_PCT = 8.0
PERSISTENCE_BARS = 2
MIN_CUMULATIVE_NOTIONAL = 1_000_000.0
MAX_DATA_AGE_SECONDS = 420
BAR_TIMEFRAME = "5Min"
MARKET_DATA_PROVIDER = "alpaca"
SCREEN_TOP_N = 50
POLL_SECONDS = 60
FULL_UNIVERSE_RTH_SWEEP_SOURCE = "full_universe_rth_sweep"
FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS = 5
MAX_FULL_UNIVERSE_RTH_SWEEP_SHARD_SIZE = 4_000
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")

OUTCOME_CANDIDATE = "CANDIDATE"
OUTCOME_BELOW_MOVE = "BELOW_MOVE"
OUTCOME_AWAITING_PERSISTENCE = "AWAITING_PERSISTENCE"
OUTCOME_BELOW_NOTIONAL = "BELOW_NOTIONAL"
OUTCOME_NO_PRIOR_CLOSE = "NO_PRIOR_CLOSE"
OUTCOME_NO_COMPLETED_RTH_BAR = "NO_COMPLETED_RTH_BAR"
OUTCOME_NO_INTRADAY_BARS_RETURNED = "NO_INTRADAY_BARS_RETURNED"
OUTCOME_NO_DAILY_BASELINE_RETURNED = "NO_DAILY_BASELINE_RETURNED"
OUTCOME_STALE = "STALE"
OUTCOME_BAR_GAP = "BAR_GAP"
OUTCOME_INVALID_DATA = "INVALID_DATA"
OUTCOME_FETCH_ERROR = "FETCH_ERROR"

HANDOFF_RTH_QUALIFIED = "RTH_QUALIFIED"
HANDOFF_POSTMARKET_QUALIFIED = "POSTMARKET_QUALIFIED"
HANDOFF_POSTMARKET_NOT_QUALIFIED = "POSTMARKET_NOT_QUALIFIED"


RTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS rth_momentum_ticks (
    tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    scheduled_tick_utc TEXT NOT NULL,
    tick_utc TEXT NOT NULL,
    completed_utc TEXT NOT NULL,
    window_start_utc TEXT NOT NULL,
    session_close_utc TEXT NOT NULL,
    momentum_version INTEGER NOT NULL,
    run_mode TEXT NOT NULL,
    run_id TEXT NOT NULL,
    code_version TEXT,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    universe_symbols INTEGER NOT NULL,
    provider_screen_rows INTEGER NOT NULL,
    provider_screen_unique_symbols INTEGER NOT NULL,
    screen_error TEXT,
    scheduled_symbols INTEGER NOT NULL,
    sweep_universe_sha256 TEXT,
    sweep_cycle_ticks INTEGER,
    sweep_shard_index INTEGER,
    sweep_shard_count INTEGER,
    sweep_shard_size INTEGER,
    sweep_shard_symbols INTEGER,
    sweep_overlap_symbols INTEGER,
    selected_symbols INTEGER NOT NULL,
    intraday_symbols_fetched INTEGER NOT NULL,
    daily_symbols_fetched INTEGER NOT NULL,
    evaluated_symbols INTEGER NOT NULL,
    candidate_observations INTEGER NOT NULL,
    new_candidates INTEGER NOT NULL,
    invariant_ok INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    missed_cycles INTEGER NOT NULL,
    scheduled_lag_ms INTEGER NOT NULL,
    screen_latency_ms INTEGER NOT NULL,
    selection_latency_ms INTEGER NOT NULL,
    bar_fetch_latency_ms INTEGER NOT NULL,
    evaluation_latency_ms INTEGER NOT NULL,
    total_latency_ms INTEGER NOT NULL,
    thresholds_json TEXT NOT NULL,
    UNIQUE(session,scheduled_tick_utc)
);
CREATE INDEX IF NOT EXISTS idx_rth_momentum_ticks_session
    ON rth_momentum_ticks(session,tick_utc);

CREATE TABLE IF NOT EXISTS rth_momentum_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    ranks_json TEXT NOT NULL,
    screen_evidence_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    prior_close REAL,
    bar_open_ts_utc TEXT,
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
    UNIQUE(tick_id,symbol)
);
CREATE INDEX IF NOT EXISTS idx_rth_momentum_observations_symbol
    ON rth_momentum_observations(session,symbol,seq);
CREATE INDEX IF NOT EXISTS idx_rth_momentum_observations_outcome
    ON rth_momentum_observations(session,outcome,seq);

CREATE TABLE IF NOT EXISTS rth_momentum_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    momentum_version INTEGER NOT NULL,
    first_detected_at TEXT NOT NULL,
    bar_open_ts_utc TEXT NOT NULL,
    prior_close REAL NOT NULL,
    close REAL NOT NULL,
    move_pct REAL NOT NULL,
    cumulative_volume INTEGER NOT NULL,
    cumulative_notional REAL NOT NULL,
    sources_json TEXT NOT NULL,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    UNIQUE(session,symbol,direction,momentum_version)
);
CREATE INDEX IF NOT EXISTS idx_rth_momentum_candidates_session
    ON rth_momentum_candidates(session,first_detected_at);

CREATE TABLE IF NOT EXISTS rth_postmarket_handoffs (
    handoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rth_candidate_id INTEGER NOT NULL,
    postmarket_candidate_id INTEGER,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    state TEXT NOT NULL,
    transition_at_utc TEXT NOT NULL,
    reason TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    UNIQUE(rth_candidate_id,state),
    CHECK (state IN (
        'RTH_QUALIFIED','POSTMARKET_QUALIFIED','POSTMARKET_NOT_QUALIFIED'
    ))
);
CREATE INDEX IF NOT EXISTS idx_rth_postmarket_handoffs_session
    ON rth_postmarket_handoffs(session,state,handoff_id);

CREATE TRIGGER IF NOT EXISTS rth_momentum_ticks_no_update
BEFORE UPDATE ON rth_momentum_ticks BEGIN
    SELECT RAISE(ABORT, 'rth_momentum_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_momentum_ticks_no_delete
BEFORE DELETE ON rth_momentum_ticks BEGIN
    SELECT RAISE(ABORT, 'rth_momentum_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_momentum_observations_no_update
BEFORE UPDATE ON rth_momentum_observations BEGIN
    SELECT RAISE(ABORT, 'rth_momentum_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_momentum_observations_no_delete
BEFORE DELETE ON rth_momentum_observations BEGIN
    SELECT RAISE(ABORT, 'rth_momentum_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_momentum_candidates_no_update
BEFORE UPDATE ON rth_momentum_candidates BEGIN
    SELECT RAISE(ABORT, 'rth_momentum_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_momentum_candidates_no_delete
BEFORE DELETE ON rth_momentum_candidates BEGIN
    SELECT RAISE(ABORT, 'rth_momentum_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_postmarket_handoffs_no_update
BEFORE UPDATE ON rth_postmarket_handoffs BEGIN
    SELECT RAISE(ABORT, 'rth_postmarket_handoffs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rth_postmarket_handoffs_no_delete
BEFORE DELETE ON rth_postmarket_handoffs BEGIN
    SELECT RAISE(ABORT, 'rth_postmarket_handoffs is append-only');
END;
"""


@dataclass(frozen=True)
class RthSelectedSymbol:
    symbol: str
    sources: tuple[str, ...]
    ranks: tuple[tuple[str, int], ...]
    screen_evidence: tuple[dict, ...]


@dataclass(frozen=True)
class RthSelection:
    symbols: tuple[RthSelectedSymbol, ...]
    universe_symbols: int
    provider_screen_rows: int
    provider_screen_unique_symbols: int
    screen_error: str | None
    scheduled_symbols: int
    excluded_symbols: int
    sweep: RthUniverseSweep | None
    sweep_overlap_symbols: int


@dataclass(frozen=True)
class RthUniverseSweep:
    symbols: tuple[str, ...]
    universe_symbols: int
    universe_sha256: str
    scheduled_tick_utc: datetime
    cycle_ticks: int
    shard_index: int
    shard_count: int
    shard_size: int
    shard_start: int


@dataclass(frozen=True)
class RthEvaluation:
    symbol: str
    session: date
    outcome: str
    reason: str
    prior_close: float | None = None
    bar: Bar | None = None
    cumulative_volume: int | None = None
    cumulative_notional: float | None = None
    move_pct: float | None = None
    direction: str | None = None
    persistence_bars: int = 0
    data_age_seconds: float | None = None


@dataclass(frozen=True)
class RthTickSchedule:
    scheduled_tick_utc: datetime
    scheduled_lag_ms: int
    missed_cycles: int


@dataclass(frozen=True)
class RthTickResult:
    tick_id: int
    session: str
    selected_symbols: int
    sweep_shard_index: int | None
    sweep_shard_count: int | None
    sweep_shard_symbols: int
    sweep_overlap_symbols: int
    intraday_symbols_fetched: int
    daily_symbols_fetched: int
    evaluated_symbols: int
    candidate_observations: int
    new_candidates: int
    invariant_ok: bool
    error_count: int
    missed_cycles: int
    scheduled_lag_ms: int
    screen_latency_ms: int
    selection_latency_ms: int
    bar_fetch_latency_ms: int
    evaluation_latency_ms: int
    latency_ms: int


@dataclass(frozen=True)
class HandoffResult:
    session: str
    rth_candidates: int
    postmarket_links_written: int
    terminal_not_qualified_written: int


def rth_poll_sleep_seconds(
    now: datetime,
    *,
    window_start: datetime,
    interval_seconds: int = POLL_SECONDS,
) -> float:
    """Return sleep to the next window-anchored minute without drift."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if window_start.tzinfo is None or window_start.utcoffset() is None:
        raise ValueError("window_start must be timezone-aware")
    elapsed = (now - window_start).total_seconds()
    if elapsed < 0:
        raise ValueError("now must not precede window_start")
    next_slot = math.floor(elapsed / interval_seconds) + 1
    next_tick = window_start + timedelta(seconds=next_slot * interval_seconds)
    return max(0.1, (next_tick - now).total_seconds())


def ensure_rth_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(RTH_SCHEMA)
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(rth_momentum_ticks)")
    }
    additions = {
        "sweep_universe_sha256": "TEXT",
        "sweep_cycle_ticks": "INTEGER",
        "sweep_shard_index": "INTEGER",
        "sweep_shard_count": "INTEGER",
        "sweep_shard_size": "INTEGER",
        "sweep_shard_symbols": "INTEGER",
        "sweep_overlap_symbols": "INTEGER",
        "screen_error": "TEXT",
    }
    for column, column_type in additions.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE rth_momentum_ticks ADD COLUMN {column} {column_type}"
            )


def rth_handoff_window(
    now: datetime,
    *,
    calendar=CALENDAR,
) -> tuple[date, datetime, datetime] | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    session = now.astimezone(ET).date()
    if not calendar.is_session(session):
        return None
    session_open = calendar.session_open(session).to_pydatetime().astimezone(timezone.utc)
    session_close = calendar.session_close(session).to_pydatetime().astimezone(timezone.utc)
    return session, max(session_open, session_close - RTH_HANDOFF_LEAD), session_close


def rth_handoff_is_active(now: datetime, *, calendar=CALENDAR) -> bool:
    window = rth_handoff_window(now, calendar=calendar)
    return window is not None and window[1] <= now <= window[2]


def rth_thresholds() -> dict:
    return {
        "move_pct": MOVE_THRESHOLD_PCT,
        "persistence_bars": PERSISTENCE_BARS,
        "minimum_cumulative_notional": MIN_CUMULATIVE_NOTIONAL,
        "maximum_data_age_seconds": MAX_DATA_AGE_SECONDS,
        "bar_timeframe": BAR_TIMEFRAME,
        "window_lead_minutes": int(RTH_HANDOFF_LEAD.total_seconds() / 60),
    }


def select_rth_symbols(
    screen: MarketWideScreen,
    active_universe: set[str],
    scheduled_earnings: set[str],
    universe_sweep: RthUniverseSweep | None = None,
    screen_error: str | None = None,
) -> RthSelection:
    canonical = {symbol.strip().upper() for symbol in active_universe}
    if canonical != active_universe or any(not symbol for symbol in active_universe):
        raise ValueError("active universe symbols must be canonical non-empty strings")
    if universe_sweep is not None:
        expected_digest = hashlib.sha256(
            "\n".join(sorted(active_universe)).encode("utf-8")
        ).hexdigest()
        if (
            universe_sweep.universe_symbols != len(active_universe)
            or universe_sweep.universe_sha256 != expected_digest
            or len(set(universe_sweep.symbols)) != len(universe_sweep.symbols)
            or not set(universe_sweep.symbols) <= active_universe
        ):
            raise ValueError("RTH full-universe sweep does not match active universe")
    scheduled = {symbol for symbol in scheduled_earnings if symbol in active_universe}
    grouped: dict[str, list] = {}
    for entry in screen.entries:
        grouped.setdefault(entry.symbol, []).append(entry)
    provider_symbols = set(grouped)
    sweep_symbols = (
        set(universe_sweep.symbols) if universe_sweep is not None else set()
    )
    sweep_positions = (
        {
            symbol: universe_sweep.shard_start + offset
            for offset, symbol in enumerate(universe_sweep.symbols)
        }
        if universe_sweep is not None
        else {}
    )
    selected = []
    for symbol in sorted(
        (provider_symbols & active_universe) | scheduled | sweep_symbols
    ):
        entries = grouped.get(symbol, [])
        sources = {entry.source for entry in entries}
        if symbol in scheduled:
            sources.add("scheduled_earnings")
        if symbol in sweep_symbols:
            sources.add(FULL_UNIVERSE_RTH_SWEEP_SOURCE)
        evidence = [
            {
                "source": entry.source,
                "rank": entry.rank,
                "source_updated_at": entry.source_updated_at.isoformat(),
                "move_pct": entry.move_pct,
                "price": entry.price,
                "volume": entry.volume,
                "trade_count": entry.trade_count,
            }
            for entry in sorted(entries, key=lambda row: (row.source, row.rank))
        ]
        if universe_sweep is not None and symbol in sweep_symbols:
            evidence.append(
                {
                    "source": FULL_UNIVERSE_RTH_SWEEP_SOURCE,
                    "scheduled_tick_utc": (
                        universe_sweep.scheduled_tick_utc.isoformat()
                    ),
                    "universe_sha256": universe_sweep.universe_sha256,
                    "universe_position": sweep_positions[symbol],
                    "cycle_ticks": universe_sweep.cycle_ticks,
                    "shard_index": universe_sweep.shard_index,
                    "shard_count": universe_sweep.shard_count,
                    "shard_size": universe_sweep.shard_size,
                }
            )
        selected.append(
            RthSelectedSymbol(
                symbol=symbol,
                sources=tuple(sorted(sources)),
                ranks=tuple(sorted((entry.source, entry.rank) for entry in entries)),
                screen_evidence=tuple(evidence),
            )
        )
    return RthSelection(
        symbols=tuple(selected),
        universe_symbols=len(active_universe),
        provider_screen_rows=len(screen.entries),
        provider_screen_unique_symbols=len(provider_symbols),
        screen_error=screen_error,
        scheduled_symbols=len(scheduled),
        excluded_symbols=len((provider_symbols | sweep_symbols) - active_universe),
        sweep=universe_sweep,
        sweep_overlap_symbols=len(
            sweep_symbols & ((provider_symbols & active_universe) | scheduled)
        ),
    )


def plan_rth_universe_sweep(
    active_symbols: set[str],
    *,
    scheduled_tick_utc: datetime,
    window_start: datetime,
    cycle_ticks: int = FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS,
    max_shard_size: int = MAX_FULL_UNIVERSE_RTH_SWEEP_SHARD_SIZE,
) -> RthUniverseSweep:
    """Plan one deterministic shard covering the active universe every cycle."""
    if cycle_ticks <= 0 or max_shard_size <= 0:
        raise ValueError("RTH sweep cycle and maximum shard size must be positive")
    for label, value in (
        ("scheduled_tick_utc", scheduled_tick_utc),
        ("window_start", window_start),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
    scheduled = scheduled_tick_utc.astimezone(timezone.utc)
    start = window_start.astimezone(timezone.utc)
    elapsed = (scheduled - start).total_seconds()
    if elapsed < 0 or elapsed % POLL_SECONDS:
        raise ValueError("RTH sweep tick must use the handoff-window minute grid")
    ordered = tuple(sorted(active_symbols))
    if any(not symbol or symbol != symbol.strip().upper() for symbol in ordered):
        raise ValueError("active universe symbols must be canonical and non-empty")
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    if not ordered:
        return RthUniverseSweep((), 0, digest, scheduled, cycle_ticks, 0, 0, 0, 0)
    shard_count = min(cycle_ticks, len(ordered))
    shard_size = math.ceil(len(ordered) / shard_count)
    if shard_size > max_shard_size:
        raise ValueError(
            f"RTH full-universe sweep shard {shard_size} exceeds safety bound "
            f"{max_shard_size}"
        )
    slot = int(elapsed // POLL_SECONDS)
    shard_index = slot % shard_count
    shard_start = shard_index * shard_size
    symbols = ordered[
        shard_start : min(len(ordered), shard_start + shard_size)
    ]
    return RthUniverseSweep(
        symbols=symbols,
        universe_symbols=len(ordered),
        universe_sha256=digest,
        scheduled_tick_utc=scheduled,
        cycle_ticks=cycle_ticks,
        shard_index=shard_index,
        shard_count=shard_count,
        shard_size=shard_size,
        shard_start=shard_start,
    )


def _valid_bar(bar: Bar, *, positive_volume: bool) -> bool:
    numeric = (bar.open, bar.high, bar.low, bar.close)
    return (
        bar.ts.tzinfo is not None
        and bar.ts.utcoffset() is not None
        and all(math.isfinite(value) and value > 0 for value in numeric)
        and bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        and (bar.volume > 0 if positive_volume else bar.volume >= 0)
    )


def _prior_close(daily_bars: Sequence[Bar], session: date) -> float | None:
    eligible = [
        bar
        for bar in daily_bars
        if bar.ts.tzinfo is not None
        and bar.ts.utcoffset() is not None
        and bar.ts.astimezone(ET).date() < session
        and math.isfinite(bar.close)
        and bar.close > 0
    ]
    return max(eligible, key=lambda bar: bar.ts).close if eligible else None


def _base_evaluation(
    symbol: str,
    session: date,
    outcome: str,
    reason: str,
) -> RthEvaluation:
    return RthEvaluation(symbol=symbol, session=session, outcome=outcome, reason=reason)


def evaluate_rth_momentum(
    symbol: str,
    session: date,
    rth_bars: Sequence[Bar],
    daily_bars: Sequence[Bar],
    *,
    session_open: datetime,
    session_close: datetime,
    now: datetime,
) -> RthEvaluation:
    for name, value in (
        ("session_open", session_open),
        ("session_close", session_close),
        ("now", now),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
    current = now.astimezone(timezone.utc)
    open_utc = session_open.astimezone(timezone.utc)
    close_utc = session_close.astimezone(timezone.utc)
    if not open_utc < close_utc or not open_utc <= current <= close_utc:
        raise ValueError("RTH evaluation time must fall inside the exchange session")
    if any(
        bar.ts.tzinfo is None or bar.ts.utcoffset() is None for bar in rth_bars
    ):
        return _base_evaluation(
            symbol,
            session,
            OUTCOME_INVALID_DATA,
            "RTH history contains a naive timestamp",
        )
    timestamps = [bar.ts for bar in rth_bars]
    if timestamps != sorted(timestamps):
        return _base_evaluation(
            symbol,
            session,
            OUTCOME_INVALID_DATA,
            "RTH history is out of timestamp order",
        )
    baseline = _prior_close(daily_bars, session)
    if baseline is None:
        return _base_evaluation(
            symbol, session, OUTCOME_NO_PRIOR_CLOSE,
            "no valid daily bar before the evaluated session",
        )
    completed = sorted(
        (
            bar for bar in rth_bars
            if open_utc <= bar.ts < close_utc and bar_close_ts(bar) <= current
        ),
        key=lambda bar: bar.ts,
    )
    if len({bar.ts for bar in completed}) != len(completed):
        return _base_evaluation(
            symbol, session, OUTCOME_INVALID_DATA,
            "duplicate completed RTH bar timestamp",
        )
    if not completed:
        return _base_evaluation(
            symbol, session, OUTCOME_NO_COMPLETED_RTH_BAR,
            "no completed five-minute RTH bar",
        )
    if any(not _valid_bar(bar, positive_volume=False) for bar in completed):
        return _base_evaluation(
            symbol, session, OUTCOME_INVALID_DATA,
            "completed RTH history contains invalid OHLCV",
        )
    latest = completed[-1]
    age = (current - bar_close_ts(latest)).total_seconds()
    if age < 0:
        return _base_evaluation(
            symbol, session, OUTCOME_INVALID_DATA,
            "latest completed RTH bar is in the future",
        )
    if age > MAX_DATA_AGE_SECONDS:
        return RthEvaluation(
            symbol, session, OUTCOME_STALE,
            f"latest completed RTH bar is stale ({age:.0f}s)",
            prior_close=baseline, bar=latest, data_age_seconds=age,
        )
    recent = completed[-PERSISTENCE_BARS:]
    if any(not _valid_bar(bar, positive_volume=True) for bar in recent):
        return RthEvaluation(
            symbol, session, OUTCOME_INVALID_DATA,
            "persistence window contains invalid OHLCV or zero volume",
            prior_close=baseline, bar=latest, data_age_seconds=age,
        )
    if len(recent) == PERSISTENCE_BARS and any(
        right.ts - left.ts != timedelta(minutes=5)
        for left, right in zip(recent, recent[1:])
    ):
        return RthEvaluation(
            symbol, session, OUTCOME_BAR_GAP,
            "completed persistence bars are not contiguous five-minute bars",
            prior_close=baseline, bar=latest, data_age_seconds=age,
        )
    move = (latest.close / baseline - 1.0) * 100.0
    direction = "up" if move > 0 else "down" if move < 0 else None
    cumulative_volume = sum(bar.volume for bar in completed)
    cumulative_notional = sum(bar.close * bar.volume for bar in completed)
    common = {
        "prior_close": baseline,
        "bar": latest,
        "cumulative_volume": cumulative_volume,
        "cumulative_notional": cumulative_notional,
        "move_pct": move,
        "direction": direction,
        "data_age_seconds": age,
    }
    if abs(move) < MOVE_THRESHOLD_PCT:
        return RthEvaluation(
            symbol, session, OUTCOME_BELOW_MOVE,
            f"absolute move {abs(move):.2f}% is below {MOVE_THRESHOLD_PCT:.2f}%",
            **common,
        )
    moves = [(bar.close / baseline - 1.0) * 100.0 for bar in recent]
    persistent = (
        len(recent) == PERSISTENCE_BARS
        and all(abs(value) >= MOVE_THRESHOLD_PCT for value in moves)
        and all((value > 0) == (move > 0) for value in moves)
    )
    if not persistent:
        return RthEvaluation(
            symbol, session, OUTCOME_AWAITING_PERSISTENCE,
            f"requires {PERSISTENCE_BARS} consecutive completed bars beyond threshold",
            persistence_bars=sum(
                abs(value) >= MOVE_THRESHOLD_PCT and (value > 0) == (move > 0)
                for value in reversed(moves)
            ),
            **common,
        )
    if cumulative_notional < MIN_CUMULATIVE_NOTIONAL:
        return RthEvaluation(
            symbol, session, OUTCOME_BELOW_NOTIONAL,
            f"cumulative RTH notional {cumulative_notional:.0f} is below "
            f"{MIN_CUMULATIVE_NOTIONAL:.0f}",
            persistence_bars=PERSISTENCE_BARS,
            **common,
        )
    return RthEvaluation(
        symbol, session, OUTCOME_CANDIDATE,
        "completed-bar move, persistence, freshness, and notional passed",
        persistence_bars=PERSISTENCE_BARS,
        **common,
    )


def plan_rth_tick_schedule(
    conn: sqlite3.Connection,
    *,
    session: date,
    window_start: datetime,
    actual_start: datetime,
    interval_seconds: int = POLL_SECONDS,
) -> RthTickSchedule:
    ensure_rth_schema(conn)
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    elapsed = (actual_start - window_start).total_seconds()
    if elapsed < 0:
        raise ValueError("actual_start must not precede RTH handoff window")
    slot = math.floor(elapsed / interval_seconds)
    scheduled = window_start + timedelta(seconds=slot * interval_seconds)
    row = conn.execute(
        """
        SELECT scheduled_tick_utc FROM rth_momentum_ticks
        WHERE session=? ORDER BY tick_id DESC LIMIT 1
        """,
        (session.isoformat(),),
    ).fetchone()
    if row is None:
        missed = slot
    else:
        previous = datetime.fromisoformat(row[0]).astimezone(timezone.utc)
        missed = max(
            0,
            round((scheduled - previous).total_seconds() / interval_seconds) - 1,
        )
    return RthTickSchedule(
        scheduled_tick_utc=scheduled,
        scheduled_lag_ms=max(0, round((actual_start - scheduled).total_seconds() * 1000)),
        missed_cycles=missed,
    )


def _record_tick(
    conn: sqlite3.Connection,
    selection: RthSelection,
    evaluations: Sequence[RthEvaluation],
    *,
    screen: MarketWideScreen,
    schedule: RthTickSchedule,
    session: date,
    window_start: datetime,
    session_close: datetime,
    tick_utc: datetime,
    completed_utc: datetime,
    run_id: str,
    run_mode: str,
    code_version: str | None,
    data_feed: str,
    intraday_symbols_fetched: int,
    daily_symbols_fetched: int,
    stage_latencies: tuple[int, int, int, int],
) -> tuple[int, int, bool]:
    ensure_rth_schema(conn)
    symbols = [row.symbol for row in selection.symbols]
    evaluated = [row.symbol for row in evaluations]
    invariant_ok = (
        len(symbols) == len(set(symbols))
        and len(evaluated) == len(set(evaluated))
        and set(symbols) == set(evaluated)
    )
    if not invariant_ok:
        raise ValueError("RTH momentum selection/evaluation conservation failed")
    screen_ms, selection_ms, fetch_ms, evaluation_ms = stage_latencies
    total_ms = sum(stage_latencies)
    selected_by_symbol = {row.symbol: row for row in selection.symbols}
    new_candidates = 0
    for evaluation in evaluations:
        bar = evaluation.bar
        if evaluation.outcome != OUTCOME_CANDIDATE or bar is None:
            continue
        selected = selected_by_symbol[evaluation.symbol]
        candidate_cursor = conn.execute(
            """
            INSERT OR IGNORE INTO rth_momentum_candidates
              (session,symbol,direction,momentum_version,first_detected_at,
               bar_open_ts_utc,prior_close,close,move_pct,cumulative_volume,
               cumulative_notional,sources_json,data_feed,market_data_provider,
               bar_timeframe,code_version,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.isoformat(), evaluation.symbol, evaluation.direction,
                RTH_MOMENTUM_VERSION, tick_utc.isoformat(), bar.ts.isoformat(),
                evaluation.prior_close, bar.close, evaluation.move_pct,
                evaluation.cumulative_volume, evaluation.cumulative_notional,
                json.dumps(selected.sources, separators=(",", ":")), data_feed,
                screen.provider, BAR_TIMEFRAME, code_version, run_id,
            ),
        )
        if not candidate_cursor.rowcount:
            continue
        new_candidates += 1
        candidate_id = int(candidate_cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO rth_postmarket_handoffs
              (rth_candidate_id,postmarket_candidate_id,session,symbol,direction,
               state,transition_at_utc,reason,code_version,run_id)
            VALUES (?,NULL,?,?,?,?,?,?,?,?)
            """,
            (
                candidate_id, session.isoformat(), evaluation.symbol,
                evaluation.direction, HANDOFF_RTH_QUALIFIED, tick_utc.isoformat(),
                "qualified in the final-RTH fast lane; retained for postmarket handoff",
                code_version, run_id,
            ),
        )

    cursor = conn.execute(
        """
        INSERT INTO rth_momentum_ticks
          (session,scheduled_tick_utc,tick_utc,completed_utc,window_start_utc,
           session_close_utc,momentum_version,run_mode,run_id,code_version,
           data_feed,market_data_provider,universe_symbols,provider_screen_rows,
           provider_screen_unique_symbols,screen_error,scheduled_symbols,
           sweep_universe_sha256,sweep_cycle_ticks,sweep_shard_index,
           sweep_shard_count,sweep_shard_size,sweep_shard_symbols,
           sweep_overlap_symbols,selected_symbols,
           intraday_symbols_fetched,daily_symbols_fetched,evaluated_symbols,
           candidate_observations,new_candidates,invariant_ok,error_count,
           missed_cycles,scheduled_lag_ms,screen_latency_ms,selection_latency_ms,
           bar_fetch_latency_ms,evaluation_latency_ms,total_latency_ms,thresholds_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            session.isoformat(), schedule.scheduled_tick_utc.isoformat(),
            tick_utc.isoformat(), completed_utc.isoformat(), window_start.isoformat(),
            session_close.isoformat(), RTH_MOMENTUM_VERSION, run_mode, run_id,
            code_version, data_feed, screen.provider, selection.universe_symbols,
            selection.provider_screen_rows, selection.provider_screen_unique_symbols,
            selection.screen_error, selection.scheduled_symbols,
            selection.sweep.universe_sha256 if selection.sweep else None,
            selection.sweep.cycle_ticks if selection.sweep else None,
            selection.sweep.shard_index if selection.sweep else None,
            selection.sweep.shard_count if selection.sweep else None,
            selection.sweep.shard_size if selection.sweep else None,
            len(selection.sweep.symbols) if selection.sweep else 0,
            selection.sweep_overlap_symbols,
            len(symbols), intraday_symbols_fetched,
            daily_symbols_fetched, len(evaluations),
            sum(row.outcome == OUTCOME_CANDIDATE for row in evaluations),
            new_candidates, 1,
            int(selection.screen_error is not None)
            + sum(row.outcome == OUTCOME_FETCH_ERROR for row in evaluations),
            schedule.missed_cycles, schedule.scheduled_lag_ms, screen_ms,
            selection_ms, fetch_ms, evaluation_ms, total_ms,
            json.dumps(rth_thresholds(), separators=(",", ":"), sort_keys=True),
        ),
    )
    tick_id = int(cursor.lastrowid)
    for evaluation in evaluations:
        selected = selected_by_symbol[evaluation.symbol]
        bar = evaluation.bar
        conn.execute(
            """
            INSERT INTO rth_momentum_observations
              (tick_id,session,symbol,sources_json,ranks_json,screen_evidence_json,
               outcome,reason,prior_close,bar_open_ts_utc,open,high,low,close,
               volume,cumulative_volume,cumulative_notional,move_pct,direction,
               persistence_bars,data_age_seconds,data_feed,market_data_provider,
               bar_timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tick_id, session.isoformat(), evaluation.symbol,
                json.dumps(selected.sources, separators=(",", ":")),
                json.dumps(selected.ranks, separators=(",", ":")),
                json.dumps(selected.screen_evidence, separators=(",", ":"), sort_keys=True),
                evaluation.outcome, evaluation.reason, evaluation.prior_close,
                bar.ts.isoformat() if bar else None, bar.open if bar else None,
                bar.high if bar else None, bar.low if bar else None,
                bar.close if bar else None, bar.volume if bar else None,
                evaluation.cumulative_volume, evaluation.cumulative_notional,
                evaluation.move_pct, evaluation.direction, evaluation.persistence_bars,
                evaluation.data_age_seconds, data_feed, screen.provider, BAR_TIMEFRAME,
            ),
        )
    conn.commit()
    return tick_id, new_candidates, invariant_ok


def run_rth_momentum_tick(
    conn: sqlite3.Connection,
    *,
    active_universe: set[str],
    scheduled_earnings: set[str],
    now: datetime,
    run_id: str,
    code_version: str | None,
    data_feed: str,
    screen_fetch: Callable[[int], MarketWideScreen],
    intraday_fetch: Callable[[list[str], date], dict[str, list[Bar]]],
    daily_fetch: Callable[[list[str]], dict[str, list[Bar]]],
    sweep_intraday_fetch: (
        Callable[[list[str], datetime, datetime], dict[str, list[Bar]]] | None
    ) = None,
    validation_now_fn: Callable[[], datetime] | None = None,
    clock: Callable[[], float] | None = None,
    top_n: int = SCREEN_TOP_N,
    sweep_cycle_ticks: int | None = None,
    run_mode: str = "rth-marketwide-handoff-shadow",
) -> tuple[RthTickResult, RthSelection, tuple[RthEvaluation, ...]]:
    window = rth_handoff_window(now)
    if window is None or not (window[1] <= now <= window[2]):
        raise ValueError("run_rth_momentum_tick requires the active final-RTH window")
    session, window_start, session_close = window
    session_open = CALENDAR.session_open(session).to_pydatetime().astimezone(timezone.utc)
    schedule = plan_rth_tick_schedule(
        conn, session=session, window_start=window_start, actual_start=now
    )
    universe_sweep = (
        plan_rth_universe_sweep(
            active_universe,
            scheduled_tick_utc=schedule.scheduled_tick_utc,
            window_start=window_start,
            cycle_ticks=sweep_cycle_ticks,
        )
        if sweep_cycle_ticks is not None
        else None
    )
    timer = clock or time.perf_counter
    started = timer()
    screen_error = None
    try:
        screen = screen_fetch(top_n)
        screen_done = timer()
        validate_marketwide_screen(
            screen,
            now=validation_now_fn() if validation_now_fn else now,
            data_feed=data_feed,
            top_n=top_n,
        )
        if active_universe and not screen.entries:
            raise ValueError(
                "market-wide screen returned no rows for a non-empty universe"
            )
    except Exception as exc:
        screen_done = timer()
        if universe_sweep is None:
            raise
        screen_error = f"{type(exc).__name__}: {exc}"[:1000]
        screen = MarketWideScreen(
            entries=(),
            requested_top_n=top_n,
            provider=MARKET_DATA_PROVIDER,
            feed=data_feed,
            endpoints=(),
            source_updates=(),
        )
    selection = select_rth_symbols(
        screen,
        active_universe,
        scheduled_earnings,
        universe_sweep,
        screen_error,
    )
    selection_done = timer()
    symbols = [row.symbol for row in selection.symbols]
    bounded_symbols = [
        row.symbol
        for row in selection.symbols
        if any(source != FULL_UNIVERSE_RTH_SWEEP_SOURCE for source in row.sources)
    ]
    bounded_symbol_set = set(bounded_symbols)
    sweep_only_symbols = [
        row.symbol
        for row in selection.symbols
        if FULL_UNIVERSE_RTH_SWEEP_SOURCE in row.sources
        and row.symbol not in bounded_symbol_set
    ]
    sweep_only_symbol_set = set(sweep_only_symbols)
    intraday = intraday_fetch(bounded_symbols, session)
    sweep_failures: dict[str, Exception] = {}
    if sweep_only_symbols:
        if sweep_intraday_fetch is None:
            raise ValueError(
                "RTH full-universe sweep requires a bounded-window bar fetch"
            )
        try:
            sweep_bars = sweep_intraday_fetch(
                sweep_only_symbols,
                max(session_open, window_start - timedelta(minutes=10)),
                now,
            )
        except Exception as exc:
            sweep_failures = {symbol: exc for symbol in sweep_only_symbols}
        else:
            duplicate = set(intraday) & set(sweep_bars)
            if duplicate:
                raise ValueError(
                    "bounded and RTH sweep bar responses overlap unexpectedly: "
                    f"{sorted(duplicate)[:5]}"
                )
            intraday.update(sweep_bars)
    daily = daily_fetch(symbols)
    fetch_done = timer()
    evaluations = []
    for symbol in symbols:
        if symbol in sweep_failures:
            failure = sweep_failures[symbol]
            evaluations.append(
                _base_evaluation(
                    symbol,
                    session,
                    OUTCOME_FETCH_ERROR,
                    (
                        f"RTH sweep fetch failed: {type(failure).__name__}: "
                        f"{failure}"
                    )[:1000],
                )
            )
            continue
        if not intraday.get(symbol):
            reason = (
                "provider returned no bars for RTH full-universe sweep window"
                if symbol in sweep_only_symbol_set
                else "provider returned no intraday bars for selected symbol"
            )
            evaluations.append(
                _base_evaluation(
                    symbol, session, OUTCOME_NO_INTRADAY_BARS_RETURNED,
                    reason,
                )
            )
            continue
        if not daily.get(symbol):
            evaluations.append(
                _base_evaluation(
                    symbol, session, OUTCOME_NO_DAILY_BASELINE_RETURNED,
                    "provider returned no daily bars for selected symbol",
                )
            )
            continue
        try:
            snapshot = partition_intraday_bars(intraday[symbol])
            evaluations.append(
                evaluate_rth_momentum(
                    symbol,
                    session,
                    snapshot.rth,
                    daily[symbol],
                    session_open=session_open,
                    session_close=session_close,
                    now=now,
                )
            )
        except Exception as exc:
            evaluations.append(
                _base_evaluation(
                    symbol, session, OUTCOME_FETCH_ERROR,
                    f"{type(exc).__name__}: {exc}"[:1000],
                )
            )
    evaluation_done = timer()
    stage_latencies = tuple(
        round(value * 1000)
        for value in (
            screen_done - started,
            selection_done - screen_done,
            fetch_done - selection_done,
            evaluation_done - fetch_done,
        )
    )
    completed = now + timedelta(milliseconds=sum(stage_latencies))
    tick_id, new_candidates, invariant_ok = _record_tick(
        conn,
        selection,
        evaluations,
        screen=screen,
        schedule=schedule,
        session=session,
        window_start=window_start,
        session_close=session_close,
        tick_utc=now,
        completed_utc=completed,
        run_id=run_id,
        run_mode=run_mode,
        code_version=code_version,
        data_feed=data_feed,
        intraday_symbols_fetched=sum(symbol in intraday for symbol in symbols),
        daily_symbols_fetched=sum(symbol in daily for symbol in symbols),
        stage_latencies=stage_latencies,
    )
    result = RthTickResult(
        tick_id=tick_id,
        session=session.isoformat(),
        selected_symbols=len(symbols),
        sweep_shard_index=(universe_sweep.shard_index if universe_sweep else None),
        sweep_shard_count=(universe_sweep.shard_count if universe_sweep else None),
        sweep_shard_symbols=(len(universe_sweep.symbols) if universe_sweep else 0),
        sweep_overlap_symbols=selection.sweep_overlap_symbols,
        intraday_symbols_fetched=sum(symbol in intraday for symbol in symbols),
        daily_symbols_fetched=sum(symbol in daily for symbol in symbols),
        evaluated_symbols=len(evaluations),
        candidate_observations=sum(
            row.outcome == OUTCOME_CANDIDATE for row in evaluations
        ),
        new_candidates=new_candidates,
        invariant_ok=invariant_ok,
        error_count=(
            int(selection.screen_error is not None)
            + sum(row.outcome == OUTCOME_FETCH_ERROR for row in evaluations)
        ),
        missed_cycles=schedule.missed_cycles,
        scheduled_lag_ms=schedule.scheduled_lag_ms,
        screen_latency_ms=stage_latencies[0],
        selection_latency_ms=stage_latencies[1],
        bar_fetch_latency_ms=stage_latencies[2],
        evaluation_latency_ms=stage_latencies[3],
        latency_ms=sum(stage_latencies),
    )
    return result, selection, tuple(evaluations)


def reconcile_rth_postmarket_handoffs(
    conn: sqlite3.Connection,
    *,
    session: date,
    now: datetime,
    postmarket_end: datetime,
    code_version: str | None,
    run_id: str,
) -> HandoffResult:
    """Append a link when PM qualifies, or a terminal state after its window."""
    ensure_rth_schema(conn)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if postmarket_end.tzinfo is None or postmarket_end.utcoffset() is None:
        raise ValueError("postmarket_end must be timezone-aware")
    candidates = conn.execute(
        """
        SELECT candidate_id,symbol,direction FROM rth_momentum_candidates
        WHERE session=? ORDER BY candidate_id
        """,
        (session.isoformat(),),
    ).fetchall()
    linked = terminal = 0
    for candidate_id, symbol, direction in candidates:
        postmarket = conn.execute(
            """
            SELECT candidate_id FROM postmarket_discovery_candidates
            WHERE session=? AND symbol=? AND direction=?
            ORDER BY candidate_id LIMIT 1
            """,
            (session.isoformat(), symbol, direction),
        ).fetchone()
        if postmarket is not None:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO rth_postmarket_handoffs
                  (rth_candidate_id,postmarket_candidate_id,session,symbol,direction,
                   state,transition_at_utc,reason,code_version,run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id, int(postmarket[0]), session.isoformat(), symbol,
                    direction, HANDOFF_POSTMARKET_QUALIFIED, now.isoformat(),
                    "same-direction postmarket candidate linked", code_version, run_id,
                ),
            )
            linked += int(bool(cursor.rowcount))
        elif now > postmarket_end:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO rth_postmarket_handoffs
                  (rth_candidate_id,postmarket_candidate_id,session,symbol,direction,
                   state,transition_at_utc,reason,code_version,run_id)
                VALUES (?,NULL,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id, session.isoformat(), symbol, direction,
                    HANDOFF_POSTMARKET_NOT_QUALIFIED, now.isoformat(),
                    "postmarket evidence window ended without same-direction qualification",
                    code_version, run_id,
                ),
            )
            terminal += int(bool(cursor.rowcount))
    conn.commit()
    return HandoffResult(
        session=session.isoformat(),
        rth_candidates=len(candidates),
        postmarket_links_written=linked,
        terminal_not_qualified_written=terminal,
    )


def latest_rth_handoff_summary(conn: sqlite3.Connection) -> dict | None:
    ensure_rth_schema(conn)
    row = conn.execute(
        """
        SELECT session,tick_id,tick_utc,selected_symbols,evaluated_symbols,
               candidate_observations,new_candidates,invariant_ok,error_count,
               total_latency_ms
        FROM rth_momentum_ticks ORDER BY tick_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    states = {
        state: count
        for state, count in conn.execute(
            """
            SELECT state,COUNT(*) FROM rth_postmarket_handoffs
            WHERE session=? GROUP BY state ORDER BY state
            """,
            (row[0],),
        ).fetchall()
    }
    return {
        "session": row[0],
        "tick_id": int(row[1]),
        "tick_utc": row[2],
        "selected_symbols": int(row[3]),
        "evaluated_symbols": int(row[4]),
        "candidate_observations": int(row[5]),
        "new_candidates": int(row[6]),
        "invariant_ok": bool(row[7]),
        "error_count": int(row[8]),
        "latency_ms": int(row[9]),
        "handoff_states": states,
    }
