import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_delivery_dry_run import route_dry_run
from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    DECISION_ELIGIBLE,
    DECISION_SUPPRESSED,
    DeliveryCandidate,
    DeliveryPolicy,
    OwnerAuthorization,
)
from tradebot.postmarket_lifecycle import STATE_CONFIRMED


NOW = datetime(2026, 8, 28, 21, 15, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def policy():
    return DeliveryPolicy(
        router_revision="abc1234",
        evidence_set_sha256="a" * 64,
        evidence_gate_sha256="b" * 64,
        rank_version=1,
        minimum_evidence_score=60,
        maximum_ordinal_rank=10,
        minimum_evidence_coverage_pct=90,
        maximum_data_age_seconds=330,
        allowed_states=(STATE_CONFIRMED,),
        allowed_evidence_revisions=("abc1234",),
        allowed_providers=("alpaca",),
        allowed_feeds=("sip",),
    )


@pytest.fixture
def authorization(policy):
    return OwnerAuthorization(
        release_id="pm-release-1",
        approved_by="owner@example.com",
        approved_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        policy_sha256=policy.sha256,
        evidence_set_sha256=policy.evidence_set_sha256,
        evidence_gate_sha256=policy.evidence_gate_sha256,
        router_revision=policy.router_revision,
        acknowledgement=ACKNOWLEDGEMENT,
        dry_run_readiness_approved=True,
    )


@pytest.fixture
def candidate():
    return DeliveryCandidate(
        transition_id=44,
        candidate_id=12,
        session="2026-08-28",
        symbol="OKTA",
        direction="up",
        lifecycle_state=STATE_CONFIRMED,
        actionability="QUALIFIED",
        transition_at=NOW - timedelta(minutes=2),
        evidence_bar_open_at=NOW - timedelta(minutes=7),
        rank_run_id=17,
        rank_version=1,
        rank_status="complete",
        rankable=True,
        ordinal_rank=3,
        evidence_score=77,
        evidence_coverage_pct=100,
        exclusion_reasons=(),
        data_feed="sip",
        market_data_provider="alpaca",
        code_version="abc1234",
    )


def _route(conn, candidate, policy, authorization, **changes):
    values = {
        "now": NOW,
        "runtime_router_revision": "abc1234",
        "run_id": "run-1",
        "dry_run_enabled": True,
        "kill_switch_engaged": False,
        "operational_status": "clean",
    }
    values.update(changes)
    return route_dry_run(conn, candidate, policy, authorization, **values)


def test_eligible_decision_is_append_only_and_transactionally_deduplicated(
    conn, candidate, policy, authorization,
):
    first = _route(conn, candidate, policy, authorization)
    second = _route(conn, candidate, policy, authorization, run_id="run-2")
    assert first.decision == DECISION_ELIGIBLE
    assert first.created is True
    assert second.created is False
    assert second.route_id == first.route_id
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_delivery_dry_runs"
    ).fetchone()[0] == 1


def test_suppressed_state_can_later_become_one_eligible_decision(
    conn, candidate, policy, authorization,
):
    blocked = _route(
        conn, candidate, policy, authorization,
        dry_run_enabled=False, kill_switch_engaged=True,
    )
    duplicate = _route(
        conn, candidate, policy, authorization,
        dry_run_enabled=False, kill_switch_engaged=True, run_id="run-2",
    )
    eligible = _route(conn, candidate, policy, authorization, run_id="run-3")
    assert blocked.decision == DECISION_SUPPRESSED
    assert duplicate.created is False
    assert eligible.decision == DECISION_ELIGIBLE
    assert eligible.created is True
    assert conn.execute(
        "SELECT COUNT(*) FROM postmarket_delivery_dry_runs"
    ).fetchone()[0] == 2


def test_distinct_suppression_states_are_preserved(conn, candidate, policy, authorization):
    first = _route(
        conn, candidate, policy, authorization, operational_status="degraded"
    )
    second = _route(
        conn, candidate, policy, authorization, runtime_router_revision="def5678"
    )
    assert first.created and second.created
    rows = conn.execute(
        "SELECT reason_codes_json FROM postmarket_delivery_dry_runs ORDER BY route_id"
    ).fetchall()
    assert "OPERATIONAL_STATUS_DEGRADED" in json.loads(rows[0][0])
    assert "RUNTIME_ROUTER_REVISION_MISMATCH" in json.loads(rows[1][0])


def test_policy_candidate_or_release_changes_produce_distinct_identity(
    conn, candidate, policy, authorization,
):
    first = _route(conn, candidate, policy, authorization)
    changed_candidate = replace(candidate, transition_id=45)
    second = _route(conn, changed_candidate, policy, authorization)
    changed_authorization = replace(authorization, release_id="pm-release-2")
    third = _route(conn, candidate, policy, changed_authorization)
    assert len({first.idempotency_key, second.idempotency_key, third.idempotency_key}) == 3


def test_ledger_rejects_update_delete_and_empty_run_id(
    conn, candidate, policy, authorization,
):
    result = _route(conn, candidate, policy, authorization)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE postmarket_delivery_dry_runs SET symbol='CRM' WHERE route_id=?",
            (result.route_id,),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM postmarket_delivery_dry_runs WHERE route_id=?",
            (result.route_id,),
        )
    conn.rollback()
    with pytest.raises(ValueError, match="run_id"):
        _route(conn, candidate, policy, authorization, run_id=" ")


def test_record_binds_authorization_policy_and_exact_evidence_ids(
    conn, candidate, policy, authorization,
):
    _route(conn, candidate, policy, authorization)
    row = conn.execute(
        """
        SELECT candidate_id,transition_id,rank_run_id,policy_sha256,
               authorization_sha256,release_id,runtime_router_revision
        FROM postmarket_delivery_dry_runs
        """
    ).fetchone()
    assert row == (
        12, 44, 17, policy.sha256, authorization.sha256,
        "pm-release-1", "abc1234",
    )


def test_router_has_no_live_provider_delivery_or_trading_dependency():
    source = Path("tradebot/postmarket_delivery_dry_run.py").read_text().lower()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    for forbidden in ("telegram", "outbox", "requests", "alpaca", "broker", "order"):
        assert not any(forbidden in line for line in imports)
