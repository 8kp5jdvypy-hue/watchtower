import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    DECISION_ELIGIBLE,
    DECISION_SUPPRESSED,
    PRESENTATION_ACTIONABLE,
    PRESENTATION_CLOSED,
    PRESENTATION_DEGRADED,
    PRESENTATION_STALE,
    DeliveryCandidate,
    DeliveryPolicy,
    OwnerAuthorization,
    evaluate_delivery_readiness,
    parse_delivery_policy,
    parse_owner_authorization,
)
from tradebot.postmarket_lifecycle import STATE_CONFIRMED, STATE_FADING


NOW = datetime(2026, 8, 28, 21, 15, tzinfo=timezone.utc)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _policy(**changes):
    values = {
        "router_revision": "abc1234",
        "evidence_set_sha256": DIGEST_A,
        "evidence_gate_sha256": DIGEST_B,
        "rank_version": 1,
        "minimum_evidence_score": 60.0,
        "maximum_ordinal_rank": 10,
        "minimum_evidence_coverage_pct": 90.0,
        "maximum_data_age_seconds": 330.0,
        "allowed_states": (STATE_CONFIRMED,),
        "allowed_evidence_revisions": ("abc1234",),
        "allowed_providers": ("alpaca",),
        "allowed_feeds": ("sip",),
    }
    values.update(changes)
    return DeliveryPolicy(**values)


def _candidate(**changes):
    values = {
        "transition_id": 44,
        "candidate_id": 12,
        "session": "2026-08-28",
        "symbol": "OKTA",
        "direction": "up",
        "lifecycle_state": STATE_CONFIRMED,
        "actionability": "QUALIFIED",
        "transition_at": NOW - timedelta(minutes=2),
        "evidence_bar_open_at": NOW - timedelta(minutes=7),
        "rank_run_id": 17,
        "rank_version": 1,
        "rank_status": "complete",
        "rankable": True,
        "ordinal_rank": 3,
        "evidence_score": 77.0,
        "evidence_coverage_pct": 100.0,
        "exclusion_reasons": (),
        "data_feed": "sip",
        "market_data_provider": "alpaca",
        "code_version": "abc1234",
    }
    values.update(changes)
    return DeliveryCandidate(**values)


def _authorization(policy=None, **changes):
    policy = policy or _policy()
    values = {
        "release_id": "pm-release-1",
        "approved_by": "owner@example.com",
        "approved_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
        "policy_sha256": policy.sha256,
        "evidence_set_sha256": policy.evidence_set_sha256,
        "evidence_gate_sha256": policy.evidence_gate_sha256,
        "router_revision": policy.router_revision,
        "acknowledgement": ACKNOWLEDGEMENT,
        "dry_run_readiness_approved": True,
    }
    values.update(changes)
    return OwnerAuthorization(**values)


def _evaluate(candidate=None, policy=None, authorization="valid", **changes):
    policy = policy or _policy()
    if authorization == "valid":
        authorization = _authorization(policy)
    runtime_router_revision = changes.pop("runtime_router_revision", "abc1234")
    return evaluate_delivery_readiness(
        candidate or _candidate(), policy, authorization,
        now=NOW, runtime_router_revision=runtime_router_revision,
        dry_run_enabled=True,
        kill_switch_engaged=False,
        operational_status="clean", **changes,
    )


def test_exact_clean_authorized_candidate_is_only_dry_run_eligible():
    decision = _evaluate()
    assert decision.decision == DECISION_ELIGIBLE
    assert decision.presentation == PRESENTATION_ACTIONABLE
    assert decision.reason_codes == ()
    assert decision.idempotency_key.startswith("postmarket-readiness-v1:")


def test_default_arguments_fail_closed_without_owner_authorization():
    decision = evaluate_delivery_readiness(
        _candidate(), _policy(), None, now=NOW,
    )
    assert decision.decision == DECISION_SUPPRESSED
    assert decision.reason_codes == (
        "RUNTIME_ROUTER_REVISION_UNKNOWN", "DRY_RUN_DISABLED", "KILL_SWITCH_ENGAGED",
        "OPERATIONAL_STATUS_DEGRADED", "OWNER_AUTHORIZATION_MISSING",
    )


def test_runtime_router_revision_must_match_the_locked_policy():
    decision = _evaluate(runtime_router_revision="def5678")
    assert decision.decision == DECISION_SUPPRESSED
    assert "RUNTIME_ROUTER_REVISION_MISMATCH" in decision.reason_codes


@pytest.mark.parametrize(
    ("authorization_changes", "reason"),
    [
        ({"dry_run_readiness_approved": False}, "OWNER_AUTHORIZATION_NOT_APPROVED"),
        ({"acknowledgement": "yes"}, "OWNER_ACKNOWLEDGEMENT_MISMATCH"),
        ({"policy_sha256": DIGEST_B}, "AUTHORIZED_POLICY_MISMATCH"),
        ({"evidence_set_sha256": DIGEST_B}, "AUTHORIZED_EVIDENCE_SET_MISMATCH"),
        ({"evidence_gate_sha256": DIGEST_A}, "AUTHORIZED_EVIDENCE_GATE_MISMATCH"),
        ({"router_revision": "other"}, "AUTHORIZED_ROUTER_REVISION_MISMATCH"),
        ({"approved_at": NOW + timedelta(seconds=1)}, "OWNER_AUTHORIZATION_FROM_FUTURE"),
        ({"expires_at": NOW}, "OWNER_AUTHORIZATION_EXPIRED"),
        ({"approved_by": ""}, "OWNER_AUTHORIZATION_IDENTITY_MISSING"),
    ],
)
def test_authorization_is_exact_evidence_bound_and_time_bounded(
    authorization_changes, reason,
):
    policy = _policy()
    decision = _evaluate(
        policy=policy, authorization=_authorization(policy, **authorization_changes)
    )
    assert decision.decision == DECISION_SUPPRESSED
    assert reason in decision.reason_codes


@pytest.mark.parametrize(
    ("candidate_changes", "reason", "presentation"),
    [
        ({"code_version": "other"}, "EVIDENCE_REVISION_NOT_ALLOWED", PRESENTATION_DEGRADED),
        ({"rank_version": 2}, "RANK_VERSION_MISMATCH", PRESENTATION_ACTIONABLE),
        ({"rank_status": "degraded"}, "RANK_RUN_DEGRADED", PRESENTATION_DEGRADED),
        ({"rankable": False}, "CANDIDATE_NOT_RANKABLE", PRESENTATION_DEGRADED),
        (
            {"exclusion_reasons": ("SPREAD_TOO_WIDE",)},
            "RANK_HARD_EXCLUSIONS_PRESENT",
            PRESENTATION_DEGRADED,
        ),
        (
            {"lifecycle_state": STATE_FADING},
            "LIFECYCLE_STATE_NOT_ALLOWED",
            PRESENTATION_DEGRADED,
        ),
        ({"actionability": "WATCH"}, "LIFECYCLE_NOT_ACTIONABLE", PRESENTATION_ACTIONABLE),
        ({"ordinal_rank": None}, "ORDINAL_RANK_MISSING", PRESENTATION_ACTIONABLE),
        ({"ordinal_rank": 11}, "ORDINAL_RANK_BELOW_POLICY", PRESENTATION_ACTIONABLE),
        ({"evidence_score": 59.999}, "EVIDENCE_SCORE_BELOW_POLICY", PRESENTATION_ACTIONABLE),
        (
            {"evidence_coverage_pct": 89.999},
            "EVIDENCE_COVERAGE_BELOW_POLICY",
            PRESENTATION_ACTIONABLE,
        ),
        ({"evidence_bar_open_at": NOW - timedelta(minutes=20)}, "DATA_STALE", PRESENTATION_STALE),
        ({"market_data_provider": "other"}, "PROVIDER_NOT_ALLOWED", PRESENTATION_DEGRADED),
        ({"data_feed": "iex"}, "FEED_NOT_ALLOWED", PRESENTATION_DEGRADED),
        (
            {"transition_at": NOW + timedelta(seconds=1)},
            "EVIDENCE_FROM_FUTURE",
            PRESENTATION_STALE,
        ),
    ],
)
def test_candidate_quality_and_freshness_fail_closed(candidate_changes, reason, presentation):
    decision = _evaluate(candidate=_candidate(**candidate_changes))
    assert decision.decision == DECISION_SUPPRESSED
    assert reason in decision.reason_codes
    assert decision.presentation == presentation


def test_boundary_values_pass_and_identity_is_deterministic():
    candidate = _candidate(
        ordinal_rank=10, evidence_score=60, evidence_coverage_pct=90,
        evidence_bar_open_at=NOW - timedelta(minutes=10, seconds=30),
    )
    first = _evaluate(candidate=candidate)
    second = _evaluate(candidate=candidate)
    assert first.decision == DECISION_ELIGIBLE
    assert first.idempotency_key == second.idempotency_key


def test_identity_changes_with_transition_rank_release_or_policy():
    baseline = _evaluate().idempotency_key
    assert _evaluate(candidate=_candidate(transition_id=45)).idempotency_key != baseline
    assert _evaluate(candidate=_candidate(rank_run_id=18)).idempotency_key != baseline
    policy = _policy(minimum_evidence_score=61)
    assert _evaluate(policy=policy).idempotency_key != baseline
    policy = _policy()
    assert _evaluate(
        policy=policy, authorization=_authorization(policy, release_id="pm-release-2")
    ).idempotency_key != baseline


def test_policy_rejects_ambiguous_or_unsafe_contracts():
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _evaluate(policy=_policy(evidence_set_sha256="NOT-A-DIGEST"))
    with pytest.raises(ValueError, match="subset of rankable states"):
        _evaluate(policy=_policy(allowed_states=(STATE_FADING,)))
    with pytest.raises(ValueError, match="providers and feeds"):
        _evaluate(policy=_policy(allowed_providers=()))


@pytest.mark.parametrize(
    ("candidate_changes", "reason"),
    [
        ({"candidate_id": 0}, "EVIDENCE_ID_INVALID"),
        ({"direction": "sideways"}, "CANDIDATE_IDENTITY_INVALID"),
        ({"evidence_score": float("nan")}, "RANK_VALUE_INVALID"),
        ({"evidence_coverage_pct": 101}, "EVIDENCE_COVERAGE_INVALID"),
        ({"ordinal_rank": 0}, "ORDINAL_RANK_INVALID"),
    ],
)
def test_malformed_candidate_values_fail_closed(candidate_changes, reason):
    decision = _evaluate(candidate=_candidate(**candidate_changes))
    assert decision.decision == DECISION_SUPPRESSED
    assert reason in decision.reason_codes


def test_module_has_no_live_provider_delivery_or_trading_dependency():
    source = Path("tradebot/postmarket_delivery_readiness.py").read_text().lower()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    for forbidden in ("telegram", "outbox", "requests", "alpaca", "broker", "order"):
        assert not any(forbidden in line for line in imports)


def test_exact_json_contracts_parse_without_implicit_approval():
    policy = _policy()
    assert parse_delivery_policy(policy.canonical_payload()) == policy
    payload = {
        "schema_version": 1,
        "release_id": "pm-release-1",
        "approved_by": "owner@example.com",
        "approved_at_utc": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at_utc": (NOW + timedelta(hours=1)).isoformat(),
        "policy_sha256": policy.sha256,
        "evidence_set_sha256": policy.evidence_set_sha256,
        "evidence_gate_sha256": policy.evidence_gate_sha256,
        "router_revision": policy.router_revision,
        "acknowledgement": ACKNOWLEDGEMENT,
        "dry_run_readiness_approved": True,
    }
    assert parse_owner_authorization(payload) == _authorization(policy)
    with pytest.raises(ValueError, match="fields must match exactly"):
        parse_owner_authorization({**payload, "surprise": True})
    with pytest.raises(ValueError, match="explicitly true"):
        parse_owner_authorization({**payload, "dry_run_readiness_approved": False})


def test_truth_schemas_match_the_runtime_contracts():
    policy_schema = json.loads(
        Path("truth/postmarket_customer_delivery_policy_v1.schema.json").read_text()
    )
    authorization_schema = json.loads(
        Path(
            "truth/postmarket_customer_delivery_authorization_v1.schema.json"
        ).read_text()
    )
    assert set(policy_schema["required"]) == set(_policy().canonical_payload())
    assert set(authorization_schema["required"]) == {
        "schema_version", "release_id", "approved_by", "approved_at_utc",
        "expires_at_utc", "policy_sha256", "evidence_set_sha256",
        "evidence_gate_sha256", "router_revision", "acknowledgement",
        "dry_run_readiness_approved",
    }
