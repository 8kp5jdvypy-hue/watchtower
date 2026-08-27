"""Pure market-wide postmarket discovery selection and append-only evidence.

The provider screen is Stage 1 only. A returned symbol is never a candidate
until the existing completed-bar evaluator independently confirms the move,
volume/notional, persistence, freshness, and RTH-close baseline.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from tradebot.marketdata import MarketScreenEntry, MarketWideScreen
from tradebot.postmarket import (
    BAR_TIMEFRAME,
    MARKET_DATA_PROVIDER,
    OUTCOME_CANDIDATE,
    OUTCOME_FETCH_ERROR,
    ReactionEvaluation,
    connect as connect_postmarket,
    thresholds,
)


DISCOVERY_VERSION = 1
DISCOVERY_SCOPE = "alpaca_top_movers_and_actives"

DISCOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_discovery_ticks (
    tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    tick_utc TEXT NOT NULL,
    completed_utc TEXT NOT NULL,
    run_id TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    discovery_version INTEGER NOT NULL,
    code_version TEXT,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    discovery_scope TEXT NOT NULL,
    endpoints_json TEXT NOT NULL,
    source_updates_json TEXT NOT NULL,
    requested_top_n INTEGER NOT NULL,
    universe_symbols INTEGER NOT NULL,
    screen_rows INTEGER NOT NULL,
    screen_unique_symbols INTEGER NOT NULL,
    excluded_symbols INTEGER NOT NULL,
    discovered_symbols INTEGER NOT NULL,
    not_returned_symbols INTEGER NOT NULL,
    fetched_symbols INTEGER NOT NULL,
    evaluated_symbols INTEGER NOT NULL,
    candidate_observations INTEGER NOT NULL,
    new_candidates INTEGER NOT NULL,
    invariant_ok INTEGER NOT NULL,
    thresholds_json TEXT NOT NULL,
    latency_ms INTEGER,
    error_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postmarket_discovery_ticks_session
    ON postmarket_discovery_ticks(session, tick_utc);

CREATE TABLE IF NOT EXISTS postmarket_discovery_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    ranks_json TEXT NOT NULL,
    screen_evidence_json TEXT NOT NULL,
    screen_move_pct REAL,
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
    UNIQUE(tick_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_discovery_observations_symbol
    ON postmarket_discovery_observations(symbol, event_date, seq);
CREATE INDEX IF NOT EXISTS idx_postmarket_discovery_observations_outcome
    ON postmarket_discovery_observations(outcome, event_date);

CREATE TABLE IF NOT EXISTS postmarket_discovery_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_date TEXT NOT NULL,
    direction TEXT NOT NULL,
    discovery_version INTEGER NOT NULL,
    first_detected_at TEXT NOT NULL,
    bar_open_ts_utc TEXT NOT NULL,
    rth_close REAL NOT NULL,
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
    UNIQUE(session, symbol, event_date, direction, discovery_version)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_discovery_candidates_session
    ON postmarket_discovery_candidates(session, first_detected_at);

CREATE TRIGGER IF NOT EXISTS postmarket_discovery_ticks_no_update
BEFORE UPDATE ON postmarket_discovery_ticks BEGIN
    SELECT RAISE(ABORT, 'postmarket_discovery_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_discovery_ticks_no_delete
BEFORE DELETE ON postmarket_discovery_ticks BEGIN
    SELECT RAISE(ABORT, 'postmarket_discovery_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_discovery_observations_no_update
BEFORE UPDATE ON postmarket_discovery_observations BEGIN
    SELECT RAISE(ABORT, 'postmarket_discovery_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_discovery_observations_no_delete
BEFORE DELETE ON postmarket_discovery_observations BEGIN
    SELECT RAISE(ABORT, 'postmarket_discovery_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_discovery_candidates_no_update
BEFORE UPDATE ON postmarket_discovery_candidates BEGIN
    SELECT RAISE(ABORT, 'postmarket_discovery_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_discovery_candidates_no_delete
BEFORE DELETE ON postmarket_discovery_candidates BEGIN
    SELECT RAISE(ABORT, 'postmarket_discovery_candidates is append-only');
END;
"""


@dataclass(frozen=True)
class DiscoveredSymbol:
    symbol: str
    sources: tuple[str, ...]
    ranks: tuple[tuple[str, int], ...]
    screen_evidence: tuple[dict, ...]
    screen_move_pct: float | None


@dataclass(frozen=True)
class DiscoverySelection:
    symbols: tuple[DiscoveredSymbol, ...]
    universe_symbols: int
    screen_rows: int
    screen_unique_symbols: int
    excluded_symbols: int
    not_returned_symbols: int


def select_discovery_symbols(
    screen: MarketWideScreen,
    active_symbols: set[str],
    scheduled_earnings: set[str] | None = None,
) -> DiscoverySelection:
    """Filter provider results to Perch's active universe and deduplicate sources."""
    earnings = scheduled_earnings or set()
    grouped: dict[str, list[MarketScreenEntry]] = {}
    for entry in screen.entries:
        symbol = entry.symbol.strip().upper()
        if symbol:
            grouped.setdefault(symbol, []).append(entry)
    selected = []
    for symbol in sorted(grouped.keys() & active_symbols):
        entries = grouped[symbol]
        sources = {entry.source for entry in entries}
        if symbol in earnings:
            sources.add("scheduled_earnings")
        ranks = tuple(sorted((entry.source, entry.rank) for entry in entries))
        evidence = tuple(
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
        )
        moves = [entry.move_pct for entry in entries if entry.move_pct is not None]
        selected.append(
            DiscoveredSymbol(
                symbol=symbol,
                sources=tuple(sorted(sources)),
                ranks=ranks,
                screen_evidence=evidence,
                screen_move_pct=max(moves, key=abs) if moves else None,
            )
        )
    returned = set(grouped)
    discovered = {row.symbol for row in selected}
    return DiscoverySelection(
        symbols=tuple(selected),
        universe_symbols=len(active_symbols),
        screen_rows=len(screen.entries),
        screen_unique_symbols=len(returned),
        excluded_symbols=len(returned - active_symbols),
        not_returned_symbols=len(active_symbols - discovered),
    )


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect_postmarket(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(DISCOVERY_SCHEMA)
    return conn


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def record_discovery_tick(
    conn: sqlite3.Connection,
    selection: DiscoverySelection,
    evaluations: Sequence[ReactionEvaluation],
    *,
    screen: MarketWideScreen,
    fetched_symbols: int,
    session: date,
    tick_utc: datetime,
    completed_utc: datetime,
    run_id: str,
    run_mode: str,
    code_version: str | None,
    data_feed: str,
    latency_ms: int | None,
) -> tuple[int, int]:
    """Atomically persist one discovery tick or no part of it."""
    if tick_utc.tzinfo is None or tick_utc.utcoffset() is None:
        raise ValueError("tick_utc must be timezone-aware")
    if completed_utc.tzinfo is None or completed_utc.utcoffset() is None:
        raise ValueError("completed_utc must be timezone-aware")
    if completed_utc < tick_utc:
        raise ValueError("completed_utc must not precede tick_utc")
    if not data_feed.strip():
        raise ValueError("data_feed must not be empty")
    if screen.provider != MARKET_DATA_PROVIDER:
        raise ValueError(
            f"unexpected market-wide screen provider: {screen.provider!r}"
        )
    if screen.feed != data_feed:
        raise ValueError(
            f"market-wide screen/bar feed mismatch: {screen.feed!r} vs {data_feed!r}"
        )
    selected_by_symbol = {row.symbol: row for row in selection.symbols}
    if len(selected_by_symbol) != len(selection.symbols):
        raise ValueError("discovery selection contains duplicate symbols")
    evaluation_by_symbol = {row.symbol: row for row in evaluations}
    if len(evaluation_by_symbol) != len(evaluations):
        raise ValueError("evaluations contain duplicate symbols")
    if set(evaluation_by_symbol) != set(selected_by_symbol):
        raise ValueError("every discovered symbol must have exactly one evaluation")
    if not 0 <= fetched_symbols <= len(selection.symbols):
        raise ValueError("fetched_symbols must be within the discovery selection")
    if any(row.event_date != session for row in evaluations):
        raise ValueError("every evaluation must match the tick session")
    for evaluation in evaluations:
        if evaluation.outcome == OUTCOME_CANDIDATE and (
            evaluation.bar is None
            or evaluation.rth_close is None
            or evaluation.cumulative_volume is None
            or evaluation.cumulative_notional is None
            or evaluation.move_pct is None
            or evaluation.direction not in {"up", "down"}
        ):
            raise ValueError(
                f"candidate evaluation is incomplete for {evaluation.symbol}"
            )
    source_updates = {source: updated.isoformat() for source, updated in screen.source_updates}
    invariant_ok = (
        selection.universe_symbols
        == len(selection.symbols) + selection.not_returned_symbols
        and selection.screen_unique_symbols
        == len(selection.symbols) + selection.excluded_symbols
        and len(evaluations) == len(selection.symbols)
    )
    candidate_observations = sum(row.outcome == OUTCOME_CANDIDATE for row in evaluations)
    error_count = sum(row.outcome == OUTCOME_FETCH_ERROR for row in evaluations)
    with conn:
        new_candidates = 0
        for evaluation in evaluations:
            if evaluation.outcome != OUTCOME_CANDIDATE or evaluation.bar is None:
                continue
            discovery = selected_by_symbol[evaluation.symbol]
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO postmarket_discovery_candidates
                    (session,symbol,event_date,direction,discovery_version,
                     first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
                     cumulative_volume,cumulative_notional,sources_json,data_feed,
                     market_data_provider,bar_timeframe,code_version,run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session.isoformat(), evaluation.symbol, session.isoformat(),
                    evaluation.direction, DISCOVERY_VERSION, completed_utc.isoformat(),
                    evaluation.bar.ts.isoformat(), evaluation.rth_close,
                    evaluation.bar.close, evaluation.move_pct,
                    evaluation.cumulative_volume, evaluation.cumulative_notional,
                    _json(discovery.sources), data_feed, MARKET_DATA_PROVIDER,
                    BAR_TIMEFRAME, code_version, run_id,
                ),
            )
            new_candidates += int(cursor.rowcount > 0)
        cursor = conn.execute(
            """
            INSERT INTO postmarket_discovery_ticks
                (session,tick_utc,completed_utc,run_id,run_mode,discovery_version,
                 code_version,data_feed,market_data_provider,bar_timeframe,
                 discovery_scope,endpoints_json,source_updates_json,requested_top_n,
                 universe_symbols,screen_rows,screen_unique_symbols,excluded_symbols,
                 discovered_symbols,not_returned_symbols,fetched_symbols,
                 evaluated_symbols,candidate_observations,new_candidates,invariant_ok,
                 thresholds_json,latency_ms,error_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.isoformat(), tick_utc.isoformat(), completed_utc.isoformat(),
                run_id, run_mode, DISCOVERY_VERSION, code_version, data_feed,
                screen.provider, BAR_TIMEFRAME, DISCOVERY_SCOPE,
                _json(screen.endpoints), _json(source_updates), screen.requested_top_n,
                selection.universe_symbols, selection.screen_rows,
                selection.screen_unique_symbols, selection.excluded_symbols,
                len(selection.symbols), selection.not_returned_symbols,
                fetched_symbols, len(evaluations), candidate_observations,
                new_candidates, int(invariant_ok), _json(thresholds()),
                latency_ms, error_count,
            ),
        )
        tick_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO postmarket_discovery_observations
                (tick_id,symbol,sources_json,ranks_json,screen_evidence_json,
                 screen_move_pct,outcome,reason,event_date,bar_open_ts_utc,
                 rth_close,open,high,low,close,volume,
                 cumulative_volume,cumulative_notional,move_pct,direction,
                 persistence_bars,data_age_seconds,data_feed,market_data_provider,
                 bar_timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    tick_id, evaluation.symbol, _json(selected_by_symbol[evaluation.symbol].sources),
                    _json(selected_by_symbol[evaluation.symbol].ranks),
                    _json(selected_by_symbol[evaluation.symbol].screen_evidence),
                    selected_by_symbol[evaluation.symbol].screen_move_pct,
                    evaluation.outcome, evaluation.reason, session.isoformat(),
                    evaluation.bar.ts.isoformat() if evaluation.bar else None,
                    evaluation.rth_close,
                    evaluation.bar.open if evaluation.bar else None,
                    evaluation.bar.high if evaluation.bar else None,
                    evaluation.bar.low if evaluation.bar else None,
                    evaluation.bar.close if evaluation.bar else None,
                    evaluation.bar.volume if evaluation.bar else None,
                    evaluation.cumulative_volume, evaluation.cumulative_notional,
                    evaluation.move_pct, evaluation.direction,
                    evaluation.persistence_bars, evaluation.data_age_seconds,
                    data_feed, MARKET_DATA_PROVIDER, BAR_TIMEFRAME,
                )
                for evaluation in evaluations
            ],
        )
    return tick_id, new_candidates
