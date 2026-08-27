"""Aggregate postmarket evidence inventory, metric, and control gates."""
from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from tradebot.postmarket_evidence_gate import (
    VERDICT_NOT_READY,
    VERDICT_OWNER_REVIEW,
    _expected_sessions,
    evaluate_evidence_gate,
    load_evidence_manifest,
    main,
)


COVERAGE_START = date(2026, 8, 27)
COVERAGE_END = date(2026, 9, 10)
SESSIONS = _expected_sessions(COVERAGE_START, COVERAGE_END)


def _ratio(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def _session_report(
    session: date,
    *,
    clean: bool = True,
    empirical: bool = True,
    tp: int = 1,
    fp: int = 0,
    tn: int = 1,
    fn: int = 0,
    ambiguous: int = 0,
    direction_mismatches: int = 0,
    latency: float = 60.0,
) -> dict:
    issues = []
    if not clean:
        issues.append(
            {
                "code": "COVERAGE_STARTED_LATE",
                "severity": "blocker",
                "detail": "synthetic late start",
            }
        )
    if empirical and fp:
        issues.append(
            {
                "code": "EMPIRICAL_FALSE_POSITIVES",
                "severity": "blocker",
                "detail": f"{fp} false positives",
            }
        )
    if empirical and fn:
        issues.append(
            {
                "code": "EMPIRICAL_FALSE_NEGATIVES",
                "severity": "blocker",
                "detail": f"{fn} false negatives",
            }
        )
    if empirical and ambiguous:
        issues.append(
            {
                "code": "EMPIRICAL_AMBIGUOUS_LABELS",
                "severity": "blocker",
                "detail": f"{ambiguous} ambiguous labels",
            }
        )
    if empirical and direction_mismatches:
        issues.append(
            {
                "code": "EMPIRICAL_DIRECTION_MISMATCH",
                "severity": "blocker",
                "detail": f"{direction_mismatches} mismatches",
            }
        )
    if not empirical:
        tp = fp = tn = fn = ambiguous = direction_mismatches = 0
        issues.append(
            {
                "code": "EMPIRICAL_MANIFEST_MISSING",
                "severity": "warning",
                "detail": "no empirical manifest",
            }
        )
    definitive = tp + fp + tn + fn
    status = "COMPLETE" if empirical else "NOT_PROVIDED"
    eligible = (
        clean
        and status == "COMPLETE"
        and not fp
        and not fn
        and not ambiguous
        and not direction_mismatches
    )
    return {
        "audit_version": 1,
        "audit_code_version": "audit123",
        "session": session.isoformat(),
        "operational_clean": clean,
        "session_evidence_eligible": eligible,
        "catalyst_ledger": {"status": "success"},
        "operational": {
            "candidate_observations": 3,
            "unique_candidates": 1,
            "code_versions": ["observer123"],
            "data_feeds": ["sip"],
            "market_data_providers": ["alpaca"],
            "observer_versions": [1],
            "threshold_snapshots": 1,
            "fetch_errors": 0,
            "failed_invariants": 0,
        },
        "empirical": {
            "status": status,
            "definitive_labels": definitive,
            "ambiguous_labels": ambiguous,
            "true_positives": tp,
            "false_positives": fp,
            "true_negatives": tn,
            "false_negatives": fn,
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "direction_mismatches": direction_mismatches,
            "mean_detection_latency_seconds": latency if tp else None,
            "max_detection_latency_seconds": latency if tp else None,
        },
        "issues": issues,
    }


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _policy() -> dict:
    return {
        "min_clean_sessions": 10,
        "min_definitive_labels": 20,
        "min_positive_labels": 10,
        "min_recall": 0.95,
        "min_precision": 0.90,
        "max_detection_latency_seconds": 330,
        "allowed_data_feeds": ["sip"],
        "allowed_market_data_providers": ["alpaca"],
        "require_zero_dirty_sessions": True,
        "require_zero_direction_mismatches": True,
        "require_complete_session_inventory": True,
    }


def _materialize(
    tmp_path: Path,
    *,
    reports: list[dict] | None = None,
    sessions: tuple[date, ...] = SESSIONS,
    controls=("failure_injection", "kill_switch", "rollback_runbook"),
    policy: dict | None = None,
) -> tuple[Path, dict]:
    reports = reports or [_session_report(session) for session in sessions]
    report_artifacts = []
    for session, report in zip(sessions, reports):
        relative = Path("reports") / f"{session}.json"
        digest = _write_json(tmp_path / relative, report)
        report_artifacts.append(
            {"session": session.isoformat(), "path": str(relative), "sha256": digest}
        )
    control_artifacts = []
    for kind in controls:
        relative = Path("controls") / f"{kind}.json"
        control_payload = {
            "schema_version": 1,
            "kind": kind,
            "status": "passed",
            "revision": "observer123",
            "completed_at_utc": "2026-09-11T10:00:00+00:00",
            "checks": [
                {
                    "name": f"{kind}_exercise",
                    "passed": True,
                    "evidence": "synthetic reviewed evidence",
                }
            ],
        }
        digest = _write_json(tmp_path / relative, control_payload)
        control_artifacts.append(
            {
                "kind": kind,
                "path": str(relative),
                "sha256": digest,
                "revision": "observer123",
                "completed_at_utc": "2026-09-11T10:00:00+00:00",
            }
        )
    manifest = {
        "schema_version": 1,
        "status": "locked",
        "evidence_set_version": "postmarket-shadow-v1",
        "created_at_utc": "2026-09-11T12:00:00+00:00",
        "coverage_start": COVERAGE_START.isoformat(),
        "coverage_end": COVERAGE_END.isoformat(),
        "policy": policy or _policy(),
        "session_reports": report_artifacts,
        "control_artifacts": control_artifacts,
    }
    manifest_path = tmp_path / "evidence.json"
    _write_json(manifest_path, manifest)
    return manifest_path, manifest


def _failed_checks(report) -> set[str]:
    return {check.code for check in report.checks if not check.passed}


def test_ten_clean_scored_sessions_and_controls_reach_owner_review(tmp_path):
    manifest_path, _ = _materialize(tmp_path)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert report.verdict == VERDICT_OWNER_REVIEW
    assert report.metrics.expected_sessions == report.metrics.clean_sessions == 10
    assert report.metrics.dirty_sessions == 0
    assert report.metrics.definitive_labels == 20
    assert report.metrics.positive_labels == 10
    assert report.metrics.precision == report.metrics.recall == 1.0
    assert report.metrics.mean_detection_latency_seconds == 60
    assert report.metrics.max_detection_latency_seconds == 60
    assert all(check.passed for check in report.checks)


def test_current_partial_session_is_honestly_not_ready(tmp_path):
    session = (date(2026, 8, 26),)
    reports = [_session_report(session[0], clean=False, empirical=False)]
    manifest_path, manifest = _materialize(tmp_path, reports=reports, sessions=session)
    manifest["coverage_start"] = manifest["coverage_end"] = "2026-08-26"
    _write_json(manifest_path, manifest)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert report.verdict == VERDICT_NOT_READY
    assert report.metrics.clean_sessions == 0
    assert report.metrics.dirty_sessions == 1
    assert {
        "MIN_CLEAN_SESSIONS",
        "ZERO_DIRTY_SESSIONS",
        "MIN_DEFINITIVE_LABELS",
        "MIN_POSITIVE_LABELS",
        "MIN_RECALL",
        "MIN_PRECISION",
        "MAX_DETECTION_LATENCY",
    } <= _failed_checks(report)


def test_missing_session_cannot_be_hidden_from_declared_range(tmp_path):
    manifest_path, _ = _materialize(tmp_path, sessions=SESSIONS[:-1])

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert report.verdict == VERDICT_NOT_READY
    assert "COMPLETE_SESSION_INVENTORY" in _failed_checks(report)


def test_one_dirty_session_fails_zero_dirty_policy(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[3] = _session_report(SESSIONS[3], clean=False)
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert report.verdict == VERDICT_NOT_READY
    assert report.metrics.clean_sessions == 9
    assert "ZERO_DIRTY_SESSIONS" in _failed_checks(report)


def test_aggregate_recall_not_best_session_recall_controls_verdict(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[0] = _session_report(SESSIONS[0], tp=0, fn=1, tn=1)
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert report.metrics.recall == 0.9
    assert "MIN_RECALL" in _failed_checks(report)
    assert report.verdict == VERDICT_NOT_READY


def test_aggregate_precision_is_reported_and_gated(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[0] = _session_report(SESSIONS[0], tp=1, fp=1, tn=0)
    policy = _policy()
    policy["min_precision"] = 0.95
    manifest_path, _ = _materialize(tmp_path, reports=reports, policy=policy)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert round(report.metrics.precision, 3) == 0.909
    assert "MIN_PRECISION" in _failed_checks(report)


def test_worst_session_latency_not_average_controls_verdict(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[5] = _session_report(SESSIONS[5], latency=331)
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert report.metrics.mean_detection_latency_seconds == 87.1
    assert report.metrics.max_detection_latency_seconds == 331
    assert "MAX_DETECTION_LATENCY" in _failed_checks(report)


def test_all_three_control_artifacts_are_required(tmp_path):
    manifest_path, _ = _materialize(
        tmp_path, controls=("failure_injection", "kill_switch")
    )

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert "REQUIRED_CONTROL_ARTIFACTS" in _failed_checks(report)
    assert report.verdict == VERDICT_NOT_READY


def test_present_but_failed_control_artifact_is_not_accepted(tmp_path):
    manifest_path, manifest = _materialize(tmp_path)
    control = manifest["control_artifacts"][0]
    path = tmp_path / control["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    payload["checks"][0]["passed"] = False
    control["sha256"] = _write_json(path, payload)
    _write_json(manifest_path, manifest)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert "CONTROL_ARTIFACTS_PASSED" in _failed_checks(report)
    assert report.verdict == VERDICT_NOT_READY


def test_control_summary_cannot_claim_pass_when_a_check_failed(tmp_path):
    manifest_path, manifest = _materialize(tmp_path)
    control = manifest["control_artifacts"][0]
    path = tmp_path / control["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["checks"][0]["passed"] = False
    control["sha256"] = _write_json(path, payload)
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="status contradicts"):
        evaluate_evidence_gate(load_evidence_manifest(manifest_path))


def test_control_from_unrelated_revision_does_not_cover_evidence_era(tmp_path):
    manifest_path, manifest = _materialize(tmp_path)
    control = manifest["control_artifacts"][0]
    path = tmp_path / control["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revision"] = control["revision"] = "stale-revision"
    control["sha256"] = _write_json(path, payload)
    _write_json(manifest_path, manifest)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert "CONTROL_REVISIONS_COVERED" in _failed_checks(report)
    assert report.verdict == VERDICT_NOT_READY


def test_unapproved_feed_or_provider_era_cannot_be_mixed_in(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[4] = deepcopy(reports[4])
    reports[4]["operational"]["data_feeds"] = ["iex"]
    reports[4]["operational"]["market_data_providers"] = ["other-provider"]
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    report = evaluate_evidence_gate(load_evidence_manifest(manifest_path))

    assert {"ALLOWED_DATA_FEEDS", "ALLOWED_MARKET_DATA_PROVIDERS"} <= _failed_checks(
        report
    )
    assert report.verdict == VERDICT_NOT_READY


def test_tampered_session_artifact_fails_before_metrics(tmp_path):
    manifest_path, manifest = _materialize(tmp_path)
    path = tmp_path / manifest["session_reports"][0]["path"]
    path.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        evaluate_evidence_gate(load_evidence_manifest(manifest_path))


def test_relative_artifacts_cannot_escape_evidence_directory(tmp_path):
    manifest_path, manifest = _materialize(tmp_path)
    manifest["session_reports"][0]["path"] = "../outside.json"
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="stay inside"):
        load_evidence_manifest(manifest_path)


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("min_clean_sessions", 9, "integer >= 10"),
        ("min_recall", 0.94, "between 0.95 and 1"),
        ("max_detection_latency_seconds", 331, "one 5-minute bar"),
        ("require_zero_dirty_sessions", False, "must all be true"),
    ],
)
def test_manifest_cannot_weaken_program_floor(tmp_path, field, value, expected):
    policy = _policy()
    policy[field] = value
    manifest_path, _ = _materialize(tmp_path, policy=policy)

    with pytest.raises(ValueError, match=expected):
        load_evidence_manifest(manifest_path)


def test_report_confusion_matrix_must_conserve_definitive_labels(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[0] = deepcopy(reports[0])
    reports[0]["empirical"]["definitive_labels"] = 999
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    with pytest.raises(ValueError, match="confusion matrix"):
        evaluate_evidence_gate(load_evidence_manifest(manifest_path))


def test_report_eligibility_cannot_contradict_its_evidence(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[0] = deepcopy(reports[0])
    reports[0]["session_evidence_eligible"] = False
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    with pytest.raises(ValueError, match="eligibility contradicts"):
        evaluate_evidence_gate(load_evidence_manifest(manifest_path))


def test_clean_report_cannot_hide_fetch_errors_or_threshold_drift(tmp_path):
    reports = [_session_report(session) for session in SESSIONS]
    reports[0] = deepcopy(reports[0])
    reports[0]["operational"]["fetch_errors"] = 1
    reports[0]["operational"]["threshold_snapshots"] = 2
    manifest_path, _ = _materialize(tmp_path, reports=reports)

    with pytest.raises(ValueError, match="clean verdict contradicts"):
        evaluate_evidence_gate(load_evidence_manifest(manifest_path))


def test_cli_exit_status_distinguishes_owner_review_not_ready_and_invalid(
    tmp_path, capsys
):
    passing_path, _ = _materialize(tmp_path / "passing")
    assert main([str(passing_path), "--compact"]) == 0
    passing = json.loads(capsys.readouterr().out)
    assert passing["verdict"] == VERDICT_OWNER_REVIEW

    partial_session = (date(2026, 8, 26),)
    partial_path, partial_manifest = _materialize(
        tmp_path / "partial",
        reports=[_session_report(partial_session[0], clean=False, empirical=False)],
        sessions=partial_session,
    )
    partial_manifest["coverage_start"] = partial_manifest["coverage_end"] = "2026-08-26"
    _write_json(partial_path, partial_manifest)
    assert main([str(partial_path), "--compact"]) == 1
    partial = json.loads(capsys.readouterr().out)
    assert partial["verdict"] == VERDICT_NOT_READY

    assert main([str(tmp_path / "missing.json"), "--compact"]) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["error"].startswith("FileNotFoundError:")


def test_gate_import_graph_has_no_live_database_or_delivery_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_evidence_gate.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = (
        "requests",
        "sqlite3",
        "tradebot.alerts",
        "tradebot.journal",
        "tradebot.marketdata",
        "tradebot.telegram_bot",
        "tradebot.vendors",
    )
    assert not any(module.startswith(forbidden) for module in imports)
