"""Prospectively lock a customer-readiness dry-run evidence campaign.

Locking records requirements only.  It cannot enable the supervisor, create
owner authorization, contact a customer, or alter collected evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    parse_delivery_policy,
    parse_owner_authorization,
)


CAMPAIGN_VERSION = 1
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
FINAL_BAR_GRACE = timedelta(minutes=5)
AUDIT_SETTLE_GRACE = timedelta(seconds=90)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DryRunCampaignPolicy:
    min_clean_sessions: int
    min_eligible_decisions: int
    min_independently_reviewed_cases: int
    min_distinct_reviewed_symbols: int
    min_owner_review_approval_rate: float
    min_session_coverage_pct: float
    max_scheduled_lag_seconds: float
    max_tick_latency_seconds: float
    allowed_audit_versions: tuple[int, ...]
    allowed_audit_code_versions: tuple[str, ...]
    allowed_runtime_router_revisions: tuple[str, ...]
    require_zero_dirty_sessions: bool
    require_complete_session_inventory: bool
    require_zero_degraded_ticks: bool
    require_zero_failed_invariants: bool
    require_zero_conservation_failures: bool
    require_zero_link_failures: bool
    require_zero_identity_failures: bool
    require_zero_input_digest_failures: bool
    require_zero_decision_attribution_failures: bool
    require_zero_orphan_routes: bool
    require_zero_duplicate_eligible_identities: bool
    require_zero_actionability_failures: bool
    require_zero_critical_review_findings: bool
    require_independent_owner_review: bool


POLICY_FIELDS = frozenset(DryRunCampaignPolicy.__dataclass_fields__)


def _positive_integer(value: object, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _finite_number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _tuple_of_ints(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} must be a non-empty integer array")
    values = tuple(_positive_integer(item, name) for item in value)
    if len(values) != len(set(values)):
        raise ValueError(f"{name} cannot contain duplicates")
    return values


def _tuple_of_revisions(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or any(
        not isinstance(item, str) or not REVISION_PATTERN.fullmatch(item)
        for item in value
    ):
        raise ValueError(f"{name} must contain concrete git revisions")
    values = tuple(value)
    if len(values) != len(set(values)):
        raise ValueError(f"{name} cannot contain duplicates")
    return values


def parse_campaign_policy(payload: Mapping[str, object]) -> DryRunCampaignPolicy:
    if set(payload) != POLICY_FIELDS:
        raise ValueError(
            "campaign policy fields must match exactly; "
            f"missing={sorted(POLICY_FIELDS - set(payload))!r} "
            f"extra={sorted(set(payload) - POLICY_FIELDS)!r}"
        )
    required_true = tuple(name for name in POLICY_FIELDS if name.startswith("require_"))
    if any(payload[name] is not True for name in required_true):
        raise ValueError("every fail-closed campaign requirement must be true")
    policy = DryRunCampaignPolicy(
        min_clean_sessions=_positive_integer(
            payload["min_clean_sessions"], "min_clean_sessions", minimum=10
        ),
        min_eligible_decisions=_positive_integer(
            payload["min_eligible_decisions"], "min_eligible_decisions", minimum=20
        ),
        min_independently_reviewed_cases=_positive_integer(
            payload["min_independently_reviewed_cases"],
            "min_independently_reviewed_cases",
            minimum=20,
        ),
        min_distinct_reviewed_symbols=_positive_integer(
            payload["min_distinct_reviewed_symbols"],
            "min_distinct_reviewed_symbols",
            minimum=10,
        ),
        min_owner_review_approval_rate=_finite_number(
            payload["min_owner_review_approval_rate"],
            "min_owner_review_approval_rate",
            minimum=0.9,
            maximum=1,
        ),
        min_session_coverage_pct=_finite_number(
            payload["min_session_coverage_pct"],
            "min_session_coverage_pct",
            minimum=100,
            maximum=100,
        ),
        max_scheduled_lag_seconds=_finite_number(
            payload["max_scheduled_lag_seconds"],
            "max_scheduled_lag_seconds",
            minimum=0.001,
            maximum=30,
        ),
        max_tick_latency_seconds=_finite_number(
            payload["max_tick_latency_seconds"],
            "max_tick_latency_seconds",
            minimum=0.001,
            maximum=10,
        ),
        allowed_audit_versions=_tuple_of_ints(
            payload["allowed_audit_versions"], "allowed_audit_versions"
        ),
        allowed_audit_code_versions=_tuple_of_revisions(
            payload["allowed_audit_code_versions"], "allowed_audit_code_versions"
        ),
        allowed_runtime_router_revisions=_tuple_of_revisions(
            payload["allowed_runtime_router_revisions"],
            "allowed_runtime_router_revisions",
        ),
        **{name: True for name in required_true},
    )
    return policy


def _sessions(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise ValueError("coverage_end cannot precede coverage_start")
    return tuple(
        timestamp.date()
        for timestamp in CALENDAR.sessions_in_range(start, end)
    )


def _campaign_settled_at(session: date) -> datetime:
    end = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return end + FINAL_BAR_GRACE + AUDIT_SETTLE_GRACE


def _digest(value: str, name: str) -> str:
    if not DIGEST_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _read_json_contract(path: Path, context: str) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{context} must be a regular file")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload


def lock_customer_dry_run_campaign(
    output_path: Path | str,
    *,
    campaign_id: str,
    locked_at: datetime,
    coverage_start: date,
    coverage_end: date,
    delivery_policy_payload: Mapping[str, object],
    owner_authorization_payload: Mapping[str, object],
    control_evidence_sha256s: tuple[str, ...],
    policy: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError("campaign_id must be non-empty")
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise ValueError("locked_at must be timezone-aware")
    locked = locked_at.astimezone(timezone.utc)
    sessions = _sessions(coverage_start, coverage_end)
    if not sessions:
        raise ValueError("campaign coverage contains no XNYS sessions")
    campaign_policy = parse_campaign_policy(policy)
    if campaign_policy.min_distinct_reviewed_symbols > campaign_policy.min_independently_reviewed_cases:
        raise ValueError(
            "min_distinct_reviewed_symbols cannot exceed reviewed cases"
        )
    if len(sessions) < campaign_policy.min_clean_sessions:
        raise ValueError(
            "campaign coverage contains fewer XNYS sessions than min_clean_sessions"
        )
    first_open = CALENDAR.session_open(sessions[0]).to_pydatetime().astimezone(timezone.utc)
    if locked >= first_open:
        raise ValueError("campaign must be locked before its first session opens")

    delivery_policy = parse_delivery_policy(delivery_policy_payload)
    authorization = parse_owner_authorization(owner_authorization_payload)
    if authorization.acknowledgement != ACKNOWLEDGEMENT:
        raise ValueError("owner authorization acknowledgement is not exact")
    if authorization.policy_sha256 != delivery_policy.sha256:
        raise ValueError("owner authorization is not bound to the delivery policy")
    if authorization.evidence_set_sha256 != delivery_policy.evidence_set_sha256:
        raise ValueError("owner authorization evidence set does not match policy")
    if authorization.evidence_gate_sha256 != delivery_policy.evidence_gate_sha256:
        raise ValueError("owner authorization evidence gate does not match policy")
    if authorization.router_revision != delivery_policy.router_revision:
        raise ValueError("owner authorization router revision does not match policy")
    if authorization.approved_at > locked:
        raise ValueError("owner authorization was approved after campaign lock")
    if authorization.expires_at <= _campaign_settled_at(sessions[-1]):
        raise ValueError("owner authorization does not cover the complete campaign")
    expected_revision = (delivery_policy.router_revision,)
    if campaign_policy.allowed_runtime_router_revisions != expected_revision:
        raise ValueError("campaign must pin the exact delivery policy router revision")
    if campaign_policy.allowed_audit_code_versions != expected_revision:
        raise ValueError("campaign must pin the audit to the exact router revision")
    if campaign_policy.allowed_audit_versions != (1,):
        raise ValueError("campaign must pin customer dry-run audit version 1")
    if len(control_evidence_sha256s) != 4:
        raise ValueError("campaign requires exactly four control evidence digests")
    controls = tuple(_digest(item, "control_evidence_sha256s") for item in control_evidence_sha256s)
    if len(controls) != len(set(controls)):
        raise ValueError("control_evidence_sha256s cannot contain duplicates")

    payload: dict[str, object] = {
        "schema_version": CAMPAIGN_VERSION,
        "status": "locked",
        "campaign_id": campaign_id.strip(),
        "locked_at_utc": locked.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "expected_sessions": [session.isoformat() for session in sessions],
        "delivery_policy_sha256": delivery_policy.sha256,
        "owner_authorization_sha256": authorization.sha256,
        "owner_authorization_expires_at_utc": authorization.expires_at.isoformat(),
        "release_id": authorization.release_id,
        "router_version": 1,
        "rank_version": delivery_policy.rank_version,
        "control_evidence_sha256s": list(controls),
        "policy": asdict(campaign_policy),
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    canonical = json.loads(raw)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("campaign output cannot be a symlink")
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest(), canonical


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--coverage-start", required=True, type=date.fromisoformat)
    parser.add_argument("--coverage-end", required=True, type=date.fromisoformat)
    parser.add_argument("--delivery-policy", required=True, type=Path)
    parser.add_argument("--owner-authorization", required=True, type=Path)
    parser.add_argument("--control-evidence-sha256", action="append", required=True)
    parser.add_argument("--min-clean-sessions", type=int, default=10)
    parser.add_argument("--min-eligible-decisions", type=int, default=20)
    parser.add_argument("--min-independently-reviewed-cases", type=int, default=20)
    parser.add_argument("--min-distinct-reviewed-symbols", type=int, default=10)
    parser.add_argument("--min-owner-review-approval-rate", type=float, default=0.9)
    parser.add_argument("--min-session-coverage-pct", type=float, default=100)
    parser.add_argument("--max-scheduled-lag-seconds", type=float, default=30)
    parser.add_argument("--max-tick-latency-seconds", type=float, default=10)
    parser.add_argument("--allowed-audit-version", type=int, action="append", required=True)
    parser.add_argument("--allowed-audit-code-version", action="append", required=True)
    parser.add_argument("--allowed-runtime-router-revision", action="append", required=True)
    args = parser.parse_args(argv)
    requirements = {
        "min_clean_sessions": args.min_clean_sessions,
        "min_eligible_decisions": args.min_eligible_decisions,
        "min_independently_reviewed_cases": args.min_independently_reviewed_cases,
        "min_distinct_reviewed_symbols": args.min_distinct_reviewed_symbols,
        "min_owner_review_approval_rate": args.min_owner_review_approval_rate,
        "min_session_coverage_pct": args.min_session_coverage_pct,
        "max_scheduled_lag_seconds": args.max_scheduled_lag_seconds,
        "max_tick_latency_seconds": args.max_tick_latency_seconds,
        "allowed_audit_versions": args.allowed_audit_version,
        "allowed_audit_code_versions": args.allowed_audit_code_version,
        "allowed_runtime_router_revisions": args.allowed_runtime_router_revision,
        **{
            name: True
            for name in POLICY_FIELDS
            if name.startswith("require_")
        },
    }
    digest, payload = lock_customer_dry_run_campaign(
        args.output,
        campaign_id=args.campaign_id,
        locked_at=datetime.now(timezone.utc),
        coverage_start=args.coverage_start,
        coverage_end=args.coverage_end,
        delivery_policy_payload=_read_json_contract(args.delivery_policy, "delivery policy"),
        owner_authorization_payload=_read_json_contract(
            args.owner_authorization, "owner authorization"
        ),
        control_evidence_sha256s=tuple(args.control_evidence_sha256),
        policy=requirements,
    )
    print(json.dumps({
        "campaign_id": payload["campaign_id"],
        "coverage_start": payload["coverage_start"],
        "coverage_end": payload["coverage_end"],
        "campaign_sha256": digest,
        "path": str(args.output),
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
