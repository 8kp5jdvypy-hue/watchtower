"""Stage 2 evaluation observability: evaluation_sessions / bar_evaluations.

The black hole this closes: a symbol reaches process_new_bar, every
detector runs, none fires, and nothing anywhere records that it happened.
"the detectors looked at TSLA at 14:35 and found nothing" and "TSLA was
never evaluated" were the same observation afterwards.

The design rests on detectors being pure -- Detection = f(bars, anchors,
market_bars), no I/O, no clock -- so recording the inputs records the
explanation, and no detector needs to change.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot import evaluations as evaluations_mod
from tradebot.alerts import AlertBudget, ConsoleAlerter
from tradebot.detectors import DailyAnchors, Detection
from tradebot.evaluations import (
    EVALUATION_VERSION,
    OUTCOME_BAR_GAP,
    OUTCOME_DETECTED,
    OUTCOME_DETECTOR_ERROR,
    OUTCOME_EVALUATION_ERROR,
    OUTCOME_HALTED_BAR,
    OUTCOME_NO_DETECTION,
)
from tradebot.journal import connect as journal_connect
from tradebot.marketdata import Bar, Quote
import tradebot.runner as runner_mod
from tradebot.runner import HeartbeatStats, process_new_bar

SESSION = date(2026, 7, 23)
BAR_TS = datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _anchors():
    return DailyAnchors(
        symbol="TSLA", session_date=SESSION, prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={0: 1000.0, 1: 2000.0},
    )


def _bar(ts=BAR_TS, *, volume=10_000, close=100.2):
    return Bar("TSLA", ts, 100.0, 100.5, 99.8, close, volume=volume)


def _series(n=20, *, volume=10_000):
    """A gapless 5-minute series, so is_bar_gap never fires by accident."""
    return [_bar(BAR_TS + timedelta(minutes=5 * i), volume=volume) for i in range(n)]


def _high_tier_result(bars):
    primary = Detection("TSLA", "gap", bars[-1].ts, 10.0, "a gap", {})
    return {
        "ts": bars[-1].ts + timedelta(minutes=5), "close": bars[-1].close, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary, "score": 10.0, "trend": "up", "detections": [primary],
    }


@pytest.fixture
def eval_conn():
    return evaluations_mod.connect(":memory:")


def _run(eval_conn, bars, *, monkeypatch=None, result="none", chain_raises=True):
    """Drive process_new_bar once with Stage 2 recording enabled."""
    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bars[-1].ts)
    stats = HeartbeatStats(start_time=bars[-1].ts, session_date=SESSION)

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bars[-1].ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", SESSION, bars, _anchors(),
        quote_fn, chain_fn, stats, run_mode="live", run_id="run-1", eval_conn=eval_conn,
    )
    return conn, stats


def _history(eval_conn, symbol="TSLA"):
    return evaluations_mod.evaluation_history_for_symbol(eval_conn, symbol, SESSION.isoformat())


# ---------------------------------------------------------------------------
# The black hole
# ---------------------------------------------------------------------------


def test_a_bar_where_no_detector_fires_is_now_recorded(eval_conn, monkeypatch):
    """The whole point. Before this, evaluate_bar returned None,
    process_new_bar returned, and nothing was written anywhere."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)
    bars = _series()

    _run(eval_conn, bars)

    [entry] = _history(eval_conn)
    assert entry.outcome == OUTCOME_NO_DETECTION
    assert entry.detection_id is None
    assert entry.bar_ts_utc == bars[-1].ts.isoformat()


def test_the_recorded_bar_carries_the_inputs_the_detectors_saw(eval_conn, monkeypatch):
    """Detectors are pure, so the stored bar plus the session's anchors
    reproduce every decision offline -- which is why this layer needs no
    detector change to explain anything."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)
    bars = _series()

    _run(eval_conn, bars)

    [entry] = _history(eval_conn)
    last = bars[-1]
    assert (entry.open, entry.high, entry.low, entry.close) == (last.open, last.high, last.low, last.close)
    assert entry.volume == last.volume
    assert entry.atr14 is not None  # resolved even though evaluate_bar returned None


def test_the_frozen_anchors_are_stored_once_for_the_session(eval_conn, monkeypatch):
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)

    _run(eval_conn, _series())

    anchors = evaluations_mod.session_anchors(eval_conn, "TSLA", SESSION.isoformat(), "run-1")
    assert anchors["prior_close"] == 100.0
    assert anchors["swing_high"] == 102.0
    # rvol_spike is unreproducible without this one.
    assert anchors["avg_cum_volume_by_bar"] == {"0": 1000.0, "1": 2000.0}
    assert conn_count(eval_conn, "evaluation_sessions") == 1


def conn_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_many_bars_share_one_session_row(eval_conn, monkeypatch):
    """Anchors are frozen per session, so they belong on the session row
    rather than repeated on all ~78 bar rows."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)
    series = _series(30)

    for i in range(15, 30):
        _run(eval_conn, series[: i + 1])

    assert conn_count(eval_conn, "evaluation_sessions") == 1
    assert conn_count(eval_conn, "bar_evaluations") == 15


# ---------------------------------------------------------------------------
# Every outcome
# ---------------------------------------------------------------------------


def test_a_halted_bar_is_recorded(eval_conn):
    bars = _series()[:-1] + [_bar(BAR_TS + timedelta(minutes=95), volume=0)]

    _, stats = _run(eval_conn, bars)

    assert [e.outcome for e in _history(eval_conn)] == [OUTCOME_HALTED_BAR]
    assert stats.data_gaps  # existing observability untouched


def test_a_bar_gap_is_recorded(eval_conn):
    bars = _series(3) + [_bar(BAR_TS + timedelta(minutes=180))]

    _, stats = _run(eval_conn, bars)

    assert [e.outcome for e in _history(eval_conn)] == [OUTCOME_BAR_GAP]
    assert stats.data_gaps


def test_a_detection_is_recorded_with_its_detection_id(eval_conn, monkeypatch):
    """DETECTED rows too, so the funnel is complete and the join to
    journal.db's detections is total."""
    bars = _series()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: _high_tier_result(bars))

    conn, _ = _run(eval_conn, bars)

    [entry] = _history(eval_conn)
    assert entry.outcome == OUTCOME_DETECTED
    assert entry.kinds == "gap"
    assert entry.cluster_score == 10.0
    assert entry.tier == "high"
    journaled = conn.execute("SELECT id FROM detections").fetchone()[0]
    assert entry.detection_id == journaled  # the cross-file join key


def test_a_crashing_detector_is_recorded_and_the_exception_still_propagates(eval_conn, monkeypatch):
    """Recorded and RE-RAISED. Swallowing it would be a behavior change,
    not instrumentation: before this layer the exception reached the
    caller's own handler, and it still must."""
    def _boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(runner_mod, "evaluate_bar", _boom)

    with pytest.raises(RuntimeError, match="detector exploded"):
        _run(eval_conn, _series())

    [entry] = _history(eval_conn)
    assert entry.outcome == OUTCOME_DETECTOR_ERROR
    assert "RuntimeError" in entry.error
    assert "detector exploded" in entry.error


def test_a_failing_data_guard_is_recorded_as_an_evaluation_error(eval_conn, monkeypatch):
    def _boom(*args, **kwargs):
        raise ValueError("guard exploded")

    monkeypatch.setattr(runner_mod, "is_halted_bar", _boom)

    with pytest.raises(ValueError, match="guard exploded"):
        _run(eval_conn, _series())

    [entry] = _history(eval_conn)
    assert entry.outcome == OUTCOME_EVALUATION_ERROR
    assert "ValueError" in entry.error


def test_a_detector_crash_is_never_also_relabelled_an_evaluation_error(eval_conn, monkeypatch):
    """Two flat try blocks, not nested ones: exactly one row per bar."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

    with pytest.raises(RuntimeError):
        _run(eval_conn, _series())

    outcomes = [e.outcome for e in _history(eval_conn)]
    assert outcomes == [OUTCOME_DETECTOR_ERROR]
    assert OUTCOME_EVALUATION_ERROR not in outcomes


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_run_attribution_is_recorded(eval_conn, monkeypatch):
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)

    _run(eval_conn, _series())

    [entry] = _history(eval_conn)
    assert entry.run_mode == "live"
    assert entry.run_id == "run-1"
    assert entry.evaluation_version == EVALUATION_VERSION


def test_a_replay_of_the_same_bar_gets_its_own_session_row(eval_conn, monkeypatch):
    """A replay drives process_new_bar too and reproduces the same
    (symbol, session, bar_ts) exactly. Without attribution in the UNIQUE
    key its rows would collide with the live ones -- the corruption class
    the journal spent three changes closing."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)
    bars = _series()
    conn = journal_connect(":memory:")
    stats = HeartbeatStats(start_time=bars[-1].ts, session_date=SESSION)

    for run_id, run_mode in (("live-1", "live"), ("replay-1", "replay")):
        process_new_bar(
            conn, AlertBudget(now=lambda: bars[-1].ts), ConsoleAlerter(), "v1", "TSLA", SESSION,
            bars, _anchors(), lambda s: Quote(symbol=s, ts=bars[-1].ts, bid=1, ask=2, last=1.5),
            lambda s, e: None, stats, run_mode=run_mode, run_id=run_id, eval_conn=eval_conn,
        )

    assert conn_count(eval_conn, "evaluation_sessions") == 2
    modes = {e.run_mode for e in _history(eval_conn)}
    assert modes == {"live", "replay"}
    # ...and each run's own view is separable.
    live_only = evaluations_mod.evaluation_history_for_symbol(
        eval_conn, "TSLA", SESSION.isoformat(), run_id="live-1")
    assert len(live_only) == 1
    assert live_only[0].run_mode == "live"


def test_re_evaluating_a_bar_in_one_run_supersedes_rather_than_duplicates(eval_conn, monkeypatch):
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)
    bars = _series()

    _run(eval_conn, bars)
    _run(eval_conn, bars)

    assert conn_count(eval_conn, "bar_evaluations") == 1


# ---------------------------------------------------------------------------
# Behavior neutrality
# ---------------------------------------------------------------------------


def test_recording_is_completely_inert_without_a_connection(monkeypatch):
    """eval_conn=None is the default, so every existing caller, test and
    replay is unaffected -- this layer does nothing until run_live opts
    in."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(evaluations_mod, "record_bar_evaluation",
                        lambda *a, **k: calls.append(1))

    _run(None, _series())

    assert calls == []


def test_a_failing_evaluation_write_never_breaks_the_bar(eval_conn, monkeypatch, caplog):
    """Knowing what the detectors saw must never change what they decided,
    nor turn a bar that would have alerted into one that raised."""
    bars = _series()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: _high_tier_result(bars))

    def _broken(*args, **kwargs):
        raise sqlite3.OperationalError("evaluations store is on fire")

    monkeypatch.setattr(evaluations_mod, "record_bar_evaluation", _broken)

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        conn, stats = _run(eval_conn, bars)

    assert conn.execute("SELECT alerted FROM detections").fetchone()[0] == 1  # still alerted
    assert stats.tier_counts["high"] == 1
    assert any("bar evaluation write failed" in r.message for r in caplog.records)


def test_the_alerting_path_is_identical_with_and_without_recording(monkeypatch):
    """Same inputs, recording on and off -> byte-identical detections row."""
    bars = _series()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: _high_tier_result(bars))

    without, _ = _run(None, bars)
    with_recording, _ = _run(evaluations_mod.connect(":memory:"), bars)

    assert (without.execute("SELECT * FROM detections").fetchall()
            == with_recording.execute("SELECT * FROM detections").fetchall())


def test_evaluations_never_touch_the_journal_connection(eval_conn, monkeypatch):
    """A separate file on its own connection, so process_new_bar's
    journal.db transaction boundary is untouchable from here -- two
    outcomes are recorded before that transaction even opens."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)

    conn, _ = _run(eval_conn, _series())

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bar_evaluations" not in tables
    assert "evaluation_sessions" not in tables
    assert conn_count(eval_conn, "bar_evaluations") == 1


def test_decision_events_is_untouched_by_this_layer(eval_conn, monkeypatch):
    """Stage 2 rows are deliberately not in the ledger: three of the six
    outcomes have no detection_id at all."""
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)

    conn, _ = _run(eval_conn, _series())

    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0


def test_a_no_detection_bar_writes_no_detections_row(eval_conn, monkeypatch):
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)

    conn, _ = _run(eval_conn, _series())

    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The investigation query
# ---------------------------------------------------------------------------


def test_a_missed_mover_bar_by_bar_story_is_queryable(eval_conn, monkeypatch):
    """The question this exists to answer: a symbol moved, no alert
    fired, and the history now shows every bar the detectors looked at,
    what they saw, and which single bar produced a detection."""
    series = _series(24)
    fired_at = 20

    def fake_evaluate(symbol, bars, anchors, market_bars=None):
        return _high_tier_result(bars) if len(bars) == fired_at + 1 else None

    monkeypatch.setattr(runner_mod, "evaluate_bar", fake_evaluate)
    for i in range(15, 24):
        _run(eval_conn, series[: i + 1])

    history = _history(eval_conn)

    assert len(history) == 9
    assert [e.outcome for e in history].count(OUTCOME_NO_DETECTION) == 8
    detected = [e for e in history if e.outcome == OUTCOME_DETECTED]
    assert len(detected) == 1
    assert detected[0].bar_ts_utc == series[fired_at].ts.isoformat()
    # In bar order, and every quiet bar carries the inputs to re-run any
    # detector against offline.
    assert [e.bar_ts_utc for e in history] == sorted(e.bar_ts_utc for e in history)
    assert all(e.close and e.volume for e in history)


def test_oversized_anchors_are_dropped_rather_than_truncated(eval_conn):
    huge = {"avg_cum_volume_by_bar": {str(i): float(i) for i in range(100_000)}}

    evaluations_mod.record_bar_evaluation(
        eval_conn, session=SESSION.isoformat(), symbol="TSLA", run_id="r", run_mode="live",
        now_utc="2026-07-23T13:35:00+00:00", bar_ts_utc=BAR_TS.isoformat(),
        outcome=OUTCOME_NO_DETECTION, open=1, high=2, low=0.5, close=1.5, volume=10,
        anchors=huge,
    )

    assert evaluations_mod.session_anchors(eval_conn, "TSLA", SESSION.isoformat(), "r") is None
    assert conn_count(eval_conn, "bar_evaluations") == 1  # the row still lands


def test_evaluation_version_is_stored_for_comparability(eval_conn, monkeypatch):
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda *a, **k: None)

    _run(eval_conn, _series())

    stored = eval_conn.execute("SELECT evaluation_version FROM evaluation_sessions").fetchone()[0]
    assert stored == EVALUATION_VERSION


def test_the_default_db_path_is_resolved_at_call_time():
    """A signature default binds when the function is defined, so a
    monkeypatched module attribute would be silently ignored and the
    suite would write the developer's real evaluations.db. This pins that
    connect() reads the attribute at call time -- which is what makes the
    conftest isolation fixture actually effective rather than merely
    present."""
    import inspect

    assert inspect.signature(evaluations_mod.connect).parameters["db_path"].default is None


def test_the_conftest_fixture_really_redirects_the_default(tmp_path):
    """End-to-end: a bare connect() must land inside tmp, not in the
    repo's data/ tree."""
    conn = evaluations_mod.connect()
    path = conn.execute("PRAGMA database_list").fetchall()[0][2]

    assert evaluations_mod.REPO_ROOT / "data" not in pathlib.Path(path).parents

