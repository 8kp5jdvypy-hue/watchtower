"""Read-only immutable daily audit for final-RTH momentum and handoff evidence."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as ecals

from tradebot.rth_momentum import POLL_SECONDS, RTH_HANDOFF_LEAD


AUDIT_VERSION = 1
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
    paths = list(audit_dir.glob("rth_momentum_audit_*_v1.json"))
    if not paths:
        return None
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    payload = max(payloads, key=lambda item: item["session"])
    return {
        "session": payload["session"],
        "operational_clean": payload["operational_clean"],
        "session_evidence_eligible": payload["session_evidence_eligible"],
        "coverage_pct": payload["operational"]["coverage_pct"],
        "issue_codes": [issue["code"] for issue in payload["issues"]],
    }
