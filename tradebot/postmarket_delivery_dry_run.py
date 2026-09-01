"""Append-only ledger for the postmarket customer-readiness dry run.

The router records readiness policy decisions only. It has no delivery,
provider, alert, order, or network dependency and cannot contact customers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from tradebot.postmarket_delivery_readiness import (
    DECISION_ELIGIBLE,
    DeliveryCandidate,
    DeliveryPolicy,
    OwnerAuthorization,
    evaluate_delivery_readiness,
)


DRY_RUN_ROUTER_VERSION = 1

DRY_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_delivery_dry_runs (
    route_id INTEGER PRIMARY KEY AUTOINCREMENT,
    router_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    decision_fingerprint_sha256 TEXT NOT NULL UNIQUE,
    evaluated_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    candidate_id INTEGER NOT NULL,
    transition_id INTEGER NOT NULL,
    rank_run_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    presentation TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    authorization_sha256 TEXT,
    release_id TEXT,
    dry_run_enabled INTEGER NOT NULL,
    kill_switch_engaged INTEGER NOT NULL,
    operational_status TEXT NOT NULL,
    runtime_router_revision TEXT,
    run_id TEXT NOT NULL,
    CHECK (decision IN ('ELIGIBLE_FOR_DRY_RUN','SUPPRESSED')),
    CHECK (presentation IN ('ACTIONABLE','STALE','DEGRADED','CLOSED')),
    CHECK (dry_run_enabled IN (0,1)),
    CHECK (kill_switch_engaged IN (0,1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_postmarket_delivery_dry_run_eligible_once
    ON postmarket_delivery_dry_runs(idempotency_key)
    WHERE decision='ELIGIBLE_FOR_DRY_RUN';
CREATE INDEX IF NOT EXISTS idx_postmarket_delivery_dry_runs_session
    ON postmarket_delivery_dry_runs(session,route_id);
CREATE INDEX IF NOT EXISTS idx_postmarket_delivery_dry_runs_candidate
    ON postmarket_delivery_dry_runs(candidate_id,route_id);

CREATE TABLE IF NOT EXISTS postmarket_delivery_dry_run_calibrations (
    route_id INTEGER PRIMARY KEY,
    projection_id INTEGER NOT NULL,
    calibration_run_id INTEGER NOT NULL,
    calibration_version INTEGER NOT NULL,
    model_sha256 TEXT NOT NULL,
    calibrated_quality REAL NOT NULL,
    projected_at_utc TEXT NOT NULL,
    code_version TEXT NOT NULL,
    CHECK (projection_id > 0),
    CHECK (calibration_run_id > 0),
    CHECK (calibration_version > 0),
    CHECK (calibrated_quality >= 0 AND calibrated_quality <= 1)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_delivery_dry_run_calibrations_model
    ON postmarket_delivery_dry_run_calibrations(model_sha256,route_id);

CREATE TABLE IF NOT EXISTS postmarket_delivery_dry_run_ticks (
    tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    router_version INTEGER NOT NULL,
    session TEXT NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    rank_run_id INTEGER,
    input_candidates INTEGER NOT NULL,
    decisions_written INTEGER NOT NULL,
    eligible_candidates INTEGER NOT NULL,
    suppressed_candidates INTEGER NOT NULL,
    duplicate_decisions INTEGER NOT NULL,
    operational_status TEXT NOT NULL,
    operational_reasons_json TEXT NOT NULL,
    input_digest_sha256 TEXT NOT NULL,
    scheduled_lag_ms INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    invariant_ok INTEGER NOT NULL,
    policy_sha256 TEXT NOT NULL,
    authorization_sha256 TEXT NOT NULL,
    runtime_router_revision TEXT NOT NULL,
    run_id TEXT NOT NULL,
    UNIQUE(session,router_version,scheduled_at_utc,policy_sha256),
    CHECK (operational_status IN ('clean','degraded')),
    CHECK (invariant_ok IN (0,1)),
    CHECK (input_candidates >= 0),
    CHECK (decisions_written >= 0),
    CHECK (eligible_candidates >= 0),
    CHECK (suppressed_candidates >= 0),
    CHECK (duplicate_decisions >= 0),
    CHECK (scheduled_lag_ms >= 0),
    CHECK (latency_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_delivery_dry_run_ticks_session
    ON postmarket_delivery_dry_run_ticks(session,tick_id);

CREATE TABLE IF NOT EXISTS postmarket_delivery_dry_run_tick_decisions (
    tick_id INTEGER NOT NULL,
    route_id INTEGER NOT NULL,
    PRIMARY KEY (tick_id,route_id)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_delivery_dry_run_tick_decisions_route
    ON postmarket_delivery_dry_run_tick_decisions(route_id,tick_id);

CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_runs_no_update
BEFORE UPDATE ON postmarket_delivery_dry_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_runs_no_delete
BEFORE DELETE ON postmarket_delivery_dry_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_run_calibrations_no_update
BEFORE UPDATE ON postmarket_delivery_dry_run_calibrations BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_run_calibrations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_run_calibrations_no_delete
BEFORE DELETE ON postmarket_delivery_dry_run_calibrations BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_run_calibrations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_run_ticks_no_update
BEFORE UPDATE ON postmarket_delivery_dry_run_ticks BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_run_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_run_ticks_no_delete
BEFORE DELETE ON postmarket_delivery_dry_run_ticks BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_run_ticks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_run_tick_decisions_no_update
BEFORE UPDATE ON postmarket_delivery_dry_run_tick_decisions BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_run_tick_decisions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_run_tick_decisions_no_delete
BEFORE DELETE ON postmarket_delivery_dry_run_tick_decisions BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_run_tick_decisions is append-only');
END;
"""


@dataclass(frozen=True)
class DryRunRouteResult:
    route_id: int
    created: bool
    decision: str
    presentation: str
    reason_codes: tuple[str, ...]
    idempotency_key: str
    decision_fingerprint_sha256: str


@dataclass(frozen=True)
class DryRunTickEvidence:
    session: str
    scheduled_at_utc: str
    started_at_utc: str
    completed_at_utc: str
    rank_run_id: int | None
    input_candidates: int
    decisions_written: int
    eligible_candidates: int
    suppressed_candidates: int
    duplicate_decisions: int
    operational_status: str
    operational_reasons: tuple[str, ...]
    scheduled_lag_ms: int
    latency_ms: int
    invariant_ok: bool
    policy_sha256: str
    authorization_sha256: str
    runtime_router_revision: str
    run_id: str


@dataclass(frozen=True)
class RecordedDryRunTick:
    tick_id: int
    created: bool
    linked_decisions: int
    input_digest_sha256: str


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def ensure_dry_run_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DRY_RUN_SCHEMA)


def record_dry_run_tick(
    conn: sqlite3.Connection,
    evidence: DryRunTickEvidence,
    route_ids: tuple[int, ...],
) -> RecordedDryRunTick:
    """Atomically append one scheduled cycle and its exact decision links."""
    ensure_dry_run_schema(conn)
    if evidence.input_candidates != len(route_ids):
        raise ValueError("input_candidates must equal the route link inventory")
    if len(route_ids) != len(set(route_ids)):
        raise ValueError("route link inventory cannot contain duplicates")
    linked_rows = []
    if route_ids:
        placeholders = ",".join("?" for _ in route_ids)
        linked_rows = conn.execute(
            f"""
            SELECT route_id,session,rank_run_id,policy_sha256,
                   authorization_sha256,runtime_router_revision
            FROM postmarket_delivery_dry_runs
            WHERE route_id IN ({placeholders})
            ORDER BY route_id
            """,
            route_ids,
        ).fetchall()
    expected_identity = (
        evidence.session,
        evidence.rank_run_id,
        evidence.policy_sha256,
        evidence.authorization_sha256,
        evidence.runtime_router_revision,
    )
    if len(linked_rows) != len(route_ids) or any(
        tuple(row[1:]) != expected_identity for row in linked_rows
    ):
        raise ValueError("route link inventory does not match the tick evidence identity")
    reasons_json = json.dumps(evidence.operational_reasons, separators=(",", ":"))
    input_digest = _digest({
        "router_version": DRY_RUN_ROUTER_VERSION,
        "session": evidence.session,
        "scheduled_at_utc": evidence.scheduled_at_utc,
        "rank_run_id": evidence.rank_run_id,
        "input_candidates": evidence.input_candidates,
        "eligible_candidates": evidence.eligible_candidates,
        "suppressed_candidates": evidence.suppressed_candidates,
        "operational_status": evidence.operational_status,
        "operational_reasons": list(evidence.operational_reasons),
        "invariant_ok": evidence.invariant_ok,
        "policy_sha256": evidence.policy_sha256,
        "authorization_sha256": evidence.authorization_sha256,
        "runtime_router_revision": evidence.runtime_router_revision,
        # Link rows are a set keyed by (tick_id, route_id).  Canonical sorting
        # keeps this digest independently reproducible from persisted data.
        "route_ids": sorted(route_ids),
    })
    with conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO postmarket_delivery_dry_run_ticks
                (router_version,session,scheduled_at_utc,started_at_utc,
                 completed_at_utc,rank_run_id,input_candidates,decisions_written,
                 eligible_candidates,suppressed_candidates,duplicate_decisions,
                 operational_status,operational_reasons_json,scheduled_lag_ms,
                 input_digest_sha256,latency_ms,invariant_ok,policy_sha256,authorization_sha256,
                 runtime_router_revision,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                DRY_RUN_ROUTER_VERSION, evidence.session,
                evidence.scheduled_at_utc, evidence.started_at_utc,
                evidence.completed_at_utc, evidence.rank_run_id,
                evidence.input_candidates, evidence.decisions_written,
                evidence.eligible_candidates, evidence.suppressed_candidates,
                evidence.duplicate_decisions, evidence.operational_status,
                reasons_json, evidence.scheduled_lag_ms, input_digest,
                evidence.latency_ms,
                int(evidence.invariant_ok), evidence.policy_sha256,
                evidence.authorization_sha256, evidence.runtime_router_revision,
                evidence.run_id,
            ),
        )
        created = cursor.rowcount == 1
        if created:
            tick_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO postmarket_delivery_dry_run_tick_decisions
                    (tick_id,route_id) VALUES (?,?)
                """,
                ((tick_id, route_id) for route_id in route_ids),
            )
        else:
            row = conn.execute(
                """
                SELECT tick_id,input_digest_sha256
                FROM postmarket_delivery_dry_run_ticks
                WHERE session=? AND router_version=? AND scheduled_at_utc=?
                  AND policy_sha256=?
                """,
                (
                    evidence.session, DRY_RUN_ROUTER_VERSION,
                    evidence.scheduled_at_utc, evidence.policy_sha256,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("dry-run tick conflict could not be resolved")
            if row[1] != input_digest:
                raise ValueError(
                    "scheduled dry-run tick already exists with different evidence"
                )
            tick_id = int(row[0])
        linked = int(conn.execute(
            """
            SELECT COUNT(*) FROM postmarket_delivery_dry_run_tick_decisions
            WHERE tick_id=?
            """,
            (tick_id,),
        ).fetchone()[0])
    return RecordedDryRunTick(
        tick_id=tick_id,
        created=created,
        linked_decisions=linked,
        input_digest_sha256=input_digest,
    )


def route_dry_run(
    conn: sqlite3.Connection,
    candidate: DeliveryCandidate,
    policy: DeliveryPolicy,
    authorization: OwnerAuthorization | None,
    *,
    now: datetime,
    runtime_router_revision: str | None,
    run_id: str,
    dry_run_enabled: bool = False,
    kill_switch_engaged: bool = True,
    operational_status: str = "degraded",
) -> DryRunRouteResult:
    """Evaluate and atomically append one distinct dry-run decision state."""
    current = _aware_utc(now, "now")
    if not run_id.strip():
        raise ValueError("run_id must be non-empty")
    decision = evaluate_delivery_readiness(
        candidate,
        policy,
        authorization,
        now=current,
        runtime_router_revision=runtime_router_revision,
        dry_run_enabled=dry_run_enabled,
        kill_switch_engaged=kill_switch_engaged,
        operational_status=operational_status,
    )
    authorization_sha256 = authorization.sha256 if authorization else None
    reasons_json = json.dumps(decision.reason_codes, separators=(",", ":"))
    fingerprint = _digest({
        "router_version": DRY_RUN_ROUTER_VERSION,
        "idempotency_key": decision.idempotency_key,
        "decision": decision.decision,
        "presentation": decision.presentation,
        "reason_codes": list(decision.reason_codes),
        "authorization_sha256": authorization_sha256,
        "dry_run_enabled": dry_run_enabled,
        "kill_switch_engaged": kill_switch_engaged,
        "operational_status": operational_status,
        "runtime_router_revision": runtime_router_revision,
    })
    ensure_dry_run_schema(conn)
    with conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO postmarket_delivery_dry_runs
                (router_version,idempotency_key,decision_fingerprint_sha256,
                 evaluated_at_utc,recorded_at_utc,session,symbol,direction,
                 candidate_id,transition_id,rank_run_id,decision,presentation,
                 reason_codes_json,policy_sha256,authorization_sha256,release_id,
                 dry_run_enabled,kill_switch_engaged,operational_status,
                 runtime_router_revision,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                DRY_RUN_ROUTER_VERSION, decision.idempotency_key, fingerprint,
                current.isoformat(), datetime.now(timezone.utc).isoformat(),
                candidate.session, candidate.symbol, candidate.direction,
                candidate.candidate_id, candidate.transition_id,
                candidate.rank_run_id, decision.decision, decision.presentation,
                reasons_json, decision.policy_sha256, authorization_sha256,
                decision.release_id, int(dry_run_enabled),
                int(kill_switch_engaged), operational_status,
                runtime_router_revision, run_id,
            ),
        )
        created = cursor.rowcount == 1
        if created:
            route_id = int(cursor.lastrowid)
        elif decision.decision == DECISION_ELIGIBLE:
            row = conn.execute(
                """
                SELECT route_id FROM postmarket_delivery_dry_runs
                WHERE idempotency_key=? AND decision=?
                """,
                (decision.idempotency_key, DECISION_ELIGIBLE),
            ).fetchone()
            if row is None:
                raise RuntimeError("eligible dry-run decision conflict could not be resolved")
            route_id = int(row[0])
        else:
            row = conn.execute(
                """
                SELECT route_id FROM postmarket_delivery_dry_runs
                WHERE decision_fingerprint_sha256=?
                """,
                (fingerprint,),
            ).fetchone()
            if row is None:
                raise RuntimeError("suppressed dry-run decision conflict could not be resolved")
            route_id = int(row[0])
        calibration_values = None
        if candidate.calibration_projection_id is not None:
            if (
                candidate.calibration_run_id is None
                or candidate.calibration_version is None
                or candidate.calibration_model_sha256 is None
                or candidate.calibrated_quality is None
                or candidate.calibration_projected_at is None
                or candidate.calibration_code_version is None
            ):
                raise ValueError("calibration projection attribution is incomplete")
            calibration_values = (
                route_id,
                candidate.calibration_projection_id,
                candidate.calibration_run_id,
                candidate.calibration_version,
                candidate.calibration_model_sha256,
                candidate.calibrated_quality,
                _aware_utc(
                    candidate.calibration_projected_at,
                    "candidate.calibration_projected_at",
                ).isoformat(),
                candidate.calibration_code_version,
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO postmarket_delivery_dry_run_calibrations
                  (route_id,projection_id,calibration_run_id,calibration_version,
                   model_sha256,calibrated_quality,projected_at_utc,code_version)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                calibration_values,
            )
        persisted_calibration = conn.execute(
            """
            SELECT route_id,projection_id,calibration_run_id,calibration_version,
                   model_sha256,calibrated_quality,projected_at_utc,code_version
            FROM postmarket_delivery_dry_run_calibrations WHERE route_id=?
            """,
            (route_id,),
        ).fetchone()
        if calibration_values is None:
            if persisted_calibration is not None:
                raise ValueError(
                    "dry-run route has calibration evidence absent from candidate"
                )
        elif persisted_calibration is None or tuple(persisted_calibration) != calibration_values:
            raise ValueError("dry-run route calibration evidence conflicts with candidate")
    return DryRunRouteResult(
        route_id=route_id,
        created=created,
        decision=decision.decision,
        presentation=decision.presentation,
        reason_codes=decision.reason_codes,
        idempotency_key=decision.idempotency_key,
        decision_fingerprint_sha256=fingerprint,
    )
