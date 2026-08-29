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

CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_runs_no_update
BEFORE UPDATE ON postmarket_delivery_dry_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_delivery_dry_runs_no_delete
BEFORE DELETE ON postmarket_delivery_dry_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_delivery_dry_runs is append-only');
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


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def ensure_dry_run_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DRY_RUN_SCHEMA)


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
    return DryRunRouteResult(
        route_id=route_id,
        created=created,
        decision=decision.decision,
        presentation=decision.presentation,
        reason_codes=decision.reason_codes,
        idempotency_key=decision.idempotency_key,
        decision_fingerprint_sha256=fingerprint,
    )
