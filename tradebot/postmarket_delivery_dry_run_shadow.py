"""Default-off supervisor for the customer-readiness dry-run ledger.

The service reads completed shadow evidence and writes dry-run decisions back
to the shadow database. It cannot render, enqueue, send, trade, or call a
market-data provider.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradebot.journal import code_version, new_run_id
from tradebot.postmarket_delivery_dry_run import (
    DryRunTickEvidence,
    ensure_dry_run_schema,
    record_dry_run_tick,
    route_dry_run,
)
from tradebot.postmarket_delivery_dry_run_audit import (
    write_completed_dry_run_audits,
)
from tradebot.postmarket_delivery_readiness import (
    DECISION_ELIGIBLE,
    DeliveryCandidate,
    DeliveryPolicy,
    OwnerAuthorization,
    parse_delivery_policy,
    parse_owner_authorization,
)
from tradebot.postmarket_shadow import (
    idle_sleep_seconds,
    postmarket_is_active,
    postmarket_window,
    write_heartbeat_atomic,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SHADOW_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "postmarket_delivery_dry_run_heartbeat.json"
DISCOVERY_HEARTBEAT_PATH = REPO_ROOT / "data" / "postmarket_discovery_heartbeat.json"
POLICY_PATH = REPO_ROOT / "data" / "postmarket_customer_delivery_policy.json"
AUTHORIZATION_PATH = (
    REPO_ROOT / "data" / "postmarket_customer_delivery_authorization.json"
)
AUDIT_DIR = REPO_ROOT / "data" / "postmarket_audits"
RUN_MODE = "postmarket-customer-readiness-dry-run"
POLL_SECONDS = 60
IDLE_SECONDS = 300
MAX_DISCOVERY_HEARTBEAT_AGE_SECONDS = 180

logger = logging.getLogger("watchtower.postmarket_delivery_dry_run_shadow")


@dataclass(frozen=True)
class DryRunTickResult:
    tick_id: int
    tick_created: bool
    session: str
    scheduled_at_utc: str
    rank_run_id: int | None
    evaluated_candidates: int
    decisions_written: int
    eligible_candidates: int
    suppressed_candidates: int
    duplicate_decisions: int
    suppression_reasons: tuple[tuple[str, int], ...]
    operational_status: str
    operational_reasons: tuple[str, ...]
    scheduled_lag_ms: int
    latency_ms: int
    invariant_ok: bool
    input_digest_sha256: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dry_run_scheduled_at(
    now: datetime,
    *,
    session_close: datetime,
    interval_seconds: int = POLL_SECONDS,
) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if session_close.tzinfo is None or session_close.utcoffset() is None:
        raise ValueError("session_close must be timezone-aware")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    elapsed = (now - session_close).total_seconds()
    if elapsed < 0:
        raise ValueError("now must not precede session_close")
    slot = int(elapsed // interval_seconds)
    return session_close + timedelta(seconds=slot * interval_seconds)


def dry_run_sleep_seconds(
    now: datetime,
    *,
    session_close: datetime,
    interval_seconds: int = POLL_SECONDS,
) -> float:
    scheduled = dry_run_scheduled_at(
        now, session_close=session_close, interval_seconds=interval_seconds
    )
    next_tick = scheduled + timedelta(seconds=interval_seconds)
    return max(0.1, (next_tick - now).total_seconds())


def dry_run_shadow_enabled(raw: str | None = None) -> bool:
    value = (
        os.environ.get("POSTMARKET_CUSTOMER_DRY_RUN_ENABLED", "0")
        if raw is None else raw
    )
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED must be one of "
        "1/0, true/false, yes/no, or on/off"
    )


def _contract_path(environment_key: str, default: Path) -> Path:
    raw = os.environ.get(environment_key)
    return Path(raw) if raw else default


def _read_contract(path: Path, context: str) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{context} cannot be resolved: {exc}") from exc
    if not resolved.is_file():
        raise ValueError(f"{context} must be a regular file")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise ValueError(f"{context} must contain readable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload


def load_contracts(
    policy_path: Path,
    authorization_path: Path,
) -> tuple[DeliveryPolicy, OwnerAuthorization]:
    policy = parse_delivery_policy(_read_contract(policy_path, "delivery policy"))
    authorization = parse_owner_authorization(
        _read_contract(authorization_path, "owner authorization")
    )
    return policy, authorization


def discovery_operational_status(
    path: Path,
    *,
    now: datetime,
    allowed_revisions: tuple[str, ...],
    max_age_seconds: int = MAX_DISCOVERY_HEARTBEAT_AGE_SECONDS,
) -> tuple[str, tuple[str, ...]]:
    """Return clean only for a fresh, error-free completed discovery cycle."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    current = now.astimezone(timezone.utc)
    reasons = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = payload["ts_utc"]
        if not isinstance(timestamp, str):
            raise TypeError("ts_utc must be a string")
        heartbeat_at = datetime.fromisoformat(timestamp)
        if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
            raise ValueError("ts_utc must be timezone-aware")
        heartbeat_at = heartbeat_at.astimezone(timezone.utc)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        return "degraded", ("DISCOVERY_HEARTBEAT_UNREADABLE",)
    age = (current - heartbeat_at).total_seconds()
    if age < 0:
        reasons.append("DISCOVERY_HEARTBEAT_FROM_FUTURE")
    elif age >= max_age_seconds:
        reasons.append("DISCOVERY_HEARTBEAT_STALE")
    if payload.get("enabled") is not True:
        reasons.append("DISCOVERY_DISABLED")
    if payload.get("status") != "ok":
        reasons.append("DISCOVERY_CYCLE_NOT_OK")
    if payload.get("code_version") not in allowed_revisions:
        reasons.append("DISCOVERY_REVISION_NOT_ALLOWED")
    if payload.get("error_count") != 0:
        reasons.append("DISCOVERY_ERRORS_PRESENT")
    for field in ("lifecycle_status", "context_backfill_status"):
        if payload.get(field) != "current":
            reasons.append(f"{field.upper()}_NOT_CURRENT")
    if payload.get("rank_status") != "complete":
        reasons.append("RANK_STATUS_NOT_COMPLETE")
    return ("clean", ()) if not reasons else ("degraded", tuple(reasons))


def _aware(value: str, name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _exclusion_reasons(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("exclusion_reasons_json must be valid JSON") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ValueError("exclusion_reasons_json must be an array of strings")
    return tuple(payload)


def load_latest_delivery_candidates(
    conn: sqlite3.Connection,
    *,
    session: date,
    rank_version: int,
) -> tuple[int | None, tuple[DeliveryCandidate, ...]]:
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            """
            SELECT rank_run_id,status,code_version
            FROM postmarket_rank_runs
            WHERE session=? AND rank_version=?
            ORDER BY rank_run_id DESC LIMIT 1
            """,
            (session.isoformat(), rank_version),
        ).fetchone()
        if run is None:
            return None, ()
        expected_rows = int(conn.execute(
            "SELECT COUNT(*) FROM postmarket_candidate_ranks WHERE rank_run_id=?",
            (run["rank_run_id"],),
        ).fetchone()[0])
        rows = conn.execute(
            """
            SELECT r.candidate_id,r.session,r.symbol,r.direction,r.transition_id,
                   r.observation_seq,r.rankable,r.ordinal_rank,r.evidence_score,
                   r.evidence_coverage_pct,r.exclusion_reasons_json,
                   rr.rank_run_id,rr.rank_version,rr.status AS rank_status,
                   rr.code_version,l.state,l.actionability,l.transition_at_utc,
                   o.evidence_bar_open_ts_utc,o.data_feed,o.market_data_provider
            FROM postmarket_candidate_ranks r
            JOIN postmarket_rank_runs rr ON rr.rank_run_id=r.rank_run_id
            JOIN postmarket_candidate_lifecycle l ON l.transition_id=r.transition_id
            JOIN postmarket_candidate_lifecycle_observations o
              ON o.seq=r.observation_seq
            WHERE r.rank_run_id=?
            ORDER BY r.candidate_id
            """,
            (run["rank_run_id"],),
        ).fetchall()
        if len(rows) != expected_rows:
            raise ValueError("rank snapshot evidence join is incomplete")
    finally:
        conn.row_factory = original
    candidates = tuple(
        DeliveryCandidate(
            transition_id=int(row["transition_id"]),
            candidate_id=int(row["candidate_id"]),
            session=row["session"],
            symbol=row["symbol"],
            direction=row["direction"],
            lifecycle_state=row["state"],
            actionability=row["actionability"],
            transition_at=_aware(row["transition_at_utc"], "transition_at_utc"),
            evidence_bar_open_at=_aware(
                row["evidence_bar_open_ts_utc"], "evidence_bar_open_ts_utc"
            ),
            rank_run_id=int(row["rank_run_id"]),
            rank_version=int(row["rank_version"]),
            rank_status=row["rank_status"],
            rankable=bool(row["rankable"]),
            ordinal_rank=(
                None if row["ordinal_rank"] is None else int(row["ordinal_rank"])
            ),
            evidence_score=float(row["evidence_score"]),
            evidence_coverage_pct=float(row["evidence_coverage_pct"]),
            exclusion_reasons=_exclusion_reasons(row["exclusion_reasons_json"]),
            data_feed=row["data_feed"],
            market_data_provider=row["market_data_provider"],
            code_version=row["code_version"] or "unknown",
        )
        for row in rows
    )
    return int(run["rank_run_id"]), candidates


def run_dry_run_tick(
    conn: sqlite3.Connection,
    policy: DeliveryPolicy,
    authorization: OwnerAuthorization,
    *,
    session: date,
    now: datetime,
    runtime_router_revision: str,
    run_id: str,
    operational_status: str,
    operational_reasons: tuple[str, ...] = (),
    scheduled_at: datetime | None = None,
) -> DryRunTickResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if scheduled_at is not None and (
        scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None
    ):
        raise ValueError("scheduled_at must be timezone-aware")
    started_at = now.astimezone(timezone.utc)
    started_clock = time.perf_counter()
    scheduled = (scheduled_at or now).astimezone(timezone.utc)
    scheduled_lag_ms = max(0, round((started_at - scheduled).total_seconds() * 1000))
    ensure_dry_run_schema(conn)
    rank_run_id, candidates = load_latest_delivery_candidates(
        conn, session=session, rank_version=policy.rank_version
    )
    results = tuple(
        route_dry_run(
            conn,
            candidate,
            policy,
            authorization,
            now=now,
            runtime_router_revision=runtime_router_revision,
            run_id=run_id,
            dry_run_enabled=True,
            kill_switch_engaged=False,
            operational_status=operational_status,
        )
        for candidate in candidates
    )
    reasons = Counter(
        reason for result in results for reason in result.reason_codes
    )
    eligible = sum(result.decision == DECISION_ELIGIBLE for result in results)
    suppressed = sum(result.decision != DECISION_ELIGIBLE for result in results)
    written = sum(result.created for result in results)
    duplicates = sum(not result.created for result in results)
    invariant_ok = (
        eligible + suppressed == len(results)
        and written + duplicates == len(results)
        and len({result.route_id for result in results}) == len(results)
        and operational_status in {"clean", "degraded"}
        and scheduled <= started_at
    )
    completed_at = _utc_now()
    latency_ms = max(0, round((time.perf_counter() - started_clock) * 1000))
    tick = record_dry_run_tick(
        conn,
        DryRunTickEvidence(
            session=session.isoformat(),
            scheduled_at_utc=scheduled.isoformat(),
            started_at_utc=started_at.isoformat(),
            completed_at_utc=completed_at.isoformat(),
            rank_run_id=rank_run_id,
            input_candidates=len(results),
            decisions_written=written,
            eligible_candidates=eligible,
            suppressed_candidates=suppressed,
            duplicate_decisions=duplicates,
            operational_status=operational_status,
            operational_reasons=operational_reasons,
            scheduled_lag_ms=scheduled_lag_ms,
            latency_ms=latency_ms,
            invariant_ok=invariant_ok,
            policy_sha256=policy.sha256,
            authorization_sha256=authorization.sha256,
            runtime_router_revision=runtime_router_revision,
            run_id=run_id,
        ),
        tuple(result.route_id for result in results),
    )
    return DryRunTickResult(
        tick_id=tick.tick_id,
        tick_created=tick.created,
        session=session.isoformat(),
        scheduled_at_utc=scheduled.isoformat(),
        rank_run_id=rank_run_id,
        evaluated_candidates=len(results),
        decisions_written=written,
        eligible_candidates=eligible,
        suppressed_candidates=suppressed,
        duplicate_decisions=duplicates,
        suppression_reasons=tuple(sorted(reasons.items())),
        operational_status=operational_status,
        operational_reasons=operational_reasons,
        scheduled_lag_ms=scheduled_lag_ms,
        latency_ms=latency_ms,
        invariant_ok=invariant_ok,
        input_digest_sha256=tick.input_digest_sha256,
    )


def _heartbeat(status: str, now: datetime, **extra: object) -> dict[str, object]:
    return {
        "ts_utc": now.astimezone(timezone.utc).isoformat(),
        "status": status,
        "enabled": True,
        "observer": RUN_MODE,
        "code_version": code_version(),
        **extra,
    }


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    configure_logging()
    try:
        enabled = dry_run_shadow_enabled()
    except ValueError:
        logger.exception("invalid postmarket customer dry-run configuration")
        return 2
    if not enabled:
        logger.info("postmarket customer-readiness dry run disabled by kill switch")
        while True:
            now = _utc_now()
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                {"ts_utc": now.isoformat(), "status": "disabled", "enabled": False},
            )
            time.sleep(IDLE_SECONDS)

    try:
        policy, authorization = load_contracts(
            _contract_path("POSTMARKET_CUSTOMER_DRY_RUN_POLICY_PATH", POLICY_PATH),
            _contract_path(
                "POSTMARKET_CUSTOMER_DRY_RUN_AUTHORIZATION_PATH", AUTHORIZATION_PATH
            ),
        )
    except ValueError:
        logger.exception("invalid postmarket customer dry-run contracts")
        return 2
    version = code_version() or "unknown"
    run_id = new_run_id()
    conn = sqlite3.connect(SHADOW_PATH, timeout=30)
    ensure_dry_run_schema(conn)
    logger.info(
        "postmarket customer-readiness dry run started revision=%s release=%s",
        version,
        authorization.release_id,
    )
    while True:
        now = _utc_now()
        if not postmarket_is_active(now):
            try:
                audits = write_completed_dry_run_audits(
                    SHADOW_PATH,
                    AUDIT_DIR,
                    now=now,
                    audit_code_version=version,
                )
            except Exception:
                logger.exception("postmarket customer dry-run daily audit failed")
                audit_payload: dict[str, object] = {"audit_status": "failed"}
            else:
                audit_payload = {
                    "audit_status": "written" if audits else "current",
                    "audits_written": len(audits),
                }
                if audits:
                    latest = audits[-1]
                    audit_payload["latest_audit"] = {
                        "session": latest.session,
                        "operational_clean": latest.operational_clean,
                        "session_evidence_eligible": latest.session_evidence_eligible,
                        "issue_codes": [issue.code for issue in latest.issues],
                    }
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "idle",
                    now,
                    release_id=authorization.release_id,
                    **audit_payload,
                ),
            )
            time.sleep(idle_sleep_seconds(now))
            continue
        window = postmarket_window(now)
        assert window is not None
        session = window[0]
        session_close = window[1]
        operational_status, operational_reasons = discovery_operational_status(
            DISCOVERY_HEARTBEAT_PATH,
            now=now,
            allowed_revisions=policy.allowed_evidence_revisions,
        )
        # Attribute scheduling lag from the instant immediately before the
        # cycle starts.  Contract/heartbeat work must not be hidden from the
        # persisted timing evidence or accidentally select the prior slot.
        tick_now = _utc_now()
        scheduled_at = dry_run_scheduled_at(
            tick_now, session_close=session_close
        )
        write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("running", now))
        try:
            result = run_dry_run_tick(
                conn,
                policy,
                authorization,
                session=session,
                now=tick_now,
                runtime_router_revision=version,
                run_id=run_id,
                operational_status=operational_status,
                operational_reasons=operational_reasons,
                scheduled_at=scheduled_at,
            )
        except Exception as exc:
            conn.rollback()
            logger.exception("postmarket customer-readiness dry-run tick failed")
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "error", _utc_now(), error=f"{type(exc).__name__}: {exc}"[:1000]
                ),
            )
        else:
            payload = {
                **result.__dict__,
                "suppression_reasons": dict(result.suppression_reasons),
                "operational_reasons": list(result.operational_reasons),
                "release_id": authorization.release_id,
                "policy_sha256": policy.sha256,
                "authorization_sha256": authorization.sha256,
            }
            logger.info(
                "postmarket_delivery_dry_run session=%s rank_run=%s evaluated=%s "
                "eligible=%s suppressed=%s written=%s duplicates=%s operational=%s",
                result.session,
                result.rank_run_id,
                result.evaluated_candidates,
                result.eligible_candidates,
                result.suppressed_candidates,
                result.decisions_written,
                result.duplicate_decisions,
                result.operational_status,
            )
            write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("ok", _utc_now(), **payload))
        sleep_now = _utc_now()
        time.sleep(
            dry_run_sleep_seconds(
                sleep_now,
                session_close=session_close,
                interval_seconds=POLL_SECONDS,
            )
        )


if __name__ == "__main__":
    sys.exit(main())
