"""Immutable daily audit for the customer-readiness dry-run router.

The audit reads only the append-only dry-run ledger.  It does not trust a
heartbeat, contact a provider, render an alert, enqueue a message, or write to
the database.  Its only optional write is an exclusive, read-only JSON
artifact after the complete exchange-calendar postmarket window has ended.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as ecals


AUDIT_VERSION = 2
ROUTER_VERSION = 1
EXPECTED_POLL_SECONDS = 60
FINAL_BAR_GRACE = timedelta(minutes=5)
AUDIT_SETTLE_GRACE = timedelta(seconds=90)
MAX_SCHEDULED_LAG_MS = 30_000
MAX_TICK_LATENCY_MS = 10_000
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "postmarket_shadow.db"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "postmarket_audits"
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")


@dataclass(frozen=True)
class AuditIssue:
    code: str
    severity: str
    detail: str


@dataclass(frozen=True)
class DryRunAuditMetrics:
    expected_ticks: int
    observed_ticks: int
    coverage_pct: float
    first_scheduled_at_utc: str | None
    final_scheduled_at_utc: str | None
    expected_start_utc: str | None
    expected_end_utc: str | None
    missing_scheduled_slots_utc: tuple[str, ...]
    max_tick_gap_seconds: int | None
    average_scheduled_lag_ms: float | None
    max_scheduled_lag_ms: int | None
    average_latency_ms: float | None
    max_latency_ms: int | None
    degraded_ticks: int
    failed_invariants: int
    input_candidates: int
    eligible_candidates: int
    suppressed_candidates: int
    decisions_written: int
    duplicate_decisions: int
    linked_decisions: int
    orphan_routes: int
    duplicate_eligible_identities: int
    conservation_failures: int
    link_failures: int
    identity_failures: int
    input_digest_failures: int
    decision_attribution_failures: int
    actionability_failures: int
    calibrated_routes: int
    calibration_link_failures: int
    calibration_attribution_failures: int
    rank_missing_after_first_bar: int
    operational_reason_counts: dict[str, int]
    suppression_reason_counts: dict[str, int]
    policy_sha256s: tuple[str, ...]
    authorization_sha256s: tuple[str, ...]
    runtime_router_revisions: tuple[str, ...]
    router_versions: tuple[int, ...]
    calibration_model_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class DailyDryRunAuditReport:
    audit_version: int
    audit_code_version: str | None
    session: str
    database: str
    created_at_utc: str
    source_evidence_sha256: str
    operational_clean: bool
    session_evidence_eligible: bool
    metrics: DryRunAuditMetrics
    issues: tuple[AuditIssue, ...]


def _issue(issues: list[AuditIssue], code: str, detail: str) -> None:
    if not any(item.code == code and item.detail == detail for item in issues):
        issues.append(AuditIssue(code=code, severity="blocker", detail=detail))


def _aware(value: str, context: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _json_strings(raw: str, context: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} must be valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{context} must be an array of strings")
    return tuple(value)


def _session_window(session: date) -> tuple[datetime, datetime] | None:
    if not CALENDAR.is_session(session):
        return None
    start = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    final_bar_close = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(
        timezone.utc
    )
    return start, final_bar_close + FINAL_BAR_GRACE


def _expected_slots(session: date) -> tuple[datetime, ...]:
    window = _session_window(session)
    if window is None:
        return ()
    start, end = window
    # ``end`` is the exclusive processing boundary.  A supervisor waking a
    # fraction after that instant is outside the active window, so the final
    # scheduled slot is one interval before it.
    count = int((end - start).total_seconds() // EXPECTED_POLL_SECONDS)
    return tuple(start + timedelta(seconds=index * EXPECTED_POLL_SECONDS) for index in range(count))


def _audit_ready_at(session: date) -> datetime | None:
    window = _session_window(session)
    return window[1] + AUDIT_SETTLE_GRACE if window else None


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_digest(
    ticks: list[sqlite3.Row],
    links: list[sqlite3.Row],
    routes: list[sqlite3.Row],
    calibrations: list[sqlite3.Row],
) -> str:
    payload = {
        "ticks": [dict(row) for row in ticks],
        "links": [dict(row) for row in links],
        "routes": [dict(row) for row in routes],
        "calibrations": [dict(row) for row in calibrations],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def audit_dry_run_session(
    conn: sqlite3.Connection,
    session: date,
    *,
    database: str,
    audit_code_version: str | None,
    created_at: datetime | None = None,
) -> DailyDryRunAuditReport:
    """Reconcile one completed session from persisted evidence only."""
    created_value = created_at or datetime.now(timezone.utc)
    if created_value.tzinfo is None or created_value.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    created = created_value.astimezone(timezone.utc)
    issues: list[AuditIssue] = []
    required = {
        "postmarket_delivery_dry_runs",
        "postmarket_delivery_dry_run_ticks",
        "postmarket_delivery_dry_run_tick_decisions",
        "postmarket_delivery_dry_run_calibrations",
        "postmarket_rank_calibration_projections",
        "postmarket_rank_calibration_runs",
        "postmarket_rank_calibrators",
    }
    missing_tables = sorted(required - _tables(conn))
    if missing_tables:
        raise ValueError(f"dry-run audit schema is incomplete: {missing_tables}")
    ticks = conn.execute(
        "SELECT * FROM postmarket_delivery_dry_run_ticks WHERE session=? "
        "ORDER BY scheduled_at_utc,tick_id",
        (session.isoformat(),),
    ).fetchall()
    links = conn.execute(
        """
        SELECT l.tick_id,l.route_id
        FROM postmarket_delivery_dry_run_tick_decisions l
        JOIN postmarket_delivery_dry_run_ticks t ON t.tick_id=l.tick_id
        WHERE t.session=? ORDER BY l.tick_id,l.route_id
        """,
        (session.isoformat(),),
    ).fetchall()
    routes = conn.execute(
        "SELECT * FROM postmarket_delivery_dry_runs WHERE session=? ORDER BY route_id",
        (session.isoformat(),),
    ).fetchall()
    calibrations = conn.execute(
        """
        SELECT c.*
        FROM postmarket_delivery_dry_run_calibrations c
        JOIN postmarket_delivery_dry_runs d ON d.route_id=c.route_id
        WHERE d.session=? ORDER BY c.route_id
        """,
        (session.isoformat(),),
    ).fetchall()
    expected_slots = _expected_slots(session)
    window = _session_window(session)
    if window is None:
        _issue(issues, "NON_TRADING_SESSION", f"{session} is not an XNYS session")
    if not ticks:
        _issue(issues, "NO_TICKS", "no customer dry-run ticks exist for the session")

    scheduled: list[datetime] = []
    started: list[datetime] = []
    completed: list[datetime] = []
    operational_reasons: Counter[str] = Counter()
    bad_json_ticks = 0
    for row in ticks:
        tick_id = int(row["tick_id"])
        try:
            scheduled_at = _aware(row["scheduled_at_utc"], f"tick {tick_id} scheduled")
            started_at = _aware(row["started_at_utc"], f"tick {tick_id} started")
            completed_at = _aware(row["completed_at_utc"], f"tick {tick_id} completed")
            reasons = _json_strings(
                row["operational_reasons_json"], f"tick {tick_id} operational reasons"
            )
        except ValueError as exc:
            bad_json_ticks += 1
            _issue(issues, "TICK_EVIDENCE_INVALID", str(exc))
            continue
        scheduled.append(scheduled_at)
        started.append(started_at)
        completed.append(completed_at)
        operational_reasons.update(reasons)
        expected_lag = round((started_at - scheduled_at).total_seconds() * 1000)
        expected_latency = round((completed_at - started_at).total_seconds() * 1000)
        if expected_lag < 0 or int(row["scheduled_lag_ms"]) != expected_lag:
            _issue(issues, "SCHEDULED_LAG_MISMATCH", f"tick {tick_id} lag is inconsistent")
        if expected_latency < 0 or abs(int(row["latency_ms"]) - expected_latency) > 100:
            _issue(issues, "TICK_LATENCY_MISMATCH", f"tick {tick_id} latency is inconsistent")

    if len(scheduled) != len(set(scheduled)):
        _issue(issues, "DUPLICATE_SCHEDULED_SLOTS", "scheduled timestamps are not unique")
    if scheduled != sorted(scheduled):
        _issue(issues, "OUT_OF_ORDER_TICKS", "scheduled timestamps are not ordered")
    expected_set = set(expected_slots)
    observed_set = set(scheduled)
    off_grid = sorted(observed_set - expected_set)
    if off_grid:
        _issue(issues, "OFF_GRID_TICKS", f"{len(off_grid)} ticks are outside the expected schedule")
    missing_slots = tuple(sorted(expected_set - observed_set))
    if missing_slots:
        _issue(issues, "TICK_GAP", f"{len(missing_slots)} expected scheduled ticks are missing")
    gaps = [
        round((current - previous).total_seconds())
        for previous, current in zip(scheduled, scheduled[1:])
    ]
    max_gap = max(gaps) if gaps else None
    if max_gap is not None and max_gap > EXPECTED_POLL_SECONDS:
        _issue(issues, "TICK_GAP", f"maximum scheduled tick gap was {max_gap}s")

    links_by_tick = Counter(int(row["tick_id"]) for row in links)
    route_ids_by_tick: dict[int, list[int]] = {}
    for row in links:
        route_ids_by_tick.setdefault(int(row["tick_id"]), []).append(int(row["route_id"]))
    route_by_id = {int(row["route_id"]): row for row in routes}
    linked_route_ids = {int(row["route_id"]) for row in links}
    tick_by_id = {int(row["tick_id"]): row for row in ticks}
    ordered_tick_ids = [int(row["tick_id"]) for row in ticks]
    tick_route_ids = {
        tick_id: route_ids_by_tick.get(tick_id, []) for tick_id in ordered_tick_ids
    }
    first_link_tick: dict[int, int] = {}
    for tick_id in ordered_tick_ids:
        for route_id in tick_route_ids[tick_id]:
            first_link_tick.setdefault(route_id, tick_id)
    conservation_failures = 0
    link_failures = 0
    identity_failures = 0
    input_digest_failures = 0
    decision_attribution_failures = 0
    for tick in ticks:
        tick_id = int(tick["tick_id"])
        inputs = int(tick["input_candidates"])
        if (
            int(tick["eligible_candidates"]) + int(tick["suppressed_candidates"]) != inputs
            or int(tick["decisions_written"]) + int(tick["duplicate_decisions"]) != inputs
        ):
            conservation_failures += 1
        if links_by_tick[tick_id] != inputs:
            link_failures += 1
        linked_routes = [
            route_by_id[route_id]
            for route_id in tick_route_ids[tick_id]
            if route_id in route_by_id
        ]
        derived_eligible = sum(
            route["decision"] == "ELIGIBLE_FOR_DRY_RUN" for route in linked_routes
        )
        derived_written = sum(
            first_link_tick.get(route_id) == tick_id
            for route_id in tick_route_ids[tick_id]
        )
        if (
            derived_eligible != int(tick["eligible_candidates"])
            or len(linked_routes) - derived_eligible
            != int(tick["suppressed_candidates"])
            or derived_written != int(tick["decisions_written"])
            or len(linked_routes) - derived_written
            != int(tick["duplicate_decisions"])
        ):
            decision_attribution_failures += 1
        try:
            reasons = _json_strings(
                tick["operational_reasons_json"],
                f"tick {tick_id} operational reasons",
            )
        except ValueError:
            reasons = ()
        expected_input_digest = _digest({
            "router_version": int(tick["router_version"]),
            "session": tick["session"],
            "scheduled_at_utc": tick["scheduled_at_utc"],
            "rank_run_id": tick["rank_run_id"],
            "input_candidates": inputs,
            "eligible_candidates": int(tick["eligible_candidates"]),
            "suppressed_candidates": int(tick["suppressed_candidates"]),
            "operational_status": tick["operational_status"],
            "operational_reasons": list(reasons),
            "invariant_ok": bool(tick["invariant_ok"]),
            "policy_sha256": tick["policy_sha256"],
            "authorization_sha256": tick["authorization_sha256"],
            "runtime_router_revision": tick["runtime_router_revision"],
            "route_ids": sorted(route_ids_by_tick.get(tick_id, [])),
        })
        if tick["input_digest_sha256"] != expected_input_digest:
            input_digest_failures += 1
    for link in links:
        tick = tick_by_id.get(int(link["tick_id"]))
        route = route_by_id.get(int(link["route_id"]))
        if tick is None or route is None:
            link_failures += 1
            continue
        tick_identity = (
            tick["session"], tick["rank_run_id"], tick["policy_sha256"],
            tick["authorization_sha256"], tick["runtime_router_revision"],
        )
        route_identity = (
            route["session"], route["rank_run_id"], route["policy_sha256"],
            route["authorization_sha256"], route["runtime_router_revision"],
        )
        if tick_identity != route_identity:
            identity_failures += 1
    if conservation_failures:
        _issue(issues, "DECISION_CONSERVATION_FAILURE", f"{conservation_failures} ticks failed count conservation")
    if link_failures:
        _issue(issues, "DECISION_LINK_FAILURE", f"{link_failures} tick/route link checks failed")
    if identity_failures:
        _issue(issues, "DECISION_IDENTITY_MISMATCH", f"{identity_failures} linked decisions disagree with tick identity")
    if input_digest_failures:
        _issue(issues, "INPUT_DIGEST_MISMATCH", f"{input_digest_failures} tick input digests could not be reproduced")
    if decision_attribution_failures:
        _issue(
            issues,
            "DECISION_ATTRIBUTION_MISMATCH",
            f"{decision_attribution_failures} ticks disagree with linked route outcomes",
        )

    orphan_routes = len(set(route_by_id) - linked_route_ids)
    if orphan_routes:
        _issue(issues, "ORPHAN_ROUTE_DECISIONS", f"{orphan_routes} session routes are not linked to a tick")
    duplicate_eligible = int(conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT idempotency_key FROM postmarket_delivery_dry_runs
          WHERE session=? AND decision='ELIGIBLE_FOR_DRY_RUN'
          GROUP BY idempotency_key HAVING COUNT(*) > 1
        )
        """,
        (session.isoformat(),),
    ).fetchone()[0])
    if duplicate_eligible:
        _issue(issues, "DUPLICATE_ELIGIBLE_IDENTITY", f"{duplicate_eligible} eligible identities were duplicated")

    actionability_failures = 0
    suppression_reasons: Counter[str] = Counter()
    for route in routes:
        try:
            reasons = _json_strings(route["reason_codes_json"], f"route {route['route_id']} reasons")
        except ValueError as exc:
            _issue(issues, "ROUTE_EVIDENCE_INVALID", str(exc))
            reasons = ()
        suppression_reasons.update(reasons)
        if route["decision"] == "ELIGIBLE_FOR_DRY_RUN" and (
            route["presentation"] != "ACTIONABLE" or reasons
        ):
            actionability_failures += 1
    if actionability_failures:
        _issue(issues, "ELIGIBLE_ACTIONABILITY_MISMATCH", f"{actionability_failures} eligible routes are not actionable")

    calibration_by_route = {
        int(row["route_id"]): row for row in calibrations
    }
    eligible_route_ids = {
        int(row["route_id"])
        for row in routes
        if row["decision"] == "ELIGIBLE_FOR_DRY_RUN"
    }
    calibration_link_failures = len(eligible_route_ids - set(calibration_by_route))
    calibration_attribution_failures = 0
    calibration_models: set[str] = set()
    for route_id, calibration in calibration_by_route.items():
        route = route_by_id.get(route_id)
        if route is None:
            calibration_link_failures += 1
            continue
        model_sha = str(calibration["model_sha256"])
        calibration_models.add(model_sha)
        projection = conn.execute(
            """
            SELECT * FROM postmarket_rank_calibration_projections
            WHERE projection_id=?
            """,
            (calibration["projection_id"],),
        ).fetchone()
        run = conn.execute(
            """
            SELECT calibration_id,split,report_json,report_sha256
            FROM postmarket_rank_calibration_runs WHERE calibration_run_id=?
            """,
            (calibration["calibration_run_id"],),
        ).fetchone()
        calibrator = None if run is None else conn.execute(
            """
            SELECT calibration_version,model_sha256,model_json
            FROM postmarket_rank_calibrators WHERE calibration_id=?
            """,
            (run["calibration_id"],),
        ).fetchone()
        failure = (
            projection is None
            or run is None
            or calibrator is None
            or int(projection["rank_run_id"]) != int(route["rank_run_id"])
            or int(projection["candidate_id"]) != int(route["candidate_id"])
            or int(projection["calibration_run_id"])
            != int(calibration["calibration_run_id"])
            or int(projection["calibration_version"])
            != int(calibration["calibration_version"])
            or str(projection["model_sha256"]) != model_sha
            or float(projection["calibrated_quality"])
            != float(calibration["calibrated_quality"])
            or str(projection["projected_at_utc"])
            != str(calibration["projected_at_utc"])
            or str(projection["code_version"])
            != str(calibration["code_version"])
            or run["split"] != "holdout"
            or int(calibrator["calibration_version"])
            != int(calibration["calibration_version"])
            or str(calibrator["model_sha256"]) != model_sha
        )
        if not failure:
            try:
                report_raw = str(run["report_json"])
                report = json.loads(report_raw)
                failure = (
                    hashlib.sha256(report_raw.encode()).hexdigest()
                    != run["report_sha256"]
                    or json.dumps(
                        report, sort_keys=True, separators=(",", ":")
                    ) != report_raw
                    or report.get("model_sha256") != model_sha
                    or report.get("calibrated_quality_claim_valid") is not True
                    or report.get("blocking_reasons") != []
                )
            except (TypeError, json.JSONDecodeError):
                failure = True
        if (
            not DIGEST_PATTERN.fullmatch(model_sha)
            or not math.isfinite(float(calibration["calibrated_quality"]))
            or not 0 <= float(calibration["calibrated_quality"]) <= 1
        ):
            failure = True
        if failure:
            calibration_attribution_failures += 1
    if calibration_link_failures:
        _issue(
            issues,
            "CALIBRATION_LINK_FAILURE",
            f"{calibration_link_failures} eligible routes lack exact calibration evidence",
        )
    if calibration_attribution_failures:
        _issue(
            issues,
            "CALIBRATION_ATTRIBUTION_FAILURE",
            f"{calibration_attribution_failures} route calibration links are not reproducible",
        )
    if len(calibration_models) > 1:
        _issue(
            issues,
            "CALIBRATION_MODEL_DRIFT",
            f"session contains {len(calibration_models)} calibration models",
        )

    policy_sha256s = tuple(sorted({str(row["policy_sha256"]) for row in ticks}))
    authorization_sha256s = tuple(sorted({str(row["authorization_sha256"]) for row in ticks}))
    runtime_revisions = tuple(sorted({str(row["runtime_router_revision"]) for row in ticks}))
    router_versions = tuple(sorted({int(row["router_version"]) for row in ticks}))
    for code, values, label in (
        ("POLICY_DRIFT", policy_sha256s, "policy digests"),
        ("AUTHORIZATION_DRIFT", authorization_sha256s, "authorization digests"),
        ("RUNTIME_REVISION_DRIFT", runtime_revisions, "runtime revisions"),
        ("ROUTER_VERSION_DRIFT", router_versions, "router versions"),
    ):
        if len(values) != 1:
            _issue(issues, code, f"session contains {len(values)} distinct {label}")
    if router_versions and router_versions != (ROUTER_VERSION,):
        _issue(issues, "ROUTER_VERSION_UNSUPPORTED", f"expected router version {ROUTER_VERSION}, got {router_versions}")
    if any(not DIGEST_PATTERN.fullmatch(value) for value in policy_sha256s):
        _issue(issues, "POLICY_DIGEST_INVALID", "a policy digest is not canonical SHA-256")
    if any(not DIGEST_PATTERN.fullmatch(value) for value in authorization_sha256s):
        _issue(issues, "AUTHORIZATION_DIGEST_INVALID", "an authorization digest is not canonical SHA-256")
    if any(not REVISION_PATTERN.fullmatch(value) for value in runtime_revisions):
        _issue(issues, "RUNTIME_REVISION_UNKNOWN", "a runtime router revision is not a concrete git revision")
    if not audit_code_version or not REVISION_PATTERN.fullmatch(audit_code_version):
        _issue(issues, "AUDIT_REVISION_UNKNOWN", "audit code version is not a concrete git revision")
    elif len(runtime_revisions) == 1 and runtime_revisions[0] != audit_code_version:
        _issue(
            issues,
            "AUDIT_RUNTIME_REVISION_MISMATCH",
            "audit and supervised router revisions differ",
        )

    degraded = sum(row["operational_status"] != "clean" for row in ticks)
    failed_invariants = sum(not bool(row["invariant_ok"]) for row in ticks)
    if degraded:
        _issue(issues, "DEGRADED_TICKS", f"{degraded} ticks were operationally degraded")
    if failed_invariants:
        _issue(issues, "FAILED_INVARIANTS", f"{failed_invariants} ticks failed their invariant")
    scheduled_lags = [int(row["scheduled_lag_ms"]) for row in ticks]
    latencies = [int(row["latency_ms"]) for row in ticks]
    if scheduled_lags and max(scheduled_lags) > MAX_SCHEDULED_LAG_MS:
        _issue(issues, "SCHEDULED_LAG_HIGH", f"maximum scheduled lag was {max(scheduled_lags)}ms")
    if latencies and max(latencies) > MAX_TICK_LATENCY_MS:
        _issue(issues, "TICK_LATENCY_HIGH", f"maximum tick latency was {max(latencies)}ms")

    rank_missing_after_first_bar = 0
    if window is not None:
        rank_required_at = window[0] + FINAL_BAR_GRACE
        rank_missing_after_first_bar = sum(
            row["rank_run_id"] is None
            and _aware(row["scheduled_at_utc"], "scheduled_at_utc") >= rank_required_at
            for row in ticks
        )
    if rank_missing_after_first_bar:
        _issue(issues, "RANK_RUN_MISSING", f"{rank_missing_after_first_bar} post-first-bar ticks lack a rank run")
    if bad_json_ticks:
        _issue(issues, "TICK_EVIDENCE_INVALID", f"{bad_json_ticks} ticks could not be fully parsed")

    coverage_pct = round(100 * len(observed_set & expected_set) / len(expected_slots), 2) if expected_slots else 0.0
    metrics = DryRunAuditMetrics(
        expected_ticks=len(expected_slots), observed_ticks=len(ticks), coverage_pct=coverage_pct,
        first_scheduled_at_utc=scheduled[0].isoformat() if scheduled else None,
        final_scheduled_at_utc=scheduled[-1].isoformat() if scheduled else None,
        expected_start_utc=window[0].isoformat() if window else None,
        expected_end_utc=expected_slots[-1].isoformat() if expected_slots else None,
        missing_scheduled_slots_utc=tuple(value.isoformat() for value in missing_slots),
        max_tick_gap_seconds=max_gap,
        average_scheduled_lag_ms=(sum(scheduled_lags) / len(scheduled_lags) if scheduled_lags else None),
        max_scheduled_lag_ms=max(scheduled_lags) if scheduled_lags else None,
        average_latency_ms=(sum(latencies) / len(latencies) if latencies else None),
        max_latency_ms=max(latencies) if latencies else None,
        degraded_ticks=degraded, failed_invariants=failed_invariants,
        input_candidates=sum(int(row["input_candidates"]) for row in ticks),
        eligible_candidates=sum(int(row["eligible_candidates"]) for row in ticks),
        suppressed_candidates=sum(int(row["suppressed_candidates"]) for row in ticks),
        decisions_written=sum(int(row["decisions_written"]) for row in ticks),
        duplicate_decisions=sum(int(row["duplicate_decisions"]) for row in ticks),
        linked_decisions=len(links), orphan_routes=orphan_routes,
        duplicate_eligible_identities=duplicate_eligible,
        conservation_failures=conservation_failures, link_failures=link_failures,
        identity_failures=identity_failures, input_digest_failures=input_digest_failures,
        decision_attribution_failures=decision_attribution_failures,
        actionability_failures=actionability_failures,
        calibrated_routes=len(calibration_by_route),
        calibration_link_failures=calibration_link_failures,
        calibration_attribution_failures=calibration_attribution_failures,
        rank_missing_after_first_bar=rank_missing_after_first_bar,
        operational_reason_counts=dict(sorted(operational_reasons.items())),
        suppression_reason_counts=dict(sorted(suppression_reasons.items())),
        policy_sha256s=policy_sha256s, authorization_sha256s=authorization_sha256s,
        runtime_router_revisions=runtime_revisions, router_versions=router_versions,
        calibration_model_sha256s=tuple(sorted(calibration_models)),
    )
    clean = not issues
    eligible = clean
    return DailyDryRunAuditReport(
        audit_version=AUDIT_VERSION, audit_code_version=audit_code_version,
        session=session.isoformat(), database=database,
        created_at_utc=created.isoformat(),
        source_evidence_sha256=_canonical_digest(
            ticks, links, routes, calibrations
        ),
        operational_clean=clean, session_evidence_eligible=eligible,
        metrics=metrics, issues=tuple(issues),
    )


def report_json(report: DailyDryRunAuditReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def write_report_atomic(path: Path | str, report: DailyDryRunAuditReport) -> bool:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return False
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(report_json(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            os.link(tmp, destination)
        except FileExistsError:
            return False
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def write_completed_dry_run_audits(
    db_path: Path | str,
    output_dir: Path | str,
    *,
    now: datetime,
    audit_code_version: str | None,
) -> tuple[DailyDryRunAuditReport, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    database = Path(db_path)
    if not database.exists():
        return ()
    conn = connect_readonly(database)
    try:
        if "postmarket_delivery_dry_run_ticks" not in _tables(conn):
            return ()
        sessions = tuple(
            date.fromisoformat(row[0])
            for row in conn.execute(
                "SELECT DISTINCT session FROM postmarket_delivery_dry_run_ticks ORDER BY session"
            )
        )
        written = []
        for session in sessions:
            ready_at = _audit_ready_at(session)
            if ready_at is None or now.astimezone(timezone.utc) <= ready_at:
                continue
            destination = Path(output_dir) / (
                f"postmarket_customer_dry_run_audit_{session.isoformat()}_v{AUDIT_VERSION}.json"
            )
            if destination.exists():
                existing = json.loads(destination.read_text(encoding="utf-8"))
                if existing.get("session") != session.isoformat() or existing.get("audit_version") != AUDIT_VERSION:
                    raise ValueError(f"existing audit report is inconsistent: {destination}")
                current = audit_dry_run_session(
                    conn,
                    session,
                    database=str(database),
                    audit_code_version=audit_code_version,
                    created_at=now,
                )
                if existing.get("source_evidence_sha256") != current.source_evidence_sha256:
                    raise ValueError(
                        f"persisted evidence changed after immutable audit: {destination}"
                    )
                continue
            report = audit_dry_run_session(
                conn, session, database=str(database),
                audit_code_version=audit_code_version, created_at=now,
            )
            if write_report_atomic(destination, report):
                written.append(report)
        return tuple(written)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--audit-code-version", default=os.environ.get("GIT_SHA"))
    parser.add_argument("--session", required=True, type=date.fromisoformat)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    conn = connect_readonly(args.db)
    try:
        report = audit_dry_run_session(
            conn, args.session, database=str(args.db),
            audit_code_version=args.audit_code_version,
        )
    finally:
        conn.close()
    if args.write:
        ready_at = _audit_ready_at(args.session)
        now = datetime.now(timezone.utc)
        if ready_at is None or now <= ready_at:
            raise ValueError("cannot publish audit before the full session has settled")
        path = args.output_dir / (
            f"postmarket_customer_dry_run_audit_{args.session}_v{AUDIT_VERSION}.json"
        )
        if not write_report_atomic(path, report):
            raise FileExistsError(f"refusing to replace immutable audit: {path}")
    else:
        print(report_json(report), end="")
    return 0 if report.operational_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
