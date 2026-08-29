"""Independent after-the-fact full-universe recall census.

The live discovery screen is a bounded provider top-N view. This module takes
an active-universe snapshot after the postmarket window, evaluates every symbol
from completed bars, and compares qualifying symbol/direction pairs with the
append-only Stage-1 discovery evidence. It cannot send alerts or alter candidates.
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

from tradebot.detectors import Bar, bar_close_ts
from tradebot.marketdata import partition_intraday_bars
from tradebot.postmarket import (
    BAR_TIMEFRAME,
    MARKET_DATA_PROVIDER,
    OUTCOME_CANDIDATE,
    evaluate_postmarket_reaction,
    thresholds,
)


CENSUS_VERSION = 1
CENSUS_CHUNK_SIZE = 500
MAX_CENSUS_ATTEMPTS = 3
FINALIZATION_GRACE = timedelta(minutes=5)
EXPECTED_FEED = "sip"
EXPECTED_RECALL_FLOOR = 0.95
PROVIDER_COMPARISON_STATUS = "NOT_CONFIGURED"
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")


CENSUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_recall_census_runs (
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
    stage1_seen_symbols INTEGER NOT NULL,
    stage1_candidate_pairs INTEGER NOT NULL,
    eligible_pairs INTEGER NOT NULL,
    true_positive_pairs INTEGER NOT NULL,
    false_negative_pairs INTEGER NOT NULL,
    false_positive_pairs INTEGER NOT NULL,
    recall REAL,
    average_detection_delay_seconds REAL,
    max_detection_delay_seconds REAL,
    status TEXT NOT NULL,
    invariant_ok INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    thresholds_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(session,census_version,attempt),
    CHECK (status IN ('success','degraded'))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_recall_census_runs_session
    ON postmarket_recall_census_runs(session,attempt);

CREATE TABLE IF NOT EXISTS postmarket_recall_census_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    census_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    data_status TEXT NOT NULL,
    final_outcome TEXT NOT NULL,
    final_reason TEXT NOT NULL,
    qualifying_directions_json TEXT NOT NULL,
    first_qualified_at_json TEXT NOT NULL,
    stage1_seen INTEGER NOT NULL,
    stage1_directions_json TEXT NOT NULL,
    false_negative_directions_json TEXT NOT NULL,
    false_positive_directions_json TEXT NOT NULL,
    miss_reasons_json TEXT NOT NULL,
    detection_delays_json TEXT NOT NULL,
    rth_close REAL,
    postmarket_bars INTEGER NOT NULL,
    first_postmarket_bar_utc TEXT,
    final_postmarket_bar_utc TEXT,
    UNIQUE(census_id,symbol)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_recall_census_events_symbol
    ON postmarket_recall_census_events(symbol,census_id);

CREATE TRIGGER IF NOT EXISTS postmarket_recall_census_runs_no_update
BEFORE UPDATE ON postmarket_recall_census_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_census_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_recall_census_runs_no_delete
BEFORE DELETE ON postmarket_recall_census_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_census_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_recall_census_events_no_update
BEFORE UPDATE ON postmarket_recall_census_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_census_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_recall_census_events_no_delete
BEFORE DELETE ON postmarket_recall_census_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_census_events is append-only');
END;
"""


@dataclass(frozen=True)
class CensusSymbolResult:
    symbol: str
    data_status: str
    final_outcome: str
    final_reason: str
    qualifying_directions: tuple[str, ...]
    first_qualified_at: dict[str, str]
    stage1_seen: bool
    stage1_directions: tuple[str, ...]
    false_negative_directions: tuple[str, ...]
    false_positive_directions: tuple[str, ...]
    miss_reasons: dict[str, str]
    detection_delays: dict[str, float]
    rth_close: float | None
    postmarket_bars: int
    first_postmarket_bar_utc: str | None
    final_postmarket_bar_utc: str | None


@dataclass(frozen=True)
class RecallCensusResult:
    census_id: int
    session: str
    attempt: int
    status: str
    universe_symbols: int
    requested_chunks: int
    fetched_symbols: int
    evaluated_symbols: int
    unavailable_symbols: int
    stage1_seen_symbols: int
    stage1_candidate_pairs: int
    eligible_pairs: int
    true_positive_pairs: int
    false_negative_pairs: int
    false_positive_pairs: int
    recall: float | None
    average_detection_delay_seconds: float | None
    max_detection_delay_seconds: float | None
    invariant_ok: bool
    error_count: int
    latency_ms: int


@dataclass(frozen=True)
class RecallCensusReport:
    report_version: int
    census_id: int
    session: str
    attempt: int
    code_version: str | None
    operational_complete: bool
    evidence_eligible: bool
    metrics: dict
    false_negatives: tuple[dict, ...]
    false_positives: tuple[dict, ...]
    unavailable: tuple[dict, ...]
    issue_codes: tuple[str, ...]


def ensure_census_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CENSUS_SCHEMA)


def census_window(session: date) -> tuple[datetime, datetime]:
    if not CALENDAR.is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    session_close = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    postmarket_end = datetime.combine(
        session, datetime.min.time().replace(hour=20), tzinfo=ET
    ).astimezone(timezone.utc)
    return session_close, postmarket_end


def next_due_census_session(
    conn: sqlite3.Connection, *, now: datetime,
) -> tuple[date, datetime, datetime] | None:
    """Choose at most one newest finalized session needing a bounded attempt."""
    current = _aware_utc(now, "now")
    ensure_census_schema(conn)
    sessions = [
        date.fromisoformat(row[0])
        for row in conn.execute(
            "SELECT DISTINCT session FROM postmarket_discovery_ticks ORDER BY session DESC"
        ).fetchall()
    ]
    for session in sessions:
        session_close, postmarket_end = census_window(session)
        if current < postmarket_end + FINALIZATION_GRACE:
            continue
        latest = conn.execute(
            """
            SELECT status,attempt FROM postmarket_recall_census_runs
            WHERE session=? AND census_version=? ORDER BY attempt DESC LIMIT 1
            """,
            (session.isoformat(), CENSUS_VERSION),
        ).fetchone()
        if latest is None or (
            latest[0] == "degraded" and int(latest[1]) < MAX_CENSUS_ATTEMPTS
        ):
            return session, session_close, postmarket_end
    return None


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _universe_digest(symbols: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def _stage1_evidence(
    conn: sqlite3.Connection, session: date,
) -> tuple[set[str], dict[str, dict[str, datetime]]]:
    seen = {
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT o.symbol
            FROM postmarket_discovery_observations o
            JOIN postmarket_discovery_ticks t ON t.tick_id=o.tick_id
            WHERE t.session=?
            """,
            (session.isoformat(),),
        ).fetchall()
    }
    candidates: dict[str, dict[str, datetime]] = {}
    for symbol, direction, first_detected in conn.execute(
        """
        SELECT symbol,direction,MIN(first_detected_at)
        FROM postmarket_discovery_candidates
        WHERE session=? GROUP BY symbol,direction
        """,
        (session.isoformat(),),
    ).fetchall():
        candidates.setdefault(symbol, {})[direction] = _aware_utc(
            datetime.fromisoformat(first_detected), "first_detected_at"
        )
    return seen, candidates


def _no_data_result(
    symbol: str,
    *,
    data_status: str,
    reason: str,
    stage1_seen: bool,
    stage1_candidates: dict[str, datetime],
) -> CensusSymbolResult:
    directions = tuple(sorted(stage1_candidates))
    return CensusSymbolResult(
        symbol=symbol,
        data_status=data_status,
        final_outcome=data_status,
        final_reason=reason,
        qualifying_directions=(),
        first_qualified_at={},
        stage1_seen=stage1_seen,
        stage1_directions=directions,
        false_negative_directions=(),
        false_positive_directions=(),
        miss_reasons={},
        detection_delays={},
        rth_close=None,
        postmarket_bars=0,
        first_postmarket_bar_utc=None,
        final_postmarket_bar_utc=None,
    )


def evaluate_census_symbol(
    symbol: str,
    session: date,
    bars: Sequence[Bar],
    *,
    session_close: datetime,
    postmarket_end: datetime,
    stage1_seen: bool,
    stage1_candidates: dict[str, datetime],
) -> CensusSymbolResult:
    """Replay every knowable completed postmarket instant for one symbol."""
    close_utc = _aware_utc(session_close, "session_close")
    end_utc = _aware_utc(postmarket_end, "postmarket_end")
    snapshot = partition_intraday_bars(bars)
    postmarket = tuple(
        bar for bar in snapshot.postmarket if bar_close_ts(bar) <= end_utc
    )
    instants = sorted({bar_close_ts(bar) for bar in postmarket})
    if end_utc not in instants:
        instants.append(end_utc)
    evaluations = [
        evaluate_postmarket_reaction(
            symbol,
            session,
            snapshot.rth,
            postmarket,
            session_close=close_utc,
            now=instant,
        )
        for instant in instants
    ]
    first_by_direction: dict[str, datetime] = {}
    for instant, evaluation in zip(instants, evaluations):
        if evaluation.outcome == OUTCOME_CANDIDATE and evaluation.direction:
            first_by_direction.setdefault(evaluation.direction, instant)
    qualifying = tuple(sorted(first_by_direction))
    stage1_directions = tuple(sorted(stage1_candidates))
    false_negatives = tuple(sorted(set(qualifying) - set(stage1_directions)))
    false_positives = tuple(sorted(set(stage1_directions) - set(qualifying)))
    miss_reasons = {
        direction: (
            "RETURNED_NOT_CONFIRMED"
            if stage1_seen
            else "NOT_RETURNED_BY_BOUNDED_SCREEN"
        )
        for direction in false_negatives
    }
    delays = {
        direction: (
            stage1_candidates[direction] - first_by_direction[direction]
        ).total_seconds()
        for direction in set(qualifying) & set(stage1_directions)
    }
    final = evaluations[-1]
    rth_close = snapshot.rth[-1].close if snapshot.rth else None
    return CensusSymbolResult(
        symbol=symbol,
        data_status="AVAILABLE",
        final_outcome=final.outcome,
        final_reason=final.reason,
        qualifying_directions=qualifying,
        first_qualified_at={
            direction: instant.isoformat()
            for direction, instant in sorted(first_by_direction.items())
        },
        stage1_seen=stage1_seen,
        stage1_directions=stage1_directions,
        false_negative_directions=false_negatives,
        false_positive_directions=false_positives,
        miss_reasons=miss_reasons,
        detection_delays=delays,
        rth_close=rth_close,
        postmarket_bars=len(postmarket),
        first_postmarket_bar_utc=(postmarket[0].ts.isoformat() if postmarket else None),
        final_postmarket_bar_utc=(postmarket[-1].ts.isoformat() if postmarket else None),
    )


def _next_attempt(conn: sqlite3.Connection, session: date) -> int:
    return int(
        conn.execute(
            """
            SELECT COALESCE(MAX(attempt),0)+1
            FROM postmarket_recall_census_runs
            WHERE session=? AND census_version=?
            """,
            (session.isoformat(), CENSUS_VERSION),
        ).fetchone()[0]
    )


def run_recall_census(
    conn: sqlite3.Connection,
    *,
    universe_symbols: Sequence[str],
    session: date,
    session_close: datetime,
    postmarket_end: datetime,
    now: datetime,
    run_id: str,
    code_version: str | None,
    data_feed: str,
    bars_fetch: Callable[[list[str], datetime, datetime], dict[str, Sequence[Bar]]],
    chunk_size: int = CENSUS_CHUNK_SIZE,
) -> tuple[RecallCensusResult, tuple[CensusSymbolResult, ...]]:
    current = _aware_utc(now, "now")
    close_utc = _aware_utc(session_close, "session_close")
    end_utc = _aware_utc(postmarket_end, "postmarket_end")
    if current < end_utc + FINALIZATION_GRACE:
        raise ValueError("recall census requires a finalized postmarket window")
    if data_feed != EXPECTED_FEED:
        raise ValueError("recall census requires SIP bars")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    symbols = tuple(sorted(set(universe_symbols)))
    if not symbols or any(not symbol or symbol != symbol.strip().upper() for symbol in symbols):
        raise ValueError("universe_symbols must contain canonical symbols")
    ensure_census_schema(conn)
    attempt = _next_attempt(conn, session)
    if attempt > MAX_CENSUS_ATTEMPTS:
        raise ValueError("maximum recall census attempts reached")
    started = time.perf_counter()
    stage1_seen, stage1_candidates = _stage1_evidence(conn, session)
    rows: list[CensusSymbolResult] = []
    chunk_errors: list[dict] = []
    evaluation_errors: list[dict] = []
    fetched_symbols = 0
    request_start = close_utc - timedelta(minutes=5)
    for index in range(0, len(symbols), chunk_size):
        chunk = list(symbols[index : index + chunk_size])
        try:
            response = bars_fetch(chunk, request_start, end_utc)
        except Exception as exc:
            chunk_errors.append(
                {
                    "chunk": index // chunk_size + 1,
                    "symbols": len(chunk),
                    "error_type": type(exc).__name__,
                }
            )
            response = {}
            failed_chunk = True
        else:
            failed_chunk = False
        for symbol in chunk:
            candidates = stage1_candidates.get(symbol, {})
            if failed_chunk:
                rows.append(
                    _no_data_result(
                        symbol,
                        data_status="FETCH_ERROR",
                        reason="bulk census fetch failed",
                        stage1_seen=symbol in stage1_seen,
                        stage1_candidates=candidates,
                    )
                )
                continue
            if symbol not in response:
                rows.append(
                    _no_data_result(
                        symbol,
                        data_status="NO_DATA_RETURNED",
                        reason="symbol absent from completed-bar response",
                        stage1_seen=symbol in stage1_seen,
                        stage1_candidates=candidates,
                    )
                )
                continue
            fetched_symbols += 1
            try:
                rows.append(
                    evaluate_census_symbol(
                        symbol,
                        session,
                        response[symbol],
                        session_close=close_utc,
                        postmarket_end=end_utc,
                        stage1_seen=symbol in stage1_seen,
                        stage1_candidates=candidates,
                    )
                )
            except Exception as exc:
                evaluation_errors.append(
                    {
                        "symbol": symbol,
                        "error_type": type(exc).__name__,
                    }
                )
                rows.append(
                    _no_data_result(
                        symbol,
                        data_status="EVALUATION_ERROR",
                        reason=f"{type(exc).__name__}: {exc}"[:1000],
                        stage1_seen=symbol in stage1_seen,
                        stage1_candidates=candidates,
                    )
                )

    eligible_pairs = sum(len(row.qualifying_directions) for row in rows)
    false_negative_pairs = sum(len(row.false_negative_directions) for row in rows)
    true_positive_pairs = eligible_pairs - false_negative_pairs
    false_positive_pairs = sum(len(row.false_positive_directions) for row in rows)
    delays = [delay for row in rows for delay in row.detection_delays.values()]
    unavailable = sum(row.data_status != "AVAILABLE" for row in rows)
    evaluated = len(rows) - unavailable
    requested_chunks = math.ceil(len(symbols) / chunk_size)
    invariant_ok = (
        len(rows) == len(symbols)
        and fetched_symbols == evaluated
        and true_positive_pairs + false_negative_pairs == eligible_pairs
    )
    error_count = len(chunk_errors) + len(evaluation_errors)
    status = "success" if not error_count and invariant_ok else "degraded"
    completed = current + timedelta(seconds=time.perf_counter() - started)
    recall = true_positive_pairs / eligible_pairs if eligible_pairs else None
    detail = {
        "chunk_errors": chunk_errors,
        "evaluation_errors": evaluation_errors,
        "request_start_utc": request_start.isoformat(),
        "request_end_utc": end_utc.isoformat(),
    }
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_recall_census_runs
                (session,census_version,attempt,run_id,started_at_utc,
                 completed_at_utc,code_version,data_feed,market_data_provider,
                 bar_timeframe,provider_comparison_status,
                 universe_snapshot_sha256,universe_symbols,requested_chunks,
                 fetched_symbols,evaluated_symbols,unavailable_symbols,
                 stage1_seen_symbols,stage1_candidate_pairs,eligible_pairs,
                 true_positive_pairs,false_negative_pairs,false_positive_pairs,
                 recall,average_detection_delay_seconds,max_detection_delay_seconds,
                 status,invariant_ok,error_count,thresholds_json,detail_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.isoformat(),
                CENSUS_VERSION,
                attempt,
                run_id,
                current.isoformat(),
                completed.isoformat(),
                code_version,
                data_feed,
                MARKET_DATA_PROVIDER,
                BAR_TIMEFRAME,
                PROVIDER_COMPARISON_STATUS,
                _universe_digest(symbols),
                len(symbols),
                requested_chunks,
                fetched_symbols,
                evaluated,
                unavailable,
                len(stage1_seen),
                sum(len(value) for value in stage1_candidates.values()),
                eligible_pairs,
                true_positive_pairs,
                false_negative_pairs,
                false_positive_pairs,
                recall,
                sum(delays) / len(delays) if delays else None,
                max(delays) if delays else None,
                status,
                int(invariant_ok),
                error_count,
                _json(thresholds()),
                _json(detail),
            ),
        )
        census_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO postmarket_recall_census_events
                (census_id,symbol,data_status,final_outcome,final_reason,
                 qualifying_directions_json,first_qualified_at_json,stage1_seen,
                 stage1_directions_json,false_negative_directions_json,
                 false_positive_directions_json,miss_reasons_json,
                 detection_delays_json,rth_close,postmarket_bars,
                 first_postmarket_bar_utc,final_postmarket_bar_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    census_id,
                    row.symbol,
                    row.data_status,
                    row.final_outcome,
                    row.final_reason,
                    _json(row.qualifying_directions),
                    _json(row.first_qualified_at),
                    int(row.stage1_seen),
                    _json(row.stage1_directions),
                    _json(row.false_negative_directions),
                    _json(row.false_positive_directions),
                    _json(row.miss_reasons),
                    _json(row.detection_delays),
                    row.rth_close,
                    row.postmarket_bars,
                    row.first_postmarket_bar_utc,
                    row.final_postmarket_bar_utc,
                )
                for row in rows
            ],
        )
    result = RecallCensusResult(
        census_id=census_id,
        session=session.isoformat(),
        attempt=attempt,
        status=status,
        universe_symbols=len(symbols),
        requested_chunks=requested_chunks,
        fetched_symbols=fetched_symbols,
        evaluated_symbols=evaluated,
        unavailable_symbols=unavailable,
        stage1_seen_symbols=len(stage1_seen),
        stage1_candidate_pairs=sum(len(value) for value in stage1_candidates.values()),
        eligible_pairs=eligible_pairs,
        true_positive_pairs=true_positive_pairs,
        false_negative_pairs=false_negative_pairs,
        false_positive_pairs=false_positive_pairs,
        recall=recall,
        average_detection_delay_seconds=(sum(delays) / len(delays) if delays else None),
        max_detection_delay_seconds=max(delays) if delays else None,
        invariant_ok=invariant_ok,
        error_count=error_count,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
    return result, tuple(rows)


def build_census_report(
    conn: sqlite3.Connection, census_id: int,
) -> RecallCensusReport:
    ensure_census_schema(conn)
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT * FROM postmarket_recall_census_runs WHERE census_id=?",
            (census_id,),
        ).fetchone()
        if run is None:
            raise ValueError("census run does not exist")
        events = conn.execute(
            "SELECT * FROM postmarket_recall_census_events WHERE census_id=? ORDER BY symbol",
            (census_id,),
        ).fetchall()
    finally:
        conn.row_factory = original
    false_negatives = tuple(
        {
            "symbol": row["symbol"],
            "directions": json.loads(row["false_negative_directions_json"]),
            "reasons": json.loads(row["miss_reasons_json"]),
            "stage1_seen": bool(row["stage1_seen"]),
            "final_outcome": row["final_outcome"],
        }
        for row in events
        if json.loads(row["false_negative_directions_json"])
    )
    false_positives = tuple(
        {
            "symbol": row["symbol"],
            "directions": json.loads(row["false_positive_directions_json"]),
            "final_outcome": row["final_outcome"],
        }
        for row in events
        if json.loads(row["false_positive_directions_json"])
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
    if run["provider_comparison_status"] != "AVAILABLE":
        issues.append("PROVIDER_COMPARISON_NOT_CONFIGURED")
    if run["recall"] is not None and run["recall"] < EXPECTED_RECALL_FLOOR:
        issues.append("RECALL_BELOW_95_PERCENT")
    if run["code_version"] in {None, "", "unknown"}:
        issues.append("CODE_VERSION_MISSING")
    operational_complete = run["status"] == "success" and bool(run["invariant_ok"])
    evidence_eligible = operational_complete and not issues
    metrics = {
        key: run[key]
        for key in (
            "universe_symbols",
            "requested_chunks",
            "fetched_symbols",
            "evaluated_symbols",
            "unavailable_symbols",
            "stage1_seen_symbols",
            "stage1_candidate_pairs",
            "eligible_pairs",
            "true_positive_pairs",
            "false_negative_pairs",
            "false_positive_pairs",
            "recall",
            "average_detection_delay_seconds",
            "max_detection_delay_seconds",
            "provider_comparison_status",
        )
    }
    return RecallCensusReport(
        report_version=int(run["attempt"]),
        census_id=int(run["census_id"]),
        session=run["session"],
        attempt=int(run["attempt"]),
        code_version=run["code_version"],
        operational_complete=operational_complete,
        evidence_eligible=evidence_eligible,
        metrics=metrics,
        false_negatives=false_negatives,
        false_positives=false_positives,
        unavailable=unavailable,
        issue_codes=tuple(issues),
    )


def write_report_atomic(path: Path | str, report: RecallCensusReport) -> bool:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return False
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(report), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, destination)
        except FileExistsError:
            tmp_path.unlink()
            return False
        tmp_path.unlink()
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_census_report(
    conn: sqlite3.Connection, output_dir: Path | str, census_id: int,
) -> tuple[RecallCensusReport, bool]:
    report = build_census_report(conn, census_id)
    destination = Path(output_dir) / (
        f"postmarket_recall_census_{report.session}_v{report.report_version}.json"
    )
    return report, write_report_atomic(destination, report)


def latest_census_report_summary(output_dir: Path | str) -> dict | None:
    latest: tuple[tuple[str, int], dict] | None = None
    for path in Path(output_dir).glob("postmarket_recall_census_*_v*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = (str(payload["session"]), int(payload["report_version"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid postmarket recall census report: {path}") from exc
        if latest is None or key > latest[0]:
            latest = (key, payload)
    if latest is None:
        return None
    payload = latest[1]
    return {
        "session": payload["session"],
        "report_version": payload["report_version"],
        "operational_complete": payload["operational_complete"],
        "evidence_eligible": payload["evidence_eligible"],
        "recall": payload["metrics"]["recall"],
        "false_negative_pairs": payload["metrics"]["false_negative_pairs"],
        "unavailable_symbols": payload["metrics"]["unavailable_symbols"],
        "issue_codes": payload["issue_codes"],
    }
