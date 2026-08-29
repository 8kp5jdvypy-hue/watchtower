"""Blinded label manifests enforce the experiment's immutable truth rule."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from tradebot.postmarket_empirical import (
    EligibilityRule,
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
)
from tradebot.postmarket_label_manifest import ingest_label_manifest, parse_label_manifest


DEV = date(2026, 8, 20)
HOLDOUT = date(2026, 8, 21)
OBSERVED = datetime(2026, 8, 21, 1, 20, tzinfo=timezone.utc)
ELIGIBILITY = EligibilityRule(8.0, 250_000, 2)


def _payload():
    return {
        "schema_version": 1,
        "status": "locked",
        "manifest_version": "review-2026-08-20-v1",
        "session": DEV.isoformat(),
        "created_at_utc": "2026-08-21T01:10:00+00:00",
        "labeler": "independent-reviewer",
        "label_method": "multi_provider_reconciliation",
        "blinded_to_observer_output": True,
        "eligibility": {
            "move_pct": 8.0,
            "min_cumulative_notional": 250_000,
            "persistence_bars": 2,
        },
        "artifacts": [
            {
                "provider": "provider-a",
                "feed": "sip",
                "endpoint": "minute-bars",
                "acquired_at_utc": "2026-08-21T01:00:00+00:00",
                "sha256": "a" * 64,
            },
            {
                "provider": "provider-b",
                "feed": "consolidated",
                "endpoint": "minute-bars",
                "acquired_at_utc": "2026-08-21T01:01:00+00:00",
                "sha256": "b" * 64,
            },
        ],
        "labels": [
            {
                "symbol": "AAA",
                "classification": "eligible",
                "direction": "up",
                "eligible_at_utc": "2026-08-20T20:10:00+00:00",
                "max_abs_move_pct": 9.0,
                "persistence_bars_observed": 2,
                "cumulative_notional": 500_000,
                "reason_code": "QUALIFIED_ON_INDEPENDENT_BARS",
                "rationale": "two independent sources agreed before review",
            },
            {
                "symbol": "BBB",
                "classification": "ineligible",
                "direction": None,
                "eligible_at_utc": None,
                "max_abs_move_pct": 4.0,
                "persistence_bars_observed": 1,
                "cumulative_notional": 100_000,
                "reason_code": "MOVE_BELOW_RULE",
                "rationale": "independent bars did not satisfy the locked move rule",
            },
        ],
    }


def _raw(payload=None):
    return json.dumps(payload or _payload(), sort_keys=True, separators=(",", ":")).encode()


def _conn():
    conn = sqlite3.connect(":memory:")
    create_locked_experiment(
        conn,
        experiment_id="rank-v1-exp-1",
        created_at=datetime(2026, 8, 21, 12, tzinfo=timezone.utc),
        created_by="owner",
        rank_version=1,
        label_method="multi_provider_reconciliation",
        development_sessions=(DEV,),
        holdout_sessions=(HOLDOUT,),
        eligibility_rule=ELIGIBILITY,
        selection_rule=SelectionRule(60, 10),
        policy=ExperimentPolicy(.9, .95, 10, 5),
    )
    return conn


def test_manifest_is_strict_blinded_and_rule_consistent():
    manifest = parse_label_manifest(_raw(), observed_at=OBSERVED)
    assert manifest.eligibility == ELIGIBILITY
    assert [label.symbol for label in manifest.labels] == ["AAA", "BBB"]
    payload = _payload()
    payload["blinded_to_observer_output"] = False
    with pytest.raises(ValueError, match="blinded"):
        parse_label_manifest(_raw(payload), observed_at=OBSERVED)
    payload = _payload()
    payload["labels"][0]["persistence_bars_observed"] = 1
    with pytest.raises(ValueError, match="did not satisfy"):
        parse_label_manifest(_raw(payload), observed_at=OBSERVED)
    payload = _payload()
    payload["labels"][1].update({
        "max_abs_move_pct": 9.0,
        "persistence_bars_observed": 2,
        "cumulative_notional": 500_000,
    })
    with pytest.raises(ValueError, match="contradicted"):
        parse_label_manifest(_raw(payload), observed_at=OBSERVED)


def test_ingestion_is_atomic_append_only_and_digest_idempotent(tmp_path):
    conn = _conn()
    path = tmp_path / "labels.json"
    path.write_bytes(_raw())
    manifest_id, created, written, manifest = ingest_label_manifest(
        conn, path, experiment_id="rank-v1-exp-1", observed_at=OBSERVED,
        code_version="abc1234", run_id="run-1",
    )
    same_id, created_again, written_again, _ = ingest_label_manifest(
        conn, path, experiment_id="rank-v1-exp-1", observed_at=OBSERVED,
        code_version="abc1234", run_id="run-2",
    )
    assert (created, written) == (True, 2)
    assert (same_id, created_again, written_again) == (manifest_id, False, 0)
    assert conn.execute("SELECT COUNT(*) FROM postmarket_independent_labels").fetchone()[0] == 2
    assert conn.execute(
        "SELECT artifact_sha256 FROM postmarket_independent_labels LIMIT 1"
    ).fetchone()[0] == manifest.manifest_sha256
    stored_raw = conn.execute(
        "SELECT manifest_json FROM postmarket_empirical_label_manifests"
    ).fetchone()[0]
    assert bytes(stored_raw) == _raw()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE postmarket_empirical_label_manifests SET labeler='changed'"
        )


def test_manifest_must_match_locked_experiment_rule(tmp_path):
    conn = _conn()
    payload = _payload()
    payload["eligibility"]["move_pct"] = 9.0
    payload["labels"][0]["max_abs_move_pct"] = 10.0
    path = tmp_path / "labels.json"
    path.write_bytes(_raw(payload))
    with pytest.raises(ValueError, match="did not match"):
        ingest_label_manifest(
            conn, path, experiment_id="rank-v1-exp-1", observed_at=OBSERVED,
            code_version="abc1234", run_id="run",
        )
    assert conn.execute("SELECT COUNT(*) FROM postmarket_independent_labels").fetchone()[0] == 0
