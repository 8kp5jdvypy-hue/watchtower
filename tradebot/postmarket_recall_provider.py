"""Append-only full-universe second-provider recall proof.

The same frozen universe and completed postmarket window are replayed from
Alpaca SIP and a configured independent historical source.  The proof is
published separately from the original census: immutable evidence is extended,
never rewritten after an independent source becomes available.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from tradebot.detectors import Bar
from tradebot.postmarket_recall_census import (
    CENSUS_VERSION,
    CensusSymbolResult,
    census_window,
    ensure_census_schema,
    evaluate_census_symbol,
)
from tradebot.vendors.historical_reference import (
    HistoricalReferenceSnapshot,
    HistoricalReferenceSource,
    require_recall_proof_capabilities,
    source as historical_reference_source,
)


PROVIDER_PROOF_VERSION = 1
MAX_PROVIDER_PROOF_ATTEMPTS = 3
CHUNK_SIZE = 500
MAX_CLOSE_DIFFERENCE_BPS = 50.0
MIN_COMPARABLE_COVERAGE = 0.99
MIN_BAR_OVERLAP_COVERAGE = 0.95
MIN_ELIGIBLE_PAIR_AGREEMENT = 0.95
MIN_INDEPENDENT_RECALL = 0.95


PROVIDER_PROOF_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_recall_provider_runs (
    comparison_id INTEGER PRIMARY KEY AUTOINCREMENT,
    census_id INTEGER NOT NULL,
    session TEXT NOT NULL,
    provider_proof_version INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    code_version TEXT,
    universe_snapshot_sha256 TEXT NOT NULL,
    universe_symbols INTEGER NOT NULL,
    primary_provider TEXT NOT NULL,
    primary_feed TEXT NOT NULL,
    independent_provider TEXT NOT NULL,
    independent_feed TEXT NOT NULL,
    independent_dataset TEXT NOT NULL,
    object_key TEXT NOT NULL,
    object_etag TEXT,
    object_last_modified_utc TEXT,
    object_bytes INTEGER,
    selected_rows_sha256 TEXT NOT NULL,
    source_rows_read INTEGER NOT NULL,
    selected_source_rows INTEGER NOT NULL,
    primary_evaluated_symbols INTEGER NOT NULL,
    independent_evaluated_symbols INTEGER NOT NULL,
    comparable_symbols INTEGER NOT NULL,
    comparable_coverage REAL,
    primary_eligible_pairs INTEGER NOT NULL,
    independent_eligible_pairs INTEGER NOT NULL,
    agreed_eligible_pairs INTEGER NOT NULL,
    eligible_pair_agreement REAL,
    independent_true_positive_pairs INTEGER NOT NULL,
    independent_false_negative_pairs INTEGER NOT NULL,
    independent_false_positive_pairs INTEGER NOT NULL,
    independent_recall REAL,
    primary_comparison_bars INTEGER NOT NULL,
    independent_comparison_bars INTEGER NOT NULL,
    compared_bars INTEGER NOT NULL,
    bar_overlap_coverage REAL,
    price_disagreement_bars INTEGER NOT NULL,
    max_abs_close_difference_bps REAL,
    status TEXT NOT NULL,
    invariant_ok INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(census_id,provider_proof_version,attempt),
    CHECK (status IN ('success','degraded'))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_recall_provider_runs_session
    ON postmarket_recall_provider_runs(session,attempt);
CREATE TABLE IF NOT EXISTS postmarket_recall_provider_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    primary_data_status TEXT NOT NULL,
    independent_data_status TEXT NOT NULL,
    primary_directions_json TEXT NOT NULL,
    independent_directions_json TEXT NOT NULL,
    independent_only_directions_json TEXT NOT NULL,
    primary_only_directions_json TEXT NOT NULL,
    stage1_directions_json TEXT NOT NULL,
    independent_false_negative_directions_json TEXT NOT NULL,
    independent_false_positive_directions_json TEXT NOT NULL,
    primary_comparison_bars INTEGER NOT NULL,
    independent_comparison_bars INTEGER NOT NULL,
    compared_bars INTEGER NOT NULL,
    price_disagreement_bars INTEGER NOT NULL,
    max_abs_close_difference_bps REAL,
    UNIQUE(comparison_id,symbol)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_recall_provider_events_symbol
    ON postmarket_recall_provider_events(symbol,comparison_id);
CREATE TRIGGER IF NOT EXISTS postmarket_recall_provider_runs_no_update
BEFORE UPDATE ON postmarket_recall_provider_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_provider_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_recall_provider_runs_no_delete
BEFORE DELETE ON postmarket_recall_provider_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_provider_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_recall_provider_events_no_update
BEFORE UPDATE ON postmarket_recall_provider_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_provider_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_recall_provider_events_no_delete
BEFORE DELETE ON postmarket_recall_provider_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_recall_provider_events is append-only');
END;
"""


@dataclass(frozen=True)
class ProviderSymbolResult:
    symbol: str
    primary_data_status: str
    independent_data_status: str
    primary_directions: tuple[str, ...]
    independent_directions: tuple[str, ...]
    independent_only_directions: tuple[str, ...]
    primary_only_directions: tuple[str, ...]
    stage1_directions: tuple[str, ...]
    independent_false_negative_directions: tuple[str, ...]
    independent_false_positive_directions: tuple[str, ...]
    primary_comparison_bars: int
    independent_comparison_bars: int
    compared_bars: int
    price_disagreement_bars: int
    max_abs_close_difference_bps: float | None


@dataclass(frozen=True)
class ProviderProofResult:
    comparison_id: int
    census_id: int
    session: str
    attempt: int
    status: str
    universe_symbols: int
    primary_evaluated_symbols: int
    independent_evaluated_symbols: int
    comparable_symbols: int
    comparable_coverage: float | None
    primary_eligible_pairs: int
    independent_eligible_pairs: int
    agreed_eligible_pairs: int
    eligible_pair_agreement: float | None
    independent_true_positive_pairs: int
    independent_false_negative_pairs: int
    independent_false_positive_pairs: int
    independent_recall: float | None
    primary_comparison_bars: int
    independent_comparison_bars: int
    compared_bars: int
    bar_overlap_coverage: float | None
    price_disagreement_bars: int
    max_abs_close_difference_bps: float | None
    invariant_ok: bool
    error_count: int
    latency_ms: int


@dataclass(frozen=True)
class ProviderProofReport:
    report_version: int
    comparison_id: int
    census_id: int
    session: str
    attempt: int
    code_version: str | None
    operational_complete: bool
    evidence_eligible: bool
    source: dict
    metrics: dict
    independent_misses: tuple[dict, ...]
    provider_disagreements: tuple[dict, ...]
    issue_codes: tuple[str, ...]


def ensure_provider_proof_schema(conn: sqlite3.Connection) -> None:
    ensure_census_schema(conn)
    conn.executescript(PROVIDER_PROOF_SCHEMA)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def next_due_provider_proof(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    independent_source: HistoricalReferenceSource | None = None,
) -> tuple[int, date] | None:
    """Return the newest primary census whose independent proof is due."""
    current = _aware_utc(now, "now")
    reference = independent_source or historical_reference_source()
    require_recall_proof_capabilities(reference)
    ensure_provider_proof_schema(conn)
    rows = conn.execute(
        """
        SELECT census_id,session,status FROM postmarket_recall_census_runs
        WHERE census_version=? ORDER BY session DESC,attempt DESC
        """,
        (CENSUS_VERSION,),
    ).fetchall()
    seen_sessions: set[str] = set()
    for census_id, raw_session, census_status in rows:
        if raw_session in seen_sessions:
            continue
        seen_sessions.add(raw_session)
        if census_status != "success":
            continue
        session = date.fromisoformat(raw_session)
        if current < reference.expected_available_at(session):
            continue
        latest = conn.execute(
            """
            SELECT status,attempt FROM postmarket_recall_provider_runs
            WHERE census_id=? AND provider_proof_version=?
            ORDER BY attempt DESC LIMIT 1
            """,
            (census_id, PROVIDER_PROOF_VERSION),
        ).fetchone()
        if latest is None or (
            latest[0] == "degraded" and int(latest[1]) < MAX_PROVIDER_PROOF_ATTEMPTS
        ):
            return int(census_id), session
    return None


def _stage1_candidates(
    conn: sqlite3.Connection, session: date,
) -> dict[str, dict[str, datetime]]:
    candidates: dict[str, dict[str, datetime]] = {}
    for symbol, direction, detected in conn.execute(
        """
        SELECT symbol,direction,MIN(first_detected_at)
        FROM postmarket_discovery_candidates
        WHERE session=? GROUP BY symbol,direction
        """,
        (session.isoformat(),),
    ).fetchall():
        candidates.setdefault(symbol, {})[direction] = _aware_utc(
            datetime.fromisoformat(detected), "first_detected_at"
        )
    return candidates


def _evaluation(
    symbol: str,
    session: date,
    bars: Sequence[Bar] | None,
    *,
    session_close: datetime,
    postmarket_end: datetime,
    stage1: dict[str, datetime],
) -> CensusSymbolResult | None:
    if bars is None:
        return None
    return evaluate_census_symbol(
        symbol, session, bars,
        session_close=session_close,
        postmarket_end=postmarket_end,
        stage1_seen=bool(stage1),
        stage1_candidates=stage1,
    )


def _price_comparison(
    primary: Sequence[Bar] | None,
    independent: Sequence[Bar] | None,
    *,
    start: datetime,
    end: datetime,
) -> tuple[int, int, int, int, float | None]:
    def closes_in_window(bars: Sequence[Bar], provider: str) -> dict[datetime, float]:
        closes = {}
        for bar in bars:
            timestamp = _aware_utc(bar.ts, f"{provider} bar timestamp")
            if start <= timestamp < end:
                closes[timestamp] = bar.close
        return closes

    primary_closes = closes_in_window(primary or (), "primary")
    independent_closes = closes_in_window(independent or (), "independent")
    differences = []
    for ts in sorted(set(primary_closes) & set(independent_closes)):
        primary_close = primary_closes[ts]
        independent_close = independent_closes[ts]
        if min(primary_close, independent_close) <= 0:
            raise ValueError("provider comparison closes must be positive")
        differences.append(abs((independent_close / primary_close - 1) * 10_000))
    return (
        len(primary_closes),
        len(independent_closes),
        len(differences),
        sum(value > MAX_CLOSE_DIFFERENCE_BPS for value in differences),
        max(differences) if differences else None,
    )


def run_provider_proof(
    conn: sqlite3.Connection,
    *,
    census_id: int,
    session: date,
    now: datetime,
    run_id: str,
    code_version: str | None,
    primary_fetch: Callable[[list[str], datetime, datetime], Mapping[str, Sequence[Bar]]],
    independent_fetch: Callable[
        [date, Sequence[str], datetime, datetime], HistoricalReferenceSnapshot
    ],
    independent_source: HistoricalReferenceSource | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> tuple[ProviderProofResult, tuple[ProviderSymbolResult, ...]]:
    current = _aware_utc(now, "now")
    reference = independent_source or historical_reference_source()
    require_recall_proof_capabilities(reference)
    if current < reference.expected_available_at(session):
        raise ValueError("provider proof cannot precede documented source availability")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    ensure_provider_proof_schema(conn)
    census = conn.execute(
        """
        SELECT session,universe_snapshot_sha256,status FROM postmarket_recall_census_runs
        WHERE census_id=? AND census_version=?
        """,
        (census_id, CENSUS_VERSION),
    ).fetchone()
    if census is None or census[0] != session.isoformat():
        raise ValueError("census identity does not match provider proof")
    if census[2] != "success":
        raise ValueError("provider proof requires a successful primary census")
    symbols = tuple(row[0] for row in conn.execute(
        "SELECT symbol FROM postmarket_recall_census_events WHERE census_id=? ORDER BY symbol",
        (census_id,),
    ).fetchall())
    if not symbols:
        raise ValueError("primary census universe is empty")
    attempt = int(conn.execute(
        """
        SELECT COALESCE(MAX(attempt),0)+1 FROM postmarket_recall_provider_runs
        WHERE census_id=? AND provider_proof_version=?
        """,
        (census_id, PROVIDER_PROOF_VERSION),
    ).fetchone()[0])
    if attempt > MAX_PROVIDER_PROOF_ATTEMPTS:
        raise ValueError("maximum provider proof attempts reached")
    session_close, postmarket_end = census_window(session)
    request_start = session_close - timedelta(minutes=5)
    started = time.perf_counter()
    errors: list[dict] = []
    independent_failed = False
    try:
        snapshot = independent_fetch(session, symbols, request_start, postmarket_end)
        if snapshot.session != session:
            raise ValueError("flat-file session did not match request")
        if snapshot.object_key != reference.object_key(session):
            raise ValueError("historical source object key did not match request")
        if not snapshot.object_etag or snapshot.object_bytes is None:
            raise ValueError("flat-file object provenance was incomplete")
        if snapshot.object_last_modified_utc is None:
            raise ValueError("flat-file last-modified time was missing")
        object_modified = _aware_utc(
            datetime.fromisoformat(snapshot.object_last_modified_utc),
            "flat-file last-modified time",
        )
        if not postmarket_end <= object_modified <= current:
            raise ValueError("flat-file last-modified time was not causally valid")
        if len(snapshot.selected_rows_sha256) != 64:
            raise ValueError("flat-file selected-row digest was invalid")
    except Exception as exc:
        independent_failed = True
        errors.append({"source": "independent_flat_file", "error_type": type(exc).__name__})
        snapshot = HistoricalReferenceSnapshot(
            session, reference.object_key(session), None, None, None,
            hashlib.sha256(b"").hexdigest(), 0, 0, 0, {},
        )
    stage1 = _stage1_candidates(conn, session)
    rows: list[ProviderSymbolResult] = []
    primary_evaluated = independent_evaluated = comparable = 0
    for index in range(0, len(symbols), chunk_size):
        chunk = list(symbols[index:index + chunk_size])
        try:
            primary_response = dict(primary_fetch(chunk, request_start, postmarket_end))
        except Exception as exc:
            errors.append({
                "chunk": index // chunk_size + 1,
                "error_type": type(exc).__name__,
            })
            primary_response = {}
            primary_failed = True
        else:
            primary_failed = False
        for symbol in chunk:
            primary_bars = None if primary_failed else primary_response.get(symbol)
            independent_bars = snapshot.bars_by_symbol.get(symbol)
            try:
                primary_result = _evaluation(
                    symbol, session, primary_bars, session_close=session_close,
                    postmarket_end=postmarket_end, stage1=stage1.get(symbol, {}),
                )
                independent_result = _evaluation(
                    symbol, session, independent_bars, session_close=session_close,
                    postmarket_end=postmarket_end, stage1=stage1.get(symbol, {}),
                )
                (
                    primary_bars_count,
                    independent_bars_count,
                    compared_bars,
                    disagreement_bars,
                    max_difference,
                ) = _price_comparison(
                    primary_bars, independent_bars,
                    start=request_start, end=postmarket_end,
                )
            except Exception as exc:
                errors.append({"symbol": symbol, "error_type": type(exc).__name__})
                primary_result = independent_result = None
                primary_bars_count = independent_bars_count = 0
                compared_bars = disagreement_bars = 0
                max_difference = None
            primary_directions = (
                primary_result.qualifying_directions if primary_result else ()
            )
            independent_directions = (
                independent_result.qualifying_directions if independent_result else ()
            )
            stage1_directions = tuple(sorted(stage1.get(symbol, {})))
            primary_evaluated += int(primary_result is not None)
            independent_evaluated += int(independent_result is not None)
            comparable += int(primary_result is not None and independent_result is not None)
            rows.append(ProviderSymbolResult(
                symbol,
                "FETCH_ERROR" if primary_failed else (
                    "AVAILABLE" if primary_result else "NO_DATA_RETURNED"
                ),
                "FETCH_ERROR" if independent_failed else (
                    "AVAILABLE" if independent_result else "NO_DATA_RETURNED"
                ),
                tuple(primary_directions), tuple(independent_directions),
                tuple(sorted(set(independent_directions) - set(primary_directions))),
                tuple(sorted(set(primary_directions) - set(independent_directions))),
                stage1_directions,
                tuple(sorted(set(independent_directions) - set(stage1_directions))),
                tuple(sorted(set(stage1_directions) - set(independent_directions))),
                primary_bars_count, independent_bars_count, compared_bars,
                disagreement_bars, max_difference,
            ))
    primary_pairs = {(row.symbol, value) for row in rows for value in row.primary_directions}
    independent_pairs = {
        (row.symbol, value) for row in rows for value in row.independent_directions
    }
    stage1_pairs = {(symbol, direction) for symbol, values in stage1.items() for direction in values}
    union_pairs = primary_pairs | independent_pairs
    agreed_pairs = primary_pairs & independent_pairs
    independent_tp = len(independent_pairs & stage1_pairs)
    independent_fn = len(independent_pairs - stage1_pairs)
    independent_fp = len(stage1_pairs - independent_pairs)
    primary_bars_count = sum(row.primary_comparison_bars for row in rows)
    independent_bars_count = sum(row.independent_comparison_bars for row in rows)
    compared_bars = sum(row.compared_bars for row in rows)
    disagreement_bars = sum(row.price_disagreement_bars for row in rows)
    differences = [
        row.max_abs_close_difference_bps for row in rows
        if row.max_abs_close_difference_bps is not None
    ]
    coverage = comparable / len(symbols)
    union_bar_count = primary_bars_count + independent_bars_count - compared_bars
    bar_overlap = compared_bars / union_bar_count if union_bar_count else None
    pair_agreement = len(agreed_pairs) / len(union_pairs) if union_pairs else 1.0
    independent_recall = independent_tp / len(independent_pairs) if independent_pairs else None
    invariant_ok = (
        len(rows) == len(symbols)
        and independent_tp + independent_fn == len(independent_pairs)
        and len(agreed_pairs) <= min(len(primary_pairs), len(independent_pairs))
    )
    status = "success" if invariant_ok and not errors else "degraded"
    completed = current + timedelta(seconds=time.perf_counter() - started)
    detail = {
        "errors": errors,
        "request_start_utc": request_start.isoformat(),
        "request_end_utc": postmarket_end.isoformat(),
        "comparison_rule": "exact_completed_5min_close_v1",
        "max_close_difference_bps": MAX_CLOSE_DIFFERENCE_BPS,
        "source_semantic": "next_day_bulk_sip_replay_not_live_signal_input",
    }
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_recall_provider_runs
                (census_id,session,provider_proof_version,attempt,run_id,
                 started_at_utc,completed_at_utc,code_version,
                 universe_snapshot_sha256,universe_symbols,primary_provider,
                 primary_feed,independent_provider,independent_feed,
                 independent_dataset,object_key,object_etag,
                 object_last_modified_utc,object_bytes,selected_rows_sha256,
                 source_rows_read,selected_source_rows,primary_evaluated_symbols,
                 independent_evaluated_symbols,comparable_symbols,
                 comparable_coverage,primary_eligible_pairs,
                 independent_eligible_pairs,agreed_eligible_pairs,
                 eligible_pair_agreement,independent_true_positive_pairs,
                 independent_false_negative_pairs,independent_false_positive_pairs,
                 independent_recall,primary_comparison_bars,
                 independent_comparison_bars,compared_bars,bar_overlap_coverage,
                 price_disagreement_bars,
                 max_abs_close_difference_bps,status,invariant_ok,error_count,detail_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                census_id, session.isoformat(), PROVIDER_PROOF_VERSION, attempt,
                run_id, current.isoformat(), completed.isoformat(), code_version,
                census[1], len(symbols), "alpaca", "sip", reference.provider,
                reference.feed, reference.dataset, snapshot.object_key,
                snapshot.object_etag, snapshot.object_last_modified_utc,
                snapshot.object_bytes, snapshot.selected_rows_sha256,
                snapshot.rows_read, snapshot.selected_rows, primary_evaluated,
                independent_evaluated, comparable, coverage, len(primary_pairs),
                len(independent_pairs), len(agreed_pairs), pair_agreement,
                independent_tp, independent_fn, independent_fp, independent_recall,
                primary_bars_count, independent_bars_count, compared_bars, bar_overlap,
                disagreement_bars, max(differences) if differences else None,
                status, int(invariant_ok), len(errors), _json(detail),
            ),
        )
        comparison_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO postmarket_recall_provider_events
                (comparison_id,symbol,primary_data_status,independent_data_status,
                 primary_directions_json,independent_directions_json,
                 independent_only_directions_json,primary_only_directions_json,
                 stage1_directions_json,independent_false_negative_directions_json,
                 independent_false_positive_directions_json,primary_comparison_bars,
                 independent_comparison_bars,compared_bars,
                 price_disagreement_bars,max_abs_close_difference_bps)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [(
                comparison_id, row.symbol, row.primary_data_status,
                row.independent_data_status, _json(row.primary_directions),
                _json(row.independent_directions),
                _json(row.independent_only_directions),
                _json(row.primary_only_directions), _json(row.stage1_directions),
                _json(row.independent_false_negative_directions),
                _json(row.independent_false_positive_directions),
                row.primary_comparison_bars, row.independent_comparison_bars,
                row.compared_bars,
                row.price_disagreement_bars, row.max_abs_close_difference_bps,
            ) for row in rows],
        )
    result = ProviderProofResult(
        comparison_id, census_id, session.isoformat(), attempt, status, len(symbols),
        primary_evaluated, independent_evaluated, comparable, coverage,
        len(primary_pairs), len(independent_pairs), len(agreed_pairs), pair_agreement,
        independent_tp, independent_fn, independent_fp, independent_recall,
        primary_bars_count, independent_bars_count, compared_bars, bar_overlap,
        disagreement_bars, max(differences) if differences else None,
        invariant_ok, len(errors), round((time.perf_counter() - started) * 1000),
    )
    return result, tuple(rows)


def build_provider_proof_report(
    conn: sqlite3.Connection, comparison_id: int,
) -> ProviderProofReport:
    ensure_provider_proof_schema(conn)
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT * FROM postmarket_recall_provider_runs WHERE comparison_id=?",
            (comparison_id,),
        ).fetchone()
        if run is None:
            raise ValueError("provider proof run does not exist")
        events = conn.execute(
            """SELECT * FROM postmarket_recall_provider_events
               WHERE comparison_id=? ORDER BY symbol""",
            (comparison_id,),
        ).fetchall()
    finally:
        conn.row_factory = original
    issues = []
    if run["status"] != "success" or not run["invariant_ok"]:
        issues.append("PROVIDER_PROOF_OPERATIONAL_FAILURE")
    if run["comparable_coverage"] is None or run["comparable_coverage"] < MIN_COMPARABLE_COVERAGE:
        issues.append("PROVIDER_SYMBOL_COVERAGE_BELOW_99_PERCENT")
    if run["bar_overlap_coverage"] is None:
        issues.append("PROVIDER_BAR_OVERLAP_UNDEFINED")
    elif run["bar_overlap_coverage"] < MIN_BAR_OVERLAP_COVERAGE:
        issues.append("PROVIDER_BAR_OVERLAP_BELOW_95_PERCENT")
    if run["eligible_pair_agreement"] is None or run["eligible_pair_agreement"] < MIN_ELIGIBLE_PAIR_AGREEMENT:
        issues.append("PROVIDER_ELIGIBLE_PAIR_AGREEMENT_BELOW_95_PERCENT")
    if run["independent_recall"] is None:
        issues.append("INDEPENDENT_RECALL_UNDEFINED")
    elif run["independent_recall"] < MIN_INDEPENDENT_RECALL:
        issues.append("INDEPENDENT_RECALL_BELOW_95_PERCENT")
    if run["price_disagreement_bars"]:
        issues.append("PROVIDER_PRICE_DISAGREEMENT")
    if run["code_version"] in {None, "", "unknown"}:
        issues.append("CODE_VERSION_MISSING")
    operational = run["status"] == "success" and bool(run["invariant_ok"])
    misses = tuple({
        "symbol": row["symbol"],
        "directions": json.loads(row["independent_false_negative_directions_json"]),
    } for row in events if json.loads(row["independent_false_negative_directions_json"]))
    disagreements = tuple({
        "symbol": row["symbol"],
        "independent_only": json.loads(row["independent_only_directions_json"]),
        "primary_only": json.loads(row["primary_only_directions_json"]),
        "price_disagreement_bars": row["price_disagreement_bars"],
        "max_abs_close_difference_bps": row["max_abs_close_difference_bps"],
    } for row in events if (
        json.loads(row["independent_only_directions_json"])
        or json.loads(row["primary_only_directions_json"])
        or row["price_disagreement_bars"]
    ))
    metric_names = (
        "universe_symbols", "primary_evaluated_symbols",
        "independent_evaluated_symbols", "comparable_symbols",
        "comparable_coverage", "primary_eligible_pairs",
        "independent_eligible_pairs", "agreed_eligible_pairs",
        "eligible_pair_agreement", "independent_true_positive_pairs",
        "independent_false_negative_pairs", "independent_false_positive_pairs",
        "independent_recall", "primary_comparison_bars",
        "independent_comparison_bars", "compared_bars", "bar_overlap_coverage",
        "price_disagreement_bars",
        "max_abs_close_difference_bps",
    )
    return ProviderProofReport(
        PROVIDER_PROOF_VERSION, int(run["comparison_id"]), int(run["census_id"]),
        run["session"], int(run["attempt"]), run["code_version"], operational,
        operational and not issues,
        {
            "primary_provider": run["primary_provider"],
            "primary_feed": run["primary_feed"],
            "independent_provider": run["independent_provider"],
            "independent_feed": run["independent_feed"],
            "independent_dataset": run["independent_dataset"],
            "object_key": run["object_key"],
            "object_etag": run["object_etag"],
            "object_last_modified_utc": run["object_last_modified_utc"],
            "selected_rows_sha256": run["selected_rows_sha256"],
        },
        {name: run[name] for name in metric_names}, misses, disagreements,
        tuple(issues),
    )


def write_provider_proof_report(
    conn: sqlite3.Connection,
    audit_dir: Path | str,
    comparison_id: int,
) -> tuple[ProviderProofReport, bool]:
    report = build_provider_proof_report(conn, comparison_id)
    destination = Path(audit_dir) / (
        f"postmarket_recall_provider_{report.session}_v{report.attempt}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return report, False
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(report), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, destination)
        except FileExistsError:
            tmp.unlink()
            return report, False
        tmp.unlink()
        return report, True
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def latest_provider_proof_summary(audit_dir: Path | str) -> dict | None:
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in Path(audit_dir).glob("postmarket_recall_provider_*_v*.json")
    ]
    if not payloads:
        return None
    payload = max(
        payloads,
        key=lambda value: (date.fromisoformat(value["session"]), int(value["attempt"])),
    )
    return {
        "session": payload["session"],
        "report_version": payload["report_version"],
        "operational_complete": payload["operational_complete"],
        "evidence_eligible": payload["evidence_eligible"],
        "independent_recall": payload["metrics"]["independent_recall"],
        "eligible_pair_agreement": payload["metrics"]["eligible_pair_agreement"],
        "comparable_coverage": payload["metrics"]["comparable_coverage"],
        "issue_codes": payload["issue_codes"],
    }
