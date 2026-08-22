"""Tests for the pure/testable pieces of tradebot.runner.

The full run_replay()/run_live() loops are exercised via an actual
--replay-date run (see the session transcript), not unit tests — they're
integration-shaped (calendars, journaling, alerting all wired together).
These tests cover the pieces that are meaningfully testable in isolation.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.alerts import AlertBudget, ConsoleAlerter, Decision
from tradebot.detectors import DailyAnchors, Detection, bar_close_ts
from tradebot.events import add_event_window
from tradebot.marketdata import Bar, Quote, ReplayMarketData, write_bars_csv
from tradebot.journal import backfill_marks
from tradebot.journal import connect as journal_connect
from tradebot.journal import write_cluster
from tradebot.telegram_bot import outbox
import tradebot.runner as runner_mod
from tradebot.detectors import atr as compute_atr
from tradebot.runner import (
    HeartbeatStats,
    _alert_if_backfill_implausible,
    _alert_if_cache_fetch_failed,
    _build_history_by_symbol,
    bar_gap_minutes,
    evaluate_bar,
    expected_rth_bar_count,
    full_session_rth_bars,
    is_bar_gap,
    is_halted_bar,
    is_stale,
    latest_required_bar_close,
    only_closed_bars,
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


def test_only_closed_bars_excludes_a_bar_whose_close_is_still_in_the_future():
    """The production defect this guards against: a still-forming bar's
    bar_close_ts is in the future, so is_stale() alone never catches it
    (now - future_close is negative, trivially "not stale") — this is
    the guard that actually drops it before anything downstream sees
    it."""
    ts = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)  # closes 13:35
    forming_bar = Bar("XPON", ts, 10, 10.5, 9.5, 10.2, 500)
    now = datetime(2026, 8, 19, 13, 32, 31, tzinfo=timezone.utc)  # queued 13:32:31Z, per the production evidence
    assert only_closed_bars([forming_bar], now) == []


def test_only_closed_bars_includes_a_bar_exactly_at_its_close():
    ts = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    bar = Bar("XPON", ts, 10, 10.5, 9.5, 10.2, 500)
    now = datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)  # exactly bar_close_ts(bar)
    assert only_closed_bars([bar], now) == [bar]
    just_after = now + timedelta(seconds=1)
    assert only_closed_bars([bar], just_after) == [bar]


def test_only_closed_bars_keeps_completed_bars_alongside_a_dropped_incomplete_one():
    """A completed bar earlier in the list must never be discarded just
    because a later, still-forming one was also returned in the same
    session_bars() response."""
    t0 = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    closed1 = Bar("XPON", t0, 10, 10.2, 9.9, 10.1, 400)
    closed2 = Bar("XPON", t0 + timedelta(minutes=5), 10.1, 10.3, 10.0, 10.2, 450)
    forming = Bar("XPON", t0 + timedelta(minutes=10), 10.2, 10.4, 10.1, 10.3, 100)
    now = t0 + timedelta(minutes=12)  # closed1/closed2 have closed; forming closes at t0+15
    assert only_closed_bars([closed1, closed2, forming], now) == [closed1, closed2]


def test_only_closed_bars_is_stable_when_no_new_bar_has_closed():
    """Same filtered result across two poll ticks with no newly-closed
    bar in between -- this is exactly what run_live()'s existing
    len(rth_bars) == rth_bar_count[symbol] dedup check depends on to
    avoid reprocessing the same bar twice."""
    t0 = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    closed = Bar("XPON", t0, 10, 10.2, 9.9, 10.1, 400)
    forming = Bar("XPON", t0 + timedelta(minutes=5), 10.1, 10.3, 10.0, 10.2, 100)
    bars = [closed, forming]
    tick1 = t0 + timedelta(minutes=6)
    tick2 = t0 + timedelta(minutes=7)  # still before forming's own close at t0+10
    result1 = only_closed_bars(bars, tick1)
    result2 = only_closed_bars(bars, tick2)
    assert result1 == result2 == [closed]


def test_only_closed_bars_keeps_every_newly_completed_bar_after_a_delayed_loop():
    """If the loop is delayed and multiple bars close between polls,
    every genuinely completed one must survive -- this guard only ever
    drops a bar that hasn't closed yet, never a real one, regardless of
    how many closed in the gap."""
    t0 = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    bars = [Bar("XPON", t0 + timedelta(minutes=5 * i), 10, 10.2, 9.9, 10.1, 400) for i in range(4)]
    # bars close at 13:35, 13:40, 13:45, 13:50 -- a delayed loop tick at 13:47
    # means the first three have closed and the fourth hasn't.
    now = datetime(2026, 8, 19, 13, 47, tzinfo=timezone.utc)
    assert only_closed_bars(bars, now) == bars[:3]


def test_only_closed_bars_combined_with_is_stale_still_detects_a_genuinely_stale_bar():
    """is_stale() and only_closed_bars() as standalone primitives: a bar
    closed 91s before `now`, with nothing newer in the list, does read
    as stale under is_stale()'s own (unmodified) now-vs-bar-close
    semantics.

    NOTE: this is NOT the production call pattern. run_live() does not
    call is_stale(bar_close_ts(rth_bars[-1]), evaluation_time) directly
    -- that reads as stale for most of every healthy candle. Production
    calls latest_required_bar_close() to find the boundary that has
    actually earned a complaint (more than STALENESS_SECONDS past its
    OWN nominal close, not past whatever the caller's "now" happens to
    be), and compares the actual bar directly against that. This test
    just documents that is_stale() itself, unmodified, still does what
    it always did when handed a genuine staleness gap."""
    t0 = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    stale_bar = Bar("XPON", t0, 10, 10.2, 9.9, 10.1, 400)  # closes 13:35
    now = t0 + timedelta(minutes=5, seconds=91)  # 91s past its close, > STALENESS_SECONDS (90)
    closed = only_closed_bars([stale_bar], now)
    assert closed == [stale_bar]
    assert is_stale(bar_close_ts(closed[-1]), now, max_seconds=90) is True


def test_latest_required_bar_close_is_none_before_any_bar_can_be_overdue():
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    now = session_open + timedelta(minutes=1)  # first bar hasn't even closed yet
    assert latest_required_bar_close(session_open, now, grace_seconds=90) is None


def test_latest_required_bar_close_requires_only_a_bar_more_than_grace_seconds_overdue():
    """PR #64 review's second, release-blocking finding, exactly
    reproduced: bar boundaries are 300s apart; comparing the actual bar
    against "what boundary exists right now" (no grace) makes a bar
    missing by even 1 second look 300s stale. latest_required_bar_close
    must only demand a boundary once it is itself more than
    grace_seconds past ITS OWN nominal close -- not "the current
    boundary, zero tolerance"."""
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    # Bars close 13:30, 13:35, 13:40, ... . At 13:35:30 the 13:35 bar is
    # only 30s late -- well within the 90s grace -- so nothing past
    # 13:30 should be required yet.
    now = datetime(2026, 8, 19, 13, 35, 30, tzinfo=timezone.utc)
    assert latest_required_bar_close(session_open, now, grace_seconds=90) == datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)


def test_latest_required_bar_close_respects_the_strict_greater_than_grace_boundary():
    """Same scenario, walked right up to and past the exact 90s grace
    boundary (13:35:00 + 90s = 13:36:30) -- matching is_stale()'s own
    strict `>` discipline: exactly AT the grace threshold is not yet
    overdue, only strictly past it is."""
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    boundary_13_35 = datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)

    just_before = datetime(2026, 8, 19, 13, 36, 29, tzinfo=timezone.utc)
    assert latest_required_bar_close(session_open, just_before, grace_seconds=90) == datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)

    exactly_at_grace = datetime(2026, 8, 19, 13, 36, 30, tzinfo=timezone.utc)  # 13:35 + 90s exactly
    assert latest_required_bar_close(session_open, exactly_at_grace, grace_seconds=90) == datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)

    just_after = datetime(2026, 8, 19, 13, 36, 31, tzinfo=timezone.utc)
    assert latest_required_bar_close(session_open, just_after, grace_seconds=90) == boundary_13_35


def test_healthy_mid_candle_data_is_not_flagged_stale():
    """PR #64 review's FIRST release-blocking finding, reproduced as a
    regression test: the exact production XPON timing. Filtering out
    the forming bar leaves a completed bar that closed ~151s ago -- more
    than STALENESS_SECONDS (90) by pure wall-clock distance, but this is
    completely normal mid-candle behavior, not delayed data."""
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    completed_bar = Bar("XPON", datetime(2026, 8, 19, 13, 25, tzinfo=timezone.utc), 10, 10.2, 9.9, 10.1, 400)  # closes 13:30
    forming_bar = Bar("XPON", datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc), 10.1, 10.3, 10.0, 10.2, 100)  # closes 13:35
    now = datetime(2026, 8, 19, 13, 32, 31, tzinfo=timezone.utc)  # the real production timing

    closed = only_closed_bars([completed_bar, forming_bar], now)
    assert closed == [completed_bar]

    required = latest_required_bar_close(session_open, now, grace_seconds=90)
    assert required == datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    assert not (bar_close_ts(closed[-1]) < required)  # NOT stale


def test_healthy_mid_candle_still_not_stale_one_poll_later():
    """Same healthy polling phase, one bar later -- the phase-lock the
    original bug (and the second, grace-window bug) would each have
    kept repeating every cycle must not recur under the corrected
    comparison."""
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    completed_bar = Bar("XPON", datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc), 10, 10.2, 9.9, 10.1, 400)  # closes 13:35
    forming_bar = Bar("XPON", datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc), 10.1, 10.3, 10.0, 10.2, 100)  # closes 13:40
    now = datetime(2026, 8, 19, 13, 37, 31, tzinfo=timezone.utc)

    closed = only_closed_bars([completed_bar, forming_bar], now)
    assert closed == [completed_bar]

    required = latest_required_bar_close(session_open, now, grace_seconds=90)
    assert required == datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)
    assert not (bar_close_ts(closed[-1]) < required)  # NOT stale


def test_if_the_expected_bar_is_actually_present_it_is_never_stale_regardless_of_timing():
    """Presence overrides timing entirely -- at any of the timings from
    the grace-boundary tests above, a bar that IS available for the
    boundary in question must never be flagged stale."""
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    bar_13_30 = Bar("XPON", datetime(2026, 8, 19, 13, 25, tzinfo=timezone.utc), 10, 10.2, 9.9, 10.1, 400)  # closes 13:30
    bar_13_35 = Bar("XPON", datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc), 10.1, 10.3, 10.0, 10.2, 400)  # closes 13:35

    for now in (
        datetime(2026, 8, 19, 13, 35, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 19, 13, 36, 29, tzinfo=timezone.utc),
        datetime(2026, 8, 19, 13, 36, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 19, 13, 36, 31, tzinfo=timezone.utc),
    ):
        closed = only_closed_bars([bar_13_30, bar_13_35], now)
        required = latest_required_bar_close(session_open, now, grace_seconds=90)
        assert required is not None
        assert not (bar_close_ts(closed[-1]) < required), f"falsely stale at {now}"


def test_genuine_missing_bar_still_triggers_staleness():
    """The corrected comparison must still catch real delayed/missing
    data -- fixing the healthy-candle false positives must not turn
    into disabling staleness protection entirely."""
    session_open = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    # Feed is stuck: the only bar available closed at 13:15, well behind
    # where data should be by 13:32:31 (required boundary 13:30, per the
    # 90s-grace rule -- the 13:30 bar itself has been available for over
    # two minutes, comfortably past its own grace window).
    stuck_bar = Bar("XPON", datetime(2026, 8, 19, 13, 10, tzinfo=timezone.utc), 10, 10.2, 9.9, 10.1, 400)  # closes 13:15
    now = datetime(2026, 8, 19, 13, 32, 31, tzinfo=timezone.utc)

    closed = only_closed_bars([stuck_bar], now)
    assert closed == [stuck_bar]

    required = latest_required_bar_close(session_open, now, grace_seconds=90)
    assert required == datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    assert bar_close_ts(closed[-1]) < required  # 13:15 < 13:30 -- genuinely stale


def test_latest_required_bar_close_never_exceeds_a_clamped_session_close():
    """The session-close-boundary finding: a sufficiently delayed loop
    iteration crossing an early or normal close must never compute a
    required boundary past when a bar could actually exist. Clamping to
    session_close (session_bounds()'s own close_ts) rather than a
    hardcoded time keeps this correct on an early-close day too."""
    session_open = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
    early_close = datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc)  # 13:00 ET early close
    way_past_close = datetime(2026, 8, 19, 17, 30, tzinfo=timezone.utc)  # a badly delayed iteration

    unclamped = latest_required_bar_close(session_open, way_past_close, grace_seconds=90)
    assert unclamped is not None and unclamped > early_close  # would demand a bar that could never exist

    clamped = latest_required_bar_close(session_open, way_past_close, grace_seconds=90, session_close=early_close)
    assert clamped is not None and clamped <= early_close


def test_final_session_bar_not_yet_required_exactly_at_close_plus_grace():
    """PR #64 review round 3's release-blocking finding, reproduced: the
    OLD ordering (clamp `now` to session_close, THEN subtract grace)
    stuck the required boundary at session_close - one bar width
    forever, since it subtracted grace from an already-capped value --
    the final bar could never earn its own grace window no matter how
    long it stayed missing. Grace must apply to the real, unclamped
    `now`; only the resulting boundary gets capped."""
    session_open = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
    session_close = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)  # 16:00 ET
    now = session_close + timedelta(seconds=90)  # exactly grace past close -- not yet required (strict >)
    required = latest_required_bar_close(session_open, now, grace_seconds=90, session_close=session_close)
    assert required == session_close - timedelta(minutes=5)  # 15:55 -- the final 16:00 bar is NOT yet required


def test_final_session_bar_required_just_past_close_plus_grace():
    session_open = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    session_close = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)
    now = session_close + timedelta(seconds=91)  # one second past strict grace
    required = latest_required_bar_close(session_open, now, grace_seconds=90, session_close=session_close)
    assert required == session_close  # the final bar is now genuinely required


def test_required_boundary_stays_capped_at_session_close_far_after_close():
    session_open = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    session_close = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)
    now = session_close + timedelta(hours=1)  # a badly delayed iteration, well past close
    required = latest_required_bar_close(session_open, now, grace_seconds=90, session_close=session_close)
    assert required == session_close  # capped, never later than the real close


def test_required_boundary_respects_a_real_early_close():
    session_open = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    early_close = datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc)  # 13:00 ET early close
    now = early_close + timedelta(hours=1)
    required = latest_required_bar_close(session_open, now, grace_seconds=90, session_close=early_close)
    assert required == early_close


def test_only_closed_bars_excludes_a_bar_that_closed_during_the_fetch_window():
    """PR #64 review round 3's other release-blocking finding: a request
    can straddle a bar boundary (started 13:34:59.8, before the 13:35
    close; the response returned 13:35:00.15, after it). bar_close_ts(bar)
    <= a POST-fetch timestamp only proves the wall clock had passed the
    boundary by the time we looked, not that the specific response held
    was assembled after it. Eligibility must use the PRE-fetch instant --
    the conservative choice, deferring an ambiguous bar to the next
    poll rather than trusting a response that might have been
    mid-formation when Alpaca built it."""
    bar = Bar("XPON", datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc), 10, 10.2, 9.9, 10.1, 400)  # closes 13:35:00
    pre_fetch_time = datetime(2026, 8, 19, 13, 34, 59, 800000, tzinfo=timezone.utc)  # before the request was sent
    post_fetch_time = datetime(2026, 8, 19, 13, 35, 0, 150000, tzinfo=timezone.utc)  # after the response returned

    assert only_closed_bars([bar], pre_fetch_time) == []  # what run_live() now does: correctly deferred
    # The rejected alternative (judging by a post-fetch timestamp) would
    # have wrongly admitted it -- exactly the bug this guards against.
    assert only_closed_bars([bar], post_fetch_time) == [bar]


def test_only_closed_bars_uses_a_fresh_per_symbol_timestamp_not_a_stale_outer_one():
    """The second review finding: closed-bar eligibility must use the
    actual per-symbol evaluation time, not an outer-loop timestamp
    captured before broad-scan work and earlier symbols in scan_symbols
    were processed. A bar that closed in that gap is legitimately
    available and must not be postponed to the next poll just because a
    stale outer timestamp was used to judge it."""
    bar = Bar("XPON", datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc), 10, 10.2, 9.9, 10.1, 400)  # closes 13:35:00
    stale_outer_loop_start = datetime(2026, 8, 19, 13, 34, 50, tzinfo=timezone.utc)  # captured before the bar closed
    fresh_evaluation_time = datetime(2026, 8, 19, 13, 35, 15, tzinfo=timezone.utc)  # this symbol's actual fetch moment

    assert only_closed_bars([bar], stale_outer_loop_start) == []  # would incorrectly postpone a real bar
    assert only_closed_bars([bar], fresh_evaluation_time) == [bar]  # correctly available


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


def test_expected_rth_bar_count_regular_day():
    assert expected_rth_bar_count(date(2026, 7, 23)) == 78  # 6.5 hours of 5-min bars


def test_expected_rth_bar_count_honors_early_close():
    # day after Thanksgiving 2026 -- a known 13:00 ET early close (3.5h)
    assert expected_rth_bar_count(date(2026, 11, 27)) == 42


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


def _plausible_session_bars(symbol: str, session_date: date, *, n: int = 78, volume: int = 1000) -> list[Bar]:
    """A full regular-day's worth of RTH bars (78 == 6.5 hours of 5-min
    bars) starting at 09:30 ET -- passes Proposal 5c's plausibility floor
    at both checks, so tests of _cache_todays_intraday_bars that aren't
    themselves about the floor don't trip it with an unrealistically
    tiny fake fetch."""
    rth_open = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=13, minutes=30)
    return [
        Bar(symbol=symbol, ts=rth_open + timedelta(minutes=5 * i), open=100, high=100.5, low=99.5, close=100.1, volume=volume)
        for i in range(n)
    ]


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


def test_cache_todays_intraday_bars_writes_a_readable_file_per_symbol(tmp_path, caplog):
    """The actual structural fix: a symbol with a real fetch result gets
    a real, readable cache file at the exact path backfill_marks() looks
    for."""
    session = date(2026, 8, 12)
    bars = _plausible_session_bars("TSLA", session)

    def fake_fetch(symbol, d):
        assert d == session
        return bars

    with caplog.at_level("ERROR"):
        succeeded, failed = runner_mod._cache_todays_intraday_bars(tmp_path, ["TSLA"], session, fetch_fn=fake_fetch)

    assert succeeded == ["TSLA"]
    assert failed == []
    assert caplog.records == []
    written_path = tmp_path / "TSLA" / "intraday_2026-08-12.csv"
    assert written_path.exists()
    md = ReplayMarketData(tmp_path, "TSLA", session)
    while md.advance():
        pass
    assert len(list(md.session_bars("TSLA", session))) == len(bars)


def test_cache_todays_intraday_bars_one_symbols_exception_does_not_block_the_rest(caplog, tmp_path):
    aapl_bars = _plausible_session_bars("AAPL", date(2026, 8, 12))

    def flaky_fetch(symbol, d):
        if symbol == "TSLA":
            raise RuntimeError("vendor is down")
        return aapl_bars

    with caplog.at_level("ERROR"):
        succeeded, failed = runner_mod._cache_todays_intraday_bars(tmp_path, ["TSLA", "AAPL"], date(2026, 8, 12), fetch_fn=flaky_fetch)

    assert succeeded == ["AAPL"]
    assert failed == ["TSLA"]
    assert len(caplog.records) == 1 and caplog.records[0].levelname == "ERROR"


def test_cache_todays_intraday_bars_empty_result_is_always_a_failure_no_holiday_ambiguity(caplog, tmp_path):
    """Unlike scripts/fetch_cache.py's historical walk-back (which must
    tell a real holiday from a real failure), every symbol passed here
    is known to have fired a real detection today -- there is no
    legitimate empty-result explanation, so this never needs a calendar
    check the way fetch_cache.py's ensure_sessions() does."""
    with caplog.at_level("ERROR"):
        succeeded, failed = runner_mod._cache_todays_intraday_bars(tmp_path, ["TSLA"], date(2026, 8, 12), fetch_fn=lambda s, d: [])

    assert succeeded == []
    assert failed == ["TSLA"]
    assert len(caplog.records) == 1


def test_cache_todays_intraday_bars_rejects_a_runt_fetch_below_the_plausibility_floor(caplog, tmp_path, monkeypatch):
    """Proposal 5c, close-time-write half: the exact shape of the
    2026-08-11/12 incident -- a fetch that returns real bars, but far too
    few of them for a regular trading day -- must be rejected rather than
    silently cached as if it were a normal session."""
    from tradebot import metrics as metrics_mod

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_path)

    session = date(2026, 8, 12)
    runt_bars = _plausible_session_bars("TSLA", session, n=5)  # 5 of 78 expected

    with caplog.at_level("ERROR"):
        succeeded, failed = runner_mod._cache_todays_intraday_bars(
            tmp_path, ["TSLA"], session, fetch_fn=lambda s, d: runt_bars,
        )

    assert succeeded == []
    assert failed == ["TSLA"]
    assert any("plausibility floor rejected" in r.message for r in caplog.records)
    assert not (tmp_path / "TSLA" / "intraday_2026-08-12.csv").exists()
    assert metrics_mod.read_all(metrics_path) == {
        "plausibility_floor_rejection{rule=implausible_bar_count,stage=close_write,symbol=TSLA}": 1,
    }


def test_cache_todays_intraday_bars_rejects_below_the_volume_floor_against_cached_history(tmp_path, caplog):
    """Same floor, the other rule: a full bar count on a fraction of the
    symbol's normal volume (the actual 2026-08-11/12 shape -- ~1M vs
    ~40M SPY volume) must also be rejected, judged against real cached
    history."""
    session = date(2026, 8, 12)
    for i in range(3):
        write_bars_csv(
            tmp_path / "TSLA" / f"intraday_2026-08-{9 + i:02d}.csv",
            _plausible_session_bars("TSLA", date(2026, 8, 9 + i), volume=1_000_000),
        )
    thin_bars = _plausible_session_bars("TSLA", session, volume=1000)  # << 20% of the 1M median

    with caplog.at_level("ERROR"):
        succeeded, failed = runner_mod._cache_todays_intraday_bars(
            tmp_path, ["TSLA"], session, fetch_fn=lambda s, d: thin_bars,
        )

    assert succeeded == []
    assert failed == ["TSLA"]
    assert any("implausible_volume" in r.message for r in caplog.records)


def test_build_history_by_symbol_excludes_a_runt_session_and_reports_it(tmp_path, caplog):
    """Proposal 5c, baseline-building half: a runt cached session must
    never join the rvol baseline (or a future TR profile) -- it's
    dropped from the accepted history, logged loudly, and surfaced on
    the heartbeat's data_gaps line, never silent."""
    from tradebot import metrics as metrics_mod

    healthy_dates = [date(2026, 8, d) for d in (3, 4, 5, 6, 7)]
    for d in healthy_dates:
        write_bars_csv(tmp_path / "TSLA" / f"intraday_{d.isoformat()}.csv", _plausible_session_bars("TSLA", d))
    runt_date = date(2026, 8, 10)
    write_bars_csv(tmp_path / "TSLA" / f"intraday_{runt_date.isoformat()}.csv", _plausible_session_bars("TSLA", runt_date, n=5))

    stats = HeartbeatStats(start_time=datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc), session_date=date(2026, 8, 11))

    with caplog.at_level("ERROR"):
        history_by_symbol = _build_history_by_symbol(tmp_path, ["TSLA"], date(2026, 8, 11), stats)

    assert len(history_by_symbol["TSLA"]) == len(healthy_dates)  # the runt never joined
    assert any("plausibility floor rejected" in r.message and "TSLA" in r.message for r in caplog.records)
    assert any(runt_date.isoformat() in gap for gap in stats.data_gaps)


def test_alert_if_cache_fetch_failed_pages_on_total_failure():
    """0 of N succeeded -- the systemic vendor/auth outage shape this
    alert exists for."""
    alerter = _SpyAlerter()

    _alert_if_cache_fetch_failed(alerter, [], ["TSLA", "QQQ"], date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert len(alerter.sent) == 1
    text, priority = alerter.sent[0]
    assert "ALL 2" in text
    assert "TSLA" in text and "QQQ" in text
    assert priority == outbox.PRIORITY_HIGH


def test_alert_if_cache_fetch_failed_logs_but_does_not_page_on_a_partial_failure(caplog):
    """A single ticker's vendor hiccup (or any partial miss) must never
    page -- confirmed explicitly, not assumed: this was the actual bug
    caught in PR #24 review, where the pre-fix code paged on ANY failed
    symbol. Still logged at ERROR, just not escalated to Telegram."""
    alerter = _SpyAlerter()

    with caplog.at_level("ERROR"):
        _alert_if_cache_fetch_failed(alerter, ["AAPL"], ["TSLA"], date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert alerter.sent == []  # no page
    assert len(caplog.records) == 1 and caplog.records[0].levelname == "ERROR"  # still logged
    assert "TSLA" in caplog.records[0].message


def test_alert_if_cache_fetch_failed_stays_quiet_when_nothing_failed():
    alerter = _SpyAlerter()

    _alert_if_cache_fetch_failed(alerter, ["AAPL", "TSLA"], [], date(2026, 8, 12), datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc))

    assert alerter.sent == []


def test_alert_if_cache_fetch_failed_and_alert_if_backfill_implausible_are_separate_signals():
    """The whole point of splitting these into two alerts: an operator
    must be able to tell "the fetch stage broke" from "backfill found
    nothing" without reading logs -- two different Telegram messages,
    not one generic one."""
    alerter = _SpyAlerter()
    when = datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc)

    _alert_if_cache_fetch_failed(alerter, [], ["TSLA"], date(2026, 8, 12), when)
    stats = HeartbeatStats(start_time=when, session_date=date(2026, 8, 12))
    stats.record_cluster("high", Decision.SEND)
    _alert_if_backfill_implausible(alerter, stats, 0, date(2026, 8, 12), when)

    assert len(alerter.sent) == 2
    assert "cache fetch failed" in alerter.sent[0][0].lower()
    assert "backfill_marks wrote only" in alerter.sent[1][0]


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


def test_backfill_marks_absent_cache_actually_reaches_the_alert_end_to_end(tmp_path, caplog):
    """The 2026-08-12 incident shape, run through the REAL pipeline, not
    two separate assertions on each half of it: a real detection is
    journaled, its session's intraday cache file never existed, the real
    backfill_marks() return value is threaded into the real
    _alert_if_backfill_implausible() -- same as runner.py's own call
    site does, unmodified -- and both the journal-layer log line AND the
    Telegram alert must fire from that single real 0. Neither
    test_backfill_marks_logs_an_error_when_the_intraday_cache_file_is_absent
    (journal.py only) nor test_alert_if_backfill_implausible_fires_on_
    zero_marks_with_real_detections (hardcodes marks_written=0) proves
    this connection on its own."""
    cache_dir = tmp_path / "cache"
    session = date(2026, 8, 12)
    rth_open = datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc)
    # daily.csv present (needed elsewhere in the real pipeline); the
    # session's own intraday file is deliberately never written.
    (cache_dir / "TSLA").mkdir(parents=True)
    (cache_dir / "TSLA" / "daily.csv").write_text("ts,open,high,low,close,volume\n")

    conn = journal_connect(tmp_path / "journal.db")
    write_cluster(
        conn, session=session.isoformat(), symbol="TSLA", ts_utc=rth_open.isoformat(),
        kinds="gap", headlines="gapped up", score=2.0, close=100.0, atr14=1.0,
        trend="up", detections=[Detection("TSLA", "gap", rth_open, 2.0, "gapped up", {})],
        code_version_str="abc123",
    )
    conn.commit()

    stats = HeartbeatStats(start_time=rth_open, session_date=session)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)
    alerter = _SpyAlerter()
    end_time = datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc)

    with caplog.at_level("ERROR"):
        marks_written = backfill_marks(conn, session, cache_dir=cache_dir)  # the real function, real return value
        _alert_if_backfill_implausible(alerter, stats, marks_written, session, end_time)  # same call shape as runner.py:1164-1165

    assert marks_written == 0
    assert len(alerter.sent) == 1  # the Telegram alert -- the thing point 4 claimed was tested
    assert any(r.name == "watchtower.journal" for r in caplog.records)  # the cache-missing log
    assert any(r.name == "watchtower.runner" for r in caplog.records)  # the implausible-count log


def test_close_time_cache_fetch_prevents_the_2026_08_12_incident_end_to_end(tmp_path, caplog):
    """The structural fix, run through the same real pipeline as the
    detection test above, with the one thing that test deliberately
    lacked: the close-time cache fetch runner.py now does BEFORE
    backfill_marks(), same call order as run_live(). Same starting
    conditions (a real detection, no intraday cache file), opposite
    outcome -- real marks get written and NEITHER alert fires, proving
    this actually PREVENTS the incident rather than just detecting it
    faster."""
    cache_dir = tmp_path / "cache"
    session = date(2026, 8, 12)
    rth_open = datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc)
    (cache_dir / "TSLA").mkdir(parents=True)
    (cache_dir / "TSLA" / "daily.csv").write_text("ts,open,high,low,close,volume\n")

    conn = journal_connect(tmp_path / "journal.db")
    write_cluster(
        conn, session=session.isoformat(), symbol="TSLA", ts_utc=rth_open.isoformat(),
        kinds="gap", headlines="gapped up", score=2.0, close=100.0, atr14=1.0,
        trend="up", detections=[Detection("TSLA", "gap", rth_open, 2.0, "gapped up", {})],
        code_version_str="abc123",
    )
    conn.commit()

    stats = HeartbeatStats(start_time=rth_open, session_date=session)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)
    alerter = _SpyAlerter()
    end_time = datetime(2026, 8, 12, 20, 5, tzinfo=timezone.utc)

    def fake_fetch(symbol, d):
        return _plausible_session_bars(symbol, d)

    with caplog.at_level("ERROR"):
        # Same order as run_live(): fetch+cache today's bars, THEN backfill.
        todays_symbols = runner_mod.detected_symbols_for_session(conn, session)
        succeeded, failed = runner_mod._cache_todays_intraday_bars(cache_dir, todays_symbols, session, fetch_fn=fake_fetch)
        _alert_if_cache_fetch_failed(alerter, succeeded, failed, session, end_time)
        marks_written = backfill_marks(conn, session, cache_dir=cache_dir)
        _alert_if_backfill_implausible(alerter, stats, marks_written, session, end_time)

    assert todays_symbols == ["TSLA"]
    assert failed == []
    assert marks_written > 0  # the incident's own number was 0 -- this is the fix
    assert alerter.sent == []  # neither alert fires -- nothing was wrong to report
    assert caplog.records == []  # not even a log line -- this is prevention, not faster detection


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


def test_evaluate_bar_reports_pct_from_prior_close_available():
    """A1 (docs/open-awareness-proposals-2026-08.md): evaluate_bar's real
    (non-mocked) output carries the signed percentage-point displacement
    from anchors.prior_close, computed by the shared
    tradebot.features.pct_from_prior_close primitive."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc), 110.0, 111.0, 109.0, 110.0, volume=10_000)

    result = evaluate_bar("TSLA", [bar], anchors)

    assert result is not None
    assert result["pct_from_prior_close"] == 10.0  # (110-100)/100 * 100
    assert result["pct_from_prior_close_status"] == "AVAILABLE"


def test_evaluate_bar_reports_pct_from_prior_close_unavailable_on_bad_prior_close():
    """A non-positive prior_close (a degenerate/bad daily bar) must never
    silently divide -- explicit UNAVAILABLE, journaled as such."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=0.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc), 110.0, 111.0, 109.0, 110.0, volume=10_000)

    result = evaluate_bar("TSLA", [bar], anchors)

    assert result is not None
    assert result["pct_from_prior_close"] is None
    assert result["pct_from_prior_close_status"] == "UNAVAILABLE:invalid_prior_close"


def test_process_new_bar_journals_pct_from_prior_close_from_a_real_evaluate_bar_call(monkeypatch):
    """End-to-end: process_new_bar -> the REAL evaluate_bar (not a
    monkeypatched fixture) -> journal.write_cluster actually threads the
    A1 columns onto the row -- the one thing no lower-level test proves
    on its own."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=112.0, opening_range_low=108.0, opening_range_volume=1000,
        swing_high=101.0, swing_low=99.0, avg_cum_volume_by_bar={},
    )
    # A single bar with a real gap (score = |110-100| / (101-99) = 5.0,
    # well above gap()'s 0.75 atr_units floor) so evaluate_bar actually
    # produces a cluster to journal -- a bar that doesn't trigger any
    # detector never reaches write_cluster at all.
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc), 110.0, 112.0, 109.0, 110.0, volume=10_000)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=109.9, ask=110.1, last=110.0)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)

    row = conn.execute(
        "SELECT pct_from_prior_close, pct_from_prior_close_status FROM detections WHERE symbol = 'TSLA'"
    ).fetchone()
    assert row == (10.0, "AVAILABLE")


def test_process_new_bar_with_a_mocked_evaluate_bar_result_journals_null_pct_from_prior_close(monkeypatch):
    """Existing tests (see _high_tier_fixture) monkeypatch evaluate_bar
    with a hand-built dict that predates the A1 keys -- process_new_bar
    must not KeyError on them, and must journal NULL/NULL rather than
    fabricate a value the mocked evaluate_bar never computed."""
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
    row = conn.execute(
        "SELECT pct_from_prior_close, pct_from_prior_close_status FROM detections WHERE symbol = 'TSLA'"
    ).fetchone()
    assert row == (None, None)


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


# --------------------------------------------------------------------------
# 2026-08-21 stale loop_start / quote-staleness fix: validation_now_fn is
# captured *after* quote_fn(symbol) returns, not at the caller's iteration
# start. See process_new_bar's docstring above.
# --------------------------------------------------------------------------


def test_process_new_bar_rejects_a_quote_stale_relative_to_the_validation_clock(monkeypatch):
    """The exact bug scenario: a quote genuinely older than
    QUOTE_MAX_STALENESS_SECONDS relative to the real evaluation time must
    be rejected -- even though an old iteration-start timestamp captured
    before the quote was fetched could have made it look fresh, or even
    negative-age, under the pre-fix behavior."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    # 120s after the quote's own timestamp -- past the 60s ceiling. A stale
    # loop_start captured *before* the quote fetch (e.g. bar.ts - 5min)
    # would instead have produced a negative age and wrongly passed.
    validation_now_fn = lambda: bar.ts + timedelta(seconds=120)

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        validation_now_fn=validation_now_fn,
    )

    row = conn.execute("SELECT alerted, suppress_reason, suppress_category FROM detections").fetchone()
    assert row[0] == 0
    assert row[1].startswith("data_integrity_failed: stale_quote")
    assert row[2] == "data_integrity"


def test_process_new_bar_accepts_a_quote_fresh_under_the_staleness_threshold(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    validation_now_fn = lambda: bar.ts + timedelta(seconds=30)  # under the 60s ceiling

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        validation_now_fn=validation_now_fn,
    )

    row = conn.execute("SELECT alerted FROM detections").fetchone()
    assert row[0] == 1


def test_process_new_bar_calls_validation_now_fn_only_after_quote_fn_returns(monkeypatch):
    """Controlled fake clock: quote_fn advances the clock as a stand-in for
    the network fetch taking real time; validation_now_fn reads that same
    clock. If validation_now_fn were (incorrectly) invoked before
    quote_fn -- e.g. at the caller's loop-iteration start, the original
    bug -- it would observe the pre-fetch clock value and the quote would
    wrongly pass as fresh. Observing the rejection here proves the
    post-fetch clock value was actually used."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    clock = {"t": bar.ts}  # pre-fetch: age would be 0s if read now (fresh)

    def quote_fn(symbol):
        clock["t"] = bar.ts + timedelta(seconds=200)  # simulated fetch delay, > 60s ceiling
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        validation_now_fn=lambda: clock["t"],
    )

    row = conn.execute("SELECT alerted, suppress_reason FROM detections").fetchone()
    assert row[0] == 0
    assert row[1].startswith("data_integrity_failed: stale_quote")


def test_process_new_bar_validation_now_fn_none_skips_the_staleness_check(monkeypatch):
    """None (the default, and what run_replay() always passes) preserves
    today's behavior exactly: the staleness check is skipped regardless of
    how old the quote's own timestamp is -- a replayed historical quote is
    definitionally "stale" relative to real time and must not be rejected
    on that basis."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        # timestamped days in the past -- would fail staleness by a wide
        # margin if the check ran at all
        return Quote(symbol=symbol, ts=bar.ts - timedelta(days=3), bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
    )  # validation_now_fn omitted entirely, same as every run_replay() call

    row = conn.execute("SELECT alerted FROM detections").fetchone()
    assert row[0] == 1


def test_process_new_bar_tags_and_renders_a_verified_extreme_mover(monkeypatch, tmp_path):
    """End-to-end Proposal 3: a >25% move persisting across two
    consecutive real-volume bars clears guard.py, gets journaled via
    set_extreme_mover, emits the metric, and the rendered card carries
    the EXTREME MOVER prefix -- not just the guard predicate in
    isolation (see test_guard.py for that)."""
    from tradebot import metrics as metrics_mod

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_path)

    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    base = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("TSLA", base, 100.0, 100.5, 99.5, 100.0, volume=10_000),
        Bar("TSLA", base + timedelta(minutes=5), 100.0, 141.0, 139.0, 140.0, volume=50_000),
        Bar("TSLA", base + timedelta(minutes=10), 140.0, 141.0, 137.0, 138.0, volume=60_000),
    ]
    primary_detection = Detection("TSLA", "gap", bars[-1].ts, 10.0, "a gap", {})
    result = {
        "ts": datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), "close": 138.0, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary_detection,
        "score": 10.0, "trend": "up", "detections": [primary_detection],
    }
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, b, anch, market_bars=None: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bars[-1].ts)
    stats = HeartbeatStats(start_time=bars[-1].ts, session_date=date(2026, 7, 23))
    alerter = _SpyAlerter()

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bars[-1].ts, bid=137.9, ask=138.1, last=138.0)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(conn, budget, alerter, "v1", "TSLA", date(2026, 7, 23), bars, anchors, quote_fn, chain_fn, stats)

    detection_id = conn.execute("SELECT id FROM detections").fetchone()[0]
    row = conn.execute(
        "SELECT extreme_mover, extreme_mover_gap_pct, extreme_mover_volume FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    assert row[0] == 1
    assert row[1] == pytest.approx(0.38)
    assert row[2] == 110_000

    assert len(alerter.sent) == 1
    assert "EXTREME MOVER" in alerter.sent[0][0]

    assert metrics_mod.read_all(metrics_path) == {"extreme_mover_verified{symbol=TSLA}": 1}


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


# -------------------------------------------------------------------- #
# CRITICAL #5 -- no alert may reference a not-yet-durable detection
# -------------------------------------------------------------------- #


class _SimulatedCrash(RuntimeError):
    """Stands in for a SIGKILL/OOM/power-loss at one specific line."""


class _DurabilityProbeAlerter:
    """Records, at the exact moment of each send, which detection rows are
    visible from an INDEPENDENT connection to journal.db.

    Why a second connection rather than actually killing the process: an
    uncommitted SQLite transaction is invisible to every other connection,
    and is exactly what a SIGKILL/OOM/power-loss discards. So "visible from
    a separate connection at send time" is precisely "would have survived
    the process dying right here" -- checkable in-process, with nothing to
    kill.

    An exception is NOT a substitute for that check, which is why no test
    below leans on one alone: `raise` unwinds the stack but leaves the
    original connection's transaction and every pending write in it fully
    intact, so a test built only on raising passes just as happily against
    the pre-fix ordering it is meant to catch."""

    def __init__(self, db_path, crash_before_send=False, crash_after_send=False):
        self.db_path = str(db_path)
        self.sends = []
        self.crash_before_send = crash_before_send
        self.crash_after_send = crash_after_send

    def send(self, text, priority=None, alert_id=None):
        if self.crash_before_send:
            raise _SimulatedCrash("died between the commit and the send")
        probe = sqlite3.connect(self.db_path)
        try:
            durable = {row[0] for row in probe.execute("SELECT id FROM detections")}
        finally:
            probe.close()
        self.sends.append({"alert_id": alert_id, "durable_ids": durable})
        if self.crash_after_send:
            raise _SimulatedCrash("died between the send and the final commit")


def _on_disk_high_tier(tmp_path, monkeypatch, max_high_per_day=8):
    """The _high_tier_fixture() cluster, but against a real on-disk
    journal.db -- ":memory:" can't be opened by a second connection, and a
    second connection is the whole point of these tests."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)
    db_path = tmp_path / "journal.db"
    conn = journal_connect(db_path)
    budget = AlertBudget(now=lambda: bar.ts, max_high_per_day=max_high_per_day)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))
    return anchors, bar, result, db_path, conn, budget, stats


def _quote_fn_for(bar):
    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    return quote_fn


def _no_trade_chain_fn(symbol, expiry):
    """NotImplementedError -> bound_chain_fn returns None -> no breakeven
    -> the NO TRADE path, which is the one the bug was live on."""
    raise NotImplementedError


def _run(conn, budget, alerter, bar, anchors, stats, chain_fn=_no_trade_chain_fn):
    process_new_bar(
        conn, budget, alerter, "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _quote_fn_for(bar), chain_fn, stats,
    )


def test_no_trade_alert_is_not_sent_until_its_detection_row_is_durable(tmp_path, monkeypatch):
    """The bug itself. On the NO TRADE path nothing used to commit
    journal.db between the detection INSERT and alerter.send(), so a crash
    in that window delivered a real subscriber alert referencing a
    detection row that then rolled back. Fails against the pre-fix
    ordering; passes now that _commit_then_send() owns the send."""
    anchors, bar, result, db_path, conn, budget, stats = _on_disk_high_tier(tmp_path, monkeypatch)
    alerter = _DurabilityProbeAlerter(db_path)

    _run(conn, budget, alerter, bar, anchors, stats)

    assert conn.execute("SELECT no_trade FROM detections").fetchone()[0] == 1  # really the NO TRADE path
    assert len(alerter.sends) == 1
    sent = alerter.sends[0]
    assert sent["alert_id"] is not None
    assert sent["alert_id"] in sent["durable_ids"]


def test_trade_path_alert_is_also_durable_before_sending(tmp_path, monkeypatch):
    """The trade path was never exposed -- record_contract_selection()
    commits, which flushes the pending detection INSERT along with it. That
    was an accident of an unrelated function's commit, not a guarantee, and
    nothing protected it. This pins it."""
    from tradebot.marketdata import OptionChain, OptionContract

    anchors, bar, result, db_path, conn, budget, stats = _on_disk_high_tier(tmp_path, monkeypatch)
    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    alerter = _DurabilityProbeAlerter(db_path)
    _run(conn, budget, alerter, bar, anchors, stats, chain_fn=chain_fn)

    assert conn.execute("SELECT no_trade FROM detections").fetchone()[0] == 0  # a contract WAS selected
    sent = alerter.sends[0]
    assert sent["alert_id"] in sent["durable_ids"]


def test_cap_reached_notice_also_commits_before_it_sends(tmp_path, monkeypatch):
    """The cap notice carries no detection_id so it cannot dangle, but it
    still goes through _commit_then_send -- every send in process_new_bar
    commits first, so "send with journal writes still pending" isn't a
    shape the function can express."""
    anchors, bar, result, db_path, conn, budget, stats = _on_disk_high_tier(tmp_path, monkeypatch, max_high_per_day=1)
    alerter = _DurabilityProbeAlerter(db_path)

    _run(conn, budget, alerter, bar, anchors, stats)  # 1st: SEND, burns the cap

    # score must clear ESCALATION_SCORE_DELTA over the first cluster's,
    # or dedup suppresses this as a non-escalating repeat and it never
    # reaches the budget (and so never reaches the cap notice) at all.
    second = {**result, "ts": result["ts"] + timedelta(minutes=5), "score": result["score"] + 3.0}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: second)
    _run(conn, budget, alerter, bar, anchors, stats)  # 2nd: CAP_REACHED_NOTICE

    notice = alerter.sends[-1]
    assert notice["alert_id"] is None  # the system notice, not a detection alert
    ids = {row[0] for row in conn.execute("SELECT id FROM detections")}
    assert len(ids) == 2
    assert ids == notice["durable_ids"]  # both detections durable before the notice went out
    for sent in alerter.sends:
        if sent["alert_id"] is not None:
            assert sent["alert_id"] in sent["durable_ids"]


def test_crash_after_the_send_leaves_the_detection_durable_with_alerted_still_zero(tmp_path, monkeypatch):
    """Partial state #1, newly possible after this fix: the alert is out,
    the detection is durable, but the alerted flag never committed. Nothing
    rescans alerted=0 (it is read-only downstream), so there is no resend
    and no duplicate -- the cost is a track record that undercounts itself
    by one, which is the only acceptable direction to be wrong for a
    product whose positioning is an unedited record."""
    anchors, bar, result, db_path, conn, budget, stats = _on_disk_high_tier(tmp_path, monkeypatch)
    alerter = _DurabilityProbeAlerter(db_path, crash_after_send=True)

    with pytest.raises(_SimulatedCrash):
        _run(conn, budget, alerter, bar, anchors, stats)

    sent = alerter.sends[0]
    assert sent["alert_id"] in sent["durable_ids"]

    fresh = sqlite3.connect(db_path)  # what a restarted process sees
    try:
        row = fresh.execute("SELECT id, alerted FROM detections").fetchone()
        assert row is not None
        detection_id, alerted = row
        assert alerted == 0  # undercount, never overcount
        # The alert's inline keyboard resolves ids exactly this way
        # (telegram_bot/handlers.py:_resolve_detection) -- "I took this"
        # still works, which is what the bug used to break.
        resolved = fresh.execute(
            "SELECT id FROM detections WHERE id = ? OR id LIKE ?",
            (detection_id[:8], f"{detection_id[:8]}%"),
        ).fetchone()
        assert resolved == (detection_id,)
    finally:
        fresh.close()


def test_crash_between_the_commit_and_the_send_journals_a_detection_nobody_was_alerted_about(tmp_path, monkeypatch):
    """Partial state #2: committed, then died before the alert went out.
    The row exists with alerted=0, which is exactly what that column means
    -- indistinguishable from any suppressed detection, self-describing,
    and honest. No alert exists to dangle."""
    anchors, bar, result, db_path, conn, budget, stats = _on_disk_high_tier(tmp_path, monkeypatch)
    alerter = _DurabilityProbeAlerter(db_path, crash_before_send=True)

    with pytest.raises(_SimulatedCrash):
        _run(conn, budget, alerter, bar, anchors, stats)

    assert alerter.sends == []  # nothing was ever sent

    fresh = sqlite3.connect(db_path)
    try:
        row = fresh.execute("SELECT alerted FROM detections").fetchone()
        assert row == (0,)
    finally:
        fresh.close()


def test_guard_rejected_path_still_rolls_the_whole_cluster_back_on_a_crash(tmp_path, monkeypatch):
    """Semantics deliberately unchanged where no alert is sent: the guard
    rejection path never reaches _commit_then_send, so a crash before the
    final commit still discards the entire cluster exactly as it did
    before. Proves the fix widened durability only where an alert forced
    it to, not everywhere."""
    anchors, bar, result, db_path, conn, budget, stats = _on_disk_high_tier(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "validate_alert_data", lambda *a, **k: "stale_quote: 900s old")

    def boom(*args, **kwargs):
        raise _SimulatedCrash("died while recording the guard rejection")

    monkeypatch.setattr(runner_mod.metrics, "increment", boom)

    alerter = _DurabilityProbeAlerter(db_path)
    with pytest.raises(_SimulatedCrash):
        _run(conn, budget, alerter, bar, anchors, stats)

    assert alerter.sends == []
    fresh = sqlite3.connect(db_path)
    try:
        assert fresh.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0
    finally:
        fresh.close()


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


def test_run_broad_scan_shadow_counts_satisfy_the_conservation_invariant(caplog):
    """Decision Ledger measurement gate: one symbol in each of the five
    mutually-exclusive Stage-1 outcomes (missing from the vendor
    response, too little history, a real Snapshot with an invalid
    baseline, a real Snapshot that's genuinely quiet, and a real
    candidate), and the shadow-count log line must both classify each
    one correctly and satisfy the two conservation equations:
        requested = missing_from_fetch + insufficient_history + snapshot
        snapshot = invalid_baseline + evaluated_quiet + candidate
    This also doubles as the real-selection check: run_broad_scan()'s
    actual return value must still be exactly the one real candidate,
    unaffected by any of the new counting logic."""
    from tradebot import universe as universe_mod
    from tradebot.broad_scan import RVOL_THRESHOLD
    from tradebot.marketdata import AssetInfo

    universe_conn = universe_mod.connect(":memory:")
    symbols = ["MISSING1", "SHORT1", "INVALID1", "QUIET1", "LOUD1"]
    universe_mod.refresh_universe(
        universe_conn,
        lambda: [AssetInfo(s, "NASDAQ", s, True, True, None, ()) for s in symbols],
        datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    def fake_bars(fetched_symbols, lookback_days):
        assert set(fetched_symbols) == set(symbols)
        out = {}
        # SHORT1: fewer than min_history (6) bars -> INSUFFICIENT_HISTORY.
        out["SHORT1"] = [Bar("SHORT1", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 4)]
        # INVALID1: 6 zero-volume history bars -> avg_volume == 0 -> INVALID_BASELINE.
        invalid_bars = [Bar("INVALID1", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 0) for d in range(1, 7)]
        invalid_bars.append(Bar("INVALID1", datetime(2026, 8, 7, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000))
        out["INVALID1"] = invalid_bars
        # QUIET1: real Snapshot, nothing crosses a Stage-1 threshold -> EVALUATED_AND_QUIET, no row/entry at all.
        out["QUIET1"] = [Bar("QUIET1", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 8)]
        # LOUD1: the one real candidate (rvol spike), same shape as the existing promotion test.
        loud_bars = [Bar("LOUD1", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 7)]
        loud_bars.append(Bar("LOUD1", datetime(2026, 8, 7, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, int(RVOL_THRESHOLD * 1000)))
        out["LOUD1"] = loud_bars
        # MISSING1: absent entirely -> MISSING_FROM_FETCH.
        return out

    with caplog.at_level(logging.INFO, logger="watchtower.runner"):
        promoted = runner_mod.run_broad_scan(universe_conn, fetch_bars_fn=fake_bars)

    assert promoted == ["LOUD1"]  # the real, unaffected selection

    [shadow_record] = [r for r in caplog.records if "broad_scan_shadow_counts" in r.getMessage()]
    msg = shadow_record.getMessage()
    assert "requested=5" in msg
    assert "fetched=4" in msg
    assert "missing_from_fetch=1" in msg
    assert "insufficient_history=1" in msg
    assert "requested_snapshot=3" in msg
    assert "invalid_baseline=1" in msg
    assert "evaluated_quiet=1" in msg
    assert "requested_candidate=1" in msg
    assert "candidate=1" in msg
    assert "eligible_for_top_n=1" in msg
    assert "selected_top_n=1" in msg
    # No vendor extras anywhere in this scenario -- every unexpected_*
    # count must be zero.
    assert "unexpected_from_fetch=0" in msg
    assert "unexpected_snapshot=0" in msg
    assert "unexpected_candidate=0" in msg
    assert "unexpected_selected=0" in msg
    assert "invariant_ok=True" in msg


def test_run_broad_scan_shadow_counts_track_unexpected_vendor_symbols_separately(caplog):
    """If the vendor's bulk response includes a symbol that was NEVER
    requested (not in active_symbols() at all), the requested-universe
    conservation invariant must still hold using only the requested
    population, the unexpected symbol must be counted separately at
    every stage it reaches (fetch, snapshot, candidate, and — since it
    scores as a real candidate here — selected), and the real returned
    selection must be unaffected by this instrumentation: nothing in
    build_snapshots_from_daily_bars/screen_snapshot/promote_candidates
    filters by "was this symbol requested", so current production
    semantics already process and would return such a symbol."""
    from tradebot import universe as universe_mod
    from tradebot.broad_scan import RVOL_THRESHOLD
    from tradebot.marketdata import AssetInfo

    universe_conn = universe_mod.connect(":memory:")
    symbols = ["QUIET1", "LOUD1"]
    universe_mod.refresh_universe(
        universe_conn,
        lambda: [AssetInfo(s, "NASDAQ", s, True, True, None, ()) for s in symbols],
        datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    def _spike_bars(symbol):
        bars = [Bar(symbol, datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 7)]
        bars.append(Bar(symbol, datetime(2026, 8, 7, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, int(RVOL_THRESHOLD * 1000)))
        return bars

    def fake_bars(fetched_symbols, lookback_days):
        assert set(fetched_symbols) == set(symbols)  # only the real universe was ever requested
        return {
            "QUIET1": [Bar("QUIET1", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 8)],
            "LOUD1": _spike_bars("LOUD1"),
            # EXTRA1 was never requested -- simulates a vendor response
            # containing an unrequested symbol.
            "EXTRA1": _spike_bars("EXTRA1"),
        }

    with caplog.at_level(logging.INFO, logger="watchtower.runner"):
        promoted = runner_mod.run_broad_scan(universe_conn, fetch_bars_fn=fake_bars)

    # Current production semantics, unrelated to this instrumentation:
    # an unrequested-but-fetched symbol that screens as a real candidate
    # is returned exactly like any other. This instrumentation must not
    # change that.
    assert set(promoted) == {"LOUD1", "EXTRA1"}

    [shadow_record] = [r for r in caplog.records if "broad_scan_shadow_counts" in r.getMessage()]
    msg = shadow_record.getMessage()
    # Requested-universe conservation holds using ONLY the 2 real symbols
    # -- EXTRA1 must not inflate any of these.
    assert "requested=2" in msg
    assert "fetched=2" in msg
    assert "missing_from_fetch=0" in msg
    assert "insufficient_history=0" in msg
    assert "requested_snapshot=2" in msg
    assert "invalid_baseline=0" in msg
    assert "evaluated_quiet=1" in msg  # QUIET1
    assert "requested_candidate=1" in msg  # LOUD1 only
    assert "invariant_ok=True" in msg
    # EXTRA1 tracked separately at every stage it reached, neither
    # folded into the requested figures nor silently dropped.
    assert "candidate=2" in msg  # true production total: LOUD1 + EXTRA1
    assert "selected_top_n=2" in msg
    assert "unexpected_from_fetch=1" in msg
    assert "unexpected_snapshot=1" in msg
    assert "unexpected_candidate=1" in msg
    assert "unexpected_selected=1" in msg


def test_broad_scan_shadow_count_failure_never_touches_the_returned_selection(caplog, monkeypatch):
    """If the shadow-count instrumentation itself breaks -- including in
    a way that bypasses its own internal try/except entirely, the
    worst case this test deliberately simulates -- run_broad_scan()'s
    real return value must be byte-identical to the unbroken case, and
    the failure must still be observable (logged), not silently lost."""
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
        quiet_bars = [Bar("QUIET", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 8)]
        loud_bars = [Bar("LOUD", datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000) for d in range(1, 7)]
        loud_bars.append(Bar("LOUD", datetime(2026, 8, 7, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, int(RVOL_THRESHOLD * 1000)))
        return {"QUIET": quiet_bars, "LOUD": loud_bars}

    def _broken(*args, **kwargs):
        raise RuntimeError("simulated instrumentation bug, bypassing its own internal guard")

    monkeypatch.setattr(runner_mod, "_log_broad_scan_shadow_counts", _broken)

    with caplog.at_level(logging.ERROR, logger="watchtower.runner"):
        promoted = runner_mod.run_broad_scan(universe_conn, fetch_bars_fn=fake_bars)

    assert promoted == ["LOUD"]  # unchanged from the non-broken case
    assert any("shadow-count instrumentation failed" in r.getMessage() for r in caplog.records)


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


# --------------------------------------------------------------------------
# 2026-08-21 runner INFO logging visibility prerequisite: configure_logging()
# attaches a handler to the "watchtower" parent logger (never the root
# logger, and never as an import-time side effect) so watchtower.runner
# and future children like watchtower.vendors.alpaca actually reach
# Docker's captured output instead of being silently dropped by Python's
# handler-less default. See configure_logging's own docstring.
# --------------------------------------------------------------------------


@pytest.fixture
def _clean_watchtower_logger():
    """configure_logging() mutates process-global logger state (the
    "watchtower" logger's handlers/level/propagate) -- exactly the kind
    of state a pre-existing test elsewhere in this file already depends
    on (e.g. the guard-rejection test's caplog.at_level(..., logger=
    "watchtower.runner"), which relies on propagation reaching pytest's
    root-attached capture handler). Every test that calls
    configure_logging() must restore the exact pre-test state afterward,
    or it risks silently breaking an unrelated test's propagation-based
    assertions depending on run order."""
    wt_logger = logging.getLogger("watchtower")
    saved_handlers = list(wt_logger.handlers)
    saved_level = wt_logger.level
    saved_propagate = wt_logger.propagate
    yield wt_logger
    wt_logger.handlers = saved_handlers
    wt_logger.setLevel(saved_level)
    wt_logger.propagate = saved_propagate


def test_configure_logging_makes_watchtower_runner_info_visible(_clean_watchtower_logger):
    stream = io.StringIO()
    runner_mod.configure_logging(level="INFO", stream=stream)

    logging.getLogger("watchtower.runner").info("hello from the runner")

    output = stream.getvalue()
    assert "hello from the runner" in output
    assert "INFO" in output
    assert "watchtower.runner" in output


def test_configure_logging_makes_a_future_vendor_child_logger_info_visible(_clean_watchtower_logger):
    """watchtower.vendors.alpaca doesn't have a logger yet (this module
    has none as of this PR -- see the vendor-observability reconnaissance),
    but any watchtower.* child must inherit visibility the same way,
    proving the fix is scoped to the namespace, not hardcoded to
    watchtower.runner specifically."""
    stream = io.StringIO()
    runner_mod.configure_logging(level="INFO", stream=stream)

    logging.getLogger("watchtower.vendors.alpaca").info("vendor_call_start operation=fetch_daily_bars_bulk")

    output = stream.getvalue()
    assert "vendor_call_start operation=fetch_daily_bars_bulk" in output
    assert "watchtower.vendors.alpaca" in output


def test_configure_logging_log_level_warning_suppresses_info_but_emits_warning(_clean_watchtower_logger):
    stream = io.StringIO()
    runner_mod.configure_logging(level="WARNING", stream=stream)

    logging.getLogger("watchtower.runner").info("should not appear")
    logging.getLogger("watchtower.runner").warning("should appear")

    output = stream.getvalue()
    assert "should not appear" not in output
    assert "should appear" in output


def test_configure_logging_called_twice_does_not_duplicate_handlers_or_lines(_clean_watchtower_logger):
    stream = io.StringIO()
    runner_mod.configure_logging(level="INFO", stream=stream)
    runner_mod.configure_logging(level="INFO", stream=stream)

    assert len(logging.getLogger("watchtower").handlers) == 1

    logging.getLogger("watchtower.runner").info("only once")

    assert stream.getvalue().count("only once") == 1


def test_configure_logging_does_not_make_an_unrelated_third_party_logger_info_visible(_clean_watchtower_logger):
    stream = io.StringIO()
    runner_mod.configure_logging(level="INFO", stream=stream)

    assert logging.getLogger("some_third_party_lib").isEnabledFor(logging.INFO) is False


def test_importing_runner_module_alone_does_not_configure_logging():
    """A real fresh interpreter, not a monkeypatched one -- proves import
    time has no logging side effect, not just that this particular test
    process's already-imported module happens not to have one right now."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import tradebot.runner, logging, sys; "
            "sys.exit(0 if logging.getLogger('watchtower').handlers == [] else 1)",
        ],
        cwd=repo_root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# --------------------------------------------------------------------------
# 2026-08-22 shared-per-iteration proxy fetch: run_live() used to build
# market_bars (2 fresh SPY/QQQ session_bars() calls) for EVERY symbol that
# advanced this iteration -- 17 + 17*2 = 51 fetch_intraday_bars calls in
# steady state. Now it's fetched lazily, once per while-loop iteration, on
# the first symbol that reaches that point, closed-bar-filtered against its
# own fetch instant, and reused unchanged for every later symbol's
# process_new_bar call this same iteration -- 17 + 2 = 19.
#
# These tests drive run_live() through exactly one while-loop iteration
# with a fake LiveMarketData and a process_new_bar spy: no need for real
# alert-worthy detections, since what's under test is the fetch/reuse
# mechanism itself, not detector output. The while loop is stopped via
# HALT_FILE once a precomputed expected number of session_bars calls has
# happened -- run_live() only checks HALT_FILE at the top of the NEXT
# iteration, so this always lets the current pass finish first.
# --------------------------------------------------------------------------

_PROXY_FROZEN_NOW = datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc)
_CLOSED_BAR_OPEN = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)  # close 13:35, well before frozen now
_FORMING_BAR_OPEN = datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc)  # close 13:45, AFTER frozen now -- must be filtered


def _proxy_test_bar(symbol: str, open_time: datetime, close: float = 100.0) -> Bar:
    return Bar(symbol, open_time, close, close + 0.1, close - 0.1, close, volume=1000)


def _drive_one_live_iteration(monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls):
    """session_bars_by_symbol[symbol] is either a list[Bar] (returned on
    every call) or a zero-arg callable (invoked fresh each call -- lets a
    test raise on the first call and succeed on a later one, e.g. test 6
    below). Returns (session_bars_calls, process_new_bar_calls, stats)."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _PROXY_FROZEN_NOW

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    halt_file = tmp_path / "HALT"

    monkeypatch.setattr(runner_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(runner_mod, "WATCHLIST", list(watchlist))
    monkeypatch.setattr(runner_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(runner_mod, "SESSION_CLOSE_STATE_FILE", tmp_path / "session_close_state.json")
    monkeypatch.setattr(runner_mod, "SESSION_OPEN_STATE_FILE", tmp_path / "session_open_state.json")
    monkeypatch.setattr(runner_mod, "HALT_FILE", halt_file)
    monkeypatch.setattr(runner_mod, "HEARTBEAT_FILE", tmp_path / "heartbeat.json")
    monkeypatch.setattr(runner_mod.time, "sleep", lambda seconds: None)

    session_bars_calls: list[str] = []
    call_counter = {"n": 0}

    class _FakeLiveMarketData:
        def __init__(self, symbol, session_date):
            self.symbol = symbol
            self.session_date = session_date

        def session_bars(self, symbol, session_date):
            session_bars_calls.append(symbol)
            call_counter["n"] += 1
            if call_counter["n"] >= halt_after_session_bars_calls:
                halt_file.touch()
            value = session_bars_by_symbol[symbol]
            return list(value()) if callable(value) else list(value)

        def daily_bars(self, symbol, n):
            return [_proxy_test_bar(symbol, _CLOSED_BAR_OPEN - timedelta(days=1), close=100.0)]

        def quote(self, symbol):
            raise NotImplementedError("process_new_bar is spied in these tests -- quote should never be reached")

        def chain(self, symbol, expiry):
            raise NotImplementedError

    monkeypatch.setattr(runner_mod, "LiveMarketData", _FakeLiveMarketData)

    process_new_bar_calls: list[dict] = []

    def _spy_process_new_bar(*args, **kwargs):
        process_new_bar_calls.append({"symbol": args[4], "market_bars": kwargs.get("market_bars")})

    monkeypatch.setattr(runner_mod, "process_new_bar", _spy_process_new_bar)

    stats = runner_mod.run_live(ConsoleAlerter(), db_path=tmp_path / "journal.db")
    return session_bars_calls, process_new_bar_calls, stats


def test_shared_proxy_fetch_happens_exactly_once_per_iteration_when_symbols_advance(monkeypatch, tmp_path):
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    closed = [_proxy_test_bar("x", _CLOSED_BAR_OPEN)]
    session_bars_by_symbol = {s: closed for s in watchlist}

    session_bars_calls, process_new_bar_calls, stats = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=6,
    )

    # 4 primary (one per watchlist symbol) + 2 shared proxy (SPY, QQQ,
    # fetched once during AAA's turn) = 6 -- not 4 + 4*2 = 12 under the
    # old per-symbol design.
    assert len(session_bars_calls) == 6
    assert session_bars_calls.count("SPY") == 2  # 1 shared proxy fetch + 1 primary fetch (SPY is also a watchlist symbol)
    assert session_bars_calls.count("QQQ") == 2
    assert [c["symbol"] for c in process_new_bar_calls] == watchlist
    assert stats.errors == []


def test_shared_proxy_fetch_does_not_happen_when_no_symbol_advances(monkeypatch, tmp_path):
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    session_bars_by_symbol = {s: [] for s in watchlist}  # every symbol: "not rth_bars: continue" fires immediately

    session_bars_calls, process_new_bar_calls, stats = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=4,
    )

    assert session_bars_calls == ["AAA", "BBB", "SPY", "QQQ"]  # exactly one primary attempt each, no proxy fetch layered on
    assert process_new_bar_calls == []


def test_shared_proxy_fetch_is_lazy_first_call_is_the_primary_symbol(monkeypatch, tmp_path):
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    closed = [_proxy_test_bar("x", _CLOSED_BAR_OPEN)]
    session_bars_by_symbol = {s: closed for s in watchlist}

    session_bars_calls, _, _ = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=6,
    )

    # If the proxy fetch were eager (top-of-loop), SPY/QQQ would be
    # among the first calls regardless of watchlist order. Lazy means
    # AAA's own primary fetch -- and its closed-bar/staleness/dedup
    # decisions -- happen first; the shared fetch triggers only once
    # AAA reaches the point the old per-symbol market_bars build used
    # to sit at.
    assert session_bars_calls[0] == "AAA"
    assert session_bars_calls[1] == "SPY"
    assert session_bars_calls[2] == "QQQ"


def test_shared_proxy_snapshot_filters_a_forming_bar_and_stays_frozen_for_later_symbols(monkeypatch, tmp_path):
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    closed = [_proxy_test_bar("x", _CLOSED_BAR_OPEN)]
    spy_raw = [_proxy_test_bar("SPY", _CLOSED_BAR_OPEN), _proxy_test_bar("SPY", _FORMING_BAR_OPEN)]  # closed + forming
    session_bars_by_symbol = {"AAA": closed, "BBB": closed, "SPY": spy_raw, "QQQ": closed}

    _, process_new_bar_calls, _ = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=6,
    )

    aaa_call = next(c for c in process_new_bar_calls if c["symbol"] == "AAA")
    spy_context = aaa_call["market_bars"]["SPY"]
    assert len(spy_context) == 1  # the forming bar (close 13:45, after frozen now 13:40) was filtered out
    assert spy_context[0].ts == _CLOSED_BAR_OPEN

    # BBB is evaluated after AAA in this same iteration but must see the
    # exact same (already-filtered, frozen) shared snapshot -- a bar
    # that only "closes" after the shared fetch can never be
    # positionally paired with a later primary symbol's bars, because
    # the snapshot isn't refetched for BBB at all.
    bbb_call = next(c for c in process_new_bar_calls if c["symbol"] == "BBB")
    assert bbb_call["market_bars"] is aaa_call["market_bars"]


def test_process_new_bar_still_runs_for_a_symbol_whose_bars_outgrew_the_shared_proxy_snapshot(monkeypatch, tmp_path):
    """runner.py must not special-case a symbol whose own bars have grown
    past the shared proxy snapshot's length -- that's
    relative_strength_break's own len(proxy_bars) < len(bars) guard to
    handle conservatively (see tests/test_detectors.py::
    test_relative_strength_break_returns_none_when_the_proxy_has_not_
    caught_up_yet, unchanged by this PR), not something the runner
    itself should detect or react to."""
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    one_bar = [_proxy_test_bar("x", _CLOSED_BAR_OPEN)]
    two_bars = [
        _proxy_test_bar("x", _CLOSED_BAR_OPEN - timedelta(minutes=5)),
        _proxy_test_bar("x", _CLOSED_BAR_OPEN),
    ]
    session_bars_by_symbol = {"AAA": one_bar, "BBB": two_bars, "SPY": one_bar, "QQQ": one_bar}

    _, process_new_bar_calls, stats = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=6,
    )

    bbb_call = next(c for c in process_new_bar_calls if c["symbol"] == "BBB")
    assert len(bbb_call["market_bars"]["SPY"]) == 1  # unchanged, shorter than BBB's own 2 bars -- not "fixed up"
    assert stats.errors == []  # the mismatch is not treated as a runner-level error


def test_shared_proxy_fetch_failure_is_isolated_and_not_retried_per_symbol(monkeypatch, tmp_path):
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    closed = [_proxy_test_bar("x", _CLOSED_BAR_OPEN)]

    spy_calls = {"n": 0}

    def _spy_bars():
        spy_calls["n"] += 1
        if spy_calls["n"] == 1:
            raise RuntimeError("simulated vendor failure")
        return closed  # SPY's own later primary-turn fetch succeeds normally

    session_bars_by_symbol = {"AAA": closed, "BBB": closed, "QQQ": closed, "SPY": _spy_bars}

    session_bars_calls, process_new_bar_calls, stats = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=5,
    )

    # Exactly one failed shared-fetch attempt (SPY raises during AAA's
    # turn); QQQ's proxy fetch is never even attempted, since the dict
    # comprehension aborts on SPY's exception, and no later symbol
    # retries the shared fetch (shared_market_bars_attempted is already
    # True) -- only SPY's own later primary-turn fetch adds a 2nd call.
    assert session_bars_calls.count("SPY") == 2
    assert session_bars_calls.count("QQQ") == 1
    assert len(stats.errors) == 1
    assert "RuntimeError" in stats.errors[0]

    # Ordinary detector processing continues for every symbol, including
    # AAA (whose turn triggered the failed shared fetch) -- with
    # market_bars=None there, not an aborted evaluation.
    assert [c["symbol"] for c in process_new_bar_calls] == watchlist
    aaa_call = next(c for c in process_new_bar_calls if c["symbol"] == "AAA")
    assert aaa_call["market_bars"] is None


def test_shared_proxy_snapshot_has_no_mutation_leakage_across_symbol_evaluations(monkeypatch, tmp_path):
    watchlist = ["AAA", "BBB", "SPY", "QQQ"]
    closed = [_proxy_test_bar("x", _CLOSED_BAR_OPEN)]
    session_bars_by_symbol = {s: closed for s in watchlist}

    _, process_new_bar_calls, _ = _drive_one_live_iteration(
        monkeypatch, tmp_path, watchlist, session_bars_by_symbol, halt_after_session_bars_calls=6,
    )

    market_bars_seen = [c["market_bars"] for c in process_new_bar_calls]
    first = market_bars_seen[0]
    for later in market_bars_seen[1:]:
        assert later is first  # identical object, not rebuilt or copied-and-diverged
        assert later == first  # and its contents never changed in between
    assert first == {"SPY": closed, "QQQ": closed}  # exactly what was fetched, unmodified by any evaluation
