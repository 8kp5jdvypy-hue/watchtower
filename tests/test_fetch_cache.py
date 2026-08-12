"""Tests for scripts/fetch_cache.py's loud-failure behavior, added
2026-08-12 -- this script had zero test coverage before this incident.
Scoped to the loud/quiet distinction the hotfix adds, not a full
behavioral test suite for the script.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_cache
from tradebot.detectors import Bar


def _frozen_today(fixed: date):
    """ensure_sessions() computes `date.today() - timedelta(days=1)`
    internally -- freezing date.today() is the minimal way to pin its
    walk-back start point without changing the function's signature."""

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return fixed

    return _FrozenDate


def _dummy_bar(d: date) -> Bar:
    ts = datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc)
    return Bar(symbol="TEST", ts=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=100)


def test_ensure_daily_logs_an_error_when_the_vendor_returns_no_bars(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fetch_cache, "fetch_daily_bars", lambda symbol, n: [])

    with caplog.at_level("ERROR", logger="watchtower.fetch_cache"):
        status = fetch_cache.ensure_daily("TEST", tmp_path, 60)

    assert status == "no data returned"
    assert len(caplog.records) == 1 and caplog.records[0].levelname == "ERROR"
    assert "TEST" in caplog.records[0].message


def test_ensure_daily_stays_quiet_when_the_file_already_exists(tmp_path, monkeypatch, caplog):
    path = tmp_path / "TEST" / "daily.csv"
    path.parent.mkdir(parents=True)
    path.write_text("ts,open,high,low,close,volume\n")

    def _must_not_fetch(symbol, n):
        raise AssertionError("must not fetch when the file already exists")

    monkeypatch.setattr(fetch_cache, "fetch_daily_bars", _must_not_fetch)

    with caplog.at_level("ERROR", logger="watchtower.fetch_cache"):
        status = fetch_cache.ensure_daily("TEST", tmp_path, 60)

    assert status == "skipped (exists)"
    assert caplog.records == []


def test_ensure_sessions_logs_an_error_for_a_real_trading_day_with_no_bars(monkeypatch, tmp_path, caplog):
    """2026-08-12 incident review finding: pre-fix, this and an actual
    holiday were labeled identically ('no data (holiday?)') -- a real
    vendor/data failure on a real trading day read as expected quiet.

    n=1 with the bad day frozen as "today - 1" means the walk-back finds
    its one satisfying session on this exact date or gives up entirely
    (MAX_LOOKBACK_DAYS caps it) -- fetch_intraday_bars always returns []
    here on purpose, so there's nothing further back to accidentally
    satisfy n and mask the assertion."""
    real_trading_day = date(2026, 6, 15)  # a real Monday, not a holiday
    monkeypatch.setattr(fetch_cache, "date", _frozen_today(real_trading_day + timedelta(days=1)))
    monkeypatch.setattr(fetch_cache, "fetch_intraday_bars", lambda symbol, d: [])
    monkeypatch.setattr(fetch_cache, "MAX_LOOKBACK_DAYS", 1)  # stop after checking just this one day

    with caplog.at_level("ERROR", logger="watchtower.fetch_cache"):
        results = fetch_cache.ensure_sessions("TEST", tmp_path, n=1)

    error_results = [(d, s) for d, s in results if s.startswith("ERROR")]
    assert error_results == [(real_trading_day, "ERROR: no data on a real trading day")]
    assert len(caplog.records) == 1 and caplog.records[0].levelname == "ERROR"


def test_ensure_sessions_never_calls_the_vendor_for_a_weekend_date(monkeypatch, tmp_path, caplog):
    # "today" frozen to Tuesday 2026-06-16, so the walk-back's candidates
    # in order are Monday 06-15, Sunday 06-14, Saturday 06-13, Friday
    # 06-12 -- the weekday() >= 5 branch must skip the Sunday/Saturday
    # pair before ever calling the vendor on them.
    monkeypatch.setattr(fetch_cache, "date", _frozen_today(date(2026, 6, 16)))
    called_dates = []

    def _record_and_satisfy(symbol, d):
        called_dates.append(d)
        return [_dummy_bar(d)]

    monkeypatch.setattr(fetch_cache, "fetch_intraday_bars", _record_and_satisfy)

    with caplog.at_level("ERROR", logger="watchtower.fetch_cache"):
        fetch_cache.ensure_sessions("TEST", tmp_path, n=2)

    assert date(2026, 6, 14) not in called_dates  # Sunday
    assert date(2026, 6, 13) not in called_dates  # Saturday
    assert caplog.records == []


def test_ensure_sessions_stays_quiet_on_a_real_holiday(monkeypatch, tmp_path, caplog):
    """2026-01-01 (New Year's Day, a real NYSE holiday, not a weekend) --
    the vendor legitimately returns no bars for it, and that specific
    date must stay quiet -- same as before this fix, since it's the
    genuinely expected case this fix must not start false-alarming on.
    The day before (2025-12-31, a real trading day) is given a real bar
    so the walk-back is satisfied there and never needs to go further
    back past the holiday, isolating exactly the one date under test."""
    holiday = date(2026, 1, 1)
    day_before = date(2025, 12, 31)
    monkeypatch.setattr(fetch_cache, "date", _frozen_today(holiday + timedelta(days=1)))

    def _empty_on_holiday_else_real(symbol, d):
        return [] if d == holiday else [_dummy_bar(d)]

    monkeypatch.setattr(fetch_cache, "fetch_intraday_bars", _empty_on_holiday_else_real)

    with caplog.at_level("ERROR", logger="watchtower.fetch_cache"):
        results = fetch_cache.ensure_sessions("TEST", tmp_path, n=1)

    holiday_result = next(s for d, s in results if d == holiday)
    assert holiday_result == "no data (holiday)"
    assert day_before in [d for d, s in results if s.startswith("fetched")]
    assert caplog.records == []
