"""Read-only daily audit for market-wide postmarket discovery evidence.

This module performs no provider, alert, journal, broker, or database-write
operation. Its only optional write is one atomic immutable JSON report after
the full exchange-calendar postmarket window has ended.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as ecals


AUDIT_VERSION = 4
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "postmarket_shadow.db"
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
START_GRACE_SECONDS = 90
END_GRACE_SECONDS = 90
MAX_TICK_GAP_SECONDS = 150
EXPECTED_POLL_SECONDS = 60
MAX_PROCESSING_LATENCY_MS = 30_000
MAX_SCHEDULED_LAG_MS = 30_000
MAX_SOURCE_AGE_SECONDS = 180
FINAL_BAR_GRACE = timedelta(minutes=5)
EXPECTED_PROVIDER = "alpaca"
EXPECTED_FEED = "sip"
EXPECTED_TIMEFRAME = "5Min"
EXPECTED_SCOPES = {
    1: "alpaca_top_movers_and_actives",
    2: "alpaca_top_movers_actives_plus_full_universe_sweep",
}
FULL_UNIVERSE_SWEEP_SOURCE = "full_universe_sweep"
EXPECTED_SWEEP_CYCLE_TICKS = 5
EXPECTED_ENDPOINTS = {
    "market_movers",
    "most_actives_volume",
    "most_actives_trades",
}
SOURCE_ENDPOINT = {
    "market_gainer": "market_movers",
    "market_loser": "market_movers",
    "most_active_volume": "most_actives_volume",
    "most_active_trades": "most_actives_trades",
}
NEAR_MISS_OUTCOMES = {
    "AWAITING_PERSISTENCE",
    "BELOW_NOTIONAL",
    "BAR_GAP",
    "UNSTABLE_PRINT",
}


@dataclass(frozen=True)
class AuditIssue:
    code: str
    severity: str
    detail: str


@dataclass(frozen=True)
class CandidateLifecycle:
    symbol: str
    direction: str
    first_detected_at: str
    initial_move_pct: float
    initial_notional: float
    observation_ticks: int
    latest_outcome: str
    latest_observed_at: str
    max_abs_move_pct: float
    max_notional: float
    sources: tuple[str, ...]


@dataclass
class _ObservationSummary:
    observation_ticks: int = 0
    latest_outcome: str = ""
    latest_tick_utc: str = ""
    max_abs_move_pct: float = 0.0
    max_notional: float = 0.0
    candidate_observations: int = 0
    first_candidate_completed_by_direction: dict[str, datetime] = field(
        default_factory=dict
    )
    near_miss: bool = False


@dataclass(frozen=True)
class OperationalMetrics:
    ticks: int
    first_tick_utc: str | None
    final_tick_utc: str | None
    expected_start_utc: str | None
    expected_end_utc: str | None
    window_coverage_pct: float
    max_tick_gap_seconds: float | None
    universe_symbols_min: int | None
    universe_symbols_max: int | None
    screen_rows_min: int | None
    screen_rows_max: int | None
    screen_unique_symbols_min: int | None
    screen_unique_symbols_max: int | None
    discovered_symbols_min: int | None
    discovered_symbols_max: int | None
    excluded_symbols_total: int
    not_returned_symbols_min: int | None
    not_returned_symbols_max: int | None
    fetched_symbols_total: int
    evaluated_symbols_total: int
    candidate_observations: int
    unique_candidates: int
    new_candidates_recorded: int
    fetch_errors: int
    failed_invariants: int
    average_latency_ms: float | None
    max_latency_ms: int | None
    timing_rows: int
    average_scheduled_lag_ms: float | None
    max_scheduled_lag_ms: int | None
    missed_cycles: int
    average_stage_latency_ms: dict[str, float]
    max_stage_latency_ms: dict[str, int]
    persistence_observations: int
    average_persistence_span_seconds: float | None
    max_persistence_span_seconds: float | None
    max_source_age_seconds: float | None
    discovery_versions: tuple[int, ...]
    code_versions: tuple[str, ...]
    data_feeds: tuple[str, ...]
    market_data_providers: tuple[str, ...]
    bar_timeframes: tuple[str, ...]
    discovery_scopes: tuple[str, ...]
    requested_top_ns: tuple[int, ...]
    endpoint_snapshots: int
    threshold_snapshots: int
    outcome_counts: dict[str, int]
    source_observations: dict[str, int]
    scheduled_overlap_symbols: int


@dataclass(frozen=True)
class DailyDiscoveryAuditReport:
    audit_version: int
    audit_code_version: str | None
    session: str
    database: str
    operational_clean: bool
    session_evidence_eligible: bool
    operational: OperationalMetrics
    candidates: tuple[CandidateLifecycle, ...]
    near_miss_symbols: tuple[str, ...]
    issues: tuple[AuditIssue, ...]


def _issue(
    issues: list[AuditIssue], code: str, detail: str, severity: str = "blocker"
) -> None:
    issues.append(AuditIssue(code, severity, detail))


def _aware_datetime(raw: Any, context: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{context} must be an ISO-8601 string")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO-8601 string") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_object(raw: Any, context: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    return value


def _json_list(raw: Any, context: str) -> list:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a JSON array")
    return value


def _session_window(session: date) -> tuple[datetime, datetime] | None:
    if not CALENDAR.is_session(session):
        return None
    start = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    end = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return start, end


def _audit_ready_at(session: date) -> datetime | None:
    """Return when the final 8:00 PM bar has had time to become observable."""
    window = _session_window(session)
    return window[1] + FINAL_BAR_GRACE if window else None


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def _range(rows: list[sqlite3.Row], key: str) -> tuple[int | None, int | None]:
    values = [int(row[key]) for row in rows]
    return (min(values), max(values)) if values else (None, None)


def _unique_strings(rows: list[sqlite3.Row], key: str) -> tuple[str, ...]:
    return tuple(sorted({str(row[key]) for row in rows if row[key] is not None}))


def _validate_tick_metadata(
    tick: sqlite3.Row,
    tick_time: datetime,
    issues: list[AuditIssue],
) -> tuple[float | None, dict[str, datetime]]:
    tick_id = tick["tick_id"]
    try:
        endpoints = _json_list(tick["endpoints_json"], f"tick {tick_id} endpoints")
        if len(endpoints) != len(EXPECTED_ENDPOINTS) or set(endpoints) != EXPECTED_ENDPOINTS:
            _issue(issues, "ENDPOINT_SET_INVALID", f"tick {tick_id} endpoint set is invalid")
    except ValueError as exc:
        _issue(issues, "MALFORMED_ENDPOINT_SNAPSHOT", str(exc))

    updates: dict[str, datetime] = {}
    max_age: float | None = None
    try:
        raw_updates = _json_object(
            tick["source_updates_json"], f"tick {tick_id} source updates"
        )
        if set(raw_updates) != EXPECTED_ENDPOINTS:
            _issue(
                issues,
                "SOURCE_TIMESTAMP_SET_INVALID",
                f"tick {tick_id} source timestamp set is invalid",
            )
        for source, raw in raw_updates.items():
            updated = _aware_datetime(raw, f"tick {tick_id} {source} update")
            updates[source] = updated
            age = (tick_time - updated).total_seconds()
            max_age = age if max_age is None else max(max_age, age)
            if age < 0:
                _issue(
                    issues,
                    "SOURCE_TIMESTAMP_FUTURE",
                    f"tick {tick_id} {source} was {-age:.0f}s in the future",
                )
            elif age > MAX_SOURCE_AGE_SECONDS:
                _issue(
                    issues,
                    "SOURCE_TIMESTAMP_STALE",
                    f"tick {tick_id} {source} was {age:.0f}s old",
                )
    except ValueError as exc:
        _issue(issues, "MALFORMED_SOURCE_TIMESTAMP_SNAPSHOT", str(exc))

    try:
        thresholds = _json_object(
            tick["thresholds_json"], f"tick {tick_id} thresholds"
        )
        required = {"move_pct", "min_cumulative_notional", "persistence_bars"}
        if not required <= thresholds.keys():
            _issue(
                issues,
                "THRESHOLD_SNAPSHOT_INCOMPLETE",
                f"tick {tick_id} lacks required thresholds",
            )
    except ValueError as exc:
        _issue(issues, "MALFORMED_THRESHOLD_SNAPSHOT", str(exc))
    return max_age, updates


def audit_discovery_session(
    conn: sqlite3.Connection,
    session: date,
    *,
    database: str = "<connection>",
    audit_code_version: str | None = None,
) -> DailyDiscoveryAuditReport:
    issues: list[AuditIssue] = []
    if audit_code_version in {None, "", "unknown"}:
        _issue(
            issues,
            "AUDIT_CODE_VERSION_UNKNOWN",
            "audit logic revision was not supplied",
            severity="warning",
        )
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        quick_check = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if quick_check != ["ok"]:
            _issue(issues, "DATABASE_INTEGRITY_FAILED", repr(quick_check))
        required = {
            "postmarket_discovery_ticks",
            "postmarket_discovery_observations",
            "postmarket_discovery_candidates",
            "postmarket_discovery_timing",
        }
        present = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = required - present
        if missing:
            raise ValueError(f"shadow database is missing tables: {sorted(missing)}")
        ticks = conn.execute(
            "SELECT * FROM postmarket_discovery_ticks WHERE session=? "
            "ORDER BY tick_utc,tick_id",
            (session.isoformat(),),
        ).fetchall()
        observations = conn.execute(
            """
            SELECT o.*,t.tick_utc,t.completed_utc,t.discovered_symbols,
                   t.fetched_symbols,t.evaluated_symbols,t.candidate_observations,
                   t.error_count,t.invariant_ok,t.data_feed AS tick_data_feed,
                   t.market_data_provider AS tick_provider,
                   t.bar_timeframe AS tick_timeframe
            FROM postmarket_discovery_observations o
            JOIN postmarket_discovery_ticks t ON t.tick_id=o.tick_id
            WHERE t.session=? ORDER BY t.tick_utc,o.symbol
            """,
            (session.isoformat(),),
        )
        candidates = conn.execute(
            "SELECT * FROM postmarket_discovery_candidates WHERE session=? "
            "ORDER BY first_detected_at,symbol",
            (session.isoformat(),),
        ).fetchall()
        timing_rows = conn.execute(
            "SELECT * FROM postmarket_discovery_timing WHERE session=? "
            "ORDER BY scheduled_tick_utc,tick_id",
            (session.isoformat(),),
        ).fetchall()
    finally:
        conn.row_factory = original_row_factory

    window = _session_window(session)
    if window is None:
        _issue(issues, "NON_TRADING_SESSION", f"{session} is not an XNYS session")
    expected_start, expected_end = window if window else (None, None)
    tick_times = [
        _aware_datetime(row["tick_utc"], f"tick {row['tick_id']} tick_utc")
        for row in ticks
    ]
    completed_times = [
        _aware_datetime(row["completed_utc"], f"tick {row['tick_id']} completed_utc")
        for row in ticks
    ]
    if not ticks:
        _issue(issues, "NO_TICKS", "no discovery ticks exist for the session")
    if len(tick_times) != len(set(tick_times)):
        _issue(issues, "DUPLICATE_TICK_TIMESTAMPS", "tick timestamps are not unique")
    if tick_times != sorted(tick_times):
        _issue(issues, "OUT_OF_ORDER_TICKS", "tick timestamps are out of order")
    if any(done < started for started, done in zip(tick_times, completed_times)):
        _issue(issues, "NEGATIVE_PROCESSING_LATENCY", "a tick completed before it started")
    gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(tick_times, tick_times[1:])
    ]
    max_gap = max(gaps) if gaps else None

    tick_rows_by_id = {int(row["tick_id"]): row for row in ticks}
    timing_by_tick: dict[int, sqlite3.Row] = {}
    stage_columns = {
        "screen": "screen_latency_ms",
        "selection": "selection_latency_ms",
        "bar_fetch": "bar_fetch_latency_ms",
        "evaluation": "evaluation_latency_ms",
    }
    stage_values: dict[str, list[int]] = {name: [] for name in stage_columns}
    scheduled_lags: list[int] = []
    missed_cycles = 0
    persistence_observations = 0
    persistence_weighted_seconds = 0.0
    persistence_max_values: list[float] = []
    if len(timing_rows) != len(ticks):
        _issue(
            issues,
            "TIMING_EVIDENCE_MISSING",
            f"stored {len(timing_rows)} timing rows for {len(ticks)} ticks",
        )
    scheduled_times: list[datetime] = []
    previous_scheduled: datetime | None = None
    for row in timing_rows:
        tick_id = int(row["tick_id"])
        if tick_id in timing_by_tick:
            _issue(issues, "TIMING_TICK_DUPLICATED", f"tick {tick_id} has duplicate timing")
            continue
        timing_by_tick[tick_id] = row
        tick = tick_rows_by_id.get(tick_id)
        if tick is None:
            _issue(issues, "TIMING_TICK_ORPHANED", f"timing references absent tick {tick_id}")
            continue
        scheduled = _aware_datetime(
            row["scheduled_tick_utc"], f"tick {tick_id} scheduled_tick_utc"
        )
        actual = _aware_datetime(row["actual_start_utc"], f"tick {tick_id} actual_start_utc")
        completed = _aware_datetime(row["completed_utc"], f"tick {tick_id} timing completed")
        scheduled_times.append(scheduled)
        tick_actual = _aware_datetime(tick["tick_utc"], f"tick {tick_id} tick_utc")
        tick_completed = _aware_datetime(
            tick["completed_utc"], f"tick {tick_id} completed_utc"
        )
        if row["session"] != session.isoformat() or actual != tick_actual or completed != tick_completed:
            _issue(
                issues,
                "TIMING_TICK_MISMATCH",
                f"timing metadata disagrees with tick {tick_id}",
            )
        expected_lag = round((actual - scheduled).total_seconds() * 1000)
        lag = int(row["scheduled_lag_ms"])
        if expected_lag < 0 or lag != expected_lag:
            _issue(issues, "SCHEDULED_LAG_MISMATCH", f"tick {tick_id} lag is invalid")
        scheduled_lags.append(lag)
        if expected_start is not None:
            if previous_scheduled is None:
                expected_missed = int(
                    (scheduled - expected_start).total_seconds() // EXPECTED_POLL_SECONDS
                )
            else:
                expected_missed = (
                    int(
                        (scheduled - previous_scheduled).total_seconds()
                        // EXPECTED_POLL_SECONDS
                    )
                    - 1
                )
            if int(row["missed_cycles"]) != max(0, expected_missed):
                _issue(
                    issues,
                    "MISSED_CYCLE_COUNT_MISMATCH",
                    f"tick {tick_id} missed-cycle count is inconsistent",
                )
        previous_scheduled = scheduled
        missed_cycles += int(row["missed_cycles"])
        stage_total = 0
        for name, column in stage_columns.items():
            value = int(row[column])
            stage_values[name].append(value)
            stage_total += value
        total = int(row["total_latency_ms"])
        if stage_total > total + 4 or tick["latency_ms"] != total:
            _issue(
                issues,
                "STAGE_LATENCY_MISMATCH",
                f"tick {tick_id} stage/total latency is inconsistent",
            )
        count = int(row["persistence_observations"])
        average_span = row["persistence_span_avg_seconds"]
        max_span = row["persistence_span_max_seconds"]
        persistence_observations += count
        if count == 0:
            if average_span is not None or max_span is not None:
                _issue(
                    issues,
                    "PERSISTENCE_TIMING_MISMATCH",
                    f"tick {tick_id} has spans without observations",
                )
        elif (
            average_span is None
            or max_span is None
            or float(average_span) < 0
            or float(max_span) < float(average_span)
        ):
            _issue(
                issues,
                "PERSISTENCE_TIMING_MISMATCH",
                f"tick {tick_id} persistence timing is invalid",
            )
        else:
            persistence_weighted_seconds += float(average_span) * count
            persistence_max_values.append(float(max_span))
    if set(timing_by_tick) != set(tick_rows_by_id):
        _issue(issues, "TIMING_TICK_SET_MISMATCH", "timing and tick ids do not match")
    if scheduled_times != sorted(scheduled_times) or len(scheduled_times) != len(set(scheduled_times)):
        _issue(issues, "SCHEDULE_GRID_INVALID", "scheduled tick timestamps are not ordered and unique")
    if missed_cycles:
        _issue(issues, "MISSED_CYCLES", f"timing ledger recorded {missed_cycles} missed cycles")
    if scheduled_lags and max(scheduled_lags) > MAX_SCHEDULED_LAG_MS:
        _issue(
            issues,
            "SCHEDULED_LAG_HIGH",
            f"maximum scheduled lag was {max(scheduled_lags)}ms",
        )
    if max_gap is not None and max_gap > MAX_TICK_GAP_SECONDS:
        detail = f"maximum tick gap was {max_gap:.0f}s"
        gap_index = gaps.index(max_gap)
        prior_tick_id = int(ticks[gap_index]["tick_id"])
        prior_timing = timing_by_tick.get(prior_tick_id)
        if prior_timing is not None:
            slowest = max(stage_columns, key=lambda name: prior_timing[stage_columns[name]])
            detail += (
                f"; prior tick slowest stage was {slowest} "
                f"({prior_timing[stage_columns[slowest]]}ms)"
            )
        _issue(issues, "TICK_GAP", detail)

    coverage_pct = 0.0
    if tick_times and expected_start is not None and expected_end is not None:
        if tick_times[0] > expected_start + timedelta(seconds=START_GRACE_SECONDS):
            delay = (tick_times[0] - expected_start).total_seconds()
            _issue(issues, "COVERAGE_STARTED_LATE", f"first tick was {delay:.0f}s after close")
        if tick_times[-1] < expected_end - timedelta(seconds=END_GRACE_SECONDS):
            early = (expected_end - tick_times[-1]).total_seconds()
            _issue(issues, "COVERAGE_ENDED_EARLY", f"final tick was {early:.0f}s early")
        if tick_times[0] < expected_start - timedelta(seconds=START_GRACE_SECONDS):
            _issue(issues, "PREWINDOW_TICK", "a tick precedes the postmarket window")
        if tick_times[-1] > expected_end + timedelta(seconds=END_GRACE_SECONDS):
            _issue(issues, "POSTWINDOW_TICK", "a tick follows the postmarket window")
        duration = (expected_end - expected_start).total_seconds()
        covered = max(
            0.0,
            (
                min(tick_times[-1], expected_end)
                - max(tick_times[0], expected_start)
            ).total_seconds(),
        )
        coverage_pct = round(100 * covered / duration, 2) if duration else 0.0

    observation_counts: Counter[int] = Counter()
    observation_candidates: Counter[int] = Counter()
    observation_errors: Counter[int] = Counter()
    observation_no_bars: Counter[int] = Counter()
    outcome_counts: Counter[str] = Counter()
    observation_summaries: dict[str, _ObservationSummary] = {}
    source_observations: Counter[str] = Counter()
    scheduled_symbols: set[str] = set()
    sweep_positions_by_tick: dict[int, bytearray] = {}
    max_source_ages: list[float] = []
    updates_by_tick: dict[int, dict[str, datetime]] = {}
    ticks_by_id = {row["tick_id"]: row for row in ticks}
    for row, tick_time in zip(ticks, tick_times):
        max_age, updates = _validate_tick_metadata(row, tick_time, issues)
        if max_age is not None:
            max_source_ages.append(max_age)
        updates_by_tick[row["tick_id"]] = updates

    for row in observations:
        tick_id = int(row["tick_id"])
        symbol = str(row["symbol"])
        outcome = str(row["outcome"])
        observation_counts[tick_id] += 1
        outcome_counts[outcome] += 1
        if outcome == "CANDIDATE":
            observation_candidates[tick_id] += 1
        elif outcome == "FETCH_ERROR":
            observation_errors[tick_id] += 1
        elif outcome == "NO_BARS_RETURNED":
            observation_no_bars[tick_id] += 1
        summary = observation_summaries.setdefault(symbol, _ObservationSummary())
        summary.observation_ticks += 1
        summary.latest_outcome = outcome
        summary.latest_tick_utc = str(row["tick_utc"])
        if row["move_pct"] is not None:
            summary.max_abs_move_pct = max(
                summary.max_abs_move_pct, abs(float(row["move_pct"]))
            )
        if row["cumulative_notional"] is not None:
            summary.max_notional = max(
                summary.max_notional, float(row["cumulative_notional"])
            )
        if outcome == "CANDIDATE":
            summary.candidate_observations += 1
            direction = row["direction"]
            if direction is not None:
                completed = _aware_datetime(
                    row["completed_utc"], "qualifying completed_utc"
                )
                prior = summary.first_candidate_completed_by_direction.get(
                    str(direction)
                )
                if prior is None or completed < prior:
                    summary.first_candidate_completed_by_direction[
                        str(direction)
                    ] = completed
        if outcome in NEAR_MISS_OUTCOMES or abs(float(row["move_pct"] or 0)) >= 5:
            summary.near_miss = True
        if row["event_date"] != session.isoformat():
            _issue(issues, "EVENT_DATE_MISMATCH", f"{symbol} has {row['event_date']}")
        if (
            row["data_feed"] != row["tick_data_feed"]
            or row["market_data_provider"] != row["tick_provider"]
            or row["bar_timeframe"] != row["tick_timeframe"]
        ):
            _issue(
                issues,
                "OBSERVATION_PROVENANCE_MISMATCH",
                f"{row['symbol']} disagrees with tick {tick_id}",
            )
        try:
            sources = _json_list(row["sources_json"], f"tick {tick_id} sources")
            if not sources or any(not isinstance(source, str) for source in sources):
                raise ValueError(f"tick {tick_id} sources must contain strings")
            source_observations.update(sources)
            if "scheduled_earnings" in sources:
                scheduled_symbols.add(row["symbol"])
            discovery_version = int(ticks_by_id[tick_id]["discovery_version"])
            attributable_sources = set(sources) - {"scheduled_earnings"}
            provider_sources = attributable_sources - {FULL_UNIVERSE_SWEEP_SOURCE}
            has_sweep = FULL_UNIVERSE_SWEEP_SOURCE in attributable_sources
            if (
                not attributable_sources
                or not provider_sources <= SOURCE_ENDPOINT.keys()
                or (discovery_version == 1 and has_sweep)
                or (discovery_version == 2 and not (provider_sources or has_sweep))
            ):
                _issue(
                    issues,
                    "OBSERVATION_SOURCE_INVALID",
                    f"{row['symbol']} has invalid sources at tick {tick_id}",
                )
            if row["outcome"] == "NO_BARS_RETURNED" and (
                attributable_sources != {FULL_UNIVERSE_SWEEP_SOURCE}
                or row["reason"]
                != "no bars returned for full-universe sweep window"
            ):
                _issue(
                    issues,
                    "NO_BARS_EVIDENCE_INVALID",
                    f"{row['symbol']} has invalid no-bars evidence at tick {tick_id}",
                )
            evidence = _json_list(
                row["screen_evidence_json"], f"tick {tick_id} screen evidence"
            )
            evidence_sources = {item.get("source") for item in evidence if isinstance(item, dict)}
            if evidence_sources != attributable_sources:
                _issue(
                    issues,
                    "SCREEN_EVIDENCE_SOURCE_MISMATCH",
                    f"{row['symbol']} source evidence disagrees at tick {tick_id}",
                )
            updates = updates_by_tick.get(tick_id, {})
            evidence_pairs: list[tuple[str, int]] = []
            for item in evidence:
                if not isinstance(item, dict):
                    _issue(
                        issues,
                        "MALFORMED_SCREEN_EVIDENCE",
                        f"{row['symbol']} has malformed evidence at tick {tick_id}",
                    )
                    continue
                if item.get("source") == FULL_UNIVERSE_SWEEP_SOURCE:
                    tick = ticks_by_id[tick_id]
                    timing = timing_by_tick.get(int(tick_id))
                    expected_scheduled = (
                        timing["scheduled_tick_utc"] if timing is not None else None
                    )
                    expected = {
                        "source": FULL_UNIVERSE_SWEEP_SOURCE,
                        "scheduled_tick_utc": expected_scheduled,
                        "universe_sha256": tick["sweep_universe_sha256"],
                        "cycle_ticks": tick["sweep_cycle_ticks"],
                        "shard_index": tick["sweep_shard_index"],
                        "shard_count": tick["sweep_shard_count"],
                        "shard_size": tick["sweep_shard_size"],
                    }
                    universe_symbols = tick["universe_symbols"]
                    shard_size = tick["sweep_shard_size"]
                    shard_index = tick["sweep_shard_index"]
                    numeric_sweep_metadata = all(
                        isinstance(value, int) and not isinstance(value, bool)
                        for value in (universe_symbols, shard_size, shard_index)
                    )
                    valid_position = (
                        numeric_sweep_metadata
                        and isinstance(item.get("universe_position"), int)
                        and not isinstance(item.get("universe_position"), bool)
                        and shard_size > 0
                        and 0 <= item["universe_position"] < universe_symbols
                        and item["universe_position"] // shard_size == shard_index
                    )
                    if (
                        discovery_version != 2
                        or any(item.get(key) != value for key, value in expected.items())
                        or not valid_position
                    ):
                        _issue(
                            issues,
                            "SWEEP_EVIDENCE_INVALID",
                            f"{row['symbol']} has invalid sweep evidence at tick {tick_id}",
                        )
                    else:
                        position = int(item["universe_position"])
                        bitmap = sweep_positions_by_tick.setdefault(
                            tick_id, bytearray(int(tick["universe_symbols"]))
                        )
                        if bitmap[position]:
                            _issue(
                                issues,
                                "SWEEP_POSITION_DUPLICATED",
                                f"tick {tick_id} maps position {position} to multiple symbols",
                            )
                        bitmap[position] = 1
                    continue
                if item.get("source") not in SOURCE_ENDPOINT:
                    _issue(
                        issues,
                        "MALFORMED_SCREEN_EVIDENCE",
                        f"{row['symbol']} has malformed evidence at tick {tick_id}",
                    )
                    continue
                rank = item.get("rank")
                top_n = ticks_by_id[tick_id]["requested_top_n"]
                valid_rank = (
                    isinstance(rank, int)
                    and not isinstance(rank, bool)
                    and 1 <= rank <= top_n
                )
                if not valid_rank:
                    _issue(
                        issues,
                        "SCREEN_EVIDENCE_RANK_INVALID",
                        f"{row['symbol']} has an invalid rank at tick {tick_id}",
                    )
                else:
                    evidence_pairs.append((item["source"], rank))
                endpoint = SOURCE_ENDPOINT[item["source"]]
                updated = _aware_datetime(
                    item.get("source_updated_at"),
                    f"{row['symbol']} tick {tick_id} evidence timestamp",
                )
                if updates.get(endpoint) != updated:
                    _issue(
                        issues,
                        "SCREEN_EVIDENCE_TIMESTAMP_MISMATCH",
                        f"{row['symbol']} evidence timestamp disagrees at tick {tick_id}",
                    )
            if len(evidence_pairs) != len(set(evidence_pairs)):
                _issue(
                    issues,
                    "SCREEN_EVIDENCE_DUPLICATED",
                    f"{row['symbol']} duplicated source/rank evidence at tick {tick_id}",
                )
            ranks = _json_list(row["ranks_json"], f"tick {tick_id} ranks")
            normalized_ranks = []
            for item in ranks:
                if (
                    isinstance(item, list)
                    and len(item) == 2
                    and isinstance(item[0], str)
                    and isinstance(item[1], int)
                    and not isinstance(item[1], bool)
                ):
                    normalized_ranks.append((item[0], item[1]))
            if (
                len(normalized_ranks) != len(ranks)
                or len(normalized_ranks) != len(evidence_pairs)
                or set(normalized_ranks) != set(evidence_pairs)
            ):
                _issue(
                    issues,
                    "RANK_EVIDENCE_MISMATCH",
                    f"{row['symbol']} ranks disagree with evidence at tick {tick_id}",
                )
        except ValueError as exc:
            _issue(issues, "MALFORMED_OBSERVATION_EVIDENCE", str(exc))

    for row in ticks:
        tick_id = row["tick_id"]
        conservation = (
            row["universe_symbols"]
            == row["discovered_symbols"] + row["not_returned_symbols"]
            and row["screen_unique_symbols"]
            == row["discovered_symbols"] + row["excluded_symbols"]
            and 0 <= row["fetched_symbols"] <= row["discovered_symbols"]
            and row["evaluated_symbols"] == row["discovered_symbols"]
        )
        if not row["invariant_ok"] or not conservation:
            _issue(issues, "TICK_INVARIANT_FAILED", f"tick {tick_id} did not conserve symbols")
        if observation_counts[tick_id] != row["evaluated_symbols"]:
            _issue(
                issues,
                "OBSERVATION_COUNT_MISMATCH",
                f"tick {tick_id} stored {observation_counts[tick_id]} observations for "
                f"{row['evaluated_symbols']} evaluations",
            )
        if observation_candidates[tick_id] != row["candidate_observations"]:
            _issue(issues, "CANDIDATE_COUNT_MISMATCH", f"tick {tick_id} candidate count drifted")
        if observation_errors[tick_id] != row["error_count"]:
            _issue(issues, "ERROR_COUNT_MISMATCH", f"tick {tick_id} error count drifted")
        if (
            observation_errors[tick_id] + observation_no_bars[tick_id]
            < row["discovered_symbols"] - row["fetched_symbols"]
        ):
            _issue(
                issues,
                "MISSING_FETCH_ERRORS",
                f"tick {tick_id} did not explain every missing bulk response as "
                "NO_BARS_RETURNED or FETCH_ERROR",
            )
        if row["screen_unique_symbols"] > row["screen_rows"]:
            _issue(issues, "SCREEN_COUNT_INVALID", f"tick {tick_id} has impossible counts")
        discovery_version = int(row["discovery_version"])
        if discovery_version == 2:
            sweep_values = (
                row["provider_screen_rows"],
                row["provider_screen_unique_symbols"],
                row["sweep_universe_sha256"],
                row["sweep_cycle_ticks"],
                row["sweep_shard_index"],
                row["sweep_shard_count"],
                row["sweep_shard_size"],
                row["sweep_shard_symbols"],
                row["sweep_overlap_symbols"],
            )
            numeric_sweep_values = (
                row["provider_screen_rows"],
                row["provider_screen_unique_symbols"],
                row["sweep_cycle_ticks"],
                row["sweep_shard_index"],
                row["sweep_shard_count"],
                row["sweep_shard_size"],
                row["sweep_shard_symbols"],
                row["sweep_overlap_symbols"],
            )
            if any(value is None for value in sweep_values) or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in numeric_sweep_values
            ):
                _issue(issues, "SWEEP_METADATA_MISSING", f"tick {tick_id} lacks sweep metadata")
                expected_rows = None
            else:
                expected_shard_count = min(
                    int(row["sweep_cycle_ticks"]), int(row["universe_symbols"])
                )
                start = int(row["sweep_shard_index"]) * int(row["sweep_shard_size"])
                expected_shard_symbols = max(
                    0,
                    min(
                        int(row["sweep_shard_size"]),
                        int(row["universe_symbols"]) - start,
                    ),
                )
                timing = timing_by_tick.get(int(tick_id))
                expected_index = None
                if timing is not None and expected_shard_count and expected_start is not None:
                    scheduled = _aware_datetime(
                        timing["scheduled_tick_utc"],
                        f"tick {tick_id} scheduled sweep time",
                    )
                    expected_index = int(
                        (scheduled - expected_start).total_seconds() // 60
                    ) % expected_shard_count
                sweep_conserved = (
                    int(row["sweep_cycle_ticks"]) == EXPECTED_SWEEP_CYCLE_TICKS
                    and int(row["sweep_shard_count"]) == expected_shard_count
                    and 0 <= int(row["sweep_shard_index"]) < expected_shard_count
                    and int(row["sweep_shard_symbols"]) == expected_shard_symbols
                    and 0 <= int(row["sweep_overlap_symbols"])
                    <= min(
                        int(row["provider_screen_unique_symbols"]),
                        int(row["sweep_shard_symbols"]),
                    )
                    and int(row["screen_rows"])
                    == int(row["provider_screen_rows"])
                    + int(row["sweep_shard_symbols"])
                    and int(row["screen_unique_symbols"])
                    == int(row["provider_screen_unique_symbols"])
                    + int(row["sweep_shard_symbols"])
                    - int(row["sweep_overlap_symbols"])
                    and len(str(row["sweep_universe_sha256"])) == 64
                    and all(
                        character in "0123456789abcdef"
                        for character in str(row["sweep_universe_sha256"])
                    )
                    and (expected_index is None or int(row["sweep_shard_index"]) == expected_index)
                )
                if not sweep_conserved:
                    _issue(
                        issues,
                        "SWEEP_INVARIANT_FAILED",
                        f"tick {tick_id} did not conserve its deterministic universe shard",
                    )
                expected_positions = set(
                    range(start, start + expected_shard_symbols)
                )
                actual_positions = sum(
                    sweep_positions_by_tick.get(int(tick_id), bytearray())
                )
                if actual_positions != len(expected_positions):
                    _issue(
                        issues,
                        "SWEEP_POSITION_COVERAGE_MISMATCH",
                        f"tick {tick_id} stored {actual_positions} of "
                        f"{len(expected_positions)} expected sweep positions",
                    )
                expected_rows = 4 * row["requested_top_n"] + int(
                    row["sweep_shard_symbols"]
                )
        else:
            expected_rows = 4 * row["requested_top_n"]
        if expected_rows is not None and row["screen_rows"] != expected_rows:
            _issue(
                issues,
                "SCREEN_SCOPE_UNDERFILLED",
                f"tick {tick_id} stored {row['screen_rows']} rows; expected {expected_rows}",
            )
        if row["universe_symbols"] > 0 and row["discovered_symbols"] == 0:
            _issue(issues, "NO_DISCOVERED_SYMBOLS", f"tick {tick_id} discovered no symbols")

    if any(row["error_count"] for row in ticks):
        _issue(issues, "FETCH_ERRORS", "one or more evaluations ended in FETCH_ERROR")
    latencies = [row["latency_ms"] for row in ticks if row["latency_ms"] is not None]
    if len(latencies) != len(ticks):
        _issue(issues, "MISSING_PROCESSING_LATENCY", "one or more ticks has no latency")
    if latencies and max(latencies) > MAX_PROCESSING_LATENCY_MS:
        _issue(
            issues,
            "PROCESSING_LATENCY_HIGH",
            f"maximum processing latency was {max(latencies)}ms",
        )

    metadata_checks = {
        "DISCOVERY_VERSION_DRIFT": {row["discovery_version"] for row in ticks},
        "CODE_VERSION_DRIFT": {row["code_version"] for row in ticks},
        "DATA_FEED_DRIFT": {row["data_feed"] for row in ticks},
        "PROVIDER_DRIFT": {row["market_data_provider"] for row in ticks},
        "BAR_TIMEFRAME_DRIFT": {row["bar_timeframe"] for row in ticks},
        "DISCOVERY_SCOPE_DRIFT": {row["discovery_scope"] for row in ticks},
        "REQUESTED_TOP_N_DRIFT": {row["requested_top_n"] for row in ticks},
        "ENDPOINT_DRIFT": {row["endpoints_json"] for row in ticks},
        "THRESHOLD_DRIFT": {row["thresholds_json"] for row in ticks},
        "UNIVERSE_COUNT_DRIFT": {row["universe_symbols"] for row in ticks},
        "SWEEP_UNIVERSE_DIGEST_DRIFT": {
            row["sweep_universe_sha256"]
            for row in ticks
            if int(row["discovery_version"]) == 2
        },
    }
    for code, values in metadata_checks.items():
        if len(values) > 1:
            _issue(issues, code, f"session contains {len(values)} distinct values")
    if any(row["code_version"] in {None, "", "unknown"} for row in ticks):
        _issue(issues, "UNKNOWN_CODE_VERSION", "one or more ticks lacks a revision")
    if any(row["data_feed"] != EXPECTED_FEED for row in ticks):
        _issue(issues, "NON_SIP_DATA", "one or more discovery ticks did not use SIP")
    if any(row["market_data_provider"] != EXPECTED_PROVIDER for row in ticks):
        _issue(issues, "UNEXPECTED_PROVIDER", "one or more ticks used another provider")
    if any(row["bar_timeframe"] != EXPECTED_TIMEFRAME for row in ticks):
        _issue(issues, "UNEXPECTED_TIMEFRAME", "one or more ticks used another timeframe")
    if any(
        row["discovery_scope"] != EXPECTED_SCOPES.get(int(row["discovery_version"]))
        for row in ticks
    ):
        _issue(issues, "UNEXPECTED_DISCOVERY_SCOPE", "one or more ticks used another scope")
    if any(not 1 <= row["requested_top_n"] <= 50 for row in ticks):
        _issue(issues, "REQUESTED_TOP_N_INVALID", "one or more ticks used an invalid bound")

    new_candidates_recorded = sum(row["new_candidates"] for row in ticks)
    if new_candidates_recorded != len(candidates):
        _issue(
            issues,
            "CANDIDATE_LEDGER_MISMATCH",
            f"ticks recorded {new_candidates_recorded} new candidates but ledger has "
            f"{len(candidates)}",
        )
    candidate_lifecycles: list[CandidateLifecycle] = []
    candidate_symbols = {row["symbol"] for row in candidates}
    for candidate in candidates:
        summary = observation_summaries.get(candidate["symbol"])
        if summary is None or not summary.candidate_observations:
            _issue(
                issues,
                "CANDIDATE_WITHOUT_OBSERVATION",
                f"{candidate['symbol']} has no qualifying observation",
            )
            continue
        first_qualifying = summary.first_candidate_completed_by_direction.get(
            candidate["direction"]
        )
        if first_qualifying is None:
            _issue(
                issues,
                "CANDIDATE_DIRECTION_MISMATCH",
                f"{candidate['symbol']} ledger direction disagrees with observations",
            )
        if candidate["event_date"] != session.isoformat():
            _issue(
                issues,
                "CANDIDATE_EVENT_DATE_MISMATCH",
                f"{candidate['symbol']} has {candidate['event_date']}",
            )
        allowed_metadata = {
            "code_version": {row["code_version"] for row in ticks},
            "data_feed": {row["data_feed"] for row in ticks},
            "market_data_provider": {row["market_data_provider"] for row in ticks},
            "bar_timeframe": {row["bar_timeframe"] for row in ticks},
        }
        if any(candidate[key] not in values for key, values in allowed_metadata.items()):
            _issue(
                issues,
                "CANDIDATE_PROVENANCE_MISMATCH",
                f"{candidate['symbol']} metadata disagrees with session ticks",
            )
        try:
            sources = tuple(sorted(_json_list(candidate["sources_json"], "candidate sources")))
        except ValueError as exc:
            _issue(issues, "MALFORMED_CANDIDATE_SOURCES", str(exc))
            sources = ()
        first_detected = _aware_datetime(
            candidate["first_detected_at"], "candidate first_detected_at"
        )
        if first_qualifying is not None and first_detected != first_qualifying:
            _issue(
                issues,
                "CANDIDATE_DETECTION_TIME_MISMATCH",
                f"{candidate['symbol']} ledger time disagrees with first qualifying tick",
            )
        candidate_lifecycles.append(
            CandidateLifecycle(
                symbol=candidate["symbol"],
                direction=candidate["direction"],
                first_detected_at=first_detected.isoformat(),
                initial_move_pct=float(candidate["move_pct"]),
                initial_notional=float(candidate["cumulative_notional"]),
                observation_ticks=summary.observation_ticks,
                latest_outcome=summary.latest_outcome,
                latest_observed_at=_aware_datetime(
                    summary.latest_tick_utc, "candidate latest tick"
                ).isoformat(),
                max_abs_move_pct=summary.max_abs_move_pct,
                max_notional=summary.max_notional,
                sources=sources,
            )
        )

    near_misses = tuple(
        sorted(
            symbol
            for symbol, summary in observation_summaries.items()
            if symbol not in candidate_symbols
            and summary.near_miss
        )
    )
    blocking = tuple(issue for issue in issues if issue.severity == "blocker")
    operational_clean = not blocking
    session_evidence_eligible = (
        operational_clean and audit_code_version not in {None, "", "unknown"}
    )
    universe_min, universe_max = _range(ticks, "universe_symbols")
    screen_min, screen_max = _range(ticks, "screen_rows")
    unique_min, unique_max = _range(ticks, "screen_unique_symbols")
    discovered_min, discovered_max = _range(ticks, "discovered_symbols")
    not_returned_min, not_returned_max = _range(ticks, "not_returned_symbols")
    operational = OperationalMetrics(
        ticks=len(ticks),
        first_tick_utc=tick_times[0].isoformat() if tick_times else None,
        final_tick_utc=tick_times[-1].isoformat() if tick_times else None,
        expected_start_utc=expected_start.isoformat() if expected_start else None,
        expected_end_utc=expected_end.isoformat() if expected_end else None,
        window_coverage_pct=coverage_pct,
        max_tick_gap_seconds=max_gap,
        universe_symbols_min=universe_min,
        universe_symbols_max=universe_max,
        screen_rows_min=screen_min,
        screen_rows_max=screen_max,
        screen_unique_symbols_min=unique_min,
        screen_unique_symbols_max=unique_max,
        discovered_symbols_min=discovered_min,
        discovered_symbols_max=discovered_max,
        excluded_symbols_total=sum(row["excluded_symbols"] for row in ticks),
        not_returned_symbols_min=not_returned_min,
        not_returned_symbols_max=not_returned_max,
        fetched_symbols_total=sum(row["fetched_symbols"] for row in ticks),
        evaluated_symbols_total=sum(row["evaluated_symbols"] for row in ticks),
        candidate_observations=sum(row["candidate_observations"] for row in ticks),
        unique_candidates=len(candidates),
        new_candidates_recorded=new_candidates_recorded,
        fetch_errors=sum(row["error_count"] for row in ticks),
        failed_invariants=sum(not row["invariant_ok"] for row in ticks),
        average_latency_ms=(sum(latencies) / len(latencies) if latencies else None),
        max_latency_ms=max(latencies) if latencies else None,
        timing_rows=len(timing_rows),
        average_scheduled_lag_ms=(
            sum(scheduled_lags) / len(scheduled_lags) if scheduled_lags else None
        ),
        max_scheduled_lag_ms=max(scheduled_lags) if scheduled_lags else None,
        missed_cycles=missed_cycles,
        average_stage_latency_ms={
            name: sum(values) / len(values)
            for name, values in stage_values.items()
            if values
        },
        max_stage_latency_ms={
            name: max(values) for name, values in stage_values.items() if values
        },
        persistence_observations=persistence_observations,
        average_persistence_span_seconds=(
            persistence_weighted_seconds / persistence_observations
            if persistence_observations
            else None
        ),
        max_persistence_span_seconds=(
            max(persistence_max_values) if persistence_max_values else None
        ),
        max_source_age_seconds=max(max_source_ages) if max_source_ages else None,
        discovery_versions=tuple(sorted({row["discovery_version"] for row in ticks})),
        code_versions=_unique_strings(ticks, "code_version"),
        data_feeds=_unique_strings(ticks, "data_feed"),
        market_data_providers=_unique_strings(ticks, "market_data_provider"),
        bar_timeframes=_unique_strings(ticks, "bar_timeframe"),
        discovery_scopes=_unique_strings(ticks, "discovery_scope"),
        requested_top_ns=tuple(sorted({row["requested_top_n"] for row in ticks})),
        endpoint_snapshots=len({row["endpoints_json"] for row in ticks}),
        threshold_snapshots=len({row["thresholds_json"] for row in ticks}),
        outcome_counts=dict(sorted(outcome_counts.items())),
        source_observations=dict(sorted(source_observations.items())),
        scheduled_overlap_symbols=len(scheduled_symbols),
    )
    return DailyDiscoveryAuditReport(
        audit_version=AUDIT_VERSION,
        audit_code_version=audit_code_version,
        session=session.isoformat(),
        database=database,
        operational_clean=operational_clean,
        session_evidence_eligible=session_evidence_eligible,
        operational=operational,
        candidates=tuple(candidate_lifecycles),
        near_miss_symbols=near_misses,
        issues=tuple(issues),
    )


def report_json(report: DailyDiscoveryAuditReport, *, compact: bool = False) -> str:
    return json.dumps(
        asdict(report),
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
        sort_keys=True,
    )


def write_report_atomic(path: Path | str, report: DailyDiscoveryAuditReport) -> bool:
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
            handle.write(report_json(report))
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


def write_completed_discovery_audits(
    db_path: Path | str,
    output_dir: Path | str,
    *,
    now: datetime,
    audit_code_version: str | None = None,
) -> tuple[DailyDiscoveryAuditReport, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    database = Path(db_path)
    if not database.exists():
        return ()
    conn = connect_readonly(database)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "postmarket_discovery_ticks" not in tables:
            return ()
        sessions = [
            date.fromisoformat(row[0])
            for row in conn.execute(
                "SELECT DISTINCT session FROM postmarket_discovery_ticks ORDER BY session"
            ).fetchall()
        ]
        reports = []
        for session in sessions:
            audit_ready_at = _audit_ready_at(session)
            if audit_ready_at is None or now.astimezone(timezone.utc) <= audit_ready_at:
                continue
            destination = (
                Path(output_dir)
                / f"postmarket_discovery_audit_{session.isoformat()}_v{AUDIT_VERSION}.json"
            )
            if destination.exists():
                existing = json.loads(destination.read_text(encoding="utf-8"))
                if (
                    existing.get("session") != session.isoformat()
                    or existing.get("audit_version") != AUDIT_VERSION
                ):
                    raise ValueError(f"existing audit report is inconsistent: {destination}")
                continue
            report = audit_discovery_session(
                conn,
                session,
                database=str(database),
                audit_code_version=audit_code_version,
            )
            if write_report_atomic(destination, report):
                reports.append(report)
        return tuple(reports)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--audit-code-version", default=os.environ.get("GIT_SHA"))
    parser.add_argument("--session", required=True, type=date.fromisoformat)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        conn = connect_readonly(args.db)
        try:
            report = audit_discovery_session(
                conn,
                args.session,
                database=str(args.db),
                audit_code_version=args.audit_code_version,
            )
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(report_json(report, compact=args.compact))
    return 0 if report.operational_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
