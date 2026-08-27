"""Daily postmarket audit coverage, provenance, and empirical-label gates."""
from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Bar
from tradebot.journal import connect as connect_journal
from tradebot.postmarket import ReactionEvaluation, connect, record_shadow_tick
from tradebot.postmarket_audit import (
    CatalystLedgerEvidence,
    _session_window,
    audit_session,
    load_catalyst_ledger_evidence,
    load_empirical_manifest,
    main,
    write_completed_operational_audits,
)


SESSION = date(2026, 8, 26)
START = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 0, 5, tzinfo=timezone.utc)


def _evaluation(symbol: str, outcome: str, tick: datetime) -> ReactionEvaluation:
    if outcome == "CANDIDATE":
        return ReactionEvaluation(
            symbol=symbol,
            outcome=outcome,
            reason="independent test candidate",
            event_date=SESSION,
            bar=Bar(symbol, tick - timedelta(minutes=5), 109, 111, 108, 110, 10_000),
            rth_close=100,
            cumulative_volume=20_000,
            cumulative_notional=2_200_000,
            move_pct=10,
            direction="up",
            persistence_bars=2,
            data_age_seconds=0,
        )
    return ReactionEvaluation(
        symbol=symbol,
        outcome=outcome,
        reason="independent test rejection",
        event_date=SESSION,
        rth_close=100,
        move_pct=1,
        direction="up",
        data_age_seconds=0,
    )


def _populate(
    path: Path,
    *,
    first: datetime = START,
    final: datetime = END,
    scheduled_symbols: int = 2,
    inject_fetch_error: bool = False,
    revision_drift: bool = False,
) -> None:
    conn = connect(path)
    tick = first
    sequence = 0
    while tick <= final:
        mover_outcome = "CANDIDATE" if tick >= START + timedelta(minutes=10) else "BELOW_MOVE"
        quiet_outcome = (
            "FETCH_ERROR" if inject_fetch_error and tick == START + timedelta(minutes=30)
            else "BELOW_MOVE"
        )
        evaluations = [_evaluation("MOVER", mover_outcome, tick)]
        if scheduled_symbols == 2:
            evaluations.append(_evaluation("QUIET", quiet_outcome, tick))
        record_shadow_tick(
            conn,
            evaluations,
            session=SESSION,
            tick_utc=tick,
            completed_utc=tick + timedelta(seconds=1),
            run_id="run-1",
            run_mode="postmarket-shadow",
            code_version="def456" if revision_drift and sequence == 30 else "abc123",
            data_feed="sip",
            scheduled_symbols=scheduled_symbols,
            latency_ms=1000,
        )
        tick += timedelta(seconds=60)
        sequence += 1
    conn.close()


def _manifest_payload() -> dict:
    return {
        "schema_version": 1,
        "status": "locked",
        "manifest_version": "2026-08-26-v1",
        "session": SESSION.isoformat(),
        "created_at_utc": "2026-08-27T14:00:00+00:00",
        "labeler": "independent-reviewer-1",
        "label_method": "blind_bar_review",
        "blinded_to_observer_output": True,
        "eligibility": {
            "move_pct": 8.0,
            "min_cumulative_notional": 100_000.0,
            "persistence_bars": 2,
        },
        "artifacts": [
            {
                "provider": "reference-provider",
                "feed": "consolidated",
                "endpoint": "historical-bars/5Min",
                "acquired_at_utc": "2026-08-27T13:00:00+00:00",
                "sha256": "a" * 64,
            }
        ],
        "labels": [
            {
                "symbol": "MOVER",
                "classification": "eligible",
                "direction": "up",
                "eligible_at_utc": "2026-08-26T20:10:00+00:00",
                "max_abs_move_pct": 11.0,
                "cumulative_notional": 2_200_000,
                "reason_code": "PERSISTENT_LIQUID_MOVE",
                "rationale": "Two completed reference bars held above eight percent.",
            },
            {
                "symbol": "QUIET",
                "classification": "ineligible",
                "direction": None,
                "eligible_at_utc": None,
                "max_abs_move_pct": 1.0,
                "cumulative_notional": 5_000_000,
                "reason_code": "BELOW_MOVE",
                "rationale": "Reference bars remained below the move threshold.",
            },
        ],
    }


def _catalyst_evidence(symbols=("MOVER", "QUIET")) -> CatalystLedgerEvidence:
    return CatalystLedgerEvidence(
        status="success",
        attempts=1,
        latest_completed_at_utc="2026-08-26T12:00:00+00:00",
        requested_symbols=13_091,
        fetched_events=2,
        matched_events=2,
        windows_created=4,
        code_version="abc123",
        run_mode="postmarket-shadow",
        run_id="ingestion-1",
        error=None,
        expected_symbols=tuple(symbols),
    )


def _write_manifest(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "empirical.json"
    path.write_text(json.dumps(payload or _manifest_payload()), encoding="utf-8")
    return path


def test_complete_session_and_locked_labels_produce_activation_evidence(tmp_path):
    db_path = tmp_path / "postmarket.db"
    _populate(db_path)
    manifest = load_empirical_manifest(_write_manifest(tmp_path))
    conn = connect(db_path)

    report = audit_session(
        conn,
        SESSION,
        database=str(db_path),
        manifest=manifest,
        catalyst_ledger=_catalyst_evidence(),
        audit_code_version="audit123",
    )

    assert report.operational_clean is True
    assert report.session_evidence_eligible is True
    assert report.operational.window_coverage_pct == 100.0
    assert report.operational.max_tick_gap_seconds == 60.0
    assert report.operational.scheduled_symbols == report.operational.observed_symbols == 2
    assert report.operational.unique_candidates == 1
    assert report.empirical.status == "COMPLETE"
    assert (report.empirical.true_positives, report.empirical.true_negatives) == (1, 1)
    assert report.empirical.precision == report.empirical.recall == 1.0
    assert report.empirical.max_detection_latency_seconds == 1.0
    assert report.issues == ()


def test_late_start_is_not_a_clean_session_even_when_every_tick_is_internally_valid(tmp_path):
    db_path = tmp_path / "partial.db"
    _populate(db_path, first=datetime(2026, 8, 26, 23, 38, tzinfo=timezone.utc))
    conn = connect(db_path)

    report = audit_session(conn, SESSION)

    assert report.operational_clean is False
    assert report.session_evidence_eligible is False
    assert report.operational.window_coverage_pct < 12
    assert "COVERAGE_STARTED_LATE" in {issue.code for issue in report.issues}


def test_fetch_error_fails_the_session_and_remains_counted(tmp_path):
    db_path = tmp_path / "fetch-error.db"
    _populate(db_path, inject_fetch_error=True)
    conn = connect(db_path)

    report = audit_session(conn, SESSION)

    assert report.operational_clean is False
    assert report.operational.fetch_errors == 1
    assert "FETCH_ERRORS" in {issue.code for issue in report.issues}


def test_symbol_conservation_failure_is_loud(tmp_path):
    db_path = tmp_path / "conservation.db"
    _populate(db_path, scheduled_symbols=3)
    conn = connect(db_path)

    report = audit_session(conn, SESSION)

    assert report.operational_clean is False
    assert report.operational.failed_invariants > 0
    assert "TICK_INVARIANT_FAILED" in {issue.code for issue in report.issues}


def test_mid_session_revision_drift_fails_cleanliness(tmp_path):
    db_path = tmp_path / "revision.db"
    _populate(db_path, revision_drift=True)
    conn = connect(db_path)

    report = audit_session(conn, SESSION)

    assert report.operational_clean is False
    assert report.operational.code_versions == ("abc123", "def456")
    assert "CODE_VERSION_DRIFT" in {issue.code for issue in report.issues}


def test_missing_empirical_manifest_is_visible_but_does_not_rewrite_operational_truth(tmp_path):
    db_path = tmp_path / "operational-only.db"
    _populate(db_path)
    conn = connect(db_path)

    report = audit_session(conn, SESSION)

    assert report.operational_clean is True
    assert report.session_evidence_eligible is False
    assert report.empirical.status == "NOT_PROVIDED"
    issue = next(issue for issue in report.issues if issue.code == "EMPIRICAL_MANIFEST_MISSING")
    assert issue.severity == "warning"


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ({"status": "draft"}, "status must be 'locked'"),
        ({"blinded_to_observer_output": False}, "blinded_to_observer_output=true"),
        ({"schema_version": True}, "unsupported empirical schema"),
    ],
)
def test_manifest_fails_closed_on_unlocked_or_unblinded_evidence(
    tmp_path, mutation, expected
):
    payload = _manifest_payload()
    payload.update(mutation)

    with pytest.raises(ValueError, match=expected):
        load_empirical_manifest(_write_manifest(tmp_path, payload))


def test_multi_provider_method_requires_two_distinct_providers(tmp_path):
    payload = _manifest_payload()
    payload["label_method"] = "multi_provider_reconciliation"

    with pytest.raises(ValueError, match="at least two providers"):
        load_empirical_manifest(_write_manifest(tmp_path, payload))


def test_manifest_rejects_malformed_artifact_digest(tmp_path):
    payload = _manifest_payload()
    payload["artifacts"][0]["sha256"] = "not-a-digest"

    with pytest.raises(ValueError, match="64-character hexadecimal"):
        load_empirical_manifest(_write_manifest(tmp_path, payload))


def test_manifest_symbol_coverage_must_match_scheduled_funnel(tmp_path):
    db_path = tmp_path / "labels.db"
    _populate(db_path)
    payload = _manifest_payload()
    payload["labels"] = payload["labels"][:1]
    manifest = load_empirical_manifest(_write_manifest(tmp_path, payload))
    conn = connect(db_path)

    report = audit_session(
        conn,
        SESSION,
        manifest=manifest,
        catalyst_ledger=_catalyst_evidence(),
        audit_code_version="audit123",
    )

    assert report.session_evidence_eligible is False
    assert report.empirical.status == "INCOMPLETE"
    assert "EMPIRICAL_LABELS_MISSING" in {issue.code for issue in report.issues}


def test_empirical_false_negative_fails_cli_quality_gate(tmp_path, capsys):
    db_path = tmp_path / "miss.db"
    _populate(db_path)
    payload = _manifest_payload()
    quiet = payload["labels"][1]
    quiet.update(
        classification="eligible",
        direction="up",
        eligible_at_utc="2026-08-26T20:10:00+00:00",
        max_abs_move_pct=9.0,
        cumulative_notional=2_000_000,
        reason_code="PERSISTENT_LIQUID_MOVE",
        rationale="Independent reference says this reaction was eligible.",
    )
    manifest_path = _write_manifest(tmp_path, payload)

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--session",
            str(SESSION),
            "--manifest",
            str(manifest_path),
            "--compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["empirical"]["false_negatives"] == 1
    assert "EMPIRICAL_FALSE_NEGATIVES" in {
        issue["code"] for issue in report["issues"]
    }


def test_empirical_policy_must_match_recorded_observer_thresholds(tmp_path):
    db_path = tmp_path / "policy.db"
    _populate(db_path)
    payload = _manifest_payload()
    payload["eligibility"]["move_pct"] = 9.0
    manifest = load_empirical_manifest(_write_manifest(tmp_path, payload))
    conn = connect(db_path)

    report = audit_session(
        conn,
        SESSION,
        manifest=manifest,
        catalyst_ledger=_catalyst_evidence(),
        audit_code_version="audit123",
    )

    assert report.session_evidence_eligible is False
    assert "EMPIRICAL_POLICY_MISMATCH" in {issue.code for issue in report.issues}


def test_operational_reports_wait_for_window_end_and_never_overwrite(tmp_path):
    db_path = tmp_path / "automatic.db"
    output_dir = tmp_path / "audits"
    _populate(db_path, first=datetime(2026, 8, 26, 23, 38, tzinfo=timezone.utc))

    assert write_completed_operational_audits(
        db_path, output_dir, now=END
    ) == ()
    reports = write_completed_operational_audits(
        db_path, output_dir, now=END + timedelta(seconds=1)
    )
    report_path = output_dir / "postmarket_audit_2026-08-26_v1.json"
    original = report_path.read_bytes()

    assert len(reports) == 1
    assert reports[0].operational_clean is False
    assert "COVERAGE_STARTED_LATE" in {issue.code for issue in reports[0].issues}
    assert write_completed_operational_audits(
        db_path, output_dir, now=END + timedelta(hours=1)
    ) == ()
    assert report_path.read_bytes() == original


def test_calendar_windows_handle_early_close_and_daylight_saving():
    early_start, early_end = _session_window(date(2026, 11, 27))
    winter_start, winter_end = _session_window(date(2026, 11, 2))

    assert early_start == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert early_end == datetime(2026, 11, 28, 1, 5, tzinfo=timezone.utc)
    assert winter_start == datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
    assert winter_end == datetime(2026, 11, 3, 1, 5, tzinfo=timezone.utc)


def test_manifest_rejects_unknown_fields_instead_of_ignoring_typos(tmp_path):
    payload = _manifest_payload()
    payload["label_methdo"] = "blind_bar_review"

    with pytest.raises(ValueError, match="fields are invalid"):
        load_empirical_manifest(_write_manifest(tmp_path, payload))


def test_catalyst_ledger_must_conserve_into_observed_symbols(tmp_path):
    db_path = tmp_path / "catalyst-mismatch.db"
    _populate(db_path)
    conn = connect(db_path)

    report = audit_session(
        conn,
        SESSION,
        catalyst_ledger=_catalyst_evidence(("MOVER", "QUIET", "OMITTED")),
    )

    assert report.operational_clean is False
    assert "CATALYST_FUNNEL_MISMATCH" in {issue.code for issue in report.issues}


def test_catalyst_ingestion_failure_is_not_a_clean_session(tmp_path):
    db_path = tmp_path / "catalyst-failed.db"
    _populate(db_path)
    conn = connect(db_path)
    evidence = _catalyst_evidence()
    evidence = replace(evidence, status="failed", error="provider timeout")

    report = audit_session(conn, SESSION, catalyst_ledger=evidence)

    assert report.operational_clean is False
    assert "CATALYST_INGESTION_UNVERIFIED" in {issue.code for issue in report.issues}


def test_catalyst_success_without_revision_is_not_attributable(tmp_path):
    db_path = tmp_path / "catalyst-unknown-revision.db"
    _populate(db_path)
    conn = connect(db_path)
    evidence = replace(_catalyst_evidence(), code_version="unknown")

    report = audit_session(conn, SESSION, catalyst_ledger=evidence)

    assert report.operational_clean is False
    assert "CATALYST_PROVENANCE_INCOMPLETE" in {
        issue.code for issue in report.issues
    }


def test_catalyst_ledger_loader_preserves_latest_attempt_provenance(tmp_path):
    journal_path = tmp_path / "journal.db"
    conn = connect_journal(journal_path)
    for symbol in ("MOVER", "QUIET"):
        conn.execute(
            """
            INSERT INTO event_windows
                (symbol,kind,start_utc,end_utc,severity,source,detail,event_date,
                 event_timing,created_at)
            VALUES (?, 'earnings', ?, ?, 'context', 'nasdaq_earnings', ?, ?,
                    'after-hours', ?)
            """,
            (
                symbol,
                START.isoformat(),
                END.isoformat(),
                f"{symbol} earnings",
                SESSION.isoformat(),
                START.isoformat(),
            ),
        )
    conn.execute(
        """
        INSERT INTO event_ingestion_runs
            (provider,kind,report_date,attempted_at,completed_at,status,
             universe_scope,requested_symbols,fetched_events,matched_events,
             windows_created,error,code_version,run_mode,run_id)
        VALUES ('nasdaq_earnings','earnings',?,?,?,'success','market',13091,
                2,2,4,NULL,'abc123','manual','ingestion-1')
        """,
        (
            SESSION.isoformat(),
            (START - timedelta(hours=8)).isoformat(),
            (START - timedelta(hours=7, minutes=59)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    evidence = load_catalyst_ledger_evidence(journal_path, SESSION)

    assert evidence.status == "success"
    assert evidence.expected_symbols == ("MOVER", "QUIET")
    assert evidence.requested_symbols == 13_091
    assert evidence.code_version == "abc123"


def test_cli_reads_database_without_mutating_it(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    _populate(db_path)
    before = db_path.read_bytes()

    exit_code = main(["--db", str(db_path), "--session", str(SESSION), "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["operational_clean"] is True
    assert payload["session_evidence_eligible"] is False
    assert db_path.read_bytes() == before


def test_audit_import_graph_has_no_live_or_delivery_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_audit.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = (
        "requests",
        "tradebot.alerts",
        "tradebot.journal",
        "tradebot.marketdata",
        "tradebot.telegram_bot",
        "tradebot.vendors",
    )
    assert not any(module.startswith(forbidden) for module in imports)
