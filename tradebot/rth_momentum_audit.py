"""Read-only immutable daily audit for final-RTH momentum and handoff evidence."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as ecals

from tradebot.rth_momentum import (
    FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS,
    FULL_UNIVERSE_RTH_SWEEP_SOURCE,
    POLL_SECONDS,
    RTH_HANDOFF_LEAD,
)


AUDIT_VERSION = 2
CALENDAR = ecals.get_calendar("XNYS")
FINALIZATION_GRACE = timedelta(minutes=5)
MAX_SCHEDULED_LAG_MS = 30_000
MAX_TOTAL_LATENCY_MS = 30_000


@dataclass(frozen=True)
class RthAuditIssue:
    code: str
    severity: str
    detail: str


@dataclass(frozen=True)
class RthOperationalMetrics:
    expected_ticks: int
    ticks: int
    coverage_pct: float
    first_scheduled_tick_utc: str | None
    final_scheduled_tick_utc: str | None
    missing_scheduled_ticks: tuple[str, ...]
    sweep_ticks: int
    sweep_cycle_ticks: int | None
    sweep_universe_sha256: tuple[str, ...]
    complete_sweep_cycles: int
    sweep_symbols_total: int
    sweep_overlap_symbols_total: int
    selected_symbols_total: int
    evaluated_symbols_total: int
    candidate_observations: int
    unique_candidates: int
    new_candidates_recorded: int
    failed_invariants: int
    errors: int
    missed_cycles: int
    average_scheduled_lag_ms: float | None
    max_scheduled_lag_ms: int | None
    average_stage_latency_ms: dict[str, float]
    max_stage_latency_ms: dict[str, int]
    average_total_latency_ms: float | None
    max_total_latency_ms: int | None
    code_versions: tuple[str, ...]
    data_feeds: tuple[str, ...]
    market_data_providers: tuple[str, ...]
    outcome_counts: dict[str, int]
    handoff_state_counts: dict[str, int]


@dataclass(frozen=True)
class RthMomentumAuditReport:
    audit_version: int
    audit_code_version: str | None
    session: str
    database: str
    expected_start_utc: str
    expected_end_utc: str
    operational_clean: bool
    session_evidence_eligible: bool
    operational: RthOperationalMetrics
    issues: tuple[RthAuditIssue, ...]


def _window(session: date) -> tuple[datetime, datetime]:
    if not CALENDAR.is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    session_open = CALENDAR.session_open(session).to_pydatetime().astimezone(timezone.utc)
    close = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    return max(session_open, close - RTH_HANDOFF_LEAD), close


def _expected_schedule(session: date) -> tuple[datetime, ...]:
    start, end = _window(session)
    count = round((end - start).total_seconds() / POLL_SECONDS) + 1
    return tuple(start + timedelta(seconds=POLL_SECONDS * index) for index in range(count))


def rth_audit_session_due(now: datetime) -> date | None:
    """Return today's XNYS session once its final-RTH audit is finalizable."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current_date = now.astimezone(CALENDAR.tz).date()
    if not CALENDAR.is_session(current_date):
        return None
    _, end = _window(current_date)
    return current_date if now.astimezone(timezone.utc) >= end + FINALIZATION_GRACE else None


def _issue(issues: list[RthAuditIssue], code: str, detail: str) -> None:
    issues.append(RthAuditIssue(code, "blocker", detail))


def _average(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _audit_universe_sweep(
    conn: sqlite3.Connection,
    ticks: list[sqlite3.Row],
    *,
    window_start: datetime,
    issues: list[RthAuditIssue],
) -> tuple[int, int | None, tuple[str, ...], int, int, int]:
    """Reconcile each attributable shard and every complete sweep cycle."""
    if not ticks:
        return 0, None, (), 0, 0, 0
    required_columns = {
        "sweep_universe_sha256",
        "sweep_cycle_ticks",
        "sweep_shard_index",
        "sweep_shard_count",
        "sweep_shard_size",
        "sweep_shard_symbols",
        "sweep_overlap_symbols",
    }
    if not required_columns <= set(ticks[0].keys()):
        _issue(
            issues,
            "SWEEP_SCHEMA_MISSING",
            "RTH tick schema predates attributable full-universe sweep evidence",
        )
        return 0, None, (), 0, 0, 0
    digests: set[str] = set()
    cycle_values: set[int] = set()
    cycle_positions: dict[int, dict[int, str]] = {}
    sweep_ticks = sweep_symbols_total = overlap_total = 0
    for row in ticks:
        tick_id = int(row["tick_id"])
        required = {
            name: row[name]
            for name in (
                "sweep_universe_sha256",
                "sweep_cycle_ticks",
                "sweep_shard_index",
                "sweep_shard_count",
                "sweep_shard_size",
                "sweep_shard_symbols",
                "sweep_overlap_symbols",
            )
        }
        if any(value is None for value in required.values()):
            _issue(
                issues,
                "SWEEP_EVIDENCE_MISSING",
                f"tick {tick_id} lacks full-universe sweep identity",
            )
            continue
        sweep_ticks += 1
        digest = str(required["sweep_universe_sha256"])
        cycle_ticks = int(required["sweep_cycle_ticks"])
        shard_index = int(required["sweep_shard_index"])
        shard_count = int(required["sweep_shard_count"])
        shard_size = int(required["sweep_shard_size"])
        shard_symbols = int(required["sweep_shard_symbols"])
        overlap = int(required["sweep_overlap_symbols"])
        universe_symbols = int(row["universe_symbols"])
        digests.add(digest)
        cycle_values.add(cycle_ticks)
        sweep_symbols_total += shard_symbols
        overlap_total += overlap
        if (
            cycle_ticks != FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS
            or universe_symbols <= 0
            or shard_count != min(cycle_ticks, universe_symbols)
            or shard_size != math.ceil(universe_symbols / shard_count)
            or not 0 <= shard_index < shard_count
            or not 0 <= overlap <= shard_symbols
            or not 0 < shard_symbols <= shard_size
        ):
            _issue(
                issues,
                "SWEEP_IDENTITY_INVALID",
                f"tick {tick_id} has invalid shard identity {required!r}",
            )
            continue
        scheduled = datetime.fromisoformat(row["scheduled_tick_utc"])
        if scheduled.tzinfo is None or scheduled.utcoffset() is None:
            continue
        slot_seconds = (
            scheduled.astimezone(timezone.utc) - window_start
        ).total_seconds()
        if slot_seconds < 0 or slot_seconds % POLL_SECONDS:
            _issue(
                issues,
                "SWEEP_SCHEDULE_INVALID",
                f"tick {tick_id} is not on the RTH sweep minute grid",
            )
            continue
        slot = int(slot_seconds // POLL_SECONDS)
        if shard_index != slot % shard_count:
            _issue(
                issues,
                "SWEEP_SHARD_SEQUENCE",
                f"tick {tick_id} shard={shard_index}; expected={slot % shard_count}",
            )
        observations = conn.execute(
            """
            SELECT symbol,sources_json,screen_evidence_json
            FROM rth_momentum_observations WHERE tick_id=? ORDER BY symbol
            """,
            (tick_id,),
        ).fetchall()
        if len(observations) != int(row["selected_symbols"]):
            _issue(
                issues,
                "OBSERVATION_CONSERVATION",
                f"tick {tick_id} selected {row['selected_symbols']} but stored "
                f"{len(observations)} observations",
            )
        positions: dict[int, str] = {}
        attributed = 0
        for observation in observations:
            try:
                sources = json.loads(observation["sources_json"])
                evidence = json.loads(observation["screen_evidence_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                _issue(
                    issues,
                    "SWEEP_EVIDENCE_INVALID",
                    f"tick {tick_id} symbol {observation['symbol']} has invalid JSON",
                )
                continue
            if FULL_UNIVERSE_RTH_SWEEP_SOURCE not in sources:
                continue
            attributed += 1
            sweep_evidence = [
                item
                for item in evidence
                if isinstance(item, dict)
                and item.get("source") == FULL_UNIVERSE_RTH_SWEEP_SOURCE
            ]
            if len(sweep_evidence) != 1:
                _issue(
                    issues,
                    "SWEEP_EVIDENCE_INVALID",
                    f"tick {tick_id} symbol {observation['symbol']} lacks one sweep fact",
                )
                continue
            fact = sweep_evidence[0]
            if (
                fact.get("universe_sha256") != digest
                or fact.get("cycle_ticks") != cycle_ticks
                or fact.get("shard_index") != shard_index
                or fact.get("shard_count") != shard_count
                or fact.get("shard_size") != shard_size
                or fact.get("scheduled_tick_utc")
                != scheduled.astimezone(timezone.utc).isoformat()
            ):
                _issue(
                    issues,
                    "SWEEP_EVIDENCE_IDENTITY_MISMATCH",
                    f"tick {tick_id} symbol {observation['symbol']} disagrees with tick",
                )
                continue
            position = fact.get("universe_position")
            if not isinstance(position, int) or isinstance(position, bool):
                _issue(
                    issues,
                    "SWEEP_POSITION_INVALID",
                    f"tick {tick_id} symbol {observation['symbol']} has invalid position",
                )
                continue
            if position in positions:
                _issue(
                    issues,
                    "SWEEP_POSITION_DUPLICATE",
                    f"tick {tick_id} repeats universe position {position}",
                )
            positions[position] = observation["symbol"]
        if attributed != shard_symbols or len(positions) != shard_symbols:
            _issue(
                issues,
                "SWEEP_OBSERVATION_MISMATCH",
                f"tick {tick_id} declares {shard_symbols}, attributed={attributed}, "
                f"positions={len(positions)}",
            )
        cycle_number = slot // shard_count
        cycle = cycle_positions.setdefault(cycle_number, {})
        for position, symbol in positions.items():
            if position in cycle:
                _issue(
                    issues,
                    "SWEEP_CYCLE_DUPLICATE_POSITION",
                    f"cycle {cycle_number} repeats position {position}",
                )
            cycle[position] = symbol
    if len(digests) > 1:
        _issue(issues, "SWEEP_UNIVERSE_DRIFT", f"digests={sorted(digests)!r}")
    if len(cycle_values) > 1:
        _issue(issues, "SWEEP_CYCLE_DRIFT", f"cycle_ticks={sorted(cycle_values)!r}")
    universe_counts = {int(row["universe_symbols"]) for row in ticks}
    if len(universe_counts) > 1:
        _issue(
            issues,
            "SWEEP_UNIVERSE_COUNT_DRIFT",
            f"universe_counts={sorted(universe_counts)!r}",
        )
    universe_count = next(iter(universe_counts), 0)
    complete_cycles = 0
    for cycle_number, positions in sorted(cycle_positions.items()):
        expected_slots = min(
            FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS,
            universe_count,
        )
        cycle_start_slot = cycle_number * expected_slots
        if cycle_start_slot + expected_slots > len(ticks):
            continue
        complete_cycles += 1
        if set(positions) != set(range(universe_count)):
            _issue(
                issues,
                "SWEEP_CYCLE_COVERAGE",
                f"cycle {cycle_number} covered {len(positions)}/{universe_count}",
            )
    return (
        sweep_ticks,
        next(iter(cycle_values), None) if len(cycle_values) == 1 else None,
        tuple(sorted(digests)),
        complete_cycles,
        sweep_symbols_total,
        overlap_total,
    )


def build_rth_momentum_audit(
    conn: sqlite3.Connection,
    *,
    session: date,
    database: str,
    audit_code_version: str | None,
) -> RthMomentumAuditReport:
    expected = _expected_schedule(session)
    start, end = expected[0], expected[-1]
    issues: list[RthAuditIssue] = []
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _build_rth_momentum_audit(
            conn,
            session=session,
            database=database,
            audit_code_version=audit_code_version,
            expected=expected,
            issues=issues,
        )
    finally:
        conn.row_factory = original_row_factory


def _build_rth_momentum_audit(
    conn: sqlite3.Connection,
    *,
    session: date,
    database: str,
    audit_code_version: str | None,
    expected: tuple[datetime, ...],
    issues: list[RthAuditIssue],
) -> RthMomentumAuditReport:
    start, end = expected[0], expected[-1]
    if not _has_table(conn, "rth_momentum_ticks"):
        ticks = []
    else:
        ticks = conn.execute(
            """
            SELECT * FROM rth_momentum_ticks
            WHERE session=? ORDER BY scheduled_tick_utc,tick_id
            """,
            (session.isoformat(),),
        ).fetchall()
    scheduled: list[datetime] = []
    duplicate_schedule = False
    for row in ticks:
        value = datetime.fromisoformat(row["scheduled_tick_utc"])
        if value.tzinfo is None or value.utcoffset() is None:
            _issue(issues, "NAIVE_SCHEDULE", f"tick {row['tick_id']} has naive schedule")
            continue
        scheduled.append(value.astimezone(timezone.utc))
    if len(set(scheduled)) != len(scheduled):
        duplicate_schedule = True
        _issue(issues, "DUPLICATE_SCHEDULE", "duplicate scheduled tick identity")
    expected_set = set(expected)
    observed_set = set(scheduled)
    missing = tuple(sorted(expected_set - observed_set))
    unexpected = tuple(sorted(observed_set - expected_set))
    if not ticks:
        _issue(issues, "NO_TICKS", "no final-RTH momentum ticks were recorded")
    if missing:
        _issue(issues, "TICK_GAP", f"{len(missing)} expected scheduled ticks are missing")
    if unexpected:
        _issue(issues, "OUTSIDE_WINDOW_TICK", f"{len(unexpected)} ticks are outside the window")
    if scheduled and min(scheduled) > start:
        _issue(
            issues,
            "COVERAGE_STARTED_LATE",
            f"first scheduled tick was {min(scheduled).isoformat()}, expected {start.isoformat()}",
        )
    if scheduled and max(scheduled) < end:
        _issue(
            issues,
            "COVERAGE_ENDED_EARLY",
            f"last scheduled tick was {max(scheduled).isoformat()}, expected {end.isoformat()}",
        )
    failed_invariants = sum(not bool(row["invariant_ok"]) for row in ticks)
    errors = sum(int(row["error_count"]) for row in ticks)
    missed_cycles = sum(int(row["missed_cycles"]) for row in ticks)
    if failed_invariants:
        _issue(issues, "FAILED_INVARIANT", f"{failed_invariants} ticks failed conservation")
    if errors:
        _issue(issues, "EVALUATION_ERROR", f"{errors} per-symbol errors were recorded")
    if missed_cycles:
        _issue(issues, "MISSED_CYCLES", f"{missed_cycles} scheduled cycles were missed")
    for row in ticks:
        if int(row["selected_symbols"]) != int(row["evaluated_symbols"]):
            _issue(
                issues,
                "SELECTION_EVALUATION_MISMATCH",
                f"tick {row['tick_id']} selected {row['selected_symbols']} but evaluated "
                f"{row['evaluated_symbols']}",
            )
        if int(row["scheduled_lag_ms"]) > MAX_SCHEDULED_LAG_MS:
            _issue(
                issues,
                "SCHEDULED_LAG",
                f"tick {row['tick_id']} lagged {row['scheduled_lag_ms']}ms",
            )
        if int(row["total_latency_ms"]) > MAX_TOTAL_LATENCY_MS:
            _issue(
                issues,
                "PROCESSING_LATENCY",
                f"tick {row['tick_id']} took {row['total_latency_ms']}ms",
            )
    code_versions = tuple(sorted({row["code_version"] for row in ticks if row["code_version"]}))
    feeds = tuple(sorted({row["data_feed"] for row in ticks}))
    providers = tuple(sorted({row["market_data_provider"] for row in ticks}))
    if len(code_versions) > 1:
        _issue(issues, "MIXED_CODE_VERSION", f"versions={code_versions!r}")
    if feeds and feeds != ("sip",):
        _issue(issues, "FEED_MISMATCH", f"feeds={feeds!r}")
    if providers and providers != ("alpaca",):
        _issue(issues, "PROVIDER_MISMATCH", f"providers={providers!r}")

    observations = (
        conn.execute(
            """
            SELECT outcome,COUNT(*) AS count FROM rth_momentum_observations
            WHERE session=? GROUP BY outcome ORDER BY outcome
            """,
            (session.isoformat(),),
        ).fetchall()
        if _has_table(conn, "rth_momentum_observations")
        else []
    )
    outcomes = {row["outcome"]: int(row["count"]) for row in observations}
    sweep_metrics = _audit_universe_sweep(
        conn,
        ticks,
        window_start=start,
        issues=issues,
    )
    has_candidates = _has_table(conn, "rth_momentum_candidates")
    has_handoffs = _has_table(conn, "rth_postmarket_handoffs")
    unique_candidates = (
        int(conn.execute(
            "SELECT COUNT(*) FROM rth_momentum_candidates WHERE session=?",
            (session.isoformat(),),
        ).fetchone()[0])
        if has_candidates
        else 0
    )
    handoff_counts = (
        {
            row[0]: int(row[1])
            for row in conn.execute(
                """
                SELECT state,COUNT(*) FROM rth_postmarket_handoffs
                WHERE session=? GROUP BY state ORDER BY state
                """,
                (session.isoformat(),),
            ).fetchall()
        }
        if has_handoffs
        else {}
    )
    seed_count = handoff_counts.get("RTH_QUALIFIED", 0)
    if seed_count != unique_candidates:
        _issue(
            issues,
            "HANDOFF_SEED_MISMATCH",
            f"candidates={unique_candidates}; RTH_QUALIFIED={seed_count}",
        )
    if has_candidates and has_handoffs:
        orphan_handoffs = int(conn.execute(
            """
            SELECT COUNT(*) FROM rth_postmarket_handoffs h
            LEFT JOIN rth_momentum_candidates c
              ON c.candidate_id=h.rth_candidate_id
            WHERE h.session=? AND c.candidate_id IS NULL
            """,
            (session.isoformat(),),
        ).fetchone()[0])
        identity_mismatches = int(conn.execute(
            """
            SELECT COUNT(*) FROM rth_postmarket_handoffs h
            JOIN rth_momentum_candidates c
              ON c.candidate_id=h.rth_candidate_id
            WHERE h.session=? AND (
              h.session<>c.session OR h.symbol<>c.symbol OR h.direction<>c.direction
            )
            """,
            (session.isoformat(),),
        ).fetchone()[0])
        terminal_conflicts = int(conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT rth_candidate_id
              FROM rth_postmarket_handoffs
              WHERE session=? AND state IN (
                'POSTMARKET_QUALIFIED','POSTMARKET_NOT_QUALIFIED'
              )
              GROUP BY rth_candidate_id HAVING COUNT(DISTINCT state)>1
            )
            """,
            (session.isoformat(),),
        ).fetchone()[0])
        invalid_links = int(conn.execute(
            """
            SELECT COUNT(*) FROM rth_postmarket_handoffs
            WHERE session=? AND (
              (state='POSTMARKET_QUALIFIED' AND postmarket_candidate_id IS NULL) OR
              (state<>'POSTMARKET_QUALIFIED' AND postmarket_candidate_id IS NOT NULL)
            )
            """,
            (session.isoformat(),),
        ).fetchone()[0])
        qualified_links = handoff_counts.get("POSTMARKET_QUALIFIED", 0)
        has_postmarket_candidates = _has_table(
            conn, "postmarket_discovery_candidates"
        )
        postmarket_identity_mismatches = qualified_links
        if has_postmarket_candidates:
            postmarket_identity_mismatches = int(conn.execute(
                """
                SELECT COUNT(*) FROM rth_postmarket_handoffs h
                LEFT JOIN postmarket_discovery_candidates p
                  ON p.candidate_id=h.postmarket_candidate_id
                WHERE h.session=? AND h.state='POSTMARKET_QUALIFIED' AND (
                  p.candidate_id IS NULL OR p.session<>h.session OR
                  p.symbol<>h.symbol OR p.direction<>h.direction
                )
                """,
                (session.isoformat(),),
            ).fetchone()[0])
        if orphan_handoffs:
            _issue(issues, "ORPHAN_HANDOFF", f"orphan_handoffs={orphan_handoffs}")
        if identity_mismatches:
            _issue(
                issues,
                "HANDOFF_IDENTITY_MISMATCH",
                f"identity_mismatches={identity_mismatches}",
            )
        if terminal_conflicts:
            _issue(
                issues,
                "HANDOFF_TERMINAL_CONFLICT",
                f"conflicted_candidates={terminal_conflicts}",
            )
        if invalid_links:
            _issue(
                issues,
                "HANDOFF_LINK_INVALID",
                f"invalid_link_rows={invalid_links}",
            )
        if postmarket_identity_mismatches:
            _issue(
                issues,
                "POSTMARKET_HANDOFF_IDENTITY_MISMATCH",
                f"identity_mismatches={postmarket_identity_mismatches}",
            )
    stage_columns = (
        "screen_latency_ms",
        "selection_latency_ms",
        "bar_fetch_latency_ms",
        "evaluation_latency_ms",
    )
    stage_values = {
        name: [int(row[name]) for row in ticks]
        for name in stage_columns
    }
    operational = RthOperationalMetrics(
        expected_ticks=len(expected),
        ticks=len(ticks),
        coverage_pct=round(len(observed_set & expected_set) / len(expected) * 100, 2),
        first_scheduled_tick_utc=(min(scheduled).isoformat() if scheduled else None),
        final_scheduled_tick_utc=(max(scheduled).isoformat() if scheduled else None),
        missing_scheduled_ticks=tuple(value.isoformat() for value in missing),
        sweep_ticks=sweep_metrics[0],
        sweep_cycle_ticks=sweep_metrics[1],
        sweep_universe_sha256=sweep_metrics[2],
        complete_sweep_cycles=sweep_metrics[3],
        sweep_symbols_total=sweep_metrics[4],
        sweep_overlap_symbols_total=sweep_metrics[5],
        selected_symbols_total=sum(int(row["selected_symbols"]) for row in ticks),
        evaluated_symbols_total=sum(int(row["evaluated_symbols"]) for row in ticks),
        candidate_observations=sum(int(row["candidate_observations"]) for row in ticks),
        unique_candidates=unique_candidates,
        new_candidates_recorded=sum(int(row["new_candidates"]) for row in ticks),
        failed_invariants=failed_invariants,
        errors=errors,
        missed_cycles=missed_cycles,
        average_scheduled_lag_ms=_average([int(row["scheduled_lag_ms"]) for row in ticks]),
        max_scheduled_lag_ms=(max(int(row["scheduled_lag_ms"]) for row in ticks) if ticks else None),
        average_stage_latency_ms={name: _average(values) or 0.0 for name, values in stage_values.items()},
        max_stage_latency_ms={name: max(values) if values else 0 for name, values in stage_values.items()},
        average_total_latency_ms=_average([int(row["total_latency_ms"]) for row in ticks]),
        max_total_latency_ms=(max(int(row["total_latency_ms"]) for row in ticks) if ticks else None),
        code_versions=code_versions,
        data_feeds=feeds,
        market_data_providers=providers,
        outcome_counts=outcomes,
        handoff_state_counts=handoff_counts,
    )
    clean = not issues and not duplicate_schedule
    return RthMomentumAuditReport(
        audit_version=AUDIT_VERSION,
        audit_code_version=audit_code_version,
        session=session.isoformat(),
        database=database,
        expected_start_utc=start.isoformat(),
        expected_end_utc=end.isoformat(),
        operational_clean=clean,
        session_evidence_eligible=clean and operational.coverage_pct == 100.0,
        operational=operational,
        issues=tuple(issues),
    )


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _write_atomic_exclusive(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to replace immutable RTH audit {path}")
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
        os.link(temporary, path)
        os.chmod(path, 0o444)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def write_completed_rth_audits(
    db_path: Path,
    audit_dir: Path,
    *,
    now: datetime,
    audit_code_version: str | None,
    expected_sessions: tuple[date, ...] = (),
) -> tuple[RthMomentumAuditReport, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not db_path.is_file():
        return ()
    conn = connect_readonly(db_path)
    try:
        if not _has_table(conn, "rth_momentum_ticks"):
            return ()
        recorded_sessions = [
            date.fromisoformat(row[0])
            for row in conn.execute(
                "SELECT DISTINCT session FROM rth_momentum_ticks ORDER BY session"
            ).fetchall()
        ]
        sessions = sorted(set(recorded_sessions) | set(expected_sessions))
        written = []
        for session in sessions:
            _, end = _window(session)
            if now.astimezone(timezone.utc) < end + FINALIZATION_GRACE:
                continue
            path = audit_dir / f"rth_momentum_audit_{session.isoformat()}_v{AUDIT_VERSION}.json"
            if path.exists():
                continue
            report = build_rth_momentum_audit(
                conn,
                session=session,
                database=db_path.name,
                audit_code_version=audit_code_version,
            )
            _write_atomic_exclusive(path, asdict(report))
            written.append(report)
        return tuple(written)
    finally:
        conn.close()


def latest_rth_audit_summary(audit_dir: Path) -> dict | None:
    paths = list(audit_dir.glob("rth_momentum_audit_*_v*.json"))
    if not paths:
        return None
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    payload = max(
        payloads,
        key=lambda item: (item["session"], int(item.get("audit_version", 0))),
    )
    return {
        "session": payload["session"],
        "operational_clean": payload["operational_clean"],
        "session_evidence_eligible": payload["session_evidence_eligible"],
        "coverage_pct": payload["operational"]["coverage_pct"],
        "issue_codes": [issue["code"] for issue in payload["issues"]],
    }
