"""The program ledger is read-only, evidence-based, and fail-closed."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tradebot.postmarket_program_status import (
    STATE_COMPLETE,
    STATE_ERROR,
    build_program_status,
    main,
)
from tradebot import postmarket_program_status as program_status
from tradebot.postmarket_customer_dry_run_campaign import POLICY_FIELDS


NOW = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
SESSIONS = tuple(f"2026-08-{day:02d}" for day in range(10, 20))


def _database(
    path: Path,
    *,
    complete: bool,
    customer_campaign_sha256: str | None,
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE postmarket_candidate_context (id INTEGER);
        CREATE TABLE postmarket_candidate_lifecycle (id INTEGER);
        CREATE TABLE postmarket_candidate_ranks (id INTEGER);
        CREATE TABLE postmarket_rank_experiments (
          experiment_id TEXT, holdout_sessions_json TEXT, policy_json TEXT
        );
        CREATE TABLE postmarket_independent_labels (
          experiment_id TEXT, session TEXT, symbol TEXT, revision INTEGER,
          classification TEXT
        );
        CREATE TABLE postmarket_rank_empirical_runs (
          experiment_id TEXT, split TEXT, input_digest_sha256 TEXT,
          report_json TEXT, report_sha256 TEXT
        );
        CREATE TABLE postmarket_rank_calibration_runs (
          experiment_id TEXT, split TEXT, input_digest_sha256 TEXT,
          report_json TEXT, report_sha256 TEXT
        );
        CREATE TABLE postmarket_customer_dry_run_reviews (
          campaign_sha256 TEXT, case_evidence_sha256 TEXT, symbol TEXT,
          reviewer_role TEXT, independent_of_implementation INTEGER,
          blinded_to_future_outcomes INTEGER
        );
        """
    )
    if complete:
        conn.execute("INSERT INTO postmarket_candidate_context VALUES (1)")
        conn.execute("INSERT INTO postmarket_candidate_lifecycle VALUES (1)")
        conn.execute("INSERT INTO postmarket_candidate_ranks VALUES (1)")
        conn.execute(
            "INSERT INTO postmarket_rank_experiments VALUES (?,?,?)",
            (
                "campaign-1",
                json.dumps(SESSIONS[:2]),
                json.dumps({"min_definitive_labels": 2}),
            ),
        )
        conn.executemany(
            "INSERT INTO postmarket_independent_labels VALUES (?,?,?,?,?)",
            [
                ("campaign-1", SESSIONS[0], "AAA", 1, "eligible"),
                ("campaign-1", SESSIONS[1], "BBB", 1, "ineligible"),
            ],
        )
        empirical = json.dumps(
            {
                "experiment_id": "campaign-1",
                "split": "holdout",
                "input_digest_sha256": "b" * 64,
                "holdout_unblinded": True,
                "passed_locked_policy": True,
                "blocking_reasons": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO postmarket_rank_empirical_runs VALUES (?,?,?,?,?)",
            (
                "campaign-1",
                "holdout",
                "b" * 64,
                empirical,
                hashlib.sha256(empirical.encode()).hexdigest(),
            ),
        )
        calibration = json.dumps(
            {
                "experiment_id": "campaign-1",
                "split": "holdout",
                "input_digest_sha256": "c" * 64,
                "holdout_unblinded": True,
                "calibrated_quality_claim_valid": True,
                "blocking_reasons": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO postmarket_rank_calibration_runs VALUES (?,?,?,?,?)",
            (
                "campaign-1",
                "holdout",
                "c" * 64,
                calibration,
                hashlib.sha256(calibration.encode()).hexdigest(),
            ),
        )
        assert customer_campaign_sha256 is not None
        conn.executemany(
            "INSERT INTO postmarket_customer_dry_run_reviews VALUES (?,?,?,?,?,?)",
            [
                (
                    customer_campaign_sha256,
                    hashlib.sha256(f"case-{index}".encode()).hexdigest(),
                    f"SYM{index % 10}",
                    "independent_market_reviewer",
                    1,
                    1,
                )
                for index in range(20)
            ],
        )
    conn.commit()
    conn.close()


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _customer_campaign() -> dict:
    policy = {
        "min_clean_sessions": 10,
        "min_eligible_decisions": 20,
        "min_independently_reviewed_cases": 20,
        "min_distinct_reviewed_symbols": 10,
        "min_owner_review_approval_rate": 0.9,
        "min_session_coverage_pct": 100,
        "max_scheduled_lag_seconds": 30,
        "max_tick_latency_seconds": 10,
        "allowed_audit_versions": [2],
        "allowed_audit_code_versions": ["abc1234"],
        "allowed_runtime_router_revisions": ["abc1234"],
        **{name: True for name in POLICY_FIELDS if name.startswith("require_")},
    }
    return {
        "schema_version": 3,
        "status": "locked",
        "campaign_id": "customer-campaign-1",
        "locked_at_utc": "2026-08-01T12:00:00+00:00",
        "coverage_start": SESSIONS[0],
        "coverage_end": SESSIONS[-1],
        "expected_sessions": list(SESSIONS),
        "delivery_policy_sha256": "1" * 64,
        "owner_authorization_sha256": "2" * 64,
        "owner_authorization_expires_at_utc": "2026-10-01T00:00:00+00:00",
        "release_id": "release-1",
        "router_version": 1,
        "rank_version": 1,
        "control_evidence_sha256s": [str(index) * 64 for index in range(3, 7)],
        "policy": policy,
        "upstream_discovery_evidence_set_sha256": "7" * 64,
        "upstream_discovery_evidence_gate_sha256": "8" * 64,
        "upstream_discovery_gate_code_version": "abc1234",
        "upstream_discovery_gate_evaluated_at_utc": "2026-08-01T11:00:00+00:00",
        "upstream_calibration_artifact_sha256": "9" * 64,
        "calibration_model_sha256": "a" * 64,
        "calibration_version": 1,
        "calibration_evaluated_at_utc": "2026-08-01T10:00:00+00:00",
    }


def _artifacts(
    audit_dir: Path, evidence_dir: Path, *, complete: bool
) -> str | None:
    audit_dir.mkdir()
    evidence_dir.mkdir()
    if not complete:
        return None
    for index, session in enumerate(SESSIONS, 1):
        _write(
            audit_dir / f"postmarket_discovery_audit_{session}_v4.json",
            {
                "session": session,
                "audit_version": 4,
                "operational_clean": True,
                "session_evidence_eligible": True,
                "issues": [],
            },
        )
        _write(
            audit_dir / f"postmarket_quality_marketwide_{session}_v1.json",
            {
                "session": session,
                "candidate_stream": "marketwide",
                "report_version": 1,
                "operational_complete": True,
                "evidence_eligible": True,
                "issue_codes": [],
            },
        )
        _write(
            audit_dir / f"postmarket_recall_census_{session}_v1.json",
            {
                "session": session,
                "report_version": 1,
                "operational_complete": True,
                "evidence_eligible": True,
                "issue_codes": [],
            },
        )
        _write(
            audit_dir / f"postmarket_recall_provider_{session}_v1.json",
            {
                "session": session,
                "report_version": 1,
                "attempt": 1,
                "operational_complete": True,
                "evidence_eligible": True,
                "issue_codes": [],
            },
        )
    _write(
        evidence_dir / "customer-gate.json",
        {
            "gate_version": 2,
            "verdict": "ELIGIBLE_FOR_SEPARATE_CUSTOMER_DELIVERY_REVIEW",
            "ready_for_customer_delivery_review": True,
            "customer_delivery_enabled": False,
            "checks": [{"code": "ALL_EVIDENCE", "passed": True}],
        },
    )
    campaign = evidence_dir / "customer-campaign.json"
    _write(campaign, _customer_campaign())
    return hashlib.sha256(campaign.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, *, complete: bool) -> tuple[Path, Path, Path]:
    database = tmp_path / "postmarket_shadow.db"
    audits = tmp_path / "audits"
    evidence = tmp_path / "evidence"
    campaign_sha256 = _artifacts(audits, evidence, complete=complete)
    _database(
        database,
        complete=complete,
        customer_campaign_sha256=campaign_sha256,
    )
    return database, audits, evidence


def _report(tmp_path: Path, *, complete: bool):
    database, audits, evidence = _fixture(tmp_path, complete=complete)
    return build_program_status(
        database, audits, evidence, generated_at=NOW
    )


def test_complete_inventory_is_eligible_for_separate_review_but_never_enables_delivery(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(program_status, "_verified_ready_customer_gates", lambda *args: 1)
    report = _report(tmp_path, complete=True)

    assert report.database_integrity_ok is True
    assert report.evidence_collection_complete is True
    assert report.eligible_for_customer_delivery_review is True
    assert report.customer_delivery_enabled is False
    assert report.blockers == ()
    assert all(item.state == STATE_COMPLETE for item in report.milestones)


def test_empty_valid_database_reports_the_first_real_next_action(tmp_path):
    report = _report(tmp_path, complete=False)

    assert report.database_integrity_ok is True
    assert report.evidence_collection_complete is False
    assert report.eligible_for_customer_delivery_review is False
    assert report.customer_delivery_enabled is False
    assert report.blockers[0] == "CLEAN_DISCOVERY_SESSIONS"
    assert "ten clean sessions" in report.next_action


def test_provider_proofs_do_not_count_outside_the_clean_session_set(tmp_path):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    for path in audits.glob("postmarket_recall_provider_*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["session"] = "2026-07-01"
        path.write_text(json.dumps(payload), encoding="utf-8")

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    milestone = next(
        item for item in report.milestones if item.code == "INDEPENDENT_PROVIDER_PROOFS"
    )
    assert milestone.observed == 0
    assert milestone.state != STATE_COMPLETE
    assert report.eligible_for_customer_delivery_review is False


def test_malformed_empirical_report_is_a_visible_error_not_an_empty_holdout(tmp_path):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE postmarket_rank_empirical_runs SET report_json='not-json'")
    conn.commit()
    conn.close()

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    error = next(
        item for item in report.milestones if item.code == "EVIDENCE_LEDGER_VALIDATION"
    )
    assert error.state == STATE_ERROR
    assert "digest does not match" in error.evidence
    assert report.eligible_for_customer_delivery_review is False


def test_conflicting_same_version_audits_fail_closed(tmp_path):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    payload = json.loads(
        (audits / f"postmarket_discovery_audit_{SESSIONS[0]}_v4.json").read_text()
    )
    payload["operational_clean"] = False
    _write(audits / "postmarket_discovery_audit_conflict_v4.json", payload)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    assert "EVIDENCE_LEDGER_VALIDATION" in report.blockers
    assert report.eligible_for_customer_delivery_review is False


def test_customer_gate_cannot_claim_readiness_when_delivery_is_enabled(tmp_path):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    gate = evidence / "customer-gate.json"
    payload = json.loads(gate.read_text())
    payload["customer_delivery_enabled"] = True
    _write(gate, payload)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    milestone = next(
        item for item in report.milestones
        if item.code == "CUSTOMER_DELIVERY_REVIEW_GATE"
    )
    assert milestone.state != STATE_COMPLETE
    assert report.customer_delivery_enabled is False
    assert report.eligible_for_customer_delivery_review is False


def test_one_review_does_not_satisfy_locked_twenty_case_floor(tmp_path, monkeypatch):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    conn = sqlite3.connect(database)
    conn.execute(
        """
        DELETE FROM postmarket_customer_dry_run_reviews
        WHERE rowid NOT IN (
          SELECT MIN(rowid) FROM postmarket_customer_dry_run_reviews
        )
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(program_status, "_verified_ready_customer_gates", lambda *args: 1)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    milestone = next(
        item for item in report.milestones
        if item.code == "INDEPENDENT_CUSTOMER_CASE_REVIEWS"
    )
    assert milestone.observed["reviewed_cases"] == 1
    assert milestone.observed["distinct_symbols"] == 1
    assert milestone.required == {"reviewed_cases": 20, "distinct_symbols": 10}
    assert milestone.state != STATE_COMPLETE
    assert report.next_action == (
        "Run the isolated dry-run campaign and collect independent case reviews."
    )


def test_reviews_are_scoped_to_exact_campaign_digest(tmp_path, monkeypatch):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE postmarket_customer_dry_run_reviews SET campaign_sha256=?",
        ("f" * 64,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(program_status, "_verified_ready_customer_gates", lambda *args: 1)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    milestone = next(
        item for item in report.milestones
        if item.code == "INDEPENDENT_CUSTOMER_CASE_REVIEWS"
    )
    assert milestone.observed["reviewed_cases"] == 0
    assert milestone.observed["distinct_symbols"] == 0
    assert milestone.state != STATE_COMPLETE


def test_holdout_stages_must_share_one_label_ready_experiment(tmp_path, monkeypatch):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    conn = sqlite3.connect(database)
    for table in (
        "postmarket_rank_empirical_runs",
        "postmarket_rank_calibration_runs",
    ):
        raw, = conn.execute(f"SELECT report_json FROM {table}").fetchone()
        payload = json.loads(raw)
        payload["experiment_id"] = "unlinked-experiment"
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        conn.execute(
            f"""
            UPDATE {table}
            SET experiment_id=?,report_json=?,report_sha256=?
            """,
            (
                "unlinked-experiment",
                changed,
                hashlib.sha256(changed.encode()).hexdigest(),
            ),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(program_status, "_verified_ready_customer_gates", lambda *args: 1)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    milestones = {item.code: item for item in report.milestones}
    assert milestones["BLINDED_HOLDOUT_LABELS"].state == STATE_COMPLETE
    assert milestones["EMPIRICAL_HOLDOUT_PASS"].observed == 0
    assert milestones["EMPIRICAL_HOLDOUT_PASS"].state != STATE_COMPLETE
    assert milestones["CALIBRATION_HOLDOUT_PASS"].observed == 0
    assert milestones["CALIBRATION_HOLDOUT_PASS"].state != STATE_COMPLETE


def test_label_revisions_count_latest_symbol_once(tmp_path, monkeypatch):
    database, audits, evidence = _fixture(tmp_path, complete=True)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE postmarket_rank_experiments SET policy_json=?",
        (json.dumps({"min_definitive_labels": 3}),),
    )
    conn.execute(
        "INSERT INTO postmarket_independent_labels VALUES (?,?,?,?,?)",
        ("campaign-1", SESSIONS[0], "AAA", 2, "eligible"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(program_status, "_verified_ready_customer_gates", lambda *args: 1)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    milestone = next(
        item for item in report.milestones if item.code == "BLINDED_HOLDOUT_LABELS"
    )
    assert milestone.observed == 2
    assert milestone.required == 3
    assert milestone.state != STATE_COMPLETE


def test_forged_ready_gate_without_digest_bound_inputs_fails_closed(tmp_path):
    database, audits, evidence = _fixture(tmp_path, complete=True)

    report = build_program_status(database, audits, evidence, generated_at=NOW)

    error = next(
        item for item in report.milestones if item.code == "EVIDENCE_LEDGER_VALIDATION"
    )
    assert error.state == STATE_ERROR
    assert "digest" in error.evidence
    assert report.eligible_for_customer_delivery_review is False


def test_cli_emits_machine_readable_progress_and_exits_incomplete(
    tmp_path, capsys,
):
    database, audits, evidence = _fixture(tmp_path, complete=False)

    exit_code = main(
        [
            "--database",
            str(database),
            "--audit-dir",
            str(audits),
            "--evidence-dir",
            str(evidence),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["eligible_for_customer_delivery_review"] is False
    assert payload["customer_delivery_enabled"] is False
    assert payload["evidence_directory"] == str(evidence.resolve())
    assert payload["blockers"][0] == "CLEAN_DISCOVERY_SESSIONS"
    assert "ten clean sessions" in payload["next_action"]
