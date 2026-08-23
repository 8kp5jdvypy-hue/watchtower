"""Tests for the pipeline's decision-ledger call sites in tradebot.runner.

test_decision_events.py covers the storage helper and the table's
append-only guarantee. This file covers the wiring: that
process_new_bar actually appends a row at each decision point it claims
to, that it appends nothing at the points it deliberately stays silent
about, that recording a decision never changes when the pipeline
commits, and that a replayed decision stays distinguishable from a live
one forever.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import dedup
from tradebot import incidents
from tradebot.alerts import AlertBudget, ConsoleAlerter
from tradebot.detectors import DailyAnchors, Detection
from tradebot.events import add_event_window
from tradebot.journal import RUN_MODE_LIVE, RUN_MODE_REPLAY, decision_events_for_detection
from tradebot.journal import connect as journal_connect
from tradebot.marketdata import Bar, OptionChain, OptionContract, Quote
import tradebot.runner as runner_mod
from tradebot.runner import HeartbeatStats, RunContext, new_run_context, process_new_bar

SESSION_DATE = date(2026, 7, 23)


# ---------------------------------------------------------------------------
# Fixtures — the same synthetic HIGH-tier scenario test_runner.py uses, so
# these tests exercise the real process_new_bar branches rather than a
# parallel pipeline of their own.
# ---------------------------------------------------------------------------


def _high_tier_fixture():
    anchors = DailyAnchors(
        symbol="TSLA", session_date=SESSION_DATE, prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), 100.0, 100.5, 99.8, 100.2, volume=10_000)
    primary_detection = Detection("TSLA", "gap", bar.ts, 10.0, "a gap", {})
    result = {
        "ts": datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), "close": 100.2, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary_detection,
        "score": 10.0, "trend": "up", "detections": [primary_detection],
    }
    return anchors, bar, result


def _flat_quote_fn(bar):
    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    return quote_fn


def _no_op_chain_fn(symbol, expiry):
    raise NotImplementedError  # what run_replay's chain_fn does — no options data


def _tradable_chain_fn(symbol, expiry):
    """A chain good enough for costs.select_contract to actually pick a
    contract, so the 'selected' branch is reachable."""
    if expiry != date(2026, 7, 31):
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )
    return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])


def _run_one_bar(conn, monkeypatch, result, *, bars=None, anchors=None, quote_fn=None,
                 chain_fn=_no_op_chain_fn, budget=None, run=None, alerter=None, stats=None):
    default_anchors, bar, _ = _high_tier_fixture()
    bars = bars if bars is not None else [bar]
    anchors = anchors if anchors is not None else default_anchors
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, b, a, market_bars=None: result)
    process_new_bar(
        conn,
        budget if budget is not None else AlertBudget(now=lambda: bar.ts),
        alerter if alerter is not None else ConsoleAlerter(),
        "v1", "TSLA", SESSION_DATE, bars, anchors,
        quote_fn if quote_fn is not None else _flat_quote_fn(bars[-1]),
        chain_fn,
        stats if stats is not None else HeartbeatStats(start_time=bar.ts, session_date=SESSION_DATE),
        run=run,
    )


def _events(conn, stage=None):
    detection_id = conn.execute("SELECT id FROM detections ORDER BY ts_utc DESC LIMIT 1").fetchone()[0]
    events = decision_events_for_detection(conn, detection_id)
    return [e for e in events if stage is None or e.stage == stage]


def _stages(conn):
    return [e.stage for e in _events(conn)]


# ---------------------------------------------------------------------------
# Rows are created — one wired branch at a time
# ---------------------------------------------------------------------------


def test_dedup_lookup_failure_appends_the_fallback_it_chose(monkeypatch):
    """The lifecycle_state left on the detections row ('watch') is
    identical whether the lookup ran and found nothing or crashed and
    defaulted. Only the ledger distinguishes them."""
    anchors, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    def boom(*args, **kwargs):
        raise RuntimeError("dedup table is locked")

    monkeypatch.setattr(dedup, "evaluate_dedup", boom)
    _run_one_bar(conn, monkeypatch, result)

    events = _events(conn, stage="dedup")
    assert len(events) == 1
    assert events[0].decision == "lookup_failed_defaulted_to_watch"
    assert events[0].reason == "RuntimeError: dedup table is locked"
    assert events[0].detail == {"symbol": "TSLA"}
    # And the fallback really was applied — the event describes what
    # happened, it does not replace it.
    assert conn.execute("SELECT lifecycle_state FROM detections").fetchone()[0] == "watch"


def test_a_successful_dedup_lookup_appends_nothing(monkeypatch):
    """Design decision 3: only actual decisions. A lookup that ran and
    returned a result has already recorded that result on the detections
    row (lifecycle_state/related_detection_id); a second copy in the
    ledger would be noise, not history."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result)

    assert "dedup" not in _stages(conn)


def test_high_to_medium_downgrade_appends_the_only_record_of_itself(monkeypatch):
    """detections.tier stays 'high' by design (it records the true
    score-based tier), and the routing tier that actually changed is a
    local variable — so without this event nothing anywhere says this
    cluster was published as a MEDIUM."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="downgrade", source="test",
        detail="scheduled release",
    )

    _run_one_bar(conn, monkeypatch, result)

    events = _events(conn, stage="event_window")
    assert len(events) == 1
    assert events[0].decision == "downgrade_high_to_medium"
    assert events[0].reason == "8-K:scheduled release"
    assert events[0].detail["journaled_tier"] == "high"
    assert events[0].detail["routed_tier"] == "medium"
    assert events[0].detail["severity"] == "downgrade"
    # The journaled tier is genuinely untouched — the event is the only
    # place the routing change is written down.
    assert conn.execute("SELECT tier FROM detections").fetchone()[0] == "high"


def test_a_sent_alert_records_its_resolved_decision(monkeypatch):
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result)

    events = _events(conn, stage="alert_decision")
    assert len(events) == 1
    assert events[0].decision == "send"
    assert events[0].reason is None  # nothing was suppressed, so there is no category
    assert events[0].detail["resolved_by"] == "alert_budget"
    assert events[0].detail["routed_tier"] == "high"


def test_a_budget_suppression_records_its_category(monkeypatch):
    """The cap is the AlertBudget's own decision — resolved_by says so."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts, max_high_per_day=0)

    _run_one_bar(conn, monkeypatch, result, budget=budget)

    events = _events(conn, stage="alert_decision")
    assert len(events) == 1
    assert events[0].decision == "daily_cap_reached_notice"
    assert events[0].reason == "budget_cap"
    assert events[0].detail["resolved_by"] == "alert_budget"


def test_a_duplicate_suppression_is_recorded_even_though_the_budget_never_saw_it(monkeypatch):
    """SUPPRESS_DUPLICATE and SUPPRESS_NEWS_BLACKOUT never reach
    AlertBudget.evaluate() at all. Recording only the budget's own return
    value would leave the ledger silent on exactly the outcomes someone
    asking 'why didn't this alert?' is most likely to be asking about."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result)
    first_id = conn.execute("SELECT id FROM detections").fetchone()[0]

    bar2 = Bar("TSLA", bar.ts + timedelta(minutes=5), 100.2, 100.7, 100.0, 100.4, volume=10_000)
    repeat = {**result, "ts": result["ts"] + timedelta(minutes=10)}
    _run_one_bar(conn, monkeypatch, repeat, bars=[bar, bar2])

    events = _events(conn, stage="alert_decision")
    assert len(events) == 1
    assert events[0].decision == "duplicate_event"
    assert events[0].reason == "duplicate"
    assert events[0].detail["resolved_by"] == "runner"
    assert events[0].detection_id != first_id


def test_a_guard_rejection_records_the_rule_that_rejected_it(monkeypatch):
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    def crossed_quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)  # bid > ask

    _run_one_bar(conn, monkeypatch, result, quote_fn=crossed_quote_fn)

    events = _events(conn, stage="data_guard")
    assert len(events) == 1
    assert events[0].decision == "rejected"
    assert events[0].detail["rule"] == "crossed_quote"
    assert events[0].reason.startswith("crossed_quote")


def test_a_guard_that_passed_appends_nothing(monkeypatch):
    """Design decision 3 again: a guard that passes has decided nothing,
    it has declined to intervene. One row per HIGH evaluation saying
    'nothing happened' is a log, not a ledger."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result)

    assert "data_guard" not in _stages(conn)


def test_a_no_trade_outcome_records_why(monkeypatch):
    """set_no_trade records THAT there was no trade, as a boolean that
    the next pass overwrites. select_contract's reason — the only thing
    separating 'no chain came back' from 'the spread was too wide' —
    otherwise lives solely in a log line."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result, chain_fn=_no_op_chain_fn)

    events = _events(conn, stage="contract_selection")
    assert len(events) == 1
    assert events[0].decision == "no_trade"
    assert events[0].reason  # a real reason string from costs.select_contract
    assert conn.execute("SELECT no_trade FROM detections").fetchone()[0] == 1


def test_a_selected_contract_records_what_was_selected(monkeypatch):
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result, chain_fn=_tradable_chain_fn)

    events = _events(conn, stage="contract_selection")
    assert len(events) == 1
    assert events[0].decision == "selected"
    assert events[0].detail["right"] == "call"
    assert events[0].detail["strike"] == 100.0
    assert events[0].detail["entry_mid"] == pytest.approx(2.025)
    assert conn.execute("SELECT no_trade FROM detections").fetchone()[0] == 0


def test_one_pass_appends_its_decisions_in_the_order_they_were_taken(monkeypatch):
    """seq is the ledger's whole point: not just which decisions, but in
    what order. A guard-rejected HIGH inside a downgrade window takes
    three of them."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="downgrade", source="test", detail="d",
    )

    def crossed_quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)

    _run_one_bar(conn, monkeypatch, result, quote_fn=crossed_quote_fn)

    # Downgraded to MEDIUM, so the alert decision is the hourly digest,
    # and the guard never runs on a MEDIUM at all.
    assert _stages(conn) == ["event_window", "alert_decision"]
    assert [e.seq for e in _events(conn)] == sorted(e.seq for e in _events(conn))


def test_no_absence_of_events_telemetry_is_recorded_on_a_clean_send(monkeypatch):
    """The whole ledger for an ordinary, uneventful HIGH alert: the
    decision to send, and the contract chosen for it. Nothing else."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result, chain_fn=_tradable_chain_fn)

    assert _stages(conn) == ["alert_decision", "contract_selection"]


def test_every_event_carries_the_code_version_that_took_the_decision(monkeypatch):
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result)

    assert [e.code_version for e in _events(conn)] == ["v1", "v1"]


# ---------------------------------------------------------------------------
# Commit ordering is preserved
# ---------------------------------------------------------------------------


class _OrderLog(list):
    """One shared, ordered transcript of the journal commits and the
    alerter sends a run performed. Comparing two transcripts is how these
    tests assert on ordering without hard-coding a commit count that any
    unrelated change to the pipeline would invalidate."""


class _CommitSpyConn:
    """Proxies a real connection, appending to the shared transcript on
    every commit. Everything else passes straight through, so the code
    under test is the real code."""

    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def commit(self):
        self._log.append("commit")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RecordingAlerter:
    def __init__(self, log):
        self._log = log

    def send(self, text, priority=None, alert_id=None):
        self._log.append(f"send:{priority}")


def _transcript(monkeypatch, scenario, *, with_ledger):
    """Run one scenario and return its ordered commit/send transcript.
    with_ledger=False no-ops the ledger write at the seam runner.py calls
    it through, which is as close to 'this PR reverted' as a test can get
    while leaving every other line of the pipeline identical."""
    log = _OrderLog()
    conn = _CommitSpyConn(journal_connect(":memory:"), log)
    if not with_ledger:
        monkeypatch.setattr(runner_mod, "record_decision_event", lambda *a, **k: 0)
    scenario(conn, monkeypatch, _RecordingAlerter(log))
    return list(log), conn


def _send_scenario(conn, monkeypatch, alerter):
    _, bar, result = _high_tier_fixture()
    _run_one_bar(conn, monkeypatch, result, alerter=alerter, chain_fn=_tradable_chain_fn)


def _guard_rejection_scenario(conn, monkeypatch, alerter):
    _, bar, result = _high_tier_fixture()

    def crossed_quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)

    _run_one_bar(conn, monkeypatch, result, alerter=alerter, quote_fn=crossed_quote_fn)


def _cap_notice_scenario(conn, monkeypatch, alerter):
    _, bar, result = _high_tier_fixture()
    _run_one_bar(
        conn, monkeypatch, result, alerter=alerter,
        budget=AlertBudget(now=lambda: bar.ts, max_high_per_day=0),
    )


@pytest.mark.parametrize(
    "scenario",
    [_send_scenario, _guard_rejection_scenario, _cap_notice_scenario],
    ids=["send", "guard_rejection", "cap_notice"],
)
def test_recording_decisions_does_not_change_when_the_pipeline_commits(monkeypatch, scenario):
    """Design decision 2. The ledger writes ride the transaction the
    pipeline was already going to commit; they never open, close, or
    reorder one of their own. Proven by transcript equality rather than
    by an absolute commit count, so this stays a real assertion if the
    pipeline's own commit points ever change."""
    with_ledger, ledger_conn = _transcript(monkeypatch, scenario, with_ledger=True)
    without_ledger, _ = _transcript(monkeypatch, scenario, with_ledger=False)

    assert with_ledger == without_ledger
    # ...and the run that kept the ledger really did write to it, so the
    # equality above isn't the trivial kind.
    assert ledger_conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] > 0


def test_decision_events_are_durable_by_the_time_the_alert_is_sent(tmp_path):
    """_commit_then_send's invariant, extended to the ledger: at the
    moment an alert leaves for a subscriber, the decisions that produced
    it are already committed and readable by another process. An event
    that only became durable after the send would be a decision the
    system can be seen acting on but cannot account for."""
    import tradebot.runner as runner_module

    db_path = tmp_path / "journal.db"
    conn = journal_connect(db_path)
    _, bar, result = _high_tier_fixture()
    seen_at_send_time = {}

    class _ReadingAlerter:
        def send(self, text, priority=None, alert_id=None):
            # A genuinely separate connection: it can only see committed data.
            observer = journal_connect(db_path)
            seen_at_send_time["detections"] = observer.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
            seen_at_send_time["events"] = observer.execute(
                "SELECT COUNT(*) FROM decision_events"
            ).fetchone()[0]
            observer.close()

    original = runner_module.evaluate_bar
    runner_module.evaluate_bar = lambda symbol, b, a, market_bars=None: result
    try:
        anchors, _, _ = _high_tier_fixture()
        process_new_bar(
            conn, AlertBudget(now=lambda: bar.ts), _ReadingAlerter(), "v1", "TSLA", SESSION_DATE,
            [bar], anchors, _flat_quote_fn(bar), _tradable_chain_fn,
            HeartbeatStats(start_time=bar.ts, session_date=SESSION_DATE),
        )
    finally:
        runner_module.evaluate_bar = original

    assert seen_at_send_time["detections"] == 1
    assert seen_at_send_time["events"] >= 2  # alert_decision + contract_selection


def test_a_ledger_write_failure_never_stops_an_alert(monkeypatch, tmp_path):
    """A record OF a decision is not part OF it. If the ledger write
    fails, the alert that should fire still fires."""
    from tradebot import metrics as metrics_mod

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_path)

    def broken_ledger(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runner_mod, "record_decision_event", broken_ledger)

    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    _run_one_bar(conn, monkeypatch, result, chain_fn=_tradable_chain_fn)

    assert conn.execute("SELECT alerted, suppress_reason FROM detections").fetchone() == (1, None)
    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    counters = metrics_mod.read_all(metrics_path)
    assert counters["decision_event_write_failed{stage=alert_decision}"] == 1


# ---------------------------------------------------------------------------
# Replay history stays distinguishable from live history, forever
# ---------------------------------------------------------------------------


def test_new_run_context_identifies_the_invocation_not_the_build():
    """Two runs of the same code on the same session are two runs."""
    first = new_run_context(RUN_MODE_REPLAY)
    second = new_run_context(RUN_MODE_REPLAY)

    assert first.run_id != second.run_id
    assert first.run_mode == second.run_mode == "replay"
    assert new_run_context(RUN_MODE_LIVE).run_mode == "live"


def test_a_run_context_cannot_change_mid_run():
    run = new_run_context(RUN_MODE_LIVE)
    with pytest.raises(Exception):
        run.run_mode = RUN_MODE_REPLAY


def test_every_event_from_one_pass_carries_that_run_s_identity(monkeypatch):
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    run = RunContext(run_id="abc123", run_mode=RUN_MODE_REPLAY)

    _run_one_bar(conn, monkeypatch, result, run=run, chain_fn=_tradable_chain_fn)

    events = _events(conn)
    assert len(events) == 2
    assert {e.run_id for e in events} == {"abc123"}
    assert {e.run_mode for e in events} == {"replay"}


def test_replayed_and_live_decisions_stay_separable_in_the_same_table(monkeypatch):
    """The ledger is append-only, so nothing can label these rows later.
    A query that wants only what really happened during the session must
    be able to get it from what was written at the time."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result, run=RunContext("live-1", RUN_MODE_LIVE))
    bar2 = Bar("TSLA", bar.ts + timedelta(minutes=5), 100.2, 100.7, 100.0, 100.4, volume=10_000)
    replayed = {**result, "ts": result["ts"] + timedelta(minutes=40), "close": 100.4}
    _run_one_bar(conn, monkeypatch, replayed, bars=[bar, bar2], run=RunContext("replay-1", RUN_MODE_REPLAY))

    by_mode = dict(conn.execute("SELECT run_mode, COUNT(*) FROM decision_events GROUP BY run_mode").fetchall())
    assert by_mode == {"live": 2, "replay": 2}
    live_only = conn.execute(
        "SELECT DISTINCT run_id FROM decision_events WHERE run_mode = ?", (RUN_MODE_LIVE,)
    ).fetchall()
    assert live_only == [("live-1",)]


def test_a_caller_that_is_not_a_run_records_null_rather_than_a_guess(monkeypatch):
    """Every existing direct caller of process_new_bar (tests, scripts)
    passes no run. NULL says 'nobody recorded this', which is true;
    inventing an id would make an unattributable row look attributable."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")

    _run_one_bar(conn, monkeypatch, result)

    events = _events(conn)
    assert events
    assert all(e.run_id is None and e.run_mode is None for e in events)


def test_run_replay_stamps_every_decision_it_takes_as_a_replay(tmp_path):
    """The entry point, not just the helper: an actual run_replay() call
    mints a replay-mode context. Runs against an empty cache — the run
    still happens, it just has no bars to decide anything about, which is
    all this needs to observe."""
    captured = []
    original = runner_mod.new_run_context
    runner_mod.new_run_context = lambda mode: captured.append(mode) or original(mode)
    try:
        runner_mod.run_replay(
            date(2026, 6, 15), ConsoleAlerter(),
            db_path=tmp_path / "journal.db", cache_dir=tmp_path / "cache",
        )
    finally:
        runner_mod.new_run_context = original

    assert captured == [RUN_MODE_REPLAY]


def test_run_live_stamps_every_decision_it_takes_as_live(monkeypatch, tmp_path):
    """The live half of the same guarantee. Everything before the run
    context is stubbed (calendar, session bounds, incident close) because
    none of it is what this asserts on — reaching the line at all is."""

    class _Stop(Exception):
        pass

    class _AlwaysASession:
        def is_session(self, session_date):
            return True

    now = datetime.now(timezone.utc)
    captured = []

    monkeypatch.setattr(runner_mod, "CALENDAR", _AlwaysASession())
    monkeypatch.setattr(
        runner_mod, "session_bounds",
        lambda session_date, calendar=None: (now - timedelta(hours=1), now + timedelta(hours=1)),
    )
    monkeypatch.setattr(runner_mod, "_load_session_close_state", lambda path: None)
    monkeypatch.setattr(incidents, "close_incident", lambda *a, **k: None)

    def _capture_then_stop(mode):
        captured.append(mode)
        raise _Stop()

    monkeypatch.setattr(runner_mod, "new_run_context", _capture_then_stop)

    with pytest.raises(_Stop):
        runner_mod.run_live(ConsoleAlerter(), db_path=tmp_path / "journal.db")

    assert captured == [RUN_MODE_LIVE]


# ---------------------------------------------------------------------------
# Existing alert behavior is unchanged
# ---------------------------------------------------------------------------


def test_the_send_path_still_alerts_exactly_as_it_did(monkeypatch):
    """The assertions test_runner.py already makes about this path, made
    again with the ledger wired in: same alerted flag, same suppress
    reason, same tier accounting, same cap consumption."""
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts, max_high_per_day=8)
    stats = HeartbeatStats(start_time=bar.ts, session_date=SESSION_DATE)
    sent = []

    class _CountingAlerter:
        def send(self, text, priority=None, alert_id=None):
            sent.append(alert_id)

    _run_one_bar(
        conn, monkeypatch, result, budget=budget, stats=stats,
        alerter=_CountingAlerter(), chain_fn=_tradable_chain_fn,
    )

    detection_id, alerted, suppress_reason = conn.execute(
        "SELECT id, alerted, suppress_reason FROM detections"
    ).fetchone()
    assert (alerted, suppress_reason) == (1, None)
    assert sent == [detection_id]
    assert stats.tier_counts["high"] == 1
    assert len(budget._high_sent_today) == 1


def test_the_guard_rejection_path_still_suppresses_exactly_as_it_did(monkeypatch):
    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    sent = []

    class _CountingAlerter:
        def send(self, text, priority=None, alert_id=None):
            sent.append(alert_id)

    def crossed_quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)

    _run_one_bar(conn, monkeypatch, result, quote_fn=crossed_quote_fn, alerter=_CountingAlerter())

    alerted, suppress_reason, category = conn.execute(
        "SELECT alerted, suppress_reason, suppress_category FROM detections"
    ).fetchone()
    assert alerted == 0
    assert suppress_reason.startswith("data_integrity_failed: crossed_quote")
    assert category == "data_integrity"
    assert sent == []  # nothing was sent


def test_the_ledger_is_still_append_only_after_the_pipeline_has_written_to_it(monkeypatch):
    """The triggers are the reason this table can be trusted; wiring real
    writers to it must not have loosened them."""
    import sqlite3

    _, bar, result = _high_tier_fixture()
    conn = journal_connect(":memory:")
    _run_one_bar(conn, monkeypatch, result)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decision_events SET decision = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM decision_events")
