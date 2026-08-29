import hashlib
import json
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_customer_dry_run_campaign import (
    POLICY_FIELDS,
    lock_customer_dry_run_campaign,
)
from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    parse_delivery_policy,
)


LOCKED = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
START = date(2026, 8, 31)
END = date(2026, 9, 18)
REVISION = "abc1234"


def _delivery_policy():
    return {
        "delivery_policy_version": 1,
        "router_revision": REVISION,
        "evidence_set_sha256": "1" * 64,
        "evidence_gate_sha256": "2" * 64,
        "rank_version": 1,
        "minimum_evidence_score": 70,
        "maximum_ordinal_rank": 10,
        "minimum_evidence_coverage_pct": 95,
        "maximum_data_age_seconds": 330,
        "allowed_states": ["CONFIRMED"],
        "allowed_evidence_revisions": [REVISION],
        "allowed_providers": ["alpaca"],
        "allowed_feeds": ["sip"],
    }


def _authorization(policy=None, **changes):
    delivery_policy = parse_delivery_policy(policy or _delivery_policy())
    payload = {
        "schema_version": 1,
        "release_id": "customer-dry-run-1",
        "approved_by": "owner@example.com",
        "approved_at_utc": "2026-08-27T12:00:00+00:00",
        "expires_at_utc": "2026-10-01T00:00:00+00:00",
        "policy_sha256": delivery_policy.sha256,
        "evidence_set_sha256": delivery_policy.evidence_set_sha256,
        "evidence_gate_sha256": delivery_policy.evidence_gate_sha256,
        "router_revision": delivery_policy.router_revision,
        "acknowledgement": ACKNOWLEDGEMENT,
        "dry_run_readiness_approved": True,
    }
    payload.update(changes)
    return payload


def _campaign_policy(**changes):
    payload = {
        "min_clean_sessions": 10,
        "min_eligible_decisions": 20,
        "min_independently_reviewed_cases": 20,
        "min_distinct_reviewed_symbols": 10,
        "min_owner_review_approval_rate": 0.9,
        "min_session_coverage_pct": 100,
        "max_scheduled_lag_seconds": 30,
        "max_tick_latency_seconds": 10,
        "allowed_audit_versions": [1],
        "allowed_audit_code_versions": [REVISION],
        "allowed_runtime_router_revisions": [REVISION],
        **{name: True for name in POLICY_FIELDS if name.startswith("require_")},
    }
    payload.update(changes)
    return payload


def _lock(path, **changes):
    values = {
        "campaign_id": "customer-dry-run-campaign-1",
        "locked_at": LOCKED,
        "coverage_start": START,
        "coverage_end": END,
        "delivery_policy_payload": _delivery_policy(),
        "owner_authorization_payload": _authorization(),
        "control_evidence_sha256s": (
            "3" * 64, "4" * 64, "5" * 64, "6" * 64
        ),
        "policy": _campaign_policy(),
    }
    values.update(changes)
    return lock_customer_dry_run_campaign(path, **values)


def test_campaign_is_prospectively_locked_read_only_and_digest_bound(tmp_path):
    output = tmp_path / "campaign.json"
    digest, payload = _lock(output)
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
    assert json.loads(output.read_text()) == payload
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    assert len(payload["expected_sessions"]) >= 10
    assert payload["delivery_policy_sha256"] == parse_delivery_policy(
        _delivery_policy()
    ).sha256
    assert payload["policy"]["min_session_coverage_pct"] == 100.0
    with pytest.raises(FileExistsError):
        _lock(output)


def test_campaign_contract_matches_truth_schema_shape(tmp_path):
    _, payload = _lock(tmp_path / "campaign.json")
    schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_campaign_v1.schema.json"
    ).read_text())
    assert set(payload) == set(schema["required"])
    assert set(payload["policy"]) == set(
        schema["properties"]["policy"]["required"]
    )


def test_lock_must_precede_first_session_and_cover_enough_sessions(tmp_path):
    with pytest.raises(ValueError, match="before its first session opens"):
        _lock(
            tmp_path / "late.json",
            locked_at=datetime(2026, 8, 31, 15, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="fewer XNYS sessions"):
        _lock(
            tmp_path / "short.json",
            coverage_end=date(2026, 9, 4),
        )


def test_authorization_must_be_exact_and_cover_complete_campaign(tmp_path):
    with pytest.raises(ValueError, match="does not cover"):
        _lock(
            tmp_path / "expired.json",
            owner_authorization_payload=_authorization(
                expires_at_utc="2026-09-18T20:00:00+00:00"
            ),
        )
    with pytest.raises(ValueError, match="not bound"):
        _lock(
            tmp_path / "mismatch.json",
            owner_authorization_payload=_authorization(policy_sha256="f" * 64),
        )


def test_campaign_quality_floors_and_fail_closed_flags_cannot_be_weakened(tmp_path):
    weak_cases = (
        ("min_clean_sessions", 9),
        ("min_eligible_decisions", 19),
        ("min_independently_reviewed_cases", 19),
        ("min_distinct_reviewed_symbols", 9),
        ("min_owner_review_approval_rate", 0.89),
        ("min_session_coverage_pct", 99.99),
        ("max_scheduled_lag_seconds", 31),
        ("max_tick_latency_seconds", 11),
        ("require_zero_dirty_sessions", False),
    )
    for index, (field, value) in enumerate(weak_cases):
        with pytest.raises(ValueError):
            _lock(
                tmp_path / f"weak-{index}.json",
                policy=_campaign_policy(**{field: value}),
            )


def test_campaign_rejects_unknown_revision_or_control_digest(tmp_path):
    with pytest.raises(ValueError, match="concrete git revisions"):
        _lock(
            tmp_path / "unknown.json",
            policy=_campaign_policy(allowed_audit_code_versions=["unknown"]),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        _lock(
            tmp_path / "digest.json",
            control_evidence_sha256s=("not-a-digest", "4" * 64, "5" * 64, "6" * 64),
        )
    with pytest.raises(ValueError, match="exactly four"):
        _lock(
            tmp_path / "controls.json",
            control_evidence_sha256s=("3" * 64,),
        )
