"""Tests for tradebot.dedup."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradebot.detectors import Detection
from tradebot.dedup import DEDUP_WINDOW_MINUTES, LifecycleState, evaluate_dedup, find_recent_anchor
from tradebot.journal import connect as journal_connect
from tradebot.journal import write_cluster

SYMBOL = "TSLA"
SESSION = "2026-07-23"
BASE_TS = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)


def _detection(kind="gap", score=4.0) -> Detection:
    return Detection(SYMBOL, kind, BASE_TS, score, "headline", {})


def _write(conn, ts: datetime, score: float, symbol: str = SYMBOL, kinds: str = "gap") -> str:
    return write_cluster(
        conn, session=SESSION, symbol=symbol, ts_utc=ts.isoformat(), kinds=kinds, headlines="h",
        score=score, close=100.0, atr14=1.0, trend="up", detections=[_detection(kind=kinds, score=score)],
        code_version_str="abc123",
    )


def test_no_prior_cluster_is_watch():
    conn = journal_connect(":memory:")
    result = evaluate_dedup(conn, SYMBOL, BASE_TS, score=4.0)
    assert result.lifecycle_state == LifecycleState.WATCH
    assert result.related_detection_id is None
    assert result.is_escalation is False


def test_a_recent_non_escalating_cluster_is_confirmed_not_an_escalation():
    conn = journal_connect(":memory:")
    anchor_id = _write(conn, BASE_TS, score=4.0)
    result = evaluate_dedup(conn, SYMBOL, BASE_TS + timedelta(minutes=10), score=4.5)
    assert result.lifecycle_state == LifecycleState.CONFIRMED
    assert result.related_detection_id == anchor_id
    assert result.is_escalation is False


def test_a_recent_cluster_with_a_much_higher_score_is_confirmed_and_an_escalation():
    conn = journal_connect(":memory:")
    anchor_id = _write(conn, BASE_TS, score=4.0)
    result = evaluate_dedup(conn, SYMBOL, BASE_TS + timedelta(minutes=10), score=7.0)
    assert result.lifecycle_state == LifecycleState.CONFIRMED
    assert result.related_detection_id == anchor_id
    assert result.is_escalation is True


def test_escalation_boundary_is_inclusive():
    conn = journal_connect(":memory:")
    _write(conn, BASE_TS, score=4.0)
    exactly_at_delta = evaluate_dedup(conn, SYMBOL, BASE_TS + timedelta(minutes=10), score=6.0, escalation_delta=2.0)
    assert exactly_at_delta.is_escalation is True
    just_under = evaluate_dedup(conn, SYMBOL, BASE_TS + timedelta(minutes=10), score=5.99, escalation_delta=2.0)
    assert just_under.is_escalation is False


def test_a_cluster_just_inside_the_window_is_confirmed():
    conn = journal_connect(":memory:")
    _write(conn, BASE_TS, score=4.0)
    result = evaluate_dedup(
        conn, SYMBOL, BASE_TS + timedelta(minutes=DEDUP_WINDOW_MINUTES - 1), score=4.5,
        window_minutes=DEDUP_WINDOW_MINUTES,
    )
    assert result.lifecycle_state == LifecycleState.CONFIRMED


def test_a_cluster_just_outside_the_window_is_watch():
    conn = journal_connect(":memory:")
    _write(conn, BASE_TS, score=4.0)
    result = evaluate_dedup(
        conn, SYMBOL, BASE_TS + timedelta(minutes=DEDUP_WINDOW_MINUTES + 1), score=4.5,
        window_minutes=DEDUP_WINDOW_MINUTES,
    )
    assert result.lifecycle_state == LifecycleState.WATCH
    assert result.related_detection_id is None


def test_a_different_symbol_never_anchors():
    conn = journal_connect(":memory:")
    _write(conn, BASE_TS, score=4.0, symbol="QQQ")
    result = evaluate_dedup(conn, SYMBOL, BASE_TS + timedelta(minutes=5), score=4.5)
    assert result.lifecycle_state == LifecycleState.WATCH


def test_a_log_tier_prior_cluster_never_anchors():
    conn = journal_connect(":memory:")
    _write(conn, BASE_TS, score=0.5)  # well below TIER_MEDIUM — journaled as 'log'
    row = conn.execute("SELECT tier FROM detections").fetchone()
    assert row[0] == "log"  # sanity check the fixture actually landed as log tier
    result = evaluate_dedup(conn, SYMBOL, BASE_TS + timedelta(minutes=5), score=4.5)
    assert result.lifecycle_state == LifecycleState.WATCH


def test_find_recent_anchor_prefers_the_most_recent_of_multiple_candidates():
    conn = journal_connect(":memory:")
    _write(conn, BASE_TS, score=4.0)
    newer_id = _write(conn, BASE_TS + timedelta(minutes=5), score=4.2, kinds="vwap_break")
    anchor = find_recent_anchor(conn, SYMBOL, BASE_TS + timedelta(minutes=8))
    assert anchor is not None
    assert anchor[0] == newer_id
