"""Tests for the Missed Mover Investigation Report.

The negative tests matter most here: a diagnostic that guesses is worse
than none, because it will be believed. So as much of this file is about
what the tool REFUSES to conclude as about what it concludes.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import evaluations as ev
from tradebot import miss_report as mr
from tradebot import universe as universe_mod
from tradebot.detectors import Detection
from tradebot.journal import connect as journal_connect
from tradebot.journal import record_decision_event, write_cluster
from tradebot.marketdata import AssetInfo
from tradebot.telegram_bot import db as users_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "miss_report.py"
spec = importlib.util.spec_from_file_location("miss_report_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
sys.modules["miss_report_cli"] = cli
spec.loader.exec_module(cli)

SESSION = "2026-08-24"
SESSION_DATE = date(2026, 8, 24)
TICK = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
SYMBOL = "ZZZZ"          # deliberately NOT on the WATCHLIST
WATCHED = "TSLA"         # on the WATCHLIST


# ---------------------------------------------------------------------------
# Fixture builders — each returns a path, so the tool opens it read-only
# exactly as it would in production.
# ---------------------------------------------------------------------------


def _universe(tmp_path, symbols=(SYMBOL,), *, active=True):
    path = tmp_path / "universe.db"
    conn = universe_mod.connect(path)
    universe_mod.refresh_universe(
        conn, lambda: [AssetInfo(s, "NASDAQ", s, True, True, None, ()) for s in symbols],
        datetime(2026, 8, 8, tzinfo=timezone.utc))
    if not active:
        conn.execute("UPDATE assets SET is_active = 0, delisted_at = ?", ("2026-08-20",))
        conn.commit()
    conn.close()
    return path


def _tick(path, *, events=(), invariant_ok=True, promotion_limit=25, counts=None):
    conn = universe_mod.connect(path)
    conn.execute(
        "INSERT INTO screening_ticks (session, tick_utc, run_id, run_mode, screen_version, "
        "code_version, audit_mode, universe_count, thresholds_json, counts_json, invariant_ok, "
        "promotion_limit, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SESSION, TICK.isoformat(), "scan-1", "live", 1, "sha", 0, 100, "{}",
         json.dumps(counts or {"quiet": 99}), int(invariant_ok), promotion_limit, 12))
    tick_id = conn.execute("SELECT MAX(tick_id) FROM screening_ticks").fetchone()[0]
    for symbol, outcome, score, rank, reasons in events:
        conn.execute(
            "INSERT INTO screening_events (tick_id, symbol, outcome, screen_score, rank, "
            "reasons_json, detail_json) VALUES (?,?,?,?,?,?,?)",
            (tick_id, symbol, outcome, score, rank, json.dumps(reasons), None))
    conn.commit()
    conn.close()
    return path


def _evaluations(tmp_path, rows, *, symbol=SYMBOL, run_id="run-1", run_mode="live"):
    """rows: list of (bar_ts, outcome, kwargs)."""
    path = tmp_path / "evaluations.db"
    conn = ev.connect(path)
    for i, (outcome, extra) in enumerate(rows):
        ev.record_bar_evaluation(
            conn, session=SESSION, symbol=symbol, run_id=run_id, run_mode=run_mode,
            now_utc=TICK.isoformat(),
            bar_ts_utc=(TICK + timedelta(minutes=5 * i)).isoformat(),
            outcome=outcome, open=100.0, high=101.0, low=99.0, close=100.5, volume=5000,
            **extra)
    conn.close()
    return path


def _journal(tmp_path, *, symbol=SYMBOL, score=10.0, alerted=False, suppress=None,
             category=None, ledger=()):
    path = tmp_path / "journal.db"
    conn = journal_connect(path)
    det = Detection(symbol, "gap", TICK, score, "h", {})
    det_id = write_cluster(
        conn, session=SESSION, symbol=symbol, ts_utc=TICK.isoformat(), kinds="gap",
        headlines="h", score=score, close=100.0, atr14=1.0, trend="up", detections=[det],
        code_version_str="sha", alerted=alerted, suppress_reason=suppress)
    if category:
        conn.execute("UPDATE detections SET suppress_category=? WHERE id=?", (category, det_id))
    for stage, decision, reason in ledger:
        record_decision_event(conn, det_id, stage=stage, decision=decision, reason=reason)
    conn.commit()
    conn.close()
    return path, det_id


def _users(tmp_path, det_id, statuses):
    path = tmp_path / "users.db"
    conn = users_db.connect(path)
    for i, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO outbox (id, alert_id, chat_id, priority, text, status, attempts, "
            "next_attempt_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"o{i}", det_id, 1000 + i, 1, "text", status, 0, TICK.isoformat(), TICK.isoformat()))
    conn.commit()
    conn.close()
    return path


def _report(*, symbol=SYMBOL, universe=None, evaluations=None, journal=None, users=None,
            move_pct=9.2, run_id=None):
    return mr.build_report(
        symbol=symbol, session=SESSION, move_pct=move_pct, run_id=run_id,
        universe=mr.open_readonly(universe), evaluations=mr.open_readonly(evaluations),
        journal=mr.open_readonly(journal), users=mr.open_readonly(users))


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


def test_connections_are_genuinely_read_only(tmp_path):
    """Refused by the database, not merely avoided by convention."""
    conn = mr.open_readonly(_universe(tmp_path))

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM assets")


def test_a_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "nope.db"

    assert mr.open_readonly(missing) is None
    assert not missing.exists()  # the diagnostic leaves no files behind


def test_the_report_runs_with_every_store_absent(tmp_path):
    report = _report()

    assert report.verdict == mr.INCONCLUSIVE
    assert all(not ok for ok in report.stores.values())
    assert "INCONCLUSIVE" in mr.render(report)


def test_running_the_report_does_not_modify_any_store(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.1, 27, ["gap"])])
    before = upath.read_bytes()

    _report(universe=upath)

    assert upath.read_bytes() == before


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_not_in_universe(tmp_path):
    report = _report(universe=_universe(tmp_path, symbols=("OTHER",)))

    assert report.verdict == mr.NOT_IN_UNIVERSE
    assert report.conclusion_evidence == mr.DIRECT_ROW


def test_inactive_symbol_is_not_in_universe(tmp_path):
    report = _report(universe=_universe(tmp_path, active=False))

    assert report.verdict == mr.NOT_IN_UNIVERSE
    assert "INACTIVE" in report.findings[0].summary


def test_stage1_not_run_when_no_tick_covered_the_session(tmp_path):
    report = _report(universe=_universe(tmp_path))

    assert report.verdict == mr.STAGE1_NOT_RUN
    assert report.conclusion_evidence == mr.DIRECT_ROW


def test_not_screened(tmp_path):
    path = _tick(_universe(tmp_path), events=[(SYMBOL, "MISSING_FROM_FETCH", None, None, [])])

    report = _report(universe=path)

    assert report.verdict == mr.NOT_SCREENED


def test_candidate_not_promoted_is_a_direct_row(tmp_path):
    path = _tick(_universe(tmp_path),
                 events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.1, 27, ["unusual_volume"])],
                 promotion_limit=25)

    report = _report(universe=path)
    text = mr.render(report)

    assert report.verdict == mr.CANDIDATE_NOT_PROMOTED
    assert report.conclusion_evidence == mr.DIRECT_ROW
    assert "rank=27" in text and "promotion_limit=25" in text
    assert "missed the cut by 2 place(s)" in text


def test_screened_quiet_is_inferred_and_says_so(tmp_path):
    path = _tick(_universe(tmp_path), events=[], invariant_ok=True)

    report = _report(universe=path)

    assert report.verdict == mr.SCREENED_QUIET
    assert report.conclusion_evidence == mr.INFERRED


def test_a_broken_invariant_downgrades_quiet_to_inconclusive(tmp_path):
    """The single place this tool could most easily lie: the quiet verdict
    is a subtraction, valid only while the counts add up."""
    path = _tick(_universe(tmp_path), events=[], invariant_ok=False)

    report = _report(universe=path)

    assert report.verdict == mr.INCONCLUSIVE
    assert report.verdict != mr.SCREENED_QUIET
    assert "invariant_ok = 0" in mr.render(report)


def test_a_direct_row_always_beats_the_quiet_inference(tmp_path):
    path = _tick(_universe(tmp_path), events=[(SYMBOL, "INVALID_BASELINE", None, None, [])])

    report = _report(universe=path)

    assert report.verdict == mr.NOT_SCREENED
    assert report.conclusion_evidence == mr.DIRECT_ROW


# ---------------------------------------------------------------------------
# The ambiguous promoted / no-evaluation case
# ---------------------------------------------------------------------------


def test_promoted_with_no_evaluation_is_reported_as_uninstrumented(tmp_path):
    """Must NOT resolve to a factual blocking reason. The gates that could
    explain it record nothing."""
    path = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])

    report = _report(universe=path)
    text = mr.render(report)

    assert report.verdict == mr.PROMOTED_NO_EVALUATION
    assert report.conclusion_evidence == mr.NOT_INSTRUMENTED
    assert "will NOT guess" in text
    assert "PROMOTED_BUT_BLOCKED" not in text  # never asserted as a fact


def test_absence_of_an_evaluation_row_is_never_reported_as_no_detection(tmp_path):
    path = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])

    report = _report(universe=path)

    assert report.verdict != mr.NO_DETECTION
    assert "Absence of a row is NOT evidence" in mr.render(report) or \
           "will NOT guess" in mr.render(report)


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------


def test_no_detection_comes_from_a_direct_row(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])
    epath = _evaluations(tmp_path, [("NO_DETECTION", {"atr14": 1.5}),
                                    ("NO_DETECTION", {"atr14": 2.5})])

    report = _report(universe=upath, evaluations=epath)
    text = mr.render(report)

    assert report.verdict == mr.NO_DETECTION
    assert report.conclusion_evidence == mr.DIRECT_ROW
    assert "2 bar(s) evaluated" in text
    assert "atr14=2.5" in text  # the largest-ATR bar is surfaced


def test_detector_error_outranks_downstream_conclusions(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])
    epath = _evaluations(tmp_path, [("NO_DETECTION", {}),
                                    ("DETECTOR_ERROR", {"error": "RuntimeError: boom"})])
    jpath, det_id = _journal(tmp_path, score=1.0)  # a sub-threshold row downstream

    report = _report(universe=upath, evaluations=epath, journal=jpath)

    assert report.verdict == mr.DETECTOR_ERROR
    assert "outranks" in mr.render(report)


def test_evaluation_error_outranks_too(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])
    epath = _evaluations(tmp_path, [("EVALUATION_ERROR", {"error": "ValueError: guard"})])

    report = _report(universe=upath, evaluations=epath)

    assert report.verdict == mr.EVALUATION_ERROR


def test_multiple_runs_are_never_merged(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])
    epath = _evaluations(tmp_path, [("NO_DETECTION", {})], run_id="run-A")
    conn = ev.connect(epath)
    ev.record_bar_evaluation(
        conn, session=SESSION, symbol=SYMBOL, run_id="run-B", run_mode="live",
        now_utc=TICK.isoformat(), bar_ts_utc=TICK.isoformat(), outcome="DETECTOR_ERROR",
        open=1, high=2, low=0.5, close=1.5, volume=1, error="RuntimeError: x")
    conn.close()

    report = _report(universe=upath, evaluations=epath)
    stage2 = [f for f in report.findings if f.stage.startswith("stage2[")]

    assert len(stage2) == 2  # one finding per run, not one merged view
    assert "run-A"[:8] in stage2[0].stage or "run-B"[:8] in stage2[0].stage
    assert "scoped to that run" in report.conclusion


def test_the_run_id_filter_narrows_to_one_run(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])
    epath = _evaluations(tmp_path, [("NO_DETECTION", {})], run_id="run-A")
    conn = ev.connect(epath)
    ev.record_bar_evaluation(
        conn, session=SESSION, symbol=SYMBOL, run_id="run-B", run_mode="live",
        now_utc=TICK.isoformat(), bar_ts_utc=TICK.isoformat(), outcome="NO_DETECTION",
        open=1, high=2, low=0.5, close=1.5, volume=1)
    conn.close()

    report = _report(universe=upath, evaluations=epath, run_id="run-A")

    assert len([f for f in report.findings if f.stage.startswith("stage2[")]) == 1


def test_a_replay_run_is_labelled_as_replay(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, ["gap"])])
    epath = _evaluations(tmp_path, [("NO_DETECTION", {})], run_mode="replay")

    assert "run_mode=replay" in mr.render(_report(universe=upath, evaluations=epath))


# ---------------------------------------------------------------------------
# Decision & delivery
# ---------------------------------------------------------------------------


def test_sub_threshold(tmp_path):
    jpath, _ = _journal(tmp_path, score=1.0)

    report = _report(universe=_tick(_universe(tmp_path),
                                    events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"kinds": "gap", "tier": "log"})]),
                     journal=jpath)

    assert report.verdict == mr.SUB_THRESHOLD


def test_suppressed_surfaces_the_ledger(tmp_path):
    jpath, _ = _journal(
        tmp_path, score=10.0, suppress="cooldown_active", category="budget_cooldown",
        ledger=[("alert_routing", "cooldown_active", "alert_budget")])

    report = _report(universe=_tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"tier": "high"})]),
                     journal=jpath)
    text = mr.render(report)

    assert report.verdict == mr.SUPPRESSED
    assert "cooldown_active" in text
    assert "ledger: alert_routing -> cooldown_active (alert_budget)" in text


def test_partial_delivery_is_never_collapsed(tmp_path):
    """One delivered + one failed is neither a delivery nor a failure."""
    jpath, det_id = _journal(tmp_path, score=10.0, alerted=True)
    upath = _users(tmp_path, det_id, ["delivered", "failed"])

    report = _report(universe=_tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"tier": "high"})]),
                     journal=jpath, users=upath)
    text = mr.render(report)

    assert report.verdict == mr.ALERT_PARTIALLY_DELIVERED
    assert "delivered=1" in text and "failed=1" in text


def test_alert_not_delivered(tmp_path):
    jpath, det_id = _journal(tmp_path, score=10.0, alerted=True)
    upath = _users(tmp_path, det_id, ["pending", "failed"])

    report = _report(universe=_tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"tier": "high"})]),
                     journal=jpath, users=upath)

    assert report.verdict == mr.ALERT_NOT_DELIVERED


def test_fully_delivered_reports_no_failure_point(tmp_path):
    jpath, det_id = _journal(tmp_path, score=10.0, alerted=True)
    upath = _users(tmp_path, det_id, ["delivered", "delivered"])

    report = _report(universe=_tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"tier": "high"})]),
                     journal=jpath, users=upath)

    assert report.verdict == mr.ALERTED
    assert "No failure point" in report.conclusion


# ---------------------------------------------------------------------------
# Ordering & output contract
# ---------------------------------------------------------------------------


def test_the_earliest_failure_point_wins(tmp_path):
    """Failing at two stages reports the first one."""
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.0, 30, [])])
    jpath, _ = _journal(tmp_path, score=1.0)

    report = _report(universe=upath, journal=jpath)

    assert report.verdict == mr.CANDIDATE_NOT_PROMOTED
    assert "STAGE1" in report.conclusion


def test_not_in_universe_short_circuits_later_stages(tmp_path):
    report = _report(universe=_universe(tmp_path, symbols=("OTHER",)),
                     evaluations=_evaluations(tmp_path, [("NO_DETECTION", {})]))

    assert report.verdict == mr.NOT_IN_UNIVERSE
    assert not any(f.stage.startswith("stage2") for f in report.findings)


def test_a_watchlist_symbol_is_not_reported_as_unscreened(tmp_path):
    """WATCHLIST names bypass Stage 1 entirely; calling that NOT_SCREENED
    would be flatly wrong."""
    upath = _tick(_universe(tmp_path, symbols=(WATCHED,)), events=[])

    report = _report(symbol=WATCHED, universe=upath)

    assert report.verdict not in (mr.NOT_SCREENED, mr.SCREENED_QUIET)
    assert "does not apply" in mr.render(report)


def test_limitations_always_appear(tmp_path):
    text = mr.render(_report(universe=_universe(tmp_path)))

    for limitation in mr.LIMITATIONS:
        assert limitation.split("—")[0].strip()[:40] in text
    assert "pre-evaluation gates" in text
    assert "retention" in text
    assert "market_bars" in text
    assert "Replay evaluations" in text


def test_the_move_is_labelled_operator_supplied(tmp_path):
    text = mr.render(_report(universe=_universe(tmp_path), move_pct=9.2))

    assert "+9.20%" in text
    assert "supplied by operator" in text


def test_versions_are_surfaced(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.0, 30, [])])

    assert "screen_version=1" in mr.render(_report(universe=upath))


def test_the_conclusion_names_exactly_one_stage(tmp_path):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.0, 30, [])])

    conclusion = _report(universe=upath).conclusion

    assert conclusion.count("first explainable failure point") == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exits_zero_for_a_valid_verdict(tmp_path, capsys):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.1, 27, [])])

    code = cli.main(["--symbol", SYMBOL, "--session", SESSION, "--move-pct", "9.2",
                     "--universe-db", str(upath), "--journal-db", str(tmp_path / "none.db"),
                     "--evaluations-db", str(tmp_path / "none.db"),
                     "--users-db", str(tmp_path / "none.db")])

    assert code == 0  # a verdict is an answer, not a tool failure
    assert "CANDIDATE_NOT_PROMOTED" in capsys.readouterr().out


def test_cli_rejects_a_malformed_session(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--symbol", SYMBOL, "--session", "24-08-2026"])

    assert excinfo.value.code != 0


def test_cli_returns_nonzero_when_no_store_can_be_opened(tmp_path, capsys):
    missing = str(tmp_path / "nothing.db")

    code = cli.main(["--symbol", SYMBOL, "--session", SESSION, "--universe-db", missing,
                     "--journal-db", missing, "--evaluations-db", missing, "--users-db", missing])

    assert code == 2
    assert "nothing to diagnose" in capsys.readouterr().err


def test_cli_uppercases_the_symbol(tmp_path, capsys):
    upath = _tick(_universe(tmp_path), events=[(SYMBOL, "CANDIDATE_NOT_PROMOTED", 4.1, 27, [])])

    cli.main(["--symbol", SYMBOL.lower(), "--session", SESSION, "--universe-db", str(upath)])

    assert SYMBOL in capsys.readouterr().out
