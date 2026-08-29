import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_delivery_dry_run_health import evaluate_dry_run_health
from tradebot.postmarket_delivery_dry_run_shadow import (
    RUN_MODE,
    discovery_operational_status,
    dry_run_scheduled_at,
    dry_run_sleep_seconds,
    dry_run_shadow_enabled,
    load_contracts,
    load_latest_delivery_candidates,
    run_dry_run_tick,
)
from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    DeliveryPolicy,
    OwnerAuthorization,
)
from tradebot.postmarket_lifecycle import STATE_CONFIRMED


NOW = datetime(2026, 8, 28, 21, 15, tzinfo=timezone.utc)
SESSION = NOW.date()


def _policy():
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


def _authorization(policy):
    return OwnerAuthorization(
        release_id="release-1",
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


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_enable_parser_accepts_only_explicit_true_values(raw):
    assert dry_run_shadow_enabled(raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off", ""])
def test_enable_parser_defaults_false(raw):
    assert dry_run_shadow_enabled(raw) is False


def test_enable_parser_rejects_ambiguous_value():
    with pytest.raises(ValueError, match="POSTMARKET_CUSTOMER_DRY_RUN_ENABLED"):
        dry_run_shadow_enabled("maybe")


def test_schedule_is_exchange_close_anchored_without_drift():
    close = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    now = close + timedelta(minutes=15, seconds=12, milliseconds=500)
    assert dry_run_scheduled_at(now, session_close=close) == close + timedelta(
        minutes=15
    )
    assert dry_run_sleep_seconds(now, session_close=close) == pytest.approx(47.5)
    with pytest.raises(ValueError, match="must not precede"):
        dry_run_scheduled_at(close - timedelta(seconds=1), session_close=close)


def test_contracts_are_exact_regular_files(tmp_path):
    policy = _policy()
    authorization = _authorization(policy)
    policy_path = tmp_path / "policy.json"
    authorization_path = tmp_path / "authorization.json"
    policy_path.write_text(json.dumps(policy.canonical_payload()))
    authorization_path.write_text(json.dumps(authorization.canonical_payload()))
    assert load_contracts(policy_path, authorization_path) == (policy, authorization)
    link = tmp_path / "policy-link.json"
    link.symlink_to(policy_path)
    with pytest.raises(ValueError, match="symlink"):
        load_contracts(link, authorization_path)


def _clean_discovery_heartbeat():
    return {
        "ts_utc": (NOW - timedelta(seconds=20)).isoformat(),
        "status": "ok",
        "enabled": True,
        "code_version": "abc1234",
        "error_count": 0,
        "lifecycle_status": "current",
        "context_backfill_status": "current",
        "rank_status": "complete",
    }


def test_discovery_operational_status_requires_fresh_complete_cycle(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(_clean_discovery_heartbeat()))
    assert discovery_operational_status(
        path, now=NOW, allowed_revisions=("abc1234",)
    ) == ("clean", ())
    path.write_text(json.dumps({
        **_clean_discovery_heartbeat(),
        "ts_utc": (NOW - timedelta(minutes=4)).isoformat(),
        "rank_status": "degraded",
        "error_count": 1,
    }))
    status, reasons = discovery_operational_status(
        path, now=NOW, allowed_revisions=("abc1234",)
    )
    assert status == "degraded"
    assert set(reasons) >= {
        "DISCOVERY_HEARTBEAT_STALE",
        "DISCOVERY_ERRORS_PRESENT",
        "RANK_STATUS_NOT_COMPLETE",
    }


def test_unreadable_discovery_heartbeat_is_degraded(tmp_path):
    assert discovery_operational_status(
        tmp_path / "missing.json", now=NOW, allowed_revisions=("abc1234",)
    ) == ("degraded", ("DISCOVERY_HEARTBEAT_UNREADABLE",))
    with pytest.raises(ValueError, match="max_age_seconds"):
        discovery_operational_status(
            tmp_path / "missing.json",
            now=NOW,
            allowed_revisions=("abc1234",),
            max_age_seconds=0,
        )


def _evidence_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE postmarket_rank_runs (
      rank_run_id INTEGER PRIMARY KEY, session TEXT, rank_version INTEGER,
      status TEXT, code_version TEXT
    );
    CREATE TABLE postmarket_candidate_ranks (
      rank_run_id INTEGER, candidate_id INTEGER, session TEXT, symbol TEXT,
      direction TEXT, transition_id INTEGER, observation_seq INTEGER,
      rankable INTEGER, ordinal_rank INTEGER, evidence_score REAL,
      evidence_coverage_pct REAL, exclusion_reasons_json TEXT
    );
    CREATE TABLE postmarket_candidate_lifecycle (
      transition_id INTEGER PRIMARY KEY, state TEXT, actionability TEXT,
      transition_at_utc TEXT
    );
    CREATE TABLE postmarket_candidate_lifecycle_observations (
      seq INTEGER PRIMARY KEY, evidence_bar_open_ts_utc TEXT, data_feed TEXT,
      market_data_provider TEXT
    );
    """)
    conn.execute(
        "INSERT INTO postmarket_rank_runs VALUES (17,?,?,?,?)",
        (SESSION.isoformat(), 1, "complete", "abc1234"),
    )
    conn.execute(
        "INSERT INTO postmarket_candidate_lifecycle VALUES (44,?,?,?)",
        (STATE_CONFIRMED, "QUALIFIED", (NOW - timedelta(minutes=2)).isoformat()),
    )
    conn.execute(
        "INSERT INTO postmarket_candidate_lifecycle_observations VALUES (8,?,?,?)",
        ((NOW - timedelta(minutes=7)).isoformat(), "sip", "alpaca"),
    )
    conn.execute(
        """
        INSERT INTO postmarket_candidate_ranks VALUES
          (17,12,?,'OKTA','up',44,8,1,3,77,100,'[]')
        """,
        (SESSION.isoformat(),),
    )
    conn.commit()
    return conn


def test_latest_exact_rank_snapshot_is_hydrated_and_routed():
    conn = _evidence_conn()
    try:
        rank_run_id, candidates = load_latest_delivery_candidates(
            conn, session=SESSION, rank_version=1
        )
        assert rank_run_id == 17
        assert len(candidates) == 1
        assert candidates[0].transition_id == 44
        assert candidates[0].rank_run_id == 17
        policy = _policy()
        result = run_dry_run_tick(
            conn,
            policy,
            _authorization(policy),
            session=SESSION,
            now=NOW,
            runtime_router_revision="abc1234",
            run_id="run-1",
            operational_status="clean",
        )
        assert result.evaluated_candidates == 1
        assert result.tick_created is True
        assert result.eligible_candidates == 1
        assert result.decisions_written == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM postmarket_delivery_dry_runs"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM postmarket_delivery_dry_run_ticks"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM postmarket_delivery_dry_run_tick_decisions"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_missing_rank_run_is_an_explicit_empty_tick():
    conn = _evidence_conn()
    try:
        policy = _policy()
        result = run_dry_run_tick(
            conn,
            policy,
            _authorization(policy),
            session=SESSION + timedelta(days=1),
            now=NOW,
            runtime_router_revision="abc1234",
            run_id="run-1",
            operational_status="degraded",
        )
        assert result.rank_run_id is None
        assert result.tick_created is True
        assert result.evaluated_candidates == 0
        assert result.operational_status == "degraded"
    finally:
        conn.close()


def test_incomplete_rank_evidence_join_fails_instead_of_silently_dropping():
    conn = _evidence_conn()
    try:
        conn.execute(
            "UPDATE postmarket_candidate_ranks SET transition_id=999"
        )
        conn.commit()
        with pytest.raises(ValueError, match="evidence join is incomplete"):
            load_latest_delivery_candidates(
                conn, session=SESSION, rank_version=1
            )
    finally:
        conn.close()


def test_health_is_safe_when_disabled_and_strict_when_enabled(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    assert evaluate_dry_run_health(
        heartbeat, enabled=False, expected_revision="abc1234", now=NOW
    ).healthy
    heartbeat.write_text(json.dumps({
        "ts_utc": (NOW - timedelta(seconds=30)).isoformat(),
        "status": "ok",
        "enabled": True,
        "observer": RUN_MODE,
        "code_version": "abc1234",
    }))
    assert evaluate_dry_run_health(
        heartbeat, enabled=True, expected_revision="abc1234", now=NOW
    ).healthy
    assert not evaluate_dry_run_health(
        heartbeat, enabled=True, expected_revision="def5678", now=NOW
    ).healthy


def test_service_and_health_have_no_live_delivery_provider_or_trading_imports():
    for filename in (
        "tradebot/postmarket_delivery_dry_run_shadow.py",
        "tradebot/postmarket_delivery_dry_run_health.py",
    ):
        source = Path(filename).read_text().lower()
        imports = [
            line for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        for forbidden in ("telegram", "outbox", "requests", "alpaca", "broker", "order"):
            assert not any(forbidden in line for line in imports)


def test_compose_service_is_independently_default_off_and_has_no_worker_dependency():
    compose = Path("docker-compose.yml").read_text()
    block = compose.split("  postmarket-customer-dry-run:", 1)[1].split("\n  api:", 1)[0]
    assert "POSTMARKET_CUSTOMER_DRY_RUN_ENABLED:-0" in block
    assert "tradebot.postmarket_delivery_dry_run_shadow" in block
    assert "tradebot.postmarket_delivery_dry_run_health" in block
    assert "depends_on" not in block
