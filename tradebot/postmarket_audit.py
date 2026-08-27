"""Read-only daily audit for the postmarket shadow observer.

Operational evidence comes from the append-only shadow database. Empirical
quality evidence is optional and comes from a separately locked manifest whose
labels must be fixed while blinded to observer output. This module performs no
network, journal, alert, Telegram, broker, or database-write operation. Its
only optional write is an atomic, immutable JSON audit report.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import exchange_calendars as ecals


AUDIT_VERSION = 1
EMPIRICAL_SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "postmarket_shadow.db"
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
POLL_SECONDS = 60
START_GRACE_SECONDS = 90
END_GRACE_SECONDS = 90
MAX_TICK_GAP_SECONDS = 150
MAX_PROCESSING_LATENCY_MS = 30_000
FINAL_BAR_GRACE = timedelta(minutes=5)
ALLOWED_LABEL_METHODS = {"blind_bar_review", "multi_provider_reconciliation"}
ALLOWED_CLASSIFICATIONS = {"eligible", "ineligible", "ambiguous"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS = {
    "schema_version",
    "status",
    "manifest_version",
    "session",
    "created_at_utc",
    "labeler",
    "label_method",
    "blinded_to_observer_output",
    "eligibility",
    "artifacts",
    "labels",
}
ARTIFACT_FIELDS = {"provider", "feed", "endpoint", "acquired_at_utc", "sha256"}
LABEL_FIELDS = {
    "symbol",
    "classification",
    "direction",
    "eligible_at_utc",
    "max_abs_move_pct",
    "cumulative_notional",
    "reason_code",
    "rationale",
}
ELIGIBILITY_FIELDS = {"move_pct", "min_cumulative_notional", "persistence_bars"}


@dataclass(frozen=True)
class AuditIssue:
    code: str
    severity: str
    detail: str


@dataclass(frozen=True)
class EmpiricalArtifact:
    provider: str
    feed: str
    endpoint: str
    acquired_at_utc: datetime
    sha256: str


@dataclass(frozen=True)
class EmpiricalLabel:
    symbol: str
    classification: str
    direction: str | None
    eligible_at_utc: datetime | None
    max_abs_move_pct: float
    cumulative_notional: float
    reason_code: str
    rationale: str


@dataclass(frozen=True)
class EmpiricalManifest:
    manifest_version: str
    session: date
    created_at_utc: datetime
    labeler: str
    label_method: str
    blinded_to_observer_output: bool
    move_threshold_pct: float
    min_cumulative_notional: float
    persistence_bars: int
    artifacts: tuple[EmpiricalArtifact, ...]
    labels: tuple[EmpiricalLabel, ...]


@dataclass(frozen=True)
class EmpiricalMetrics:
    status: str
    manifest_version: str | None
    definitive_labels: int
    ambiguous_labels: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    direction_mismatches: int
    mean_detection_latency_seconds: float | None
    max_detection_latency_seconds: float | None


@dataclass(frozen=True)
class CatalystLedgerEvidence:
    status: str
    attempts: int
    latest_completed_at_utc: str | None
    requested_symbols: int | None
    fetched_events: int | None
    matched_events: int | None
    windows_created: int | None
    code_version: str | None
    run_mode: str | None
    run_id: str | None
    error: str | None
    expected_symbols: tuple[str, ...]


@dataclass(frozen=True)
class OperationalMetrics:
    ticks: int
    first_tick_utc: str | None
    final_tick_utc: str | None
    expected_start_utc: str | None
    expected_end_utc: str | None
    window_coverage_pct: float
    max_tick_gap_seconds: float | None
    scheduled_symbols: int | None
    observed_symbols: int
    candidate_observations: int
    unique_candidates: int
    new_candidates_recorded: int
    fetch_errors: int
    failed_invariants: int
    average_latency_ms: float | None
    max_latency_ms: int | None
    observer_versions: tuple[int, ...]
    code_versions: tuple[str, ...]
    data_feeds: tuple[str, ...]
    market_data_providers: tuple[str, ...]
    threshold_snapshots: int


@dataclass(frozen=True)
class DailyAuditReport:
    audit_version: int
    audit_code_version: str | None
    session: str
    database: str
    operational_clean: bool
    session_evidence_eligible: bool
    scheduled_symbol_list: tuple[str, ...]
    catalyst_ledger: CatalystLedgerEvidence | None
    operational: OperationalMetrics
    empirical: EmpiricalMetrics
    issues: tuple[AuditIssue, ...]


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _exact_fields(mapping: dict[str, Any], expected: set[str], context: str) -> None:
    missing = expected - mapping.keys()
    extra = mapping.keys() - expected
    if missing or extra:
        raise ValueError(
            f"{context} fields are invalid; missing={sorted(missing)} extra={sorted(extra)}"
        )


def _nonempty_string(raw: Any, context: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return raw.strip()


def _finite_number(raw: Any, context: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(f"{context} must be numeric")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{context} must be finite")
    return value


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


def _parse_artifact(raw: Any, index: int) -> EmpiricalArtifact:
    context = f"artifacts[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _exact_fields(raw, ARTIFACT_FIELDS, context)
    digest = _nonempty_string(_required(raw, "sha256", context), f"{context}.sha256").lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError(f"{context}.sha256 must be a 64-character hexadecimal digest")
    return EmpiricalArtifact(
        provider=_nonempty_string(_required(raw, "provider", context), f"{context}.provider"),
        feed=_nonempty_string(_required(raw, "feed", context), f"{context}.feed"),
        endpoint=_nonempty_string(_required(raw, "endpoint", context), f"{context}.endpoint"),
        acquired_at_utc=_aware_datetime(
            _required(raw, "acquired_at_utc", context), f"{context}.acquired_at_utc"
        ),
        sha256=digest,
    )


def _parse_label(raw: Any, index: int, session: date) -> EmpiricalLabel:
    context = f"labels[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _exact_fields(raw, LABEL_FIELDS, context)
    symbol = _nonempty_string(_required(raw, "symbol", context), f"{context}.symbol").upper()
    classification = _required(raw, "classification", context)
    if classification not in ALLOWED_CLASSIFICATIONS:
        raise ValueError(
            f"{context}.classification must be one of {sorted(ALLOWED_CLASSIFICATIONS)}"
        )
    direction = raw.get("direction")
    eligible_at_raw = raw.get("eligible_at_utc")
    eligible_at = (
        _aware_datetime(eligible_at_raw, f"{context}.eligible_at_utc")
        if eligible_at_raw is not None
        else None
    )
    if classification == "eligible":
        if direction not in {"up", "down"} or eligible_at is None:
            raise ValueError(f"{context} eligible labels require direction and eligible_at_utc")
        if eligible_at.astimezone(ET).date() != session:
            raise ValueError(f"{context}.eligible_at_utc must fall in the labeled session")
    elif direction is not None or eligible_at is not None:
        raise ValueError(
            f"{context} ineligible/ambiguous labels must not declare direction or eligibility time"
        )
    max_move = _finite_number(
        _required(raw, "max_abs_move_pct", context), f"{context}.max_abs_move_pct"
    )
    notional = _finite_number(
        _required(raw, "cumulative_notional", context), f"{context}.cumulative_notional"
    )
    if max_move < 0 or notional < 0:
        raise ValueError(f"{context} move and notional must be non-negative")
    return EmpiricalLabel(
        symbol=symbol,
        classification=classification,
        direction=direction,
        eligible_at_utc=eligible_at,
        max_abs_move_pct=max_move,
        cumulative_notional=notional,
        reason_code=_nonempty_string(
            _required(raw, "reason_code", context), f"{context}.reason_code"
        ),
        rationale=_nonempty_string(_required(raw, "rationale", context), f"{context}.rationale"),
    )


def load_empirical_manifest(path: Path | str) -> EmpiricalManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("empirical manifest root must be an object")
    _exact_fields(payload, MANIFEST_FIELDS, "empirical manifest")
    schema = _required(payload, "schema_version", "empirical manifest")
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != EMPIRICAL_SCHEMA_VERSION
    ):
        raise ValueError(
            f"unsupported empirical schema version {schema!r}; expected {EMPIRICAL_SCHEMA_VERSION}"
        )
    if payload.get("status") != "locked":
        raise ValueError("empirical manifest status must be 'locked'")
    session_raw = _nonempty_string(
        _required(payload, "session", "empirical manifest"), "empirical manifest.session"
    )
    try:
        session = date.fromisoformat(session_raw)
    except ValueError as exc:
        raise ValueError("empirical manifest.session must be an ISO date") from exc
    method = _required(payload, "label_method", "empirical manifest")
    if method not in ALLOWED_LABEL_METHODS:
        raise ValueError(
            "empirical manifest.label_method must be one of "
            f"{sorted(ALLOWED_LABEL_METHODS)}"
        )
    blinded = _required(payload, "blinded_to_observer_output", "empirical manifest")
    if blinded is not True:
        raise ValueError("empirical manifest must be blinded_to_observer_output=true")
    eligibility = _required(payload, "eligibility", "empirical manifest")
    if not isinstance(eligibility, dict):
        raise ValueError("empirical manifest.eligibility must be an object")
    _exact_fields(eligibility, ELIGIBILITY_FIELDS, "empirical manifest.eligibility")
    move_threshold = _finite_number(
        _required(eligibility, "move_pct", "eligibility"), "eligibility.move_pct"
    )
    min_notional = _finite_number(
        _required(eligibility, "min_cumulative_notional", "eligibility"),
        "eligibility.min_cumulative_notional",
    )
    persistence = _required(eligibility, "persistence_bars", "eligibility")
    if move_threshold <= 0 or min_notional <= 0:
        raise ValueError("empirical eligibility thresholds must be positive")
    if not isinstance(persistence, int) or isinstance(persistence, bool) or persistence < 2:
        raise ValueError("eligibility.persistence_bars must be an integer >= 2")
    artifacts_raw = _required(payload, "artifacts", "empirical manifest")
    labels_raw = _required(payload, "labels", "empirical manifest")
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise ValueError("empirical manifest.artifacts must be a non-empty list")
    if not isinstance(labels_raw, list) or not labels_raw:
        raise ValueError("empirical manifest.labels must be a non-empty list")
    artifacts = tuple(_parse_artifact(raw, index) for index, raw in enumerate(artifacts_raw))
    labels = tuple(_parse_label(raw, index, session) for index, raw in enumerate(labels_raw))
    symbols = [label.symbol for label in labels]
    if len(symbols) != len(set(symbols)):
        raise ValueError("empirical manifest label symbols must be unique")
    if method == "multi_provider_reconciliation":
        providers = {artifact.provider.lower() for artifact in artifacts}
        if len(providers) < 2:
            raise ValueError("multi_provider_reconciliation requires at least two providers")
    window = _session_window(session)
    if window is None:
        raise ValueError("empirical manifest.session must be an XNYS trading session")
    _, expected_end = window
    created_at = _aware_datetime(
        _required(payload, "created_at_utc", "empirical manifest"),
        "empirical manifest.created_at_utc",
    )
    if created_at < expected_end:
        raise ValueError("empirical manifest cannot be locked before the session window ends")
    if any(artifact.acquired_at_utc < expected_end for artifact in artifacts):
        raise ValueError("empirical artifacts must be acquired after the session window ends")
    if any(artifact.acquired_at_utc > created_at for artifact in artifacts):
        raise ValueError("empirical artifacts cannot be acquired after the manifest is locked")
    for label in labels:
        if label.classification == "eligible" and (
            label.max_abs_move_pct < move_threshold
            or label.cumulative_notional < min_notional
        ):
            raise ValueError(
                f"eligible label {label.symbol} does not meet the declared move/notional policy"
            )
    return EmpiricalManifest(
        manifest_version=_nonempty_string(
            _required(payload, "manifest_version", "empirical manifest"),
            "empirical manifest.manifest_version",
        ),
        session=session,
        created_at_utc=created_at,
        labeler=_nonempty_string(
            _required(payload, "labeler", "empirical manifest"), "empirical manifest.labeler"
        ),
        label_method=method,
        blinded_to_observer_output=blinded,
        move_threshold_pct=move_threshold,
        min_cumulative_notional=min_notional,
        persistence_bars=persistence,
        artifacts=artifacts,
        labels=labels,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _session_window(session: date) -> tuple[datetime, datetime] | None:
    if not CALENDAR.is_session(session):
        return None
    start = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    end = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return start, end + FINAL_BAR_GRACE


def _unique_strings(rows: Iterable[sqlite3.Row], key: str) -> tuple[str, ...]:
    return tuple(sorted({str(row[key]) for row in rows if row[key] is not None}))


def _issue(issues: list[AuditIssue], code: str, detail: str, severity: str = "blocker") -> None:
    issues.append(AuditIssue(code=code, severity=severity, detail=detail))


def load_catalyst_ledger_evidence(
    journal_path: Path | str,
    session: date,
) -> CatalystLedgerEvidence:
    """Read the independent scheduled-event ledger without importing journal I/O."""
    conn = connect_readonly(journal_path)
    conn.row_factory = sqlite3.Row
    try:
        quick_check = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if quick_check != ["ok"]:
            raise ValueError(f"journal database quick_check failed: {quick_check!r}")
        required = {"event_windows", "event_ingestion_runs"}
        present = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = required - present
        if missing:
            raise ValueError(f"journal database is missing tables: {sorted(missing)}")
        attempts = conn.execute(
            """
            SELECT * FROM event_ingestion_runs
            WHERE provider='nasdaq_earnings' AND kind='earnings'
              AND report_date=? AND universe_scope='market'
            ORDER BY completed_at,id
            """,
            (session.isoformat(),),
        ).fetchall()
        symbols = tuple(
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT symbol FROM event_windows
                WHERE kind='earnings' AND source='nasdaq_earnings'
                  AND event_date=? AND event_timing='after-hours'
                  AND symbol IS NOT NULL
                ORDER BY symbol
                """,
                (session.isoformat(),),
            ).fetchall()
        )
    finally:
        conn.close()
    if not attempts:
        return CatalystLedgerEvidence(
            status="missing",
            attempts=0,
            latest_completed_at_utc=None,
            requested_symbols=None,
            fetched_events=None,
            matched_events=None,
            windows_created=None,
            code_version=None,
            run_mode=None,
            run_id=None,
            error=None,
            expected_symbols=symbols,
        )
    latest = attempts[-1]
    return CatalystLedgerEvidence(
        status=latest["status"],
        attempts=len(attempts),
        latest_completed_at_utc=_aware_datetime(
            latest["completed_at"], "event_ingestion_runs.completed_at"
        ).isoformat(),
        requested_symbols=latest["requested_symbols"],
        fetched_events=latest["fetched_events"],
        matched_events=latest["matched_events"],
        windows_created=latest["windows_created"],
        code_version=latest["code_version"],
        run_mode=latest["run_mode"],
        run_id=latest["run_id"],
        error=latest["error"],
        expected_symbols=symbols,
    )


def _empirical_metrics(
    manifest: EmpiricalManifest | None,
    session: date,
    scheduled_symbols: set[str],
    candidates: list[sqlite3.Row],
    issues: list[AuditIssue],
) -> EmpiricalMetrics:
    if manifest is None:
        _issue(
            issues,
            "EMPIRICAL_MANIFEST_MISSING",
            "no independently locked empirical manifest was supplied",
            severity="warning",
        )
        return EmpiricalMetrics("NOT_PROVIDED", None, 0, 0, 0, 0, 0, 0, None, None, 0, None, None)
    if manifest.session != session:
        _issue(
            issues,
            "EMPIRICAL_SESSION_MISMATCH",
            f"manifest session {manifest.session} does not match audit session {session}",
        )
    labels_by_symbol = {label.symbol: label for label in manifest.labels}
    missing = scheduled_symbols - labels_by_symbol.keys()
    extra = labels_by_symbol.keys() - scheduled_symbols
    if missing:
        _issue(issues, "EMPIRICAL_LABELS_MISSING", f"missing labels for {sorted(missing)}")
    if extra:
        _issue(issues, "EMPIRICAL_LABELS_EXTRA", f"labels are not scheduled: {sorted(extra)}")
    candidates_by_symbol: dict[str, list[sqlite3.Row]] = {}
    for row in candidates:
        candidates_by_symbol.setdefault(row["symbol"], []).append(row)
    tp = fp = tn = fn = direction_mismatches = 0
    latencies: list[float] = []
    definitive = ambiguous = 0
    for symbol in sorted(scheduled_symbols & labels_by_symbol.keys()):
        label = labels_by_symbol[symbol]
        observed = candidates_by_symbol.get(symbol, [])
        if label.classification == "ambiguous":
            ambiguous += 1
            continue
        definitive += 1
        if label.classification == "eligible":
            if observed:
                tp += 1
                first = min(observed, key=lambda row: row["first_detected_at"])
                if first["direction"] != label.direction:
                    direction_mismatches += 1
                detected_at = _aware_datetime(
                    first["first_detected_at"], "candidate.first_detected_at"
                )
                if label.eligible_at_utc is not None:
                    latency = (detected_at - label.eligible_at_utc).total_seconds()
                    latencies.append(latency)
                    if latency < 0:
                        _issue(
                            issues,
                            "EMPIRICAL_NEGATIVE_DETECTION_LATENCY",
                            f"{symbol} was detected {abs(latency):.0f}s before label eligibility",
                        )
            else:
                fn += 1
        elif observed:
            fp += 1
        else:
            tn += 1
    status = "COMPLETE"
    if missing or extra or manifest.session != session or ambiguous:
        status = "INCOMPLETE"
    if ambiguous:
        _issue(
            issues,
            "EMPIRICAL_AMBIGUOUS_LABELS",
            f"{ambiguous} scheduled symbols remain ambiguous",
        )
    if fp:
        _issue(issues, "EMPIRICAL_FALSE_POSITIVES", f"observer produced {fp} false positives")
    if fn:
        _issue(issues, "EMPIRICAL_FALSE_NEGATIVES", f"observer missed {fn} eligible reactions")
    if direction_mismatches:
        _issue(
            issues,
            "EMPIRICAL_DIRECTION_MISMATCH",
            f"observer direction disagreed for {direction_mismatches} eligible reactions",
        )
    return EmpiricalMetrics(
        status=status,
        manifest_version=manifest.manifest_version,
        definitive_labels=definitive,
        ambiguous_labels=ambiguous,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=_ratio(tp, tp + fp),
        recall=_ratio(tp, tp + fn),
        direction_mismatches=direction_mismatches,
        mean_detection_latency_seconds=(sum(latencies) / len(latencies) if latencies else None),
        max_detection_latency_seconds=(max(latencies) if latencies else None),
    )


def audit_session(
    conn: sqlite3.Connection,
    session: date,
    *,
    database: str = "<connection>",
    manifest: EmpiricalManifest | None = None,
    catalyst_ledger: CatalystLedgerEvidence | None = None,
    audit_code_version: str | None = None,
) -> DailyAuditReport:
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
        required_tables = {"postmarket_ticks", "postmarket_observations", "postmarket_candidates"}
        present = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing_tables = required_tables - present
        if missing_tables:
            raise ValueError(f"shadow database is missing tables: {sorted(missing_tables)}")
        ticks = conn.execute(
            "SELECT * FROM postmarket_ticks WHERE session=? ORDER BY tick_utc,tick_id",
            (session.isoformat(),),
        ).fetchall()
        observations = conn.execute(
            """
            SELECT o.*,t.tick_utc,t.scheduled_symbols,t.evaluated_symbols,
                   t.candidate_observations,t.error_count,t.invariant_ok,
                   t.data_feed AS tick_data_feed,
                   t.market_data_provider AS tick_market_data_provider,
                   t.bar_timeframe AS tick_bar_timeframe,
                   t.catalyst_source AS tick_catalyst_source
            FROM postmarket_observations o
            JOIN postmarket_ticks t ON t.tick_id=o.tick_id
            WHERE t.session=? ORDER BY t.tick_utc,o.symbol
            """,
            (session.isoformat(),),
        ).fetchall()
        candidates = conn.execute(
            "SELECT * FROM postmarket_candidates WHERE session=? ORDER BY first_detected_at,symbol",
            (session.isoformat(),),
        ).fetchall()
    finally:
        conn.row_factory = original_row_factory

    window = _session_window(session)
    if window is None:
        _issue(issues, "NON_TRADING_SESSION", f"{session} is not an XNYS session")
    expected_start, expected_end = window if window else (None, None)
    tick_times = [_aware_datetime(row["tick_utc"], "postmarket_ticks.tick_utc") for row in ticks]
    completed_times = [
        _aware_datetime(row["completed_utc"], "postmarket_ticks.completed_utc") for row in ticks
    ]
    if not ticks:
        _issue(issues, "NO_TICKS", "no postmarket shadow ticks exist for the session")
    if len(tick_times) != len(set(tick_times)):
        _issue(issues, "DUPLICATE_TICK_TIMESTAMPS", "tick timestamps are not unique")
    if tick_times != sorted(tick_times):
        _issue(issues, "OUT_OF_ORDER_TICKS", "tick timestamps are out of order")
    if any(completed < tick for tick, completed in zip(tick_times, completed_times)):
        _issue(issues, "NEGATIVE_PROCESSING_LATENCY", "a tick completed before it started")
    gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(tick_times, tick_times[1:])
    ]
    max_gap = max(gaps) if gaps else None
    if max_gap is not None and max_gap > MAX_TICK_GAP_SECONDS:
        _issue(issues, "TICK_GAP", f"maximum tick gap was {max_gap:.0f}s")
    if tick_times and expected_start is not None and expected_end is not None:
        if tick_times[0] > expected_start + timedelta(seconds=START_GRACE_SECONDS):
            delay = (tick_times[0] - expected_start).total_seconds()
            _issue(issues, "COVERAGE_STARTED_LATE", f"first tick was {delay:.0f}s after close")
        if tick_times[-1] < expected_end - timedelta(seconds=END_GRACE_SECONDS):
            early = (expected_end - tick_times[-1]).total_seconds()
            _issue(
                issues,
                "COVERAGE_ENDED_EARLY",
                f"final tick was {early:.0f}s before audit window end",
            )
        if tick_times[0] < expected_start - timedelta(seconds=START_GRACE_SECONDS):
            _issue(issues, "PREWINDOW_TICK", "a tick precedes the postmarket window")
        if tick_times[-1] > expected_end + timedelta(seconds=END_GRACE_SECONDS):
            _issue(issues, "POSTWINDOW_TICK", "a tick follows the postmarket window")
        duration = (expected_end - expected_start).total_seconds()
        covered = max(
            0.0,
            (
                min(tick_times[-1], expected_end) - max(tick_times[0], expected_start)
            ).total_seconds(),
        )
        coverage_pct = round(100 * covered / duration, 2) if duration else 0.0
    else:
        coverage_pct = 0.0

    tick_ids = [row["tick_id"] for row in ticks]
    observation_counts: dict[int, int] = {tick_id: 0 for tick_id in tick_ids}
    observation_candidates: dict[int, int] = {tick_id: 0 for tick_id in tick_ids}
    observation_errors: dict[int, int] = {tick_id: 0 for tick_id in tick_ids}
    symbols_by_tick: dict[int, set[str]] = {tick_id: set() for tick_id in tick_ids}
    for row in observations:
        tick_id = row["tick_id"]
        observation_counts[tick_id] += 1
        observation_candidates[tick_id] += int(row["outcome"] == "CANDIDATE")
        observation_errors[tick_id] += int(row["outcome"] == "FETCH_ERROR")
        symbols_by_tick[tick_id].add(row["symbol"])
        if row["event_date"] != session.isoformat():
            _issue(issues, "EVENT_DATE_MISMATCH", f"{row['symbol']} has {row['event_date']}")
        provenance_pairs = (
            ("data_feed", "tick_data_feed"),
            ("market_data_provider", "tick_market_data_provider"),
            ("bar_timeframe", "tick_bar_timeframe"),
            ("catalyst_source", "tick_catalyst_source"),
        )
        if any(
            row[observation_key] != row[tick_key]
            for observation_key, tick_key in provenance_pairs
        ):
            _issue(
                issues,
                "OBSERVATION_PROVENANCE_MISMATCH",
                f"{row['symbol']} provenance disagrees with tick {tick_id}",
            )
    for row in ticks:
        tick_id = row["tick_id"]
        if not row["invariant_ok"] or row["scheduled_symbols"] != row["evaluated_symbols"]:
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
    symbol_sets = {tuple(sorted(symbols)) for symbols in symbols_by_tick.values()}
    if len(symbol_sets) > 1:
        _issue(
            issues,
            "SCHEDULE_SYMBOL_DRIFT",
            "scheduled symbol membership changed during session",
        )
    scheduled_counts = {row["scheduled_symbols"] for row in ticks}
    if len(scheduled_counts) > 1:
        _issue(issues, "SCHEDULE_COUNT_DRIFT", "scheduled symbol count changed during session")
    scheduled_symbol_list = tuple(sorted(set().union(*symbols_by_tick.values()))) if ticks else ()
    scheduled_count = next(iter(scheduled_counts)) if len(scheduled_counts) == 1 else None
    if scheduled_count == 0:
        _issue(
            issues,
            "NO_SCHEDULED_SYMBOLS",
            "observer ran but the session had no scheduled symbols",
            severity="warning",
        )
    if catalyst_ledger is None:
        _issue(
            issues,
            "CATALYST_LEDGER_NOT_PROVIDED",
            "scheduled-event ledger was not supplied for end-to-end reconciliation",
            severity="warning",
        )
    else:
        if catalyst_ledger.status != "success":
            _issue(
                issues,
                "CATALYST_INGESTION_UNVERIFIED",
                f"latest market-wide earnings ingestion status is {catalyst_ledger.status}",
            )
        elif (
            catalyst_ledger.requested_symbols is None
            or catalyst_ledger.requested_symbols <= 0
            or catalyst_ledger.fetched_events is None
            or catalyst_ledger.matched_events is None
            or catalyst_ledger.windows_created is None
            or catalyst_ledger.code_version in {None, "", "unknown"}
            or catalyst_ledger.fetched_events < catalyst_ledger.matched_events
            or len(catalyst_ledger.expected_symbols) > catalyst_ledger.matched_events
        ):
            _issue(
                issues,
                "CATALYST_PROVENANCE_INCOMPLETE",
                "successful ingestion lacks coherent universe/count/revision provenance",
            )
        expected = set(catalyst_ledger.expected_symbols)
        observed = set(scheduled_symbol_list)
        if expected != observed:
            _issue(
                issues,
                "CATALYST_FUNNEL_MISMATCH",
                f"missing_from_observer={sorted(expected - observed)} "
                f"unexpected_in_observer={sorted(observed - expected)}",
            )
    if any(row["error_count"] for row in ticks):
        _issue(issues, "FETCH_ERRORS", "one or more symbol evaluations ended in FETCH_ERROR")
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
        "OBSERVER_VERSION_DRIFT": {row["observer_version"] for row in ticks},
        "CODE_VERSION_DRIFT": {row["code_version"] for row in ticks},
        "DATA_FEED_DRIFT": {row["data_feed"] for row in ticks},
        "PROVIDER_DRIFT": {row["market_data_provider"] for row in ticks},
        "BAR_TIMEFRAME_DRIFT": {row["bar_timeframe"] for row in ticks},
        "CATALYST_SOURCE_DRIFT": {row["catalyst_source"] for row in ticks},
        "THRESHOLD_DRIFT": {row["thresholds_json"] for row in ticks},
    }
    for code, values in metadata_checks.items():
        if len(values) > 1:
            _issue(issues, code, f"session contains {len(values)} distinct values")
    if any(row["code_version"] in {None, "", "unknown"} for row in ticks):
        _issue(issues, "UNKNOWN_CODE_VERSION", "one or more ticks lacks an attributable revision")
    threshold_payloads: list[dict[str, Any]] = []
    for raw in {row["thresholds_json"] for row in ticks}:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            _issue(issues, "MALFORMED_THRESHOLD_SNAPSHOT", repr(raw))
            continue
        if not isinstance(parsed, dict):
            _issue(issues, "MALFORMED_THRESHOLD_SNAPSHOT", repr(raw))
            continue
        threshold_payloads.append(parsed)
    if manifest is not None and len(threshold_payloads) == 1:
        snapshot = threshold_payloads[0]
        empirical_policy = {
            "move_pct": manifest.move_threshold_pct,
            "min_cumulative_notional": manifest.min_cumulative_notional,
            "persistence_bars": manifest.persistence_bars,
        }
        if any(snapshot.get(key) != value for key, value in empirical_policy.items()):
            _issue(
                issues,
                "EMPIRICAL_POLICY_MISMATCH",
                "manifest eligibility policy differs from the observer threshold snapshot",
            )
    new_candidates_recorded = sum(row["new_candidates"] for row in ticks)
    if new_candidates_recorded != len(candidates):
        _issue(
            issues,
            "CANDIDATE_LEDGER_MISMATCH",
            f"ticks recorded {new_candidates_recorded} new candidates but ledger "
            f"has {len(candidates)}",
        )
    candidate_directions: dict[str, set[str]] = {}
    for row in candidates:
        candidate_directions.setdefault(row["symbol"], set()).add(row["direction"])
        if row["event_date"] != session.isoformat():
            _issue(
                issues,
                "CANDIDATE_EVENT_DATE_MISMATCH",
                f"{row['symbol']} has {row['event_date']}",
            )
        if row["symbol"] not in scheduled_symbol_list:
            _issue(
                issues,
                "UNSCHEDULED_CANDIDATE",
                f"{row['symbol']} was not in the observed funnel",
            )
        candidate_metadata = (
            ("code_version", {tick["code_version"] for tick in ticks}),
            ("data_feed", {tick["data_feed"] for tick in ticks}),
            (
                "market_data_provider",
                {tick["market_data_provider"] for tick in ticks},
            ),
            ("bar_timeframe", {tick["bar_timeframe"] for tick in ticks}),
            ("catalyst_source", {tick["catalyst_source"] for tick in ticks}),
        )
        if any(row[key] not in allowed for key, allowed in candidate_metadata):
            _issue(
                issues,
                "CANDIDATE_PROVENANCE_MISMATCH",
                f"{row['symbol']} provenance does not match any session tick",
            )
    flipped = sorted(
        symbol
        for symbol, directions in candidate_directions.items()
        if len(directions) > 1
    )
    if flipped:
        _issue(issues, "CANDIDATE_DIRECTION_CHANGED", f"direction changed for {flipped}")

    label_universe = (
        set(catalyst_ledger.expected_symbols)
        if catalyst_ledger is not None and catalyst_ledger.status == "success"
        else set(scheduled_symbol_list)
    )
    empirical = _empirical_metrics(manifest, session, label_universe, candidates, issues)
    blocking_issues = tuple(issue for issue in issues if issue.severity == "blocker")
    operational_codes = {
        issue.code for issue in blocking_issues if not issue.code.startswith("EMPIRICAL_")
    }
    operational_clean = not operational_codes
    session_evidence_eligible = (
        operational_clean
        and not blocking_issues
        and catalyst_ledger is not None
        and catalyst_ledger.status == "success"
        and audit_code_version not in {None, "", "unknown"}
        and empirical.status == "COMPLETE"
        and empirical.ambiguous_labels == 0
        and empirical.direction_mismatches == 0
        and empirical.false_positives == 0
        and empirical.false_negatives == 0
    )
    operational = OperationalMetrics(
        ticks=len(ticks),
        first_tick_utc=tick_times[0].isoformat() if tick_times else None,
        final_tick_utc=tick_times[-1].isoformat() if tick_times else None,
        expected_start_utc=expected_start.isoformat() if expected_start else None,
        expected_end_utc=expected_end.isoformat() if expected_end else None,
        window_coverage_pct=coverage_pct,
        max_tick_gap_seconds=max_gap,
        scheduled_symbols=scheduled_count,
        observed_symbols=len(scheduled_symbol_list),
        candidate_observations=sum(row["candidate_observations"] for row in ticks),
        unique_candidates=len(candidates),
        new_candidates_recorded=new_candidates_recorded,
        fetch_errors=sum(row["error_count"] for row in ticks),
        failed_invariants=sum(not row["invariant_ok"] for row in ticks),
        average_latency_ms=(sum(latencies) / len(latencies) if latencies else None),
        max_latency_ms=max(latencies) if latencies else None,
        observer_versions=tuple(sorted({row["observer_version"] for row in ticks})),
        code_versions=_unique_strings(ticks, "code_version"),
        data_feeds=_unique_strings(ticks, "data_feed"),
        market_data_providers=_unique_strings(ticks, "market_data_provider"),
        threshold_snapshots=len({row["thresholds_json"] for row in ticks}),
    )
    return DailyAuditReport(
        audit_version=AUDIT_VERSION,
        audit_code_version=audit_code_version,
        session=session.isoformat(),
        database=database,
        operational_clean=operational_clean,
        session_evidence_eligible=session_evidence_eligible,
        scheduled_symbol_list=scheduled_symbol_list,
        catalyst_ledger=catalyst_ledger,
        operational=operational,
        empirical=empirical,
        issues=tuple(issues),
    )


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def report_json(report: DailyAuditReport, *, compact: bool = False) -> str:
    return json.dumps(
        asdict(report),
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
        sort_keys=True,
    )


def write_report_atomic(path: Path | str, report: DailyAuditReport) -> bool:
    """Create one immutable report; return False when it already exists."""
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
        if destination.exists():
            tmp_path.unlink()
            return False
        os.replace(tmp_path, destination)
        return True
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_completed_operational_audits(
    db_path: Path | str,
    output_dir: Path | str,
    *,
    now: datetime,
    journal_path: Path | str | None = None,
    audit_code_version: str | None = None,
) -> tuple[DailyAuditReport, ...]:
    """Write missing reports only after each session's full window closes."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    database = Path(db_path)
    if not database.exists():
        return ()
    conn = connect_readonly(database)
    try:
        sessions = [
            date.fromisoformat(row[0])
            for row in conn.execute(
                "SELECT DISTINCT session FROM postmarket_ticks ORDER BY session"
            ).fetchall()
        ]
        reports: list[DailyAuditReport] = []
        for session in sessions:
            window = _session_window(session)
            if window is None or now.astimezone(timezone.utc) <= window[1]:
                continue
            destination = (
                Path(output_dir)
                / f"postmarket_audit_{session.isoformat()}_v{AUDIT_VERSION}.json"
            )
            if destination.exists():
                existing = json.loads(destination.read_text(encoding="utf-8"))
                if (
                    existing.get("session") != session.isoformat()
                    or existing.get("audit_version") != AUDIT_VERSION
                ):
                    raise ValueError(f"existing audit report is inconsistent: {destination}")
                continue
            report = audit_session(
                conn,
                session,
                database=str(database),
                catalyst_ledger=(
                    load_catalyst_ledger_evidence(journal_path, session)
                    if journal_path is not None
                    else None
                ),
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
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--audit-code-version", default=os.environ.get("GIT_SHA"))
    parser.add_argument("--session", required=True, type=date.fromisoformat)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_empirical_manifest(args.manifest) if args.manifest else None
        catalyst_ledger = (
            load_catalyst_ledger_evidence(args.journal, args.session)
            if args.journal
            else None
        )
        conn = connect_readonly(args.db)
        try:
            report = audit_session(
                conn,
                args.session,
                database=str(args.db),
                manifest=manifest,
                catalyst_ledger=catalyst_ledger,
                audit_code_version=args.audit_code_version,
            )
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(report_json(report, compact=args.compact))
    if not report.operational_clean:
        return 1
    if args.manifest is not None and not report.session_evidence_eligible:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
