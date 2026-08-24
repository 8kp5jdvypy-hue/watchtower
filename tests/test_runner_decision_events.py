"""Tests for the runner's decision_events wiring.

PR #72 added the append-only ledger and the helper; nothing called it.
This covers the five branches that now do, and — at least as important —
the branches that deliberately still don't. The scope rule these pin is
that the ledger records *decisions*, never the absence of one: no "no
event window found", no "guard passed", no "dedup agreed", no row for a
halted bar or a bar gap. A ledger of everything that didn't happen buries
the handful of rows that say what did.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import dedup as dedup_mod
from tradebot.alerts import AlertBudget, ConsoleAlerter, Decision
from tradebot.detectors import DailyAnchors, Detection
from tradebot.events import add_event_window
from tradebot.journal import (
    RUN_MODE_LIVE,
    RUN_MODE_REPLAY,
    RUN_MODE_UNKNOWN,
    UNATTRIBUTED_RUN_ID,
    decision_events_for_detection,
    new_run_id,
)
from tradebot.journal import connect as journal_connect
from tradebot.marketdata import Bar, OptionChain, OptionContract, Quote
import tradebot.runner as runner_mod
from tradebot.runner import HeartbeatStats, process_new_bar

SESSION = date(2026, 7, 23)


# ---------------------------------------------------------------------------
# Fixtures — a synthetic HIGH-tier cluster, the same shape test_runner.py's
# _high_tier_fixture uses, kept local so this file stands on its own.
# ---------------------------------------------------------------------------


def _high_tier_fixture(score: float = 10.0):
    anchors = DailyAnchors(
        symbol="TSLA", session_date=SESSION, prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), 100.0, 100.5, 99.8, 100.2, volume=10_000)
    primary = Detection("TSLA", "gap", bar.ts, score, "a gap", {})
    result = {
        "ts": datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), "close": 100.2, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary, "score": score, "trend": "up", "detections": [primary],
    }
    return anchors, bar, result


def _quote_fn(bar):
    return lambda symbol: Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)


def _crossed_quote_fn(bar):
    # bid > ask — trips the guard's crossed_quote rule
    return lambda symbol: Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)


def _no_trade_chain_fn(symbol, expiry):
    """NotImplementedError -> bound_chain_fn returns None -> no breakeven
    -> the NO TRADE branch of contract selection."""
    raise NotImplementedError


def _tradable_chain_fn(symbol, expiry):
    if expiry != date(2026, 7, 31):
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )
    return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])


def _setup(monkeypatch, *, score=10.0, db=":memory:", max_high_per_day=8):
    anchors, bar, result = _high_tier_fixture(score)
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)
    conn = journal_connect(db)
    budget = AlertBudget(now=lambda: bar.ts, max_high_per_day=max_high_per_day)
    stats = HeartbeatStats(start_time=bar.ts, session_date=SESSION)
    return anchors, bar, result, conn, budget, stats


def _run(conn, budget, stats, bar, anchors, *, quote_fn=None, chain_fn=_no_trade_chain_fn,
         alerter=None, run_mode=None, run_id=None):
    kwargs = {}
    if run_mode is not None:
        kwargs["run_mode"] = run_mode
    if run_id is not None:
        kwargs["run_id"] = run_id
    process_new_bar(
        conn, budget, alerter or ConsoleAlerter(), "v1", "TSLA", SESSION, [bar], anchors,
        quote_fn or _quote_fn(bar), chain_fn, stats, **kwargs,
    )


def _events(conn):
    detection_id = conn.execute("SELECT id FROM detections").fetchone()[0]
    return decision_events_for_detection(conn, detection_id)


def _stages(conn):
    return [(e.stage, e.decision) for e in _events(conn)]


# ---------------------------------------------------------------------------
# A. dedup lookup failure fallback
# ---------------------------------------------------------------------------


def test_dedup_lookup_failure_records_that_the_watch_was_forced(monkeypatch):
    """The whole point of this row. On a dedup crash the pipeline forces
    WATCH, and `detections` then holds lifecycle_state='watch' with a NULL
    related_detection_id — byte for byte what a WATCH the dedup logic
    actually decided on looks like. Nothing but the ledger can tell the
    two apart afterwards."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("dedup table is locked")

    monkeypatch.setattr(dedup_mod, "evaluate_dedup", _boom)

    _run(conn, budget, stats, bar, anchors)

    dedup_events = [e for e in _events(conn) if e.stage == "dedup"]
    assert len(dedup_events) == 1
    event = dedup_events[0]
    assert event.decision == "WATCH_ON_LOOKUP_FAILURE"
    assert event.reason == "dedup_lookup_failed"
    assert event.detail["error_type"] == "RuntimeError"
    assert event.detail["error"] == "dedup table is locked"

    # ...and the row it is disambiguating still looks like a plain WATCH.
    lifecycle = conn.execute("SELECT lifecycle_state, related_detection_id FROM detections").fetchone()
    assert lifecycle == ("watch", None)


def test_a_dedup_lookup_that_succeeds_records_nothing(monkeypatch):
    """No 'dedup passed' event: absence of a problem is not a decision."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors)

    assert [e for e in _events(conn) if e.stage == "dedup"] == []


# ---------------------------------------------------------------------------
# B. HIGH -> MEDIUM downgrade from event-window routing
# ---------------------------------------------------------------------------


def test_event_window_downgrade_is_recorded_without_touching_the_journaled_tier(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="downgrade", source="test", detail="earnings day",
    )

    _run(conn, budget, stats, bar, anchors)

    routing = [e for e in _events(conn) if e.stage == "event_window_routing"]
    assert len(routing) == 1
    assert routing[0].decision == "DOWNGRADE_HIGH_TO_MEDIUM"
    assert routing[0].reason == "earnings"
    assert routing[0].detail["journaled_tier"] == "high"
    assert routing[0].detail["routed_tier"] == "medium"
    assert routing[0].detail["event_severity"] == "downgrade"

    # detections.tier semantics are untouched: still the true score-based
    # tier, exactly as before this PR.
    assert conn.execute("SELECT tier FROM detections").fetchone()[0] == "high"
    assert stats.tier_counts["medium"] == 1
    assert "high" not in stats.tier_counts


def test_no_downgrade_event_when_no_event_window_applies(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors)

    assert [e for e in _events(conn) if e.stage == "event_window_routing"] == []


# ---------------------------------------------------------------------------
# C. resolved AlertBudget outcome
# ---------------------------------------------------------------------------


def test_resolved_send_decision_is_recorded(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors)

    routing = [e for e in _events(conn) if e.stage == "alert_routing"]
    assert len(routing) == 1
    assert routing[0].decision == Decision.SEND.value
    assert routing[0].reason == "alert_budget"
    assert routing[0].detail == {"tier": "high", "journaled_tier": "high", "score": 10.0}


def test_queued_for_digest_decision_is_recorded(monkeypatch):
    """A medium-tier cluster resolves to a queue, not a send — the ledger
    records the outcome AlertBudget actually returned, whatever it is."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch, score=3.0)

    _run(conn, budget, stats, bar, anchors)

    routing = [e for e in _events(conn) if e.stage == "alert_routing"]
    assert [e.decision for e in routing] == [Decision.QUEUED_FOR_DIGEST.value]
    assert routing[0].reason == "alert_budget"


def test_cap_suppression_decision_is_recorded(monkeypatch):
    """SUPPRESS_CAP, reached by burning the daily HIGH cap first."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch, max_high_per_day=1)
    _run(conn, budget, stats, bar, anchors)  # burns the cap

    # A second, escalating cluster: a non-escalating repeat would be
    # suppressed by dedup before the budget ever sees it.
    anchors2, bar2, result2 = _high_tier_fixture(score=20.0)
    result2["ts"] = result["ts"] + timedelta(minutes=5)
    result2["kinds"] = "gap,volume"
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result2)
    _run(conn, budget, stats, bar2, anchors2)

    second_id = conn.execute(
        "SELECT id FROM detections ORDER BY ts_utc DESC LIMIT 1"
    ).fetchone()[0]
    routing = [e for e in decision_events_for_detection(conn, second_id) if e.stage == "alert_routing"]
    assert [e.decision for e in routing] == [Decision.CAP_REACHED_NOTICE.value]


def test_news_blackout_suppression_records_that_the_budget_never_saw_it(monkeypatch):
    """SUPPRESS_NEWS_BLACKOUT is set by the runner directly and never
    reaches AlertBudget.evaluate() — so it burns no cap and trips no
    cooldown. A ledger that implied the budget decided it would be
    wrong about where the decision came from, hence `reason`."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )

    _run(conn, budget, stats, bar, anchors)

    routing = [e for e in _events(conn) if e.stage == "alert_routing"]
    assert [(e.decision, e.reason) for e in routing] == [
        (Decision.SUPPRESS_NEWS_BLACKOUT.value, "event_window")
    ]


def test_duplicate_suppression_records_that_dedup_decided_it(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    _run(conn, budget, stats, bar, anchors)

    # Same symbol, same score, a few minutes later: CONFIRMED, not an
    # escalation -> SUPPRESS_DUPLICATE, set by the runner, not the budget.
    _, bar2, result2 = _high_tier_fixture()
    result2["ts"] = result["ts"] + timedelta(minutes=5)
    result2["kinds"] = "gap,again"
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result2)
    _run(conn, budget, stats, bar2, anchors)

    second_id = conn.execute("SELECT id FROM detections ORDER BY ts_utc DESC LIMIT 1").fetchone()[0]
    routing = [e for e in decision_events_for_detection(conn, second_id) if e.stage == "alert_routing"]
    assert [(e.decision, e.reason) for e in routing] == [(Decision.SUPPRESS_DUPLICATE.value, "dedup")]


def test_alert_budget_gains_no_io(monkeypatch):
    """The decision is recorded by the caller from the value evaluate()
    returned. AlertBudget is never handed a connection and never writes."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    seen = {}

    real_evaluate = budget.evaluate

    def _spy(cluster):
        seen["args"] = cluster
        return real_evaluate(cluster)

    monkeypatch.setattr(budget, "evaluate", _spy)
    _run(conn, budget, stats, bar, anchors)

    assert seen["args"].id  # called with just the Cluster, as before
    assert not hasattr(budget, "conn")


# ---------------------------------------------------------------------------
# D. data-guard rejection
# ---------------------------------------------------------------------------


def test_data_guard_rejection_is_recorded_with_its_rule_and_reason(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors, quote_fn=_crossed_quote_fn(bar))

    guard = [e for e in _events(conn) if e.stage == "data_guard"]
    assert len(guard) == 1
    assert guard[0].decision == "REJECT"
    assert guard[0].reason.startswith("crossed_quote")
    assert guard[0].detail == {"rule": "crossed_quote", "category": "data_integrity"}

    # Existing suppression semantics are untouched.
    row = conn.execute("SELECT alerted, suppress_reason, suppress_category FROM detections").fetchone()
    assert row[0] == 0
    assert row[1].startswith("data_integrity_failed: crossed_quote")
    assert row[2] == "data_integrity"


def test_a_guard_that_passes_records_nothing(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors)

    assert [e for e in _events(conn) if e.stage == "data_guard"] == []


# ---------------------------------------------------------------------------
# E. contract-selection outcome
# ---------------------------------------------------------------------------


def test_no_trade_records_the_reason_the_bare_flag_cannot_hold(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors, chain_fn=_no_trade_chain_fn)

    selection = [e for e in _events(conn) if e.stage == "contract_selection"]
    assert len(selection) == 1
    assert selection[0].decision == "NO_TRADE"
    assert selection[0].reason == "no_liquid_strike"
    assert selection[0].detail["no_trade_detail"]  # the human-readable half, also preserved

    # detections.no_trade still says only THAT there was none.
    assert conn.execute("SELECT no_trade FROM detections").fetchone()[0] == 1


def test_a_tradable_selection_is_recorded_too(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors, chain_fn=_tradable_chain_fn)

    selection = [e for e in _events(conn) if e.stage == "contract_selection"]
    assert len(selection) == 1
    assert selection[0].decision == "TRADABLE"
    assert selection[0].reason is None
    assert selection[0].detail["strike"] == 100.0
    assert selection[0].detail["right"] == "call"
    assert conn.execute("SELECT no_trade FROM detections").fetchone()[0] == 0


def test_no_selection_event_when_the_guard_rejected_first(monkeypatch):
    """Selection never runs on a guard rejection, so there is no outcome
    to record — the ledger must not invent one."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors, quote_fn=_crossed_quote_fn(bar))

    assert [e for e in _events(conn) if e.stage == "contract_selection"] == []


# ---------------------------------------------------------------------------
# Append order
# ---------------------------------------------------------------------------


def test_events_from_one_bar_are_appended_in_the_order_the_pipeline_took_them(monkeypatch):
    """Four decisions in one pass: the forced WATCH, the routing
    downgrade, the resolved outcome. seq must totally order them even
    though they share a stage of the same function call."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="downgrade", source="test", detail="earnings day",
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("dedup exploded")

    monkeypatch.setattr(dedup_mod, "evaluate_dedup", _boom)

    _run(conn, budget, stats, bar, anchors)

    events = _events(conn)
    assert [e.stage for e in events] == ["dedup", "event_window_routing", "alert_routing"]
    assert [e.decision for e in events] == [
        "WATCH_ON_LOOKUP_FAILURE", "DOWNGRADE_HIGH_TO_MEDIUM", Decision.QUEUED_FOR_DIGEST.value,
    ]
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    assert len({e.seq for e in events}) == 3


def test_send_path_orders_routing_before_selection(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors)

    assert _stages(conn) == [("alert_routing", "send"), ("contract_selection", "NO_TRADE")]


def test_guard_rejection_path_orders_routing_before_the_rejection(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors, quote_fn=_crossed_quote_fn(bar))

    assert _stages(conn) == [("alert_routing", "send"), ("data_guard", "REJECT")]


# ---------------------------------------------------------------------------
# run_mode / run_id
# ---------------------------------------------------------------------------


def test_run_mode_and_run_id_are_persisted_on_every_event(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    run_id = new_run_id()

    _run(conn, budget, stats, bar, anchors, run_mode=RUN_MODE_LIVE, run_id=run_id)

    events = _events(conn)
    assert len(events) >= 2
    assert all(e.run_mode == RUN_MODE_LIVE for e in events)
    assert all(e.run_id == run_id for e in events)


def test_unattributed_callers_say_so_rather_than_looking_live(monkeypatch):
    """The default is loud, not silent, and above all not 'live'."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)

    _run(conn, budget, stats, bar, anchors)

    events = _events(conn)
    assert events
    assert all(e.run_mode == RUN_MODE_UNKNOWN for e in events)
    assert all(e.run_id == UNATTRIBUTED_RUN_ID for e in events)
    assert all(e.run_mode != RUN_MODE_LIVE for e in events)


def test_a_replay_of_a_live_session_is_distinguishable_from_the_live_run(monkeypatch):
    """The case the columns exist for. cluster_id hashes
    symbol/session/ts/kinds, so a replay produces the SAME detection_id
    and appends its events after the live ones — where, without these
    columns, they would read as the later, superseding truth about that
    detection."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    live_id, replay_id = new_run_id(), new_run_id()

    _run(conn, budget, stats, bar, anchors, run_mode=RUN_MODE_LIVE, run_id=live_id)
    # Same fixture replayed: a fresh budget/stats, same connection, same
    # detection row.
    budget2 = AlertBudget(now=lambda: bar.ts)
    stats2 = HeartbeatStats(start_time=bar.ts, session_date=SESSION)
    _run(conn, budget2, stats2, bar, anchors, run_mode=RUN_MODE_REPLAY, run_id=replay_id)

    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 1  # same detection, replayed
    events = _events(conn)
    live = [e for e in events if e.run_mode == RUN_MODE_LIVE]
    replay = [e for e in events if e.run_mode == RUN_MODE_REPLAY]
    assert live and replay
    assert len(live) + len(replay) == len(events)
    assert {e.run_id for e in live} == {live_id}
    assert {e.run_id for e in replay} == {replay_id}
    # The replay's events are strictly later in the ledger — which is why
    # run_mode, not seq, is what tells a reader which run is authoritative.
    assert max(e.seq for e in live) < min(e.seq for e in replay)


def test_two_executions_of_the_same_replay_get_different_run_ids(monkeypatch):
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    first, second = new_run_id(), new_run_id()
    assert first != second

    _run(conn, budget, stats, bar, anchors, run_mode=RUN_MODE_REPLAY, run_id=first)
    budget2 = AlertBudget(now=lambda: bar.ts)
    stats2 = HeartbeatStats(start_time=bar.ts, session_date=SESSION)
    _run(conn, budget2, stats2, bar, anchors, run_mode=RUN_MODE_REPLAY, run_id=second)

    run_ids = {e.run_id for e in _events(conn)}
    assert run_ids == {first, second}


def test_new_run_id_is_unique_per_call():
    assert len({new_run_id() for _ in range(100)}) == 100


# ---------------------------------------------------------------------------
# Transaction boundary: the ledger must not commit on its own
# ---------------------------------------------------------------------------


class _MidPipelineProbe:
    """quote_fn is called immediately AFTER the alert_routing event is
    appended and BEFORE _commit_then_send() — the exact window in which an
    early commit from the ledger would be observable. A second connection
    sees only committed state, which is precisely what survives a
    SIGKILL, so "visible here" == "the pipeline committed early"."""

    def __init__(self, db_path, bar):
        self.db_path = str(db_path)
        self.bar = bar
        self.detections_visible = None
        self.events_visible = None

    def __call__(self, symbol):
        probe = sqlite3.connect(self.db_path)
        try:
            self.detections_visible = probe.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
            self.events_visible = probe.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
        finally:
            probe.close()
        return Quote(symbol=symbol, ts=self.bar.ts, bid=100.1, ask=100.3, last=100.2)


def test_recording_a_decision_does_not_commit_the_pending_detection_early(tmp_path, monkeypatch):
    """The constraint this PR was scoped around. process_new_bar keeps a
    transaction open from the detection INSERT until _commit_then_send(),
    so the detection is durable before any alert referencing it exists.
    record_decision_event commits by default; every runner call site
    overrides that. If one forgot, the alert_routing event's commit would
    flush the pending detection here, ahead of the ordering the helper
    exists to enforce."""
    db_path = tmp_path / "journal.db"
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch, db=db_path)
    probe = _MidPipelineProbe(db_path, bar)

    _run(conn, budget, stats, bar, anchors, quote_fn=probe)

    assert probe.detections_visible == 0  # nothing committed yet...
    assert probe.events_visible == 0  # ...including the event just appended
    # and both are durable once the pipeline's own commit runs.
    after = sqlite3.connect(db_path)
    assert after.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 1
    assert after.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] >= 1


class _SendTimeProbe:
    """Records which decision_events rows are durable at the moment of
    each send."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.sends = []

    def send(self, text, priority=None, alert_id=None):
        probe = sqlite3.connect(self.db_path)
        try:
            rows = probe.execute("SELECT stage, decision FROM decision_events ORDER BY seq").fetchall()
            detections = {r[0] for r in probe.execute("SELECT id FROM detections")}
        finally:
            probe.close()
        self.sends.append({"alert_id": alert_id, "events": rows, "detections": detections})


def test_decisions_taken_before_an_alert_are_durable_before_it_is_sent(tmp_path, monkeypatch):
    """The flip side: riding the caller's transaction must not mean the
    events land LATE. _commit_then_send commits everything pending —
    detection and ledger together — before the send returns, so CLAUDE.md's
    'journaled before any alert is sent' now covers the decisions too."""
    db_path = tmp_path / "journal.db"
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch, db=db_path)
    alerter = _SendTimeProbe(db_path)

    _run(conn, budget, stats, bar, anchors, alerter=alerter)

    assert len(alerter.sends) == 1
    sent = alerter.sends[0]
    assert sent["alert_id"] in sent["detections"]  # the pre-existing guarantee, unchanged
    assert ("alert_routing", "send") in sent["events"]
    assert ("contract_selection", "NO_TRADE") in sent["events"]


def test_a_rolled_back_pipeline_takes_its_decision_events_with_it(tmp_path, monkeypatch):
    """Events belong to the caller's transaction, not to one of their own.
    A detection that never happened must not leave a decision behind."""
    db_path = tmp_path / "journal.db"
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch, db=db_path)

    from tradebot.journal import record_decision_event, write_cluster

    detection_id = write_cluster(
        conn, session=SESSION.isoformat(), symbol="TSLA", ts_utc=result["ts"].isoformat(),
        kinds="gap", headlines="a gap", score=10.0, close=100.2, atr14=1.0, trend="up",
        detections=result["detections"], code_version_str="v1",
    )
    record_decision_event(
        conn, detection_id, stage="alert_routing", decision="send",
        run_mode=RUN_MODE_LIVE, run_id=new_run_id(), commit=False,
    )
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Behavior neutrality
# ---------------------------------------------------------------------------


def test_a_failing_ledger_write_changes_nothing_about_the_alert(tmp_path, monkeypatch, caplog):
    """Instrumentation must never be able to turn a bar that would have
    alerted into a bar that raised instead."""
    db_path = tmp_path / "journal.db"
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch, db=db_path)
    alerter = _SendTimeProbe(db_path)

    def _broken(*args, **kwargs):
        raise sqlite3.OperationalError("ledger is on fire")

    monkeypatch.setattr(runner_mod, "record_decision_event", _broken)

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        _run(conn, budget, stats, bar, anchors, alerter=alerter)

    assert len(alerter.sends) == 1  # the alert still went out
    assert conn.execute("SELECT alerted FROM detections").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    assert stats.tier_counts["high"] == 1
    assert any("decision event write failed" in r.message for r in caplog.records)


def test_routing_outcomes_are_identical_with_the_ledger_in_place(monkeypatch):
    """A spot-check across the branches: every observable the pipeline
    already produced is unchanged. The ledger only adds rows to a table
    nothing else reads."""
    anchors, bar, result, conn, budget, stats = _setup(monkeypatch)
    _run(conn, budget, stats, bar, anchors)
    row = conn.execute(
        "SELECT alerted, suppress_reason, suppress_category, tier, no_trade, lifecycle_state FROM detections"
    ).fetchone()
    assert row == (1, None, None, "high", 1, "watch")
    assert stats.tier_counts["high"] == 1
    assert stats.suppression_counts == {}


# ---------------------------------------------------------------------------
# Nothing-happened branches stay silent
# ---------------------------------------------------------------------------


def test_a_halted_bar_records_no_decision(monkeypatch, tmp_path):
    conn = journal_connect(tmp_path / "journal.db")
    budget = AlertBudget(now=lambda: datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc))
    stats = HeartbeatStats(start_time=datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), session_date=SESSION)
    anchors, bar, _ = _high_tier_fixture()
    halted = Bar("TSLA", bar.ts, 100.0, 100.0, 100.0, 100.0, volume=0)

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", SESSION, [halted], anchors,
        _quote_fn(bar), _no_trade_chain_fn, stats,
    )

    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    assert stats.data_gaps  # the existing observability for this is untouched


def test_a_bar_that_produces_no_detection_records_no_decision(monkeypatch, tmp_path):
    conn = journal_connect(tmp_path / "journal.db")
    anchors, bar, _ = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: None)
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=SESSION)

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", SESSION, [bar], anchors,
        _quote_fn(bar), _no_trade_chain_fn, stats,
    )

    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Entry-point wiring: run_replay stamps its own mode and a fresh id
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)


def _replay_cache(tmp_path: Path, symbol: str, session: date) -> Path:
    cache_dir = tmp_path / "cache"
    rth_open = datetime(session.year, session.month, session.day, 13, 30, tzinfo=timezone.utc)
    _write_csv(
        cache_dir / symbol / f"intraday_{session.isoformat()}.csv",
        [
            {"ts": (rth_open + timedelta(minutes=5 * i)).isoformat(), "open": 100.0, "high": 100.5,
             "low": 99.5, "close": 100.0 + i * 0.1, "volume": 1000}
            for i in range(4)
        ],
    )
    _write_csv(
        cache_dir / symbol / "daily.csv",
        [
            {"ts": (rth_open - timedelta(days=d)).isoformat(), "open": 99.0, "high": 101.0,
             "low": 98.0, "close": 100.0, "volume": 1_000_000}
            for d in range(5, 0, -1)
        ],
    )
    return cache_dir


def test_run_replay_stamps_replay_mode_and_a_fresh_id_per_call(tmp_path, monkeypatch):
    symbol = "TSLA"
    cache_dir = _replay_cache(tmp_path, symbol, SESSION)
    monkeypatch.setattr(runner_mod, "WATCHLIST", [symbol])
    monkeypatch.setattr(runner_mod, "MARKET_PROXY_SYMBOLS", [])

    captured: list[dict] = []
    monkeypatch.setattr(
        runner_mod, "process_new_bar",
        lambda *args, **kwargs: captured.append(
            {"run_mode": kwargs.get("run_mode"), "run_id": kwargs.get("run_id")}
        ),
    )

    runner_mod.run_replay(SESSION, ConsoleAlerter(), db_path=tmp_path / "a.db", cache_dir=cache_dir)
    first = {c["run_id"] for c in captured}
    assert captured, "the replay loop never reached process_new_bar"
    assert {c["run_mode"] for c in captured} == {RUN_MODE_REPLAY}
    assert len(first) == 1  # one id for the whole run...

    captured.clear()
    runner_mod.run_replay(SESSION, ConsoleAlerter(), db_path=tmp_path / "b.db", cache_dir=cache_dir)
    second = {c["run_id"] for c in captured}
    assert len(second) == 1
    assert first != second  # ...and a different one for the next execution


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_connect_backfills_run_columns_onto_a_pre_pr73_ledger(tmp_path):
    """A journal.db created between PR #72 and this change has
    decision_events but neither run column. connect() must add both, and
    the NOT NULL defaults must hold."""
    db_path = tmp_path / "journal.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE decision_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_id TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            stage TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            detail_json TEXT,
            code_version TEXT
        );
        """
    )
    raw.execute(
        "INSERT INTO decision_events (detection_id, ts_utc, stage, decision) VALUES ('old', 'x', 's', 'd')"
    )
    raw.commit()
    raw.close()

    conn = journal_connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_events)")}
    assert {"run_mode", "run_id"} <= columns

    row = conn.execute("SELECT run_mode, run_id FROM decision_events WHERE detection_id='old'").fetchone()
    assert row == (RUN_MODE_UNKNOWN, UNATTRIBUTED_RUN_ID)  # never NULL, never mistakable for live
