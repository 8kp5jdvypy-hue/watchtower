"""Tests for scripts/fetch_cache.py's loud-failure behavior, added
2026-08-12 -- this script had zero test coverage before this incident.
Scoped to the loud/quiet distinction the hotfix adds, not a full
behavioral test suite for the script.

Extended 2026-08-19 for full-code-review.md findings C3 (silent exit 0
on failure) and #11 (non-atomic cache writes) -- see docs/BACKLOG.md's
"C3 remnant" for why the earlier hotfix only covered the live runner's
own close-time cacher, not this script's out-of-band callers.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------- #
# C3 (docs/BACKLOG.md's "C3 remnant" / full-code-review.md finding #3):
# main() must exit non-zero and print a clear failure summary to stderr
# when any symbol genuinely fails, but stay quiet (exit 0) for a
# legitimate no-op run (everything already cached).
# ---------------------------------------------------------------------- #


def test_main_exits_non_zero_and_summarizes_failures_on_stderr(monkeypatch, tmp_path, capsys, caplog):
    """SYM1's vendor calls always come back empty (both daily and
    intraday) -- a genuine post-retry failure. SYM2 succeeds cleanly on
    every call. Only SYM1 may appear in the failure summary."""
    monkeypatch.setattr(fetch_cache, "date", _frozen_today(date(2026, 6, 16)))
    monkeypatch.setattr(fetch_cache, "MAX_LOOKBACK_DAYS", 3)

    def _daily(symbol, n):
        return [] if symbol == "SYM1" else [_dummy_bar(date(2026, 6, 15))]

    def _intraday(symbol, d):
        return [] if symbol == "SYM1" else [_dummy_bar(d)]

    monkeypatch.setattr(fetch_cache, "fetch_daily_bars", _daily)
    monkeypatch.setattr(fetch_cache, "fetch_intraday_bars", _intraday)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_cache.py", "--symbols", "SYM1,SYM2", "--daily-n", "1", "--sessions-n", "1",
            "--cache-dir", str(tmp_path),
        ],
    )

    with caplog.at_level("ERROR", logger="watchtower.fetch_cache"):
        exit_code = fetch_cache.main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "1/2 symbols failed: SYM1" in captured.err
    assert "SYM2" not in captured.err
    # SYM2's real success is unaffected -- both its files actually landed
    assert (tmp_path / "SYM2" / "daily.csv").exists()
    assert (tmp_path / "SYM2" / "intraday_2026-06-15.csv").exists()


def test_main_exits_zero_when_everything_is_already_cached(monkeypatch, tmp_path):
    """A legitimate no-op run (nothing to fetch, everything already on
    disk) must never be confused with a failure -- the exact "successful
    partial run" case C3's fix must not false-alarm on."""
    for symbol in ("SYM1", "SYM2"):
        d = tmp_path / symbol
        d.mkdir()
        (d / "daily.csv").write_text("ts,open,high,low,close,volume\n")
        (d / "intraday_2026-06-15.csv").write_text("ts,open,high,low,close,volume\n")

    monkeypatch.setattr(fetch_cache, "date", _frozen_today(date(2026, 6, 16)))

    def _must_not_fetch(*_a, **_k):
        raise AssertionError("must not fetch when everything is already cached")

    monkeypatch.setattr(fetch_cache, "fetch_daily_bars", _must_not_fetch)
    monkeypatch.setattr(fetch_cache, "fetch_intraday_bars", _must_not_fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_cache.py", "--symbols", "SYM1,SYM2", "--daily-n", "1", "--sessions-n", "1",
            "--cache-dir", str(tmp_path),
        ],
    )

    assert fetch_cache.main() == 0


# ---------------------------------------------------------------------- #
# #11 (full-code-review.md): cache writes must be atomic -- a crash
# mid-write must never leave a truncated file visible at the final
# path, since ensure_daily/ensure_sessions key idempotency on
# path.exists() and would otherwise lock a truncated file in forever.
# ---------------------------------------------------------------------- #


def test_atomic_write_bars_csv_writes_via_rename_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "TEST" / "daily.csv"
    bars = [_dummy_bar(date(2026, 6, 15))]

    fetch_cache._atomic_write_bars_csv(target, bars)

    assert target.exists()
    assert target.read_text().splitlines()[0] == "ts,open,high,low,close,volume"
    assert list(target.parent.iterdir()) == [target]  # no leftover .tmp file


def test_atomic_write_bars_csv_leaves_no_partial_file_at_the_final_path_on_failure(tmp_path, monkeypatch):
    """Mocks the failure point (a real kill mid-write can't be caught by
    test code or by the function under test either -- what matters is
    the guarantee about the FINAL path, not that this exception handler
    runs): the temp file gets partial bytes, then the write blows up.
    The final path must never see anything, since os.replace() only
    ever runs after a complete, successful write to the temp file."""
    target = tmp_path / "TEST" / "daily.csv"
    target.parent.mkdir(parents=True)

    def _partial_write_then_crash(path, bars):
        path.write_text("ts,open,high,low,close,volume\ntruncated")
        raise OSError("simulated disk failure mid-write")

    monkeypatch.setattr(fetch_cache, "_write_bars_csv", _partial_write_then_crash)

    with pytest.raises(OSError):
        fetch_cache._atomic_write_bars_csv(target, [_dummy_bar(date(2026, 6, 15))])

    assert not target.exists()
    assert list(target.parent.iterdir()) == []  # the crashed temp file was cleaned up too


def test_atomic_write_bars_csv_never_calls_the_shared_writer_on_the_final_path_directly(tmp_path, monkeypatch):
    """The shared tradebot.marketdata.write_bars_csv must only ever see
    the temp path, never the real target -- otherwise this is just the
    old direct-write bug with extra steps."""
    target = tmp_path / "TEST" / "daily.csv"
    seen_paths = []

    def _record_path(path, bars):
        seen_paths.append(path)
        path.write_text("ts,open,high,low,close,volume\n")

    monkeypatch.setattr(fetch_cache, "_write_bars_csv", _record_path)

    fetch_cache._atomic_write_bars_csv(target, [_dummy_bar(date(2026, 6, 15))])

    assert seen_paths == [target.parent / seen_paths[0].name]
    assert seen_paths[0] != target
    assert seen_paths[0].parent == target.parent
