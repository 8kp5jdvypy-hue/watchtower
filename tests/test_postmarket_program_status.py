"""The program ledger is read-only, evidence-based, and fail-closed."""
from __future__ import annotations

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


NOW = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
SESSIONS = tuple(f"2026-08-{day:02d}" for day in range(10, 20))


def _database(path: Path, *, complete: bool) -> None:
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
          experiment_id TEXT, session TEXT, classification TEXT
        );
        CREATE TABLE postmarket_rank_empirical_runs (split TEXT, report_json TEXT);
        CREATE TABLE postmarket_rank_calibration_runs (split TEXT, report_json TEXT);
        CREATE TABLE postmarket_customer_dry_run_reviews (reviewer_role TEXT);
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
            "INSERT INTO postmarket_independent_labels VALUES (?,?,?)",
            [
                ("campaign-1", SESSIONS[0], "eligible"),
                ("campaign-1", SESSIONS[1], "ineligible"),
            ],
        )
        conn.execute(
            "INSERT INTO postmarket_rank_empirical_runs VALUES (?,?)",
            (
                "holdout",
                json.dumps(
                    {
                        "split": "holdout",
                        "holdout_unblinded": True,
                        "passed_locked_policy": True,
                        "blocking_reasons": [],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO postmarket_rank_calibration_runs VALUES (?,?)",
            (
                "holdout",
                json.dumps(
                    {
                        "split": "holdout",
                        "holdout_unblinded": True,
                        "calibrated_quality_claim_valid": True,
                        "blocking_reasons": [],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO postmarket_customer_dry_run_reviews VALUES (?)",
            ("independent_market_reviewer",),
        )
    conn.commit()
    conn.close()


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _artifacts(audit_dir: Path, evidence_dir: Path, *, complete: bool) -> None:
    audit_dir.mkdir()
    evidence_dir.mkdir()
    if not complete:
        return
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


def _fixture(tmp_path: Path, *, complete: bool) -> tuple[Path, Path, Path]:
    database = tmp_path / "postmarket_shadow.db"
    audits = tmp_path / "audits"
    evidence = tmp_path / "evidence"
    _database(database, complete=complete)
    _artifacts(audits, evidence, complete=complete)
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
    assert "malformed" in error.evidence
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
