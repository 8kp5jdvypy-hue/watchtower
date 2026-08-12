"""Tests for the pure/testable pieces of tradebot.runner.

The full run_replay()/run_live() loops are exercised via an actual
--replay-date run (see the session transcript), not unit tests — they're
integration-shaped (calendars, journaling, alerting all wired together).
These tests cover the pieces that are meaningfully testable in isolation.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.alerts import AlertBudget, ConsoleAlerter, Decision
from tradebot.detectors import DailyAnchors, Detection
from tradebot.events import add_event_window
from tradebot.marketdata import Bar, Quote
from tradebot.journal import connect as journal_connect
from tradebot.journal import write_cluster
from tradebot.telegram_bot import outbox
import tradebot.runner as runner_mod
from tradebot.detectors import atr as compute_atr
from tradebot.runner import (
    HeartbeatStats,
    _alert_if_backfill_implausible,
    bar_gap_minutes,
    evaluate_bar,
    full_session_rth_bars,
    is_bar_gap,
    is_halted_bar,
    is_stale,
    process_new_bar,
    session_bounds,
)


def test_is_stale_true_past_the_threshold():
    latest_close = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    now = latest_close + timedelta(seconds=91)
    assert is_stale(latest_close, now, max_seconds=90) is True


def test_is_stale_false_within_the_threshold():
    latest_close = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    now = latest_close + timedelta(seconds=89)
    assert is_stale(latest_close, now, max_seconds=90) is False


def test_is_halted_bar_detects_zero_volume():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    assert is_halted_bar(Bar("BE", ts, 10, 10, 10, 10, volume=0)) is True
    assert is_halted_bar(Bar("BE", ts, 10, 10.5, 9.5, 10.2, volume=100)) is False


def test_bar_gap_minutes_none_with_fewer_than_two_bars():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    assert bar_gap_minutes([]) is None
    assert bar_gap_minutes([Bar("TSLA", ts, 100, 100, 100, 100, volume=100)]) is None


def test_bar_gap_minutes_measures_the_open_to_open_delta():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar("TSLA", ts, 100, 100, 100, 100, volume=100),
        Bar("TSLA", ts + timedelta(minutes=15), 100, 100, 100, 100, volume=100),
    ]
    assert bar_gap_minutes(bars) == 15.0


def test_is_bar_gap_false_on_the_expected_five_minute_cadence():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar("TSLA", ts, 100, 100, 100, 100, volume=100),
        Bar("TSLA", ts + timedelta(minutes=5), 100, 100, 100, 100, volume=100),
    ]
    assert is_bar_gap(bars) is False


def test_is_bar_gap_true_when_a_bar_was_silently_skipped():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar("TSLA", ts, 100, 100, 100, 100, volume=100),
        Bar("TSLA", ts + timedelta(minutes=10), 100, 100, 100, 100, volume=100),  # a 5-min bar never arrived
    ]
    assert is_bar_gap(bars) is True


def test_is_bar_gap_respects_tolerance_minutes():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar("TSLA", ts, 100, 100, 100, 100, volume=100),
        Bar("TSLA", ts + timedelta(minutes=6), 100, 100, 100, 100, volume=100),  # 1 min of jitter
    ]
    assert is_bar_gap(bars, tolerance_minutes=0.0) is True
    assert is_bar_gap(bars, tolerance_minutes=2.0) is False


def test_session_bounds_regular_day():
    open_ts, close_ts = session_bounds(date(2026, 7, 23))
    assert open_ts.hour == 13 and open_ts.minute == 30  # 09:30 ET in UTC (EDT)
    assert close_ts.hour == 20 and close_ts.minute == 0  # 16:00 ET in UTC (EDT)


def test_session_bounds_honors_early_close():
    # day after Thanksgiving 2026 — a known 13:00 ET early close
    open_ts, close_ts = session_bounds(date(2026, 11, 27))
    assert close_ts.hour == 18  # 13:00 ET in UTC (EST, UTC-5)


def test_session_bounds_rejects_non_trading_day():
    with pytest.raises(ValueError):
        session_bounds(date(2026, 7, 25))  # a Saturday


def test_run_live_idles_cleanly_on_a_non_trading_day_instead_of_crashing(monkeypatch):
    """Regression test: run_live() used to call session_bounds() (which
    raises ValueError for a non-trading day) before any other check, so a
    process supervisor that starts it unconditionally every day — as
    docker-compose.yml's `restart: unless-stopped` does, with no backoff
    — would crash-loop all weekend. It must idle and return cleanly
    instead."""
    saturday_utc = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)  # a real Saturday

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return saturday_utc

    monkeypatch.setattr(runner_mod, "datetime", _FrozenDatetime)
    sleep_calls = []
    monkeypatch.setattr(runner_mod.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    stats = runner_mod.run_live(ConsoleAlerter())

    assert stats.session_date == saturday_utc.astimezone(runner_mod.ET).date()
    assert sleep_calls == [runner_mod.OFF_SESSION_IDLE_SECONDS]


def test_run_live_does_not_resend_the_close_report_on_a_restart_after_close(monkeypatch, tmp_path):
    """Regression test for a real live incident (2026-08-10): Docker's
    `restart: unless-stopped` restarts run_live() on ANY exit, including
    the clean one at the end of a normal trading day. Without a
    same-session guard on the close side (the open side already has one
    — see maybe_send_session_open_messages' docstring), every restart
    landing after today's close fell straight through the while loop's
    first `loop_start >= close_ts` check and resent the full close report
    (log summary + heartbeat) again — a fast, unthrottled restart loop
    that spammed Telegram once per restart for hours."""
    trading_day = date(2026, 7, 23)
    open_ts, close_ts = runner_mod.session_bounds(trading_day)
    after_close = close_ts + timedelta(minutes=5)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_close

    monkeypatch.setattr(runner_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(runner_mod, "SESSION_CLOSE_STATE_FILE", tmp_path / "session_close_state.json")
    monkeypatch.setattr(runner_mod, "SESSION_OPEN_STATE_FILE", tmp_path / "session_open_state.json")
    monkeypatch.setattr(runner_mod, "HALT_FILE", tmp_path / "HALT")
    monkeypatch.setattr(runner_mod, "HEARTBEAT_FILE", tmp_path / "heartbeat.json")
    monkeypatch.setattr(runner_mod.time, "sleep", lambda seconds: None)

    sent = []

    class _SpyAlerter:
        def send(self, text, priority=None, alert_id=None):
            sent.append(text)

    db_path = tmp_path / "journal.db"

    first = runner_mod.run_live(_SpyAlerter(), db_path=db_path)
    assert first.session_date == trading_day
    assert len(sent) > 0  # the real close report, sent exactly once

    sent.clear()
    second = runner_mod.run_live(_SpyAlerter(), db_path=db_path)
    assert second.session_date == trading_day
    assert sent == []  # the restart must NOT resend it


def test_heartbeat_stats_record_cluster_tracks_tier_and_suppression_counts():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    stats = HeartbeatStats(start_time=start, session_date=date(2026, 7, 23))
    stats.record_cluster("high", Decision.SEND)
    stats.record_cluster("high", Decision.SUPPRESS_COOLDOWN)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)

    assert stats.tier_counts["high"] == 2
    assert stats.tier_counts["log"] == 1
    assert stats.suppression_counts["cooldown_active"] == 1
    # a plain SEND is not a suppression
    assert "send" not in stats.suppression_counts


class _SpyAlerter:
    def __init__(self):
        self.sent = []

    def send(self, text, priority=None, alert_id=None):
        self.sent.append((text, priority))


def test_alert_if_backfill_implausible_fires_on_zero_marks_with_real_detections(caplog):
    """2026-08-12 incident shape, exactly: ~160 real detections, 0 marks
    written, nothing anywhere signaled it. This is the tripwire that
    must have fired that day."""
    stats = HeartbeatStats(start_time=datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc), session_date=date(2026, 8, 12))
    stats.record_cluster("high", Decision.SEND)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)
    alerter = _SpyAlerter()

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        _alert_if_backfill_implausible(alerter, stats, 0, date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert len(alerter.sent) == 1
    text, priority = alerter.sent[0]
    assert "0 mark" in text and "2 detection" in text
    assert priority == outbox.PRIORITY_HIGH
    assert len(caplog.records) == 1 and caplog.records[0].levelname == "ERROR"


def test_alert_if_backfill_implausible_fires_on_implausibly_few_not_just_zero(caplog):
    """Not just the literal-zero case -- 1 mark for 50 detections is just
    as broken as 0 (a healthy day writes at least a CLOSE mark per
    detection), and must be just as loud."""
    stats = HeartbeatStats(start_time=datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc), session_date=date(2026, 8, 12))
    for _ in range(50):
        stats.record_cluster("log", Decision.QUEUED_FOR_EOD)
    alerter = _SpyAlerter()

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        _alert_if_backfill_implausible(alerter, stats, 1, date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert len(alerter.sent) == 1
    assert len(caplog.records) == 1


def test_alert_if_backfill_implausible_stays_quiet_on_a_healthy_day():
    stats = HeartbeatStats(start_time=datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc), session_date=date(2026, 8, 12))
    stats.record_cluster("high", Decision.SEND)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)
    alerter = _SpyAlerter()

    # 2 detections, 5 marks written (a plausible healthy outcome -- more
    # than one mark per detection) -- must not alert.
    _alert_if_backfill_implausible(alerter, stats, 5, date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert alerter.sent == []


def test_alert_if_backfill_implausible_stays_quiet_on_a_genuinely_quiet_session():
    """No detections at all today -- 0 marks is exactly correct, not a
    failure. Must never false-alarm on a legitimately quiet day."""
    stats = HeartbeatStats(start_time=datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc), session_date=date(2026, 8, 12))
    alerter = _SpyAlerter()

    _alert_if_backfill_implausible(alerter, stats, 0, date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert alerter.sent == []


def test_alert_if_backfill_implausible_logs_even_if_sending_the_alert_itself_fails():
    """The alert channel being the thing that's broken must not swallow
    the failure a second time -- see the incident's own shape: the first
    layer failing silently is exactly what this whole fix exists to
    prevent, including at this last-resort layer."""
    stats = HeartbeatStats(start_time=datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc), session_date=date(2026, 8, 12))
    stats.record_cluster("high", Decision.SEND)

    class _BrokenAlerter:
        def send(self, text, priority=None, alert_id=None):
            raise RuntimeError("telegram is down")

    # Must not raise -- a broken alert channel must never crash the
    # session-close sequence that runs after it (contract mid backfills,
    # heartbeat, session-close state write).
    _alert_if_backfill_implausible(_BrokenAlerter(), stats, 0, date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))


def _high_tier_fixture():
    """A synthetic evaluate_bar() result scored well above TIER_HIGH, for
    exercising process_new_bar's SEND branch without needing to hand-craft
    real detector-triggering bars."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=101.0, prior_low=99.0,
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


def test_process_new_bar_without_a_subscriber_hook_behaves_exactly_as_before(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)
    # no subscriber_hook passed — must not raise, and behaves like the pre-hook implementation
    assert stats.tier_counts["high"] == 1


def test_process_new_bar_defaults_data_feed_none_and_origin_watchlist(monkeypatch):
    """Every existing caller/test that doesn't pass data_feed/origin (this
    one included) must keep journaling exactly as before -- None/
    'watchlist' are the same defaults journal.write_cluster() itself
    uses."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)
    row = conn.execute("SELECT data_feed, origin FROM detections WHERE symbol = 'TSLA'").fetchone()
    assert row == (None, "watchlist")


def test_process_new_bar_journals_the_data_feed_and_origin_it_was_given(monkeypatch):
    """Both callers (run_replay/run_live) resolve data_feed/origin once
    per invocation/tick and pass them straight through -- this confirms
    process_new_bar actually threads them to the journal row, the one
    thing neither caller can verify about itself in isolation."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        data_feed="sip", origin="screening",
    )
    row = conn.execute("SELECT data_feed, origin FROM detections WHERE symbol = 'TSLA'").fetchone()
    assert row == ("sip", "screening")


def test_process_new_bar_calls_subscriber_hook_with_the_cluster_and_rendered_text_on_a_high_send(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    calls = []
    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=lambda cluster, text, entry_mid: calls.append((cluster, text, entry_mid)),
    )

    assert len(calls) == 1
    cluster, text, entry_mid = calls[0]
    assert cluster.symbol == "TSLA" and cluster.tier == "high"
    assert "TSLA" in text
    assert entry_mid is None  # this fixture's chain_fn always raises NotImplementedError -> NO TRADE


def test_process_new_bar_swallows_a_subscriber_hook_exception_without_dropping_the_alert(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    def broken_hook(cluster, text, entry_mid):
        raise RuntimeError("simulated fan-out failure")

    process_new_bar(  # must not raise
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=broken_hook,
    )
    assert any("fan-out failed" in e for e in stats.errors)
    row = conn.execute("SELECT alerted FROM detections").fetchone()
    assert row[0] == 1  # the alert itself still went out despite the hook blowing up


def test_process_new_bar_suppresses_on_a_bar_gap_without_journaling_or_evaluating(monkeypatch):
    """Same precedent as the existing halted-bar (zero-volume) skip: no
    Detection was ever produced, so nothing should be journaled — the
    gap is visible only via stats.data_gaps and the data_health_suppression
    metric, not a detections row."""
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar("TSLA", ts, 100, 100, 100, 100, volume=100),
        Bar("TSLA", ts + timedelta(minutes=15), 100, 100, 100, 100, volume=100),
    ]
    anchors, _, _ = _high_tier_fixture()

    called = []
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, b, a: called.append(1) or None)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bars[-1].ts)
    stats = HeartbeatStats(start_time=bars[-1].ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bars[-1].ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), bars, anchors, quote_fn, chain_fn, stats)

    assert called == []  # evaluate_bar never even ran
    assert any("bar gap" in g for g in stats.data_gaps)
    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0


def test_evaluate_bar_cluster_atr14_matches_the_primary_detectors_own_atr():
    """Regression test for a real production bug: an alert showed ATR(14)
    as two different numbers in one message — the headline (built from a
    detector's own window) and the stats block (built from
    evaluate_bar's independently-recomputed atr(bars)) could disagree,
    because range_expansion scores against atr(bars[:-1]) (deliberately
    excluding the current, possibly-anomalous bar) while evaluate_bar used
    to compute atr(bars) (including it) for cluster.atr14. On a genuinely
    wide current bar those two windows can diverge sharply. The fix reuses
    whatever ATR the primary/headline detector actually used instead of
    recomputing a second, independent number."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=115.0, prior_low=85.0,
        opening_range_high=120.0, opening_range_low=80.0, opening_range_volume=10_000,
        swing_high=130.0, swing_low=70.0, avg_cum_volume_by_bar={},
    )
    base = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = []
    price = 100.0
    for i in range(16):
        bars.append(Bar("TSLA", base + timedelta(minutes=5 * i), price, price + 0.1, price - 0.1, price + 0.05, volume=1_000))
        price += 0.05
    # The current bar has a huge range relative to the last 16 quiet bars —
    # this is exactly what triggers range_expansion, and exactly the
    # scenario where atr(bars) vs atr(bars[:-1]) diverge sharply.
    bars.append(Bar("TSLA", base + timedelta(minutes=80), 100.8, 115.0, 95.0, 101.0, volume=5_000))

    result = evaluate_bar("TSLA", bars, anchors)

    assert result["primary_kind"] == "range_expansion"
    assert "ATR(14)=0.20" in result["primary_headline"]

    primary_detection = result["detections"][0]
    assert result["atr14"] == primary_detection.context["atr14"]

    # Prove this is a real behavior change, not a vacuous assertion: the
    # old buggy computation (atr on the full window, including the huge
    # current bar) is a materially different number.
    old_buggy_value = compute_atr(bars)
    assert result["atr14"] != old_buggy_value
    assert abs(old_buggy_value - result["atr14"]) > 1.0  # ~1.6 vs ~0.2 in this fixture


def test_process_new_bar_guard_rejection_logs_error_and_emits_a_metric(monkeypatch, caplog, tmp_path):
    from tradebot import metrics as metrics_mod

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_path)

    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def bad_quote_fn(symbol):
        # crossed quote — bid > ask — must trip the guard
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, bad_quote_fn, chain_fn, stats)

    assert any("alert suppressed by data guard" in r.message and "rule=crossed_quote" in r.message for r in caplog.records)

    row = conn.execute("SELECT alerted, suppress_reason, suppress_category FROM detections").fetchone()
    assert row[0] == 0
    assert row[1].startswith("data_integrity_failed: crossed_quote")
    assert row[2] == "data_integrity"

    assert metrics_mod.read_all(metrics_path) == {
        "validator_rejection{rule=crossed_quote}": 1,
        "suppression{category=data_integrity}": 1,
    }


def test_process_new_bar_selects_a_contract_and_journals_it(monkeypatch):
    """End-to-end: a real chain_fn produces a real ContractSelection that
    reaches templates.render_high_alert and gets journaled — not a
    None-chain stub like the other process_new_bar tests above."""
    from tradebot.marketdata import OptionChain, OptionContract

    anchors, bar, result = _high_tier_fixture()
    result = {**result, "trend": "up"}  # bullish -> calls
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    # 100.2 spot, target delta 0.40-0.55 -> the 100-strike call at delta 0.50
    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)

    detection_id = conn.execute("SELECT id FROM detections").fetchone()[0]
    row = conn.execute(
        "SELECT symbol, right, strike, dte, delta, is_vertical FROM contract_selections WHERE detection_id = ?",
        (detection_id,),
    ).fetchone()
    assert row == ("TSLA", "call", 100.0, 8, 0.50, 0)

    no_trade_flag = conn.execute("SELECT no_trade FROM detections WHERE id = ?", (detection_id,)).fetchone()[0]
    assert no_trade_flag == 0  # a contract WAS selected

    iv_row = conn.execute("SELECT iv FROM iv_history WHERE symbol = ?", ("TSLA",)).fetchone()
    assert iv_row == (0.35,)


def test_process_new_bar_passes_the_real_entry_mid_to_the_subscriber_hook(monkeypatch):
    """The position-size calculator (tradebot.costs.position_size, wired
    up in tradebot.telegram_bot.delivery) is tied to this SAME entry_mid,
    not a separately re-derived one — confirm it actually reaches the
    subscriber_hook call, not just the journal."""
    from tradebot.marketdata import OptionChain, OptionContract

    anchors, bar, result = _high_tier_fixture()
    result = {**result, "trend": "up"}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    calls = []
    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=lambda cluster, text, entry_mid: calls.append(entry_mid),
    )

    assert calls == [pytest.approx((2.00 + 2.05) / 2)]  # the long leg's own bid/ask mid, single-leg (no short)


def test_process_new_bar_blackouts_a_contract_when_a_real_earnings_event_falls_before_expiry(monkeypatch):
    """End-to-end: runner.py's bound_earnings_check_fn now reads the real
    event_windows table (tradebot.events.has_earnings_before) instead of
    the old, always-empty telegram_bot.db events table — confirm the
    wiring actually blocks a trade, not just that has_earnings_before()
    works correctly in isolation."""
    from tradebot.marketdata import OptionChain, OptionContract

    anchors, bar, result = _high_tier_fixture()
    result = {**result, "trend": "up"}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    # earnings between today (2026-07-23) and the expiry the fixture below
    # will pick (2026-07-31, same DTE math as the test above)
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=datetime(2026, 7, 28, 13, 30, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc), severity="suppress", source="nasdaq_earnings",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)

    detection_id = conn.execute("SELECT id FROM detections").fetchone()[0]
    no_trade_flag = conn.execute("SELECT no_trade FROM detections WHERE id = ?", (detection_id,)).fetchone()[0]
    assert no_trade_flag == 1
    assert conn.execute("SELECT COUNT(*) FROM contract_selections").fetchone()[0] == 0


def _no_op_chain_fn(symbol, expiry):
    raise NotImplementedError


def _flat_quote_fn(bar):
    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)
    return quote_fn


def test_process_new_bar_suppresses_a_non_escalating_repeat_as_a_duplicate_without_burning_the_cap(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts, max_high_per_day=8)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    # Same score, 10 minutes later — well inside the default 30-minute
    # dedup window, not a material escalation.
    bar2 = Bar("TSLA", bar.ts + timedelta(minutes=5), 100.2, 100.7, 100.0, 100.4, volume=10_000)
    result2 = {**result, "ts": result["ts"] + timedelta(minutes=10)}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result2)
    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar, bar2], anchors,
        _flat_quote_fn(bar2), _no_op_chain_fn, stats,
    )

    rows = conn.execute(
        "SELECT alerted, suppress_reason, suppress_category, lifecycle_state, related_detection_id "
        "FROM detections ORDER BY ts_utc"
    ).fetchall()
    assert len(rows) == 2
    first_id = conn.execute("SELECT id FROM detections ORDER BY ts_utc LIMIT 1").fetchone()[0]
    assert rows[0][0] == 1 and rows[0][3] == "watch" and rows[0][4] is None
    assert rows[1][0] == 0  # not alerted
    assert rows[1][1] == f"duplicate_event:{first_id}"
    assert rows[1][2] == "duplicate"
    assert rows[1][3] == "confirmed"
    assert rows[1][4] == first_id

    # The duplicate must not have burned a daily-cap slot — budget still
    # has all 8 real sends available (1 used by the first cluster).
    assert len(budget._high_sent_today) == 1


def test_process_new_bar_still_sends_a_material_escalation_within_the_dedup_window(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts, max_high_per_day=8)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    # Much higher score, a DIFFERENT detector kind (the realistic dedup
    # case — a second, different signal on the same symbol shortly after
    # the first), still inside the window — a real escalation. Using the
    # same kind as the first cluster would also trip AlertBudget's own
    # unrelated 45-minute per-(symbol,kind) cooldown, which would
    # confound this test's one variable (dedup letting an escalation
    # through) with a second, different suppression mechanism.
    bar2 = Bar("TSLA", bar.ts + timedelta(minutes=5), 100.2, 100.7, 100.0, 100.4, volume=10_000)
    escalated_detection = Detection("TSLA", "vwap_break", bar2.ts, 20.0, "a bigger break", {})
    result2 = {
        **result, "ts": result["ts"] + timedelta(minutes=10), "score": 20.0,
        "kinds": "vwap_break", "primary_kind": "vwap_break", "primary_headline": "a bigger break",
        "headlines": "a bigger break", "primary_detection": escalated_detection, "detections": [escalated_detection],
    }
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result2)
    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar, bar2], anchors,
        _flat_quote_fn(bar2), _no_op_chain_fn, stats,
    )

    rows = conn.execute("SELECT alerted, suppress_reason, lifecycle_state FROM detections ORDER BY ts_utc").fetchall()
    assert len(rows) == 2
    assert rows[1][0] == 1  # sent normally, not suppressed
    assert rows[1][1] is None
    assert rows[1][2] == "confirmed"  # still tagged confirmed for lifecycle visibility
    assert len(budget._high_sent_today) == 2  # both real sends counted


def test_process_new_bar_suppresses_high_alert_inside_a_suppress_severity_event_window(monkeypatch):
    """News as suppression, not an alert source: a HIGH cluster whose bar
    close falls inside a 'suppress' severity event window must never be
    sent, and the journal must say why — see tradebot.events module
    docstring."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT alerted, suppress_reason, suppress_category, tier, news_driven FROM detections").fetchone()
    assert row[0] == 0  # never alerted
    assert row[1] == "news_blackout:8-K:material event"
    assert row[2] == "news_blackout"  # structured counterpart, parallel to the free-text reason above
    assert row[3] == "high"  # the journal's ground-truth tier is score-based and unaffected
    assert row[4] == 1
    assert stats.suppression_counts["news_blackout"] == 1


def test_process_new_bar_downgrades_high_alert_inside_a_downgrade_severity_event_window(monkeypatch):
    """A 'downgrade' severity window still gets a look — just batched into
    the medium digest instead of pushed immediately as HIGH."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="downgrade", source="test", detail="earnings day",
    )
    clock = {"t": bar.ts}
    budget = AlertBudget(now=lambda: clock["t"])
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT alerted, suppress_reason, tier, news_driven FROM detections").fetchone()
    assert row[0] == 0  # not alerted immediately — queued for the medium digest instead
    assert row[1] is None  # queued, not suppressed
    assert row[2] == "high"  # ground-truth journal tier still reflects the real score
    assert row[3] == 1
    assert stats.tier_counts["medium"] == 1  # routed as medium from here on
    assert "high" not in stats.tier_counts

    # Prove it actually landed in the medium queue (not lost, not sent as
    # high) by crossing an hour boundary and popping the digest.
    clock["t"] = clock["t"] + timedelta(hours=1)
    digest = budget.pop_medium_digest_if_due()
    assert digest is not None and len(digest) == 1
    assert digest[0].symbol == "TSLA" and digest[0].tier == "medium"


def test_process_new_bar_event_window_suppression_does_not_burn_cap_or_cooldown(monkeypatch):
    """A blackout-suppressed HIGH alert must never reach AlertBudget.evaluate()
    at all — otherwise it would silently consume the daily HIGH cap or start
    the per-(symbol, kind) cooldown for an alert nobody ever saw, blocking a
    later, legitimate alert of the same kind."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    # A second bar, same symbol/kind, at the SAME budget "now" — if the
    # first (suppressed) alert had wrongly started the cooldown, this one
    # would see zero elapsed time and get suppressed too. Its RESULT ts
    # (below, independent of bar2's own ts since evaluate_bar is mocked)
    # is past both the event window's end (result["ts"] + 5min) AND the
    # dedup module's default DEDUP_WINDOW_MINUTES (30) — a cluster inside
    # the dedup window would get suppressed as a duplicate of the first
    # one too, which is real, correct, *new* behavior (tradebot.dedup)
    # but would confound this test's one variable: proving cooldown
    # specifically was never wrongly started by a suppressed alert. bar2
    # itself stays on the normal 5-minute cadence — a wider gap there
    # would trip the unrelated bar-gap data-health check.
    bar2 = Bar("TSLA", bar.ts + timedelta(minutes=5), 100.2, 100.7, 100.0, 100.4, volume=10_000)
    result2 = {**result, "ts": result["ts"] + timedelta(minutes=35)}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result2)

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar, bar2], anchors,
        _flat_quote_fn(bar2), _no_op_chain_fn, stats,
    )

    rows = conn.execute("SELECT alerted, suppress_reason FROM detections ORDER BY ts_utc").fetchall()
    assert len(rows) == 2
    assert rows[0] == (0, "news_blackout:8-K:material event")
    assert rows[1] == (1, None)  # sent normally — cooldown was never started by the blackout


def test_process_new_bar_tags_but_does_not_reroute_non_high_tiers_inside_an_event_window(monkeypatch):
    """Suppress/downgrade ROUTING only applies to HIGH — MEDIUM/LOG are
    already batched, so there's no immediate-publish race for a blackout
    window to protect against. But news_driven TAGGING applies to every
    tier: a medium-tier cluster overlapping a known event is still not a
    clean technical setup, and historical_performance() excludes it from
    the sample pool regardless of tier. See tradebot.events module
    docstring."""
    anchors, bar, result = _high_tier_fixture()
    medium_result = {**result, "score": 3.0}  # below HIGH threshold
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: medium_result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=medium_result["ts"] - timedelta(minutes=5),
        end_utc=medium_result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT suppress_reason, tier, news_driven FROM detections").fetchone()
    assert row[0] is None  # normal medium routing (queued, not suppressed) — untouched by the event window
    assert row[1] == "medium"
    assert row[2] == 1  # tagged news-driven regardless of tier


def test_process_new_bar_journals_the_primary_kind_and_symbol_class(monkeypatch):
    """Full alert context, not just the multi-detector kinds string —
    primary_kind and symbol_class must be real, queryable columns so any
    stat can be recomputed later without guessing."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT primary_kind, symbol_class FROM detections").fetchone()
    assert row == ("gap", "deep")  # TSLA is in config.DEEP_LIQUIDITY_SYMBOLS


def test_process_new_bar_records_which_event_kind_and_severity_applied(monkeypatch):
    """set_news_driven's kind/severity snapshot (see journal.py) must
    reflect the ACTUAL window that fired, not just a bare boolean."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT event_kind, event_severity FROM detections").fetchone()
    assert row == ("8-K", "suppress")


# -------------------------------------------------------------------- #
# Contract forward-mid backfill — live only, see runner.py's module
# comment on backfill_pending_contract_mids/_close_mids.
# -------------------------------------------------------------------- #


def _fake_chain(*contracts):
    from tradebot.marketdata import OptionChain

    return OptionChain(symbol="X", expiry=date(2026, 8, 14), contracts=list(contracts))


def _fake_contract(right, strike, bid, ask):
    from tradebot.marketdata import OptionContract

    return OptionContract(
        symbol="X", expiry=date(2026, 8, 14), strike=strike, right=right,
        bid=bid, ask=ask, last=(bid + ask) / 2, delta=None, theta=None, open_interest=100,
    )


def test_contract_mid_finds_the_matching_contract_by_right_and_strike():
    chain = _fake_chain(_fake_contract("put", 365.0, 4.00, 4.10), _fake_contract("put", 360.0, 2.00, 2.10))
    assert runner_mod._contract_mid(chain, "put", 365.0) == pytest.approx(4.05)


def test_contract_mid_returns_none_when_not_found_rather_than_fabricating():
    chain = _fake_chain(_fake_contract("put", 360.0, 2.00, 2.10))
    assert runner_mod._contract_mid(chain, "put", 365.0) is None


def test_forward_mid_single_leg_is_just_the_contracts_own_mid():
    md = {"GOOGL": type("MD", (), {"chain": staticmethod(lambda s, expiry: _fake_chain(_fake_contract("put", 365.0, 4.00, 4.10)))})()}
    mid = runner_mod._forward_mid(md, "GOOGL", "put", 365.0, "2026-08-14", is_vertical=False, short_strike=None)
    assert mid == pytest.approx(4.05)


def test_forward_mid_vertical_is_long_minus_short_same_as_entry_mid_formula():
    chain = _fake_chain(_fake_contract("call", 100.0, 2.00, 2.10), _fake_contract("call", 105.0, 0.50, 0.60))
    md = {"TSLA": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})()}
    mid = runner_mod._forward_mid(md, "TSLA", "call", 100.0, "2026-08-14", is_vertical=True, short_strike=105.0)
    assert mid == pytest.approx(2.05 - 0.55)


def test_forward_mid_none_when_the_short_leg_is_missing_from_the_chain():
    chain = _fake_chain(_fake_contract("call", 100.0, 2.00, 2.10))  # short leg absent
    md = {"TSLA": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})()}
    mid = runner_mod._forward_mid(md, "TSLA", "call", 100.0, "2026-08-14", is_vertical=True, short_strike=105.0)
    assert mid is None


def test_forward_mid_none_when_the_chain_fetch_raises():
    def _raise(s, expiry):
        raise RuntimeError("vendor hiccup")

    md = {"TSLA": type("MD", (), {"chain": staticmethod(_raise)})()}
    mid = runner_mod._forward_mid(md, "TSLA", "put", 365.0, "2026-08-14", is_vertical=False, short_strike=None)
    assert mid is None


def _write_minimal_detection(conn, entry_ts, symbol="GOOGL"):
    """A real detections row for contract_selections' FK-shaped join
    (pending_contract_close_backfills) — the specific score/kind values
    don't matter to these backfill tests, only that the row exists."""
    return write_cluster(
        conn, session=entry_ts.date().isoformat(), symbol=symbol, ts_utc=entry_ts.isoformat(),
        kinds="level_break", headlines="h", score=10.0, close=100.0, atr14=1.0,
        trend="up", detections=[Detection(symbol, "level_break", entry_ts, 10.0, "h", {})],
        code_version_str="v1", primary_kind="level_break",
    )


def test_backfill_pending_contract_mids_writes_a_real_fetched_mid(tmp_path):
    from tradebot.journal import record_contract_selection

    conn = journal_connect(":memory:")
    entry_ts = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    detection_id = _write_minimal_detection(conn, entry_ts)
    record_contract_selection(
        conn, detection_id, symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13,
        delta=-0.47, entry_mid=4.20, entry_ts=entry_ts,
    )
    chain = _fake_chain(_fake_contract("put", 365.0, 3.90, 4.00))
    md = {"GOOGL": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})()}

    runner_mod.backfill_pending_contract_mids(conn, md, entry_ts + timedelta(minutes=31))

    mid_30 = conn.execute("SELECT mid_30m FROM contract_selections WHERE detection_id = ?", (detection_id,)).fetchone()[0]
    assert mid_30 == pytest.approx(3.95)


def test_backfill_pending_contract_mids_one_failure_does_not_block_another(tmp_path):
    from tradebot.journal import record_contract_selection

    conn = journal_connect(":memory:")
    entry_ts = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    ok_id = _write_minimal_detection(conn, entry_ts, symbol="GOOGL")
    broken_id = _write_minimal_detection(conn, entry_ts, symbol="TSLA")
    record_contract_selection(
        conn, ok_id, symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13,
        delta=-0.47, entry_mid=4.20, entry_ts=entry_ts,
    )
    record_contract_selection(
        conn, broken_id, symbol="TSLA", right="call", strike=100.0, expiry=date(2026, 8, 14), dte=13,
        delta=0.50, entry_mid=2.00, entry_ts=entry_ts,
    )

    def _raise(s, expiry):
        raise RuntimeError("vendor hiccup")

    chain = _fake_chain(_fake_contract("put", 365.0, 3.90, 4.00))
    md = {
        "GOOGL": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})(),
        "TSLA": type("MD", (), {"chain": staticmethod(_raise)})(),
    }

    runner_mod.backfill_pending_contract_mids(conn, md, entry_ts + timedelta(minutes=31))

    ok_mid = conn.execute("SELECT mid_30m FROM contract_selections WHERE detection_id = ?", (ok_id,)).fetchone()[0]
    broken_mid = conn.execute("SELECT mid_30m FROM contract_selections WHERE detection_id = ?", (broken_id,)).fetchone()[0]
    assert ok_mid == pytest.approx(3.95)
    assert broken_mid is None  # never fabricated, and didn't stop the other from backfilling


def test_backfill_pending_contract_close_mids_uses_the_close_sentinel(tmp_path):
    from tradebot.journal import CLOSE_MARK_OFFSET_MIN, record_contract_selection

    conn = journal_connect(":memory:")
    entry_ts = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    detection_id = _write_minimal_detection(conn, entry_ts)
    record_contract_selection(
        conn, detection_id, symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13,
        delta=-0.47, entry_mid=4.20, entry_ts=entry_ts,
    )
    chain = _fake_chain(_fake_contract("put", 365.0, 3.50, 3.60))
    md = {"GOOGL": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})()}

    runner_mod.backfill_pending_contract_close_mids(conn, md, date(2026, 7, 23))

    mid_close = conn.execute("SELECT mid_close FROM contract_selections WHERE detection_id = ?", (detection_id,)).fetchone()[0]
    assert mid_close == pytest.approx(3.55)


# ---------------------------------------------------------------------- #
# Contract day-range backfill — see runner.backfill_contract_day_ranges /
# journal.pending_contract_day_range_backfills.
# ---------------------------------------------------------------------- #


def test_backfill_contract_day_ranges_writes_a_real_fetched_range(tmp_path, monkeypatch):
    from tradebot.journal import record_contract_selection

    conn = journal_connect(":memory:")
    entry_ts = datetime(2026, 4, 8, 16, 5, tzinfo=timezone.utc)
    detection_id = _write_minimal_detection(conn, entry_ts, symbol="META")
    record_contract_selection(
        conn, detection_id, symbol="META", right="call", strike=600.0, expiry=date(2026, 4, 17), dte=9,
        delta=0.45, entry_mid=2.96, entry_ts=entry_ts,
    )
    contract = _fake_contract("call", 600.0, 1.80, 1.90)
    chain = _fake_chain(contract)
    md = {"META": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})()}

    monkeypatch.setattr(
        "tradebot.vendors.alpaca.fetch_option_day_range", lambda occ_symbol, session_date: (1.43, 3.90)
    )

    runner_mod.backfill_contract_day_ranges(conn, md, date(2026, 4, 8))

    row = conn.execute(
        "SELECT day_low, day_high FROM contract_selections WHERE detection_id = ?", (detection_id,)
    ).fetchone()
    assert row == (1.43, 3.90)


def test_backfill_contract_day_ranges_skips_verticals(tmp_path, monkeypatch):
    from tradebot.journal import record_contract_selection

    conn = journal_connect(":memory:")
    entry_ts = datetime(2026, 4, 8, 16, 5, tzinfo=timezone.utc)
    detection_id = _write_minimal_detection(conn, entry_ts, symbol="META")
    record_contract_selection(
        conn, detection_id, symbol="META", right="call", strike=600.0, expiry=date(2026, 4, 17), dte=9,
        delta=0.45, entry_mid=1.50, entry_ts=entry_ts, is_vertical=True, short_strike=610.0, short_delta=0.20,
    )
    calls = []
    monkeypatch.setattr(
        "tradebot.vendors.alpaca.fetch_option_day_range",
        lambda occ_symbol, session_date: calls.append(occ_symbol) or (1.0, 2.0),
    )

    runner_mod.backfill_contract_day_ranges(conn, {}, date(2026, 4, 8))

    assert calls == []  # never even attempted for a vertical
    row = conn.execute(
        "SELECT day_low, day_high FROM contract_selections WHERE detection_id = ?", (detection_id,)
    ).fetchone()
    assert row == (None, None)


def test_backfill_contract_day_ranges_one_failure_does_not_block_another(tmp_path, monkeypatch):
    from tradebot.journal import record_contract_selection

    conn = journal_connect(":memory:")
    entry_ts = datetime(2026, 4, 8, 16, 5, tzinfo=timezone.utc)
    ok_id = _write_minimal_detection(conn, entry_ts, symbol="META")
    broken_id = _write_minimal_detection(conn, entry_ts, symbol="TSLA")
    record_contract_selection(
        conn, ok_id, symbol="META", right="call", strike=600.0, expiry=date(2026, 4, 17), dte=9,
        delta=0.45, entry_mid=2.96, entry_ts=entry_ts,
    )
    record_contract_selection(
        conn, broken_id, symbol="TSLA", right="call", strike=100.0, expiry=date(2026, 8, 14), dte=13,
        delta=0.50, entry_mid=2.00, entry_ts=entry_ts,
    )

    def _raise(s, expiry):
        raise RuntimeError("vendor hiccup")

    chain = _fake_chain(_fake_contract("call", 600.0, 1.80, 1.90))
    md = {
        "META": type("MD", (), {"chain": staticmethod(lambda s, expiry: chain)})(),
        "TSLA": type("MD", (), {"chain": staticmethod(_raise)})(),
    }
    monkeypatch.setattr(
        "tradebot.vendors.alpaca.fetch_option_day_range", lambda occ_symbol, session_date: (1.43, 3.90)
    )

    runner_mod.backfill_contract_day_ranges(conn, md, date(2026, 4, 8))

    ok_row = conn.execute("SELECT day_low, day_high FROM contract_selections WHERE detection_id = ?", (ok_id,)).fetchone()
    broken_row = conn.execute("SELECT day_low, day_high FROM contract_selections WHERE detection_id = ?", (broken_id,)).fetchone()
    assert ok_row == (1.43, 3.90)
    assert broken_row == (None, None)  # never fabricated, and didn't stop the other


# ---------------------------------------------------------------------- #
# Weekly recap scheduling — see runner.maybe_send_weekly_recap. A cursor
# file, not a "is today Monday" check, so a skipped day never silently
# drops a week.
# ---------------------------------------------------------------------- #


class _FakeAlerter:
    def __init__(self):
        self.sent = []

    def send(self, text, priority=None, alert_id=None):
        self.sent.append((text, priority))


def test_weekly_recap_fires_on_the_first_call_and_covers_the_prior_week(tmp_path):
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "weekly_recap_state.json"
    now = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)  # a Monday

    runner_mod.maybe_send_weekly_recap(conn, alerter, now, state_path=state_path)

    assert len(alerter.sent) == 1
    assert "Weekly recap" in alerter.sent[0][0]
    assert "2026-07-27" in alerter.sent[0][0]  # the prior Monday
    assert state_path.exists()


def test_weekly_recap_does_not_resend_within_the_same_week(tmp_path):
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "weekly_recap_state.json"
    monday = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    runner_mod.maybe_send_weekly_recap(conn, alerter, monday, state_path=state_path)

    wednesday = monday + timedelta(days=2)
    runner_mod.maybe_send_weekly_recap(conn, alerter, wednesday, state_path=state_path)

    assert len(alerter.sent) == 1  # no second send


def test_weekly_recap_fires_again_the_following_week(tmp_path):
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "weekly_recap_state.json"
    monday1 = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    runner_mod.maybe_send_weekly_recap(conn, alerter, monday1, state_path=state_path)
    monday2 = monday1 + timedelta(days=7)
    runner_mod.maybe_send_weekly_recap(conn, alerter, monday2, state_path=state_path)

    assert len(alerter.sent) == 2
    assert "2026-07-27" in alerter.sent[0][0]
    assert "2026-08-03" in alerter.sent[1][0]


# ---------------------------------------------------------------------- #
# Session-open messages — see runner.maybe_send_session_open_messages.
# Guards the morning briefing + pre-open card against duplicate sends
# when run_live() is invoked more than once for the same session_date
# (e.g. a supervised container restart after a clean end-of-session
# exit, or a human re-running scripts/start.sh mid-morning).
# ---------------------------------------------------------------------- #


def test_session_open_messages_send_once(tmp_path):
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "session_open_state.json"
    session_date = date(2026, 8, 10)
    now = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)

    runner_mod.maybe_send_session_open_messages(conn, alerter, session_date, now, state_path=state_path)

    assert len(alerter.sent) == 2  # morning briefing + pre-open card
    assert state_path.exists()


def test_session_open_messages_do_not_resend_for_the_same_session(tmp_path):
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "session_open_state.json"
    session_date = date(2026, 8, 10)
    first_call = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)
    runner_mod.maybe_send_session_open_messages(conn, alerter, session_date, first_call, state_path=state_path)

    # A restart later the same session (e.g. after a crash, or a Docker
    # restart following a clean end-of-session exit) must not resend.
    restart = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    runner_mod.maybe_send_session_open_messages(conn, alerter, session_date, restart, state_path=state_path)

    assert len(alerter.sent) == 2  # still just the first call's two sends


def test_session_open_messages_send_again_for_a_new_session(tmp_path):
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "session_open_state.json"
    runner_mod.maybe_send_session_open_messages(
        conn, alerter, date(2026, 8, 10), datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc), state_path=state_path
    )
    runner_mod.maybe_send_session_open_messages(
        conn, alerter, date(2026, 8, 11), datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc), state_path=state_path
    )

    assert len(alerter.sent) == 4  # two sessions, two sends each


class _FakeResponse:
    def __init__(self, result, status_code=200):
        self._result = result
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise runner_mod.requests.HTTPError(f"{self.status_code}")

    def json(self):
        return {"ok": True, "result": self._result}


def test_pinned_status_first_call_sends_and_pins_then_saves_state(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("sendMessage"):
            return _FakeResponse({"message_id": 42})
        return _FakeResponse({})

    monkeypatch.setattr(runner_mod.requests, "post", fake_post)
    conn = journal_connect(":memory:")
    state_path = tmp_path / "pinned_status_state.json"

    runner_mod.maybe_update_pinned_status("tok", 555, conn, datetime(2026, 8, 6, tzinfo=timezone.utc), state_path=state_path)

    methods = [c[0] for c in calls]
    assert methods == ["sendMessage", "pinChatMessage"]
    assert calls[1][1]["message_id"] == 42
    assert calls[1][1]["disable_notification"] is True
    saved = json.loads(state_path.read_text())
    assert saved == {"chat_id": 555, "message_id": 42}


def test_pinned_status_second_call_edits_in_place_without_repinning(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url.rsplit("/", 1)[-1], json))
        return _FakeResponse({"message_id": 42})

    monkeypatch.setattr(runner_mod.requests, "post", fake_post)
    conn = journal_connect(":memory:")
    state_path = tmp_path / "pinned_status_state.json"
    state_path.write_text(json.dumps({"chat_id": 555, "message_id": 42}))

    runner_mod.maybe_update_pinned_status("tok", 555, conn, datetime(2026, 8, 6, tzinfo=timezone.utc), state_path=state_path)

    assert [c[0] for c in calls] == ["editMessageText"]
    assert calls[0][1]["message_id"] == 42


def test_pinned_status_recreates_if_the_edit_fails(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, json))
        if method == "editMessageText":
            return _FakeResponse({}, status_code=400)  # e.g. message was deleted/unpinned by hand
        if method == "sendMessage":
            return _FakeResponse({"message_id": 99})
        return _FakeResponse({})

    monkeypatch.setattr(runner_mod.requests, "post", fake_post)
    conn = journal_connect(":memory:")
    state_path = tmp_path / "pinned_status_state.json"
    state_path.write_text(json.dumps({"chat_id": 555, "message_id": 42}))

    runner_mod.maybe_update_pinned_status("tok", 555, conn, datetime(2026, 8, 6, tzinfo=timezone.utc), state_path=state_path)

    assert [c[0] for c in calls] == ["editMessageText", "sendMessage", "pinChatMessage"]
    assert json.loads(state_path.read_text())["message_id"] == 99


def test_weekly_recap_catches_up_correctly_after_a_skipped_run(tmp_path):
    """The whole reason this is a cursor, not a "is today Monday" check:
    a holiday or an outage on the actual Monday must not silently drop
    that week's recap forever."""
    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    state_path = tmp_path / "weekly_recap_state.json"
    monday1 = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    runner_mod.maybe_send_weekly_recap(conn, alerter, monday1, state_path=state_path)

    # bot was down all of the following week; first run back is a Thursday
    late_thursday = monday1 + timedelta(days=10)
    runner_mod.maybe_send_weekly_recap(conn, alerter, late_thursday, state_path=state_path)

    assert len(alerter.sent) == 2
    assert "2026-08-03" in alerter.sent[1][0]  # covers the week that was missed, up to the following Monday


def test_send_medium_digest_if_due_calls_the_optional_personal_fanout_fn():
    """personal_fanout_fn is how 'aggressive'-sensitivity subscribers get
    the MEDIUM digest personally (see delivery.make_medium_fanout_fn) —
    None (the default, used by replay and every other existing caller)
    must leave behavior exactly as it was before this parameter existed."""
    from tradebot.alerts import AlertBudget, Cluster

    def _medium_cluster(cid):
        return Cluster(
            id=cid, ts_utc="2026-07-23T14:00:00+00:00", session="2026-07-23", symbol="TSLA",
            kinds="gap", headlines="h", primary_headline="h", score=2.0, tier="medium",
            close=100.0, atr14=1.0, trend="up", code_version="v1",
        )

    clock = {"t": datetime(2026, 7, 23, 13, 5, tzinfo=timezone.utc)}
    budget = AlertBudget(now=lambda: clock["t"])
    budget.evaluate(_medium_cluster("m1"))
    clock["t"] += timedelta(hours=1, minutes=5)  # cross the hour boundary so the digest is due

    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    fanout_calls = []

    def fanout(clusters, text, when):
        fanout_calls.append(([c.id for c in clusters], text, when))

    runner_mod.send_medium_digest_if_due(budget, alerter, conn, clock["t"], fanout)

    assert len(alerter.sent) == 1  # the ops-channel digest still goes out exactly as before
    assert len(fanout_calls) == 1
    assert fanout_calls[0][0] == ["m1"]
    assert fanout_calls[0][1] == alerter.sent[0][0]  # the SAME rendered text, not a second copy


def test_send_medium_digest_if_due_without_a_fanout_fn_behaves_exactly_as_before():
    from tradebot.alerts import AlertBudget, Cluster

    cluster = Cluster(
        id="m1", ts_utc="2026-07-23T14:00:00+00:00", session="2026-07-23", symbol="TSLA",
        kinds="gap", headlines="h", primary_headline="h", score=2.0, tier="medium",
        close=100.0, atr14=1.0, trend="up", code_version="v1",
    )
    clock = {"t": datetime(2026, 7, 23, 13, 5, tzinfo=timezone.utc)}
    budget = AlertBudget(now=lambda: clock["t"])
    budget.evaluate(cluster)
    clock["t"] += timedelta(hours=1, minutes=5)

    conn = journal_connect(":memory:")
    alerter = _FakeAlerter()
    runner_mod.send_medium_digest_if_due(budget, alerter, conn, clock["t"])  # no fanout fn — must not raise

    assert len(alerter.sent) == 1


# ---------------------------------------------------------------------- #
# run_broad_scan — Stage 1 orchestration (universe -> bulk fetch -> cheap
# screen -> promoted symbols). fetch_bars_fn is injected so this never
# touches the real Alpaca adapter.
# ---------------------------------------------------------------------- #


def test_run_broad_scan_promotes_only_symbols_that_screen_as_unusual():
    from tradebot import universe as universe_mod
    from tradebot.broad_scan import RVOL_THRESHOLD
    from tradebot.marketdata import AssetInfo

    universe_conn = universe_mod.connect(":memory:")

    def fake_asset(symbol):
        return AssetInfo(symbol=symbol, exchange="NASDAQ", name=symbol, tradable=True, options_enabled=True, overnight_eligible=None, attributes=())

    universe_mod.refresh_universe(
        universe_conn, lambda: [fake_asset("QUIET"), fake_asset("LOUD")], datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    def fake_bars(symbols, lookback_days):
        assert set(symbols) == {"QUIET", "LOUD"}
        quiet_bars = [Bar("QUIET", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 8)]
        loud_bars = [Bar("LOUD", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 7)]
        loud_bars.append(Bar("LOUD", datetime(2026, 8, 7, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, int(RVOL_THRESHOLD * 1000)))
        return {"QUIET": quiet_bars, "LOUD": loud_bars}

    promoted = runner_mod.run_broad_scan(universe_conn, fetch_bars_fn=fake_bars)

    assert promoted == ["LOUD"]


def test_run_broad_scan_respects_the_promotion_limit():
    from tradebot import universe as universe_mod
    from tradebot.broad_scan import RVOL_THRESHOLD
    from tradebot.marketdata import AssetInfo

    universe_conn = universe_mod.connect(":memory:")
    symbols = [f"SYM{i}" for i in range(5)]
    universe_mod.refresh_universe(
        universe_conn,
        lambda: [AssetInfo(s, "NASDAQ", s, True, True, None, ()) for s in symbols],
        datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    def fake_bars(fetched_symbols, lookback_days):
        out = {}
        for i, s in enumerate(fetched_symbols):
            bars = [Bar(s, datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 7)]
            bars.append(Bar(s, datetime(2026, 8, 7, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, int(RVOL_THRESHOLD * 1000 * (i + 1))))
            out[s] = bars
        return out

    promoted = runner_mod.run_broad_scan(universe_conn, fetch_bars_fn=fake_bars, promotion_limit=2)
    assert len(promoted) == 2
    assert promoted == ["SYM4", "SYM3"]  # strongest first


def _write_intraday_csv(cache_dir: Path, symbol: str, session: date, closes: list[float]) -> None:
    path = cache_dir / symbol / f"intraday_{session.isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rth_open = datetime(session.year, session.month, session.day, 13, 30, tzinfo=timezone.utc)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for i, close in enumerate(closes):
            ts = rth_open + timedelta(minutes=5 * i)
            writer.writerow({"ts": ts.isoformat(), "open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000})


def test_full_session_rth_bars_reads_from_the_given_cache_dir_not_the_default(tmp_path):
    """docs/sip-migration-proposal.md's Phase 1 needs run_replay's --cache-dir
    override to actually redirect where historical bars come from, not just
    accept the argument -- two separate cache dirs with different bar data
    must produce different results."""
    symbol = "TEST"
    session = date(2026, 6, 15)

    cache_a = tmp_path / "cache-a"
    cache_b = tmp_path / "cache-b"
    _write_intraday_csv(cache_a, symbol, session, [100.0, 101.0])
    _write_intraday_csv(cache_b, symbol, session, [200.0, 201.0, 202.0])

    bars_a = full_session_rth_bars(symbol, session, cache_a)
    bars_b = full_session_rth_bars(symbol, session, cache_b)

    assert [b.close for b in bars_a] == [100.0, 101.0]
    assert [b.close for b in bars_b] == [200.0, 201.0, 202.0]


def test_full_session_rth_bars_defaults_to_the_module_cache_dir():
    """No cache_dir passed -- signature default is runner.CACHE_DIR itself,
    not None, so a caller that forgets the argument still gets the real
    live cache, never a silent empty/broken default."""
    import inspect

    default = inspect.signature(full_session_rth_bars).parameters["cache_dir"].default
    assert default == runner_mod.CACHE_DIR
