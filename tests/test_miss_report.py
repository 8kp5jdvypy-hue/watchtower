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


DEFAULT_WINDOW = mr.EventWindow(event_time=TICK, minutes=60)


def _report(*, symbol=SYMBOL, universe=None, evaluations=None, journal=None, users=None,
            move_pct=9.2, run_id=None, window=DEFAULT_WINDOW):
    return mr.build_report(
        symbol=symbol, session=SESSION, move_pct=move_pct, run_id=run_id, window=window,
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

    assert report.verdict == mr.ALERT_PARTIALLY_DELIVERED_IN_WINDOW
    assert "delivered=1" in text and "failed=1" in text


def test_alert_not_delivered(tmp_path):
    jpath, det_id = _journal(tmp_path, score=10.0, alerted=True)
    upath = _users(tmp_path, det_id, ["pending", "failed"])

    report = _report(universe=_tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"tier": "high"})]),
                     journal=jpath, users=upath)

    assert report.verdict == mr.ALERT_NOT_DELIVERED_IN_WINDOW


def test_fully_delivered_reports_no_failure_point(tmp_path):
    jpath, det_id = _journal(tmp_path, score=10.0, alerted=True)
    upath = _users(tmp_path, det_id, ["delivered", "delivered"])

    report = _report(universe=_tick(_universe(tmp_path), events=[(SYMBOL, "PROMOTED", 8.0, 1, [])]),
                     evaluations=_evaluations(tmp_path, [("DETECTED", {"tier": "high"})]),
                     journal=jpath, users=upath)

    assert report.verdict == mr.ALERTED_IN_WINDOW
    assert "No pipeline failure identified within the selected window." in report.conclusion


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



# ===========================================================================
# TEMPORAL SCOPING — the reproduced failure and its neighbours
# ===========================================================================


def T(h, m):
    return datetime(2026, 8, 24, h, m, tzinfo=timezone.utc)


def _multi_tick_universe(tmp_path, ticks):
    """ticks: [(when, [(outcome, score, rank)])]"""
    return _multi_tick_universe_on(_universe(tmp_path), ticks)


def _multi_tick_universe_on(path, ticks):
    conn = universe_mod.connect(path)
    for when, events in ticks:
        conn.execute(
            "INSERT INTO screening_ticks (session, tick_utc, run_id, run_mode, screen_version, "
            "code_version, audit_mode, universe_count, thresholds_json, counts_json, invariant_ok, "
            "promotion_limit, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (SESSION, when.isoformat(), "scan-1", "live", 1, "sha", 0, 100, "{}",
             json.dumps({"quiet": 99}), 1, 25, 10))
        tick_id = conn.execute("SELECT MAX(tick_id) FROM screening_ticks").fetchone()[0]
        for outcome, score, rank in events:
            conn.execute(
                "INSERT INTO screening_events (tick_id, symbol, outcome, screen_score, rank, "
                "reasons_json, detail_json) VALUES (?,?,?,?,?,?,?)",
                (tick_id, SYMBOL, outcome, score, rank, json.dumps(["unusual_volume"]), None))
    conn.commit()
    conn.close()
    return path


def _bars(tmp_path, rows, *, symbol=SYMBOL, run_id="runA", run_mode="live", path=None):
    """rows: [(when, outcome, extra)] -- `when` is the bar's OPEN."""
    path = path or (tmp_path / "evaluations.db")
    conn = ev.connect(path)
    for when, outcome, extra in rows:
        ev.record_bar_evaluation(
            conn, session=SESSION, symbol=symbol, run_id=run_id, run_mode=run_mode,
            now_utc=when.isoformat(), bar_ts_utc=when.isoformat(), outcome=outcome,
            open=100.0, high=104.0, low=99.0, close=103.0, volume=800_000, **extra)
    conn.close()
    return path


def _detection_at(tmp_path, when, *, score=9.0, alerted=True, path=None):
    path = path or (tmp_path / "journal.db")
    conn = journal_connect(path)
    det_id = write_cluster(
        conn, session=SESSION, symbol=SYMBOL, ts_utc=when.isoformat(), kinds="gap", headlines="h",
        score=score, close=103.0, atr14=1.0, trend="up",
        detections=[Detection(SYMBOL, "gap", when, score, "h", {})], code_version_str="sha",
        alerted=alerted)
    conn.commit()
    conn.close()
    return path, det_id


def _delivery(tmp_path, det_id, rows, *, path=None):
    """rows: [(status, delivered_at|None)]"""
    path = path or (tmp_path / "users.db")
    conn = users_db.connect(path)
    for i, (status, delivered_at) in enumerate(rows):
        conn.execute(
            "INSERT INTO outbox (id, alert_id, chat_id, priority, text, status, attempts, "
            "next_attempt_at, created_at, delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"o{det_id[:4]}{i}", det_id, 1000 + i, 1, "t", status, 0, TICK.isoformat(),
             TICK.isoformat(), delivered_at.isoformat() if delivered_at else None))
    conn.commit()
    conn.close()
    return path


def _full_timeline(tmp_path):
    """The exact reproduced failure from the review:
        10:00 quiet | 10:30 CANDIDATE_NOT_PROMOTED | 11:00 PROMOTED
        11:05 NO_DETECTION | 14:30 DETECTED | 14:31 delivered
    """
    upath = _multi_tick_universe(tmp_path, [
        (T(10, 0), []),
        (T(10, 30), [("CANDIDATE_NOT_PROMOTED", 4.1, 27)]),
        (T(11, 0), [("PROMOTED", 8.4, 3)]),
    ])
    epath = _bars(tmp_path, [
        (T(11, 5), "NO_DETECTION", {"atr14": 2.9}),
        (T(14, 30), "DETECTED", {"kinds": "gap", "cluster_score": 9.0, "tier": "high"}),
    ])
    jpath, det_id = _detection_at(tmp_path, T(14, 35))
    ppath = _delivery(tmp_path, det_id, [("delivered", T(14, 36))])
    return upath, epath, jpath, ppath


def test_regression_a_1035_move_is_not_exonerated_by_a_1430_alert(tmp_path):
    """THE bug. Session-wide selection returned ALERTED / 'No failure
    point' for a move Perch missed at 10:35."""
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=60))
    text = mr.render(report)

    assert report.verdict != mr.ALERTED_IN_WINDOW
    assert "No failure point" not in text
    assert "this move was detected" not in text
    # The governing tick at 10:35 is 10:30 -- the near-miss, not the 11:00 promotion.
    assert report.verdict == mr.CANDIDATE_NOT_PROMOTED
    assert "rank=27" in text
    assert "governing tick 2026-08-24T10:30" in text


def test_regression_a_the_same_symbol_at_1430_does_see_the_detection(tmp_path):
    """Case A: the later detection/delivery must still be reportable when
    that IS the moment under investigation."""
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=T(14, 30), minutes=60))

    assert report.verdict == mr.ALERTED_IN_WINDOW
    assert "delivery_latency_from_event_time" in mr.render(report)


def test_regression_b_same_run_earlier_miss_survives_a_later_detection(tmp_path):
    """Case B: the within-run DETECTED short-circuit."""
    upath = _multi_tick_universe(tmp_path, [(T(11, 0), [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [
        (T(11, 5), "NO_DETECTION", {"atr14": 2.9}),
        (T(14, 30), "DETECTED", {"kinds": "gap", "tier": "high"}),
    ])

    report = _report(universe=upath, evaluations=epath,
                     window=mr.EventWindow(event_time=T(11, 5), minutes=30))
    text = mr.render(report)

    assert report.verdict == mr.NO_DETECTION
    assert "cannot bear on this move" in text  # the 14:30 detection is context only


def test_regression_c_a_later_restart_does_not_erase_an_earlier_run(tmp_path):
    """Case C: per-run separation under temporal scoping."""
    upath = _multi_tick_universe(tmp_path, [(T(11, 0), [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [(T(11, 5), "NO_DETECTION", {"atr14": 2.9})], run_id="runA")
    _bars(tmp_path, [(T(11, 5), "DETECTED", {"kinds": "gap", "tier": "high"})],
          run_id="runB", path=epath)

    report = _report(universe=upath, evaluations=epath,
                     window=mr.EventWindow(event_time=T(11, 5), minutes=30))
    stage2 = [f for f in report.findings if f.stage.startswith("stage2[")]

    assert len(stage2) == 2
    assert report.verdict == mr.NO_DETECTION  # runA's miss still explains
    assert "scoped to that run" in report.conclusion


def test_regression_d_a_later_promotion_does_not_overwrite_an_earlier_state(tmp_path):
    """Case D: Stage 1 governing-tick semantics."""
    upath = _multi_tick_universe(tmp_path, [
        (T(10, 0), []), (T(10, 30), []), (T(15, 30), [("PROMOTED", 9.0, 1)])])

    report = _report(universe=upath, window=mr.EventWindow(event_time=T(10, 35), minutes=60))

    assert report.verdict == mr.SCREENED_QUIET  # quiet at 10:30, the governing tick
    assert report.conclusion_evidence == mr.INFERRED


def test_regression_e_a_watchlist_symbol_bypasses_stage1_but_is_time_scoped(tmp_path):
    """Case E."""
    upath = _universe(tmp_path, symbols=(SYMBOL, WATCHED))
    upath = _multi_tick_universe_on(upath, [(T(10, 0), [])])
    epath = _bars(tmp_path, [
        (T(10, 35), "NO_DETECTION", {"atr14": 3.0}),
        (T(14, 30), "DETECTED", {"kinds": "gap", "tier": "high"}),
    ], symbol=WATCHED)

    report = _report(symbol=WATCHED, universe=upath, evaluations=epath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=30))
    text = mr.render(report)

    assert "does not apply" in text          # Stage 1 bypassed
    assert report.verdict == mr.NO_DETECTION  # and Stage 2 still time-scoped
    assert report.verdict != mr.NOT_SCREENED


def test_regression_f_no_event_time_refuses_a_verdict(tmp_path):
    """Case F: timeline only, no explanatory verdict."""
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath, window=None)
    text = mr.render(report)

    assert report.verdict == mr.INCONCLUSIVE_NO_EVENT_TIME
    for banned in (mr.ALERTED_IN_WINDOW, mr.NO_DETECTION, mr.CANDIDATE_NOT_PROMOTED, mr.SCREENED_QUIET):
        assert report.verdict != banned
    assert "first explainable failure point" not in text
    assert "Event window: NONE" in text
    # the timeline is present and ordered
    assert "stage1   CANDIDATE_NOT_PROMOTED" in text
    assert "stage2   NO_DETECTION" in text
    assert "delivery delivered" in text


def test_regression_g_delivery_is_event_scoped_with_latency(tmp_path):
    """Case G: only the in-window detection's recipients count, and the
    latency is visible."""
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [(T(10, 30), "DETECTED", {"kinds": "gap", "tier": "high"})])
    jpath, early = _detection_at(tmp_path, T(10, 35), alerted=True)
    _, late = _detection_at(tmp_path, T(15, 0), alerted=True, path=jpath)
    ppath = _delivery(tmp_path, early, [("delivered", T(10, 40)), ("failed", None)])
    _delivery(tmp_path, late, [("delivered", T(15, 1))], path=ppath)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.ALERT_PARTIALLY_DELIVERED_IN_WINDOW
    assert "delivered=1" in text and "failed=1" in text
    assert "delivery_latency_from_event_time: min=+5m" in text
    assert "cannot exonerate this move" in text  # the 15:00 detection is context only


# ---------------------------------------------------------------------------
# Window semantics
# ---------------------------------------------------------------------------


def test_the_window_is_forward_not_symmetric(tmp_path):
    """A bar before event-time is outside the window: Perch cannot act on
    a move that had not happened yet."""
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [(T(9, 0), "NO_DETECTION", {"atr14": 3.0})])

    report = _report(universe=upath, evaluations=epath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=60))

    assert report.verdict == mr.NO_EVALUATION  # the 09:00 bar does not count
    assert "no bar was evaluated inside the event window" in mr.render(report)


def test_bar_decision_time_is_the_close_not_the_open(tmp_path):
    """A bar OPENING at 10:32 is not knowable until 10:37, so with a
    5-minute window from 10:35 it still counts -- while one opening at
    10:28 (knowable 10:33) does not. Comparing opens would credit Perch
    with information it did not have."""
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [(T(10, 32), "NO_DETECTION", {"atr14": 3.0})])

    report = _report(universe=upath, evaluations=epath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=5))

    assert report.verdict == mr.NO_DETECTION


def test_bar_minutes_matches_the_detector_definition():
    """Pins the local constant against detectors.bar_close_ts."""
    from tradebot.detectors import bar_close_ts
    from tradebot.marketdata import Bar

    bar = Bar("X", TICK, 1, 2, 0.5, 1.5, 10)

    assert bar_close_ts(bar) - bar.ts == timedelta(minutes=mr.BAR_MINUTES)


def test_no_governing_tick_before_event_time_is_inconclusive(tmp_path):
    """Every tick ran after the move: none can establish what state was in
    force during it."""
    upath = _multi_tick_universe(tmp_path, [(T(15, 0), [("PROMOTED", 9.0, 1)])])

    report = _report(universe=upath, window=mr.EventWindow(event_time=T(10, 35), minutes=60))

    assert report.verdict == mr.INCONCLUSIVE
    assert "governing state unknown" in mr.render(report)


def test_offsets_label_events_relative_to_event_time(tmp_path):
    upath = _multi_tick_universe(tmp_path, [(T(10, 30), [("CANDIDATE_NOT_PROMOTED", 4.1, 27)])])

    text = mr.render(_report(universe=upath,
                             window=mr.EventWindow(event_time=T(10, 35), minutes=60)))

    assert "-5m before event-time" in text


def test_the_window_is_stated_in_the_report(tmp_path):
    text = mr.render(_report(universe=_universe(tmp_path),
                             window=mr.EventWindow(event_time=T(10, 35), minutes=45)))

    assert "+45m" in text and "forward" in text


# ---------------------------------------------------------------------------
# CLI time parsing
# ---------------------------------------------------------------------------


def test_cli_accepts_hhmm(tmp_path, capsys):
    upath = _multi_tick_universe(tmp_path, [(T(10, 30), [("CANDIDATE_NOT_PROMOTED", 4.1, 27)])])

    code = cli.main(["--symbol", SYMBOL, "--session", SESSION, "--event-time", "10:35",
                     "--universe-db", str(upath)])

    assert code == 0
    assert "CANDIDATE_NOT_PROMOTED" in capsys.readouterr().out


def test_cli_accepts_a_full_iso_timestamp(tmp_path, capsys):
    upath = _multi_tick_universe(tmp_path, [(T(10, 30), [("CANDIDATE_NOT_PROMOTED", 4.1, 27)])])

    code = cli.main(["--symbol", SYMBOL, "--session", SESSION,
                     "--event-time", "2026-08-24T10:35:00+00:00", "--universe-db", str(upath)])

    assert code == 0
    assert "CANDIDATE_NOT_PROMOTED" in capsys.readouterr().out


def test_cli_rejects_a_malformed_event_time(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--symbol", SYMBOL, "--session", SESSION, "--event-time", "half past ten"])

    assert excinfo.value.code != 0


def test_cli_rejects_a_non_positive_window(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--symbol", SYMBOL, "--session", SESSION, "--event-time", "10:35",
                  "--window-minutes", "0"])


def test_cli_without_event_time_returns_the_timeline(tmp_path, capsys):
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    code = cli.main(["--symbol", SYMBOL, "--session", SESSION, "--universe-db", str(upath),
                     "--evaluations-db", str(epath), "--journal-db", str(jpath),
                     "--users-db", str(ppath)])
    out = capsys.readouterr().out

    assert code == 0
    assert "INCONCLUSIVE_NO_EVENT_TIME" in out
    assert "Re-run with --event-time" in out


# ===========================================================================
# SUCCESS-SIDE SEMANTICS — scope in the name, latency as data, no timeliness
# ===========================================================================


def _alerted_at(tmp_path, event_time, detection_offset_min, *, delivery_offset_min=None,
                statuses=("delivered",)):
    """Promoted, detected and delivered `detection_offset_min` after the
    event. Returns the four store paths."""
    detect_at = event_time + timedelta(minutes=detection_offset_min)
    upath = _multi_tick_universe(tmp_path, [(event_time - timedelta(minutes=35),
                                             [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [(detect_at - timedelta(minutes=mr.BAR_MINUTES), "DETECTED",
                              {"kinds": "gap", "tier": "high"})])
    jpath, det_id = _detection_at(tmp_path, detect_at)
    delivered = (event_time + timedelta(minutes=delivery_offset_min)
                 if delivery_offset_min is not None else detect_at)
    ppath = _delivery(tmp_path, det_id,
                      [(st, delivered if st == "delivered" else None) for st in statuses])
    return upath, epath, jpath, ppath


@pytest.mark.parametrize("offset", [2, 58])
def test_a_prompt_and_a_late_in_window_alert_are_the_same_verdict(tmp_path, offset):
    """Case A: +2m and +58m both land inside a 60m window and both get
    ALERTED_IN_WINDOW. Neither is called timely, caught, or a success —
    the tool has no threshold that could justify the distinction."""
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, offset)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=event, minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.ALERTED_IN_WINDOW
    # Flat bans are unambiguous CLAIMS only. "timely" itself appears in the
    # mandated disclaimer, so it is checked per-line below instead.
    for banned in ("caught the move", "this move was detected", "No failure point",
                   "successfully"):
        assert banned not in text, f"overclaiming language present: {banned!r}"
    # "timely" may appear ONLY inside a disclaimer that denies encoding it.
    for line in text.splitlines():
        if "timely" in line:
            assert any(marker in line for marker in
                       ("not encoded by this tool", "does not judge", "defines no")), \
                f"unqualified timeliness claim: {line!r}"
    assert "No pipeline failure identified within the selected window." in text
    assert "not encoded by this tool" in text


def test_the_verdict_token_itself_carries_the_window_scope(tmp_path):
    """The token is what gets grepped and quoted out of context."""
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, 5)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict.endswith("_IN_WINDOW")


def test_detection_latency_is_surfaced_explicitly(tmp_path):
    """Case B: it was missing before — delivery latency was reported and
    detection latency was not."""
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, 12, delivery_offset_min=20)

    text = mr.render(_report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                             window=mr.EventWindow(event_time=event, minutes=60)))

    assert "detection_latency_from_event_time = +12m" in text


def test_detection_and_delivery_latency_stay_separate(tmp_path):
    """Case C: they measure different things — Perch's responsiveness vs
    the outbox worker's."""
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, 12, delivery_offset_min=40)

    text = mr.render(_report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                             window=mr.EventWindow(event_time=event, minutes=60)))

    assert "detection_latency_from_event_time = +12m" in text
    assert "delivery_latency_from_event_time=+40m" in text


def test_the_timing_block_carries_every_required_field(tmp_path):
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, 5)

    text = mr.render(_report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                             window=mr.EventWindow(event_time=event, minutes=60)))

    for field in ("event_time", "selected_window", "detection_time",
                  "detection_latency_from_event_time", "delivery_time",
                  "delivery_latency_from_event_time"):
        assert field in text, f"timing block missing {field}"
    assert "reported as DATA" in text


def test_recipients_are_listed_individually_not_collapsed(tmp_path):
    """One delivered promptly and one never delivered must not read as a
    single prompt delivery."""
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, 5,
                                             statuses=("delivered", "failed"))

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=event, minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.ALERT_PARTIALLY_DELIVERED_IN_WINDOW
    assert "status=delivered" in text
    assert "status=failed" in text
    assert "never delivered" in text


# ---------------------------------------------------------------------------
# Half-open window boundary
# ---------------------------------------------------------------------------


def test_an_event_exactly_at_the_window_end_is_excluded(tmp_path):
    """Case D. [start, end) — the end is exclusive."""
    window = mr.EventWindow(event_time=T(10, 35), minutes=60)

    assert window.contains(T(10, 35)) is True    # start inclusive
    assert window.contains(T(11, 34)) is True
    assert window.contains(T(11, 35)) is False   # end exclusive


def test_the_same_event_belongs_to_the_next_adjacent_window(tmp_path):
    """Case E: no double-counting across adjacent investigations, which
    would inflate any aggregate over many reports."""
    first = mr.EventWindow(event_time=T(10, 35), minutes=60)   # [10:35, 11:35)
    second = mr.EventWindow(event_time=T(11, 35), minutes=60)  # [11:35, 12:35)
    edge = T(11, 35)

    assert first.contains(edge) is False
    assert second.contains(edge) is True
    assert not (first.contains(edge) and second.contains(edge))


def test_a_bar_decidable_exactly_at_the_window_end_is_excluded(tmp_path):
    """The boundary rule applied through decision-time normalization: a
    bar OPENING at 11:30 is decidable at 11:35, which is the exclusive
    end."""
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _bars(tmp_path, [(T(11, 30), "NO_DETECTION", {"atr14": 3.0})])

    report = _report(universe=upath, evaluations=epath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=60))

    assert report.verdict == mr.NO_EVALUATION  # the 11:35-decidable bar is out
    assert "no bar was evaluated inside the event window" in mr.render(report)


def test_the_report_states_the_half_open_window(tmp_path):
    event = T(10, 35)
    upath, epath, jpath, ppath = _alerted_at(tmp_path, event, 5)

    text = mr.render(_report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                             window=mr.EventWindow(event_time=event, minutes=60)))

    assert "end EXCLUSIVE" in text


# ---------------------------------------------------------------------------
# The preserved protections, re-pinned after the rename
# ---------------------------------------------------------------------------


def test_1035_regression_survives_the_rename(tmp_path):
    """Case F."""
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=T(10, 35), minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.CANDIDATE_NOT_PROMOTED
    assert report.verdict != mr.ALERTED_IN_WINDOW
    assert "governing tick 2026-08-24T10:30" in text


def test_1430_regression_reports_latency_without_judging_it(tmp_path):
    """Case G."""
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=T(14, 30), minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.ALERTED_IN_WINDOW
    assert "detection_latency_from_event_time" in text
    assert "delivery_latency_from_event_time" in text
    # The tool states the latency and explicitly declines to grade it.
    assert "not encoded by this tool" in text
    assert "caught the move" not in text
    for line in text.splitlines():
        if "timely" in line:
            assert any(marker in line for marker in
                       ("not encoded by this tool", "does not judge", "defines no")), \
                f"unqualified timeliness claim: {line!r}"


def test_no_event_time_still_refuses_after_the_rename(tmp_path):
    """Case H."""
    upath, epath, jpath, ppath = _full_timeline(tmp_path)

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath, window=None)

    assert report.verdict == mr.INCONCLUSIVE_NO_EVENT_TIME
    assert report.verdict != mr.ALERTED_IN_WINDOW


def test_no_timing_block_without_an_in_window_detection(tmp_path):
    """The block is about in-window detections; there is nothing to time
    when there are none."""
    upath = _multi_tick_universe(tmp_path, [(T(10, 30), [("CANDIDATE_NOT_PROMOTED", 4.1, 27)])])

    text = mr.render(_report(universe=upath,
                             window=mr.EventWindow(event_time=T(10, 35), minutes=60)))

    assert "detection_latency_from_event_time" not in text


# ===========================================================================
# MEDIUM routing — a batched medium is not a suppression
# ===========================================================================


def _detection_tier(tmp_path, when, *, score, tier_forced=None, alerted=False,
                    suppress=None, category=None, ledger=(), path=None):
    """A detection whose tier follows from its score (tier_for_score), with
    optional real suppression evidence."""
    path = path or (tmp_path / "journal.db")
    conn = journal_connect(path)
    det_id = write_cluster(
        conn, session=SESSION, symbol=SYMBOL, ts_utc=when.isoformat(), kinds="gap",
        headlines="h", score=score, close=100.0, atr14=1.0, trend="up",
        detections=[Detection(SYMBOL, "gap", when, score, "h", {})],
        code_version_str="sha", alerted=alerted, suppress_reason=suppress)
    if category:
        conn.execute("UPDATE detections SET suppress_category=? WHERE id=?", (category, det_id))
    for stage, decision, reason in ledger:
        record_decision_event(conn, det_id, stage=stage, decision=decision, reason=reason)
    conn.commit()
    conn.close()
    return path, det_id


MEDIUM_SCORE = 2.5   # between TIER_MEDIUM (1.9) and TIER_HIGH (3.8)
HIGH_SCORE = 9.0


def _stage2_detected(tmp_path, event, name="ev_stage2.db"):
    """An in-window DETECTED bar, so the funnel walk reaches the decision
    stage instead of stopping at PROMOTED_NO_EVALUATION. A journaled
    detection implies Stage 2 saw the bar, so this is the realistic
    pairing, not a convenience."""
    return _bars(tmp_path, [(event - timedelta(minutes=mr.BAR_MINUTES), "DETECTED",
                             {"kinds": "gap", "tier": "high"})],
                 path=tmp_path / name)


def test_medium_tier_scores_really_are_medium():
    """Guards the fixture: if the thresholds move, these tests must fail
    loudly rather than silently exercise the wrong branch."""
    from tradebot.detectors import tier_for_score

    assert tier_for_score(MEDIUM_SCORE).value == "medium"
    assert tier_for_score(HIGH_SCORE).value == "high"


def test_a_normal_medium_is_not_reported_as_suppressed(tmp_path):
    """THE defect. A medium with alerted=0 and no suppress_reason is the
    designed digest route, not a suppression."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=MEDIUM_SCORE)

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.ROUTED_TO_DIGEST
    assert report.verdict != mr.SUPPRESSED
    assert "suppressed" not in text.lower().replace("not a suppression", "")
    assert "routed to the hourly digest" in text


def test_a_normal_medium_says_it_is_the_designed_path(tmp_path):
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=MEDIUM_SCORE)

    text = mr.render(_report(universe=upath, evaluations=epath, journal=jpath,
                             window=mr.EventWindow(event_time=event, minutes=60)))

    assert "designed path for MEDIUM" in text
    assert "QUEUED_FOR_DIGEST" in text


def test_a_medium_never_claims_the_digest_was_delivered(tmp_path):
    """send_medium_digest_if_due sends with no alert_id, so no outbox row
    links back to the detection. The report must not invent one."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, det_id = _detection_tier(tmp_path, event, score=MEDIUM_SCORE)
    ppath = _delivery(tmp_path, det_id, [])  # users.db exists, no rows for it

    text = mr.render(_report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                             window=mr.EventWindow(event_time=event, minutes=60)))

    assert "NOT provable here" in text
    assert "no alert_id" in text


def test_a_medium_with_ledger_evidence_is_a_direct_row(tmp_path):
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(
        tmp_path, event, score=MEDIUM_SCORE,
        ledger=[("alert_routing", "queued_for_hourly_digest", "alert_budget")])

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict == mr.ROUTED_TO_DIGEST
    assert report.conclusion_evidence == mr.DIRECT_ROW


def test_a_medium_without_a_ledger_row_is_inferred_not_direct(tmp_path):
    """A journal written before decision_events existed — like the real
    replay-era one — has no row to quote, so the claim is INFERRED from
    tier semantics and must say so."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=MEDIUM_SCORE)

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict == mr.ROUTED_TO_DIGEST
    assert report.conclusion_evidence == mr.INFERRED


def test_a_genuinely_suppressed_medium_still_reports_suppressed(tmp_path):
    """Real suppression evidence outranks the tier rule."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=MEDIUM_SCORE,
                               suppress="news_blackout:8-K:material",
                               category="news_blackout")

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict == mr.SUPPRESSED
    assert report.verdict != mr.ROUTED_TO_DIGEST
    assert "news_blackout" in mr.render(report)


def test_a_high_that_never_alerted_stays_distinguishable_as_anomalous(tmp_path):
    """Must not be silently absorbed into a normal-looking bucket."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=HIGH_SCORE, alerted=False)

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.HIGH_NOT_ALERTED_UNEXPLAINED
    assert report.verdict not in (mr.SUPPRESSED, mr.ROUTED_TO_DIGEST)
    assert report.conclusion_evidence == mr.NOT_INSTRUMENTED
    assert "anomalous" in text


def test_a_genuinely_suppressed_high_still_reports_suppressed(tmp_path):
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=HIGH_SCORE,
                               suppress="cooldown_active", category="budget_cooldown")

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict == mr.SUPPRESSED
    assert "cooldown_active" in mr.render(report)


def test_a_log_tier_detection_is_unchanged(tmp_path):
    """The log branch is checked first and is untouched by this fix."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=1.0)

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict == mr.SUB_THRESHOLD


def test_an_alerted_high_is_unchanged(tmp_path):
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, det_id = _detection_tier(tmp_path, event, score=HIGH_SCORE, alerted=True)
    ppath = _delivery(tmp_path, det_id, [("delivered", event + timedelta(minutes=1))])

    report = _report(universe=upath, evaluations=epath, journal=jpath, users=ppath,
                     window=mr.EventWindow(event_time=event, minutes=60))

    assert report.verdict == mr.ALERTED_IN_WINDOW


def test_medium_and_unexplained_high_are_different_verdicts(tmp_path):
    """Both have alerted=0 and no suppress_reason; conflating them was the
    bug. They must not collapse back together."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    med_path, _ = _detection_tier(tmp_path, event, score=MEDIUM_SCORE,
                                  path=tmp_path / "med.db")
    high_path, _ = _detection_tier(tmp_path, event, score=HIGH_SCORE,
                                   path=tmp_path / "high.db")
    w = mr.EventWindow(event_time=event, minutes=60)

    med = _report(universe=upath, evaluations=epath, journal=med_path, window=w)
    high = _report(universe=upath, evaluations=epath, journal=high_path, window=w)

    assert med.verdict != high.verdict
    assert med.verdict == mr.ROUTED_TO_DIGEST
    assert high.verdict == mr.HIGH_NOT_ALERTED_UNEXPLAINED


def test_a_downgraded_high_does_not_claim_nothing_records_why(tmp_path):
    """An event-window downgrade leaves detections.tier at the true
    score-based 'high' (that column is ground truth and routing never
    rewrites it), so a downgraded HIGH reaches the not-alerted branch with
    its explanation sitting in decision_events. Claiming "nothing records
    why" while those rows print directly above would contradict the
    report's own evidence."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(
        tmp_path, event, score=HIGH_SCORE, alerted=False,
        ledger=[("event_window_routing", "DOWNGRADE_HIGH_TO_MEDIUM", "earnings"),
                ("alert_routing", "queued_for_hourly_digest", "alert_budget")])

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.HIGH_NOT_ALERTED_UNEXPLAINED  # still not ROUTED_TO_DIGEST
    assert "nothing records why" not in text                  # the false claim is gone
    assert "DOWNGRADE_HIGH_TO_MEDIUM" in text                 # the ledger is shown
    assert "Read those rows for the reason" in text
    assert report.conclusion_evidence == mr.DIRECT_ROW


def test_a_high_with_no_ledger_still_says_nothing_records_why(tmp_path):
    """The genuinely unexplained case keeps its stronger wording."""
    event = T(10, 35)
    upath = _multi_tick_universe(tmp_path, [(T(10, 0), [("PROMOTED", 8.4, 3)])])
    epath = _stage2_detected(tmp_path, event)
    jpath, _ = _detection_tier(tmp_path, event, score=HIGH_SCORE, alerted=False)

    report = _report(universe=upath, evaluations=epath, journal=jpath,
                     window=mr.EventWindow(event_time=event, minutes=60))
    text = mr.render(report)

    assert report.verdict == mr.HIGH_NOT_ALERTED_UNEXPLAINED
    assert "nothing records why" in text
    assert "anomalous" in text
    assert report.conclusion_evidence == mr.NOT_INSTRUMENTED

