"""Tests for tradebot.marketdata.ReplayMarketData — the lookahead guard.

This is the most important test in the project: it proves that replaying
history can never leak a future bar to a detector. See CLAUDE.md.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Bar
from tradebot.marketdata import (
    LiveMarketData,
    ReplayMarketData,
    _is_postmarket,
    _is_rth,
    filter_plausible_sessions,
    implausible_session_reason,
    median_session_volume,
    write_bars_csv,
)

SYMBOL = "TEST"
SESSION = date(2026, 6, 15)
BAR_MINUTES = 5
FIELDNAMES = ["ts", "open", "high", "low", "close", "volume"]


def _bar_row(ts: datetime, price: float, volume: int = 1000) -> dict:
    return {
        "ts": ts.isoformat(),
        "open": price,
        "high": price + 0.5,
        "low": price - 0.5,
        "close": price + 0.1,
        "volume": volume,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _write_intraday_csv(cache_dir: Path, symbol: str, session: date, bars: list[dict]) -> None:
    _write_csv(cache_dir / symbol / f"intraday_{session.isoformat()}.csv", bars)


def _write_daily_csv(cache_dir: Path, symbol: str, bars: list[dict]) -> None:
    _write_csv(cache_dir / symbol / "daily.csv", bars)


@pytest.fixture
def cache_dir(tmp_path):
    # 09:30 ET on 2026-06-15 (EDT, UTC-4) == 13:30 UTC.
    session_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        _bar_row(session_open + timedelta(minutes=BAR_MINUTES * i), price=100 + i)
        for i in range(10)
    ]
    _write_intraday_csv(tmp_path, SYMBOL, SESSION, bars)
    _write_daily_csv(tmp_path, SYMBOL, [_bar_row(session_open - timedelta(days=1), price=99)])
    return tmp_path


def test_no_bars_visible_before_first_advance(cache_dir):
    md = ReplayMarketData(cache_dir, SYMBOL, SESSION)
    assert md.session_bars(SYMBOL, SESSION) == ()


def test_advance_false_when_session_has_no_bars(tmp_path):
    _write_intraday_csv(tmp_path, SYMBOL, SESSION, [])
    _write_daily_csv(tmp_path, SYMBOL, [_bar_row(datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc), 99)])
    md = ReplayMarketData(tmp_path, SYMBOL, SESSION)
    assert md.advance() is False
    assert md.session_bars(SYMBOL, SESSION) == ()


def test_advance_reveals_exactly_one_bar_at_a_time(cache_dir):
    md = ReplayMarketData(cache_dir, SYMBOL, SESSION)
    for expected_count in range(1, 11):
        assert md.advance() is True
        assert len(md.session_bars(SYMBOL, SESSION)) == expected_count

    # only 10 bars exist — advance() must report exhaustion, not fabricate an 11th
    assert md.advance() is False
    assert len(md.session_bars(SYMBOL, SESSION)) == 10


def test_cannot_see_a_bar_at_or_after_the_cursor(cache_dir):
    """The core lookahead guard: at every cursor position, every bar
    session_bars() returns must be strictly earlier than every bar still
    hidden beyond the cursor."""
    reference = ReplayMarketData(cache_dir, SYMBOL, SESSION)
    while reference.advance():
        pass
    full_timeline = reference.session_bars(SYMBOL, SESSION)
    assert len(full_timeline) == 10

    for cursor in range(len(full_timeline) + 1):
        probe = ReplayMarketData(cache_dir, SYMBOL, SESSION)
        for _ in range(cursor):
            probe.advance()

        visible = probe.session_bars(SYMBOL, SESSION)
        hidden = full_timeline[cursor:]

        assert visible == full_timeline[:cursor]
        assert len(visible) == cursor

        if hidden:
            first_hidden_ts = hidden[0].ts
            for bar in visible:
                assert bar.ts < first_hidden_ts, (
                    f"lookahead leak: visible bar at {bar.ts} is not strictly "
                    f"before the next hidden bar at {first_hidden_ts}"
                )


def test_premarket_bars_gated_by_the_same_cursor(tmp_path):
    # 04:00 ET premarket == 08:00 UTC; 09:30 ET RTH open == 13:30 UTC (EDT).
    premarket_open = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        _bar_row(premarket_open + timedelta(minutes=BAR_MINUTES * i), price=90 + i)
        for i in range(3)
    ] + [
        _bar_row(rth_open + timedelta(minutes=BAR_MINUTES * i), price=100 + i)
        for i in range(3)
    ]
    _write_intraday_csv(tmp_path, SYMBOL, SESSION, bars)
    _write_daily_csv(tmp_path, SYMBOL, [_bar_row(rth_open - timedelta(days=1), price=99)])

    md = ReplayMarketData(tmp_path, SYMBOL, SESSION)
    assert md.premarket_bars(SYMBOL, SESSION) == ()

    md.advance()  # reveals only the first premarket bar
    assert len(md.premarket_bars(SYMBOL, SESSION)) == 1
    assert len(md.session_bars(SYMBOL, SESSION)) == 0

    for _ in range(5):
        md.advance()
    assert len(md.premarket_bars(SYMBOL, SESSION)) == 3
    assert len(md.session_bars(SYMBOL, SESSION)) == 3


def test_postmarket_bars_are_gated_by_the_same_cursor(tmp_path):
    rth_close_bar = datetime(2026, 6, 15, 19, 55, tzinfo=timezone.utc)
    postmarket_open = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    bars = [
        _bar_row(rth_close_bar, price=100),
        _bar_row(postmarket_open, price=108),
        _bar_row(postmarket_open + timedelta(minutes=5), price=109),
    ]
    _write_intraday_csv(tmp_path, SYMBOL, SESSION, bars)
    _write_daily_csv(tmp_path, SYMBOL, [_bar_row(rth_close_bar - timedelta(days=1), price=99)])

    md = ReplayMarketData(tmp_path, SYMBOL, SESSION)
    md.advance()
    assert len(md.session_bars(SYMBOL, SESSION)) == 1
    assert md.postmarket_bars(SYMBOL, SESSION) == ()

    md.advance()
    assert len(md.postmarket_bars(SYMBOL, SESSION)) == 1
    md.advance()
    assert len(md.postmarket_bars(SYMBOL, SESSION)) == 2


def test_early_close_uses_real_xnys_close_for_rth_and_postmarket():
    # 2026-11-27 closes at 13:00 ET (18:00 UTC). A 13:00 print is
    # postmarket, not an extra three hours of fake RTH.
    last_rth = Bar(SYMBOL, datetime(2026, 11, 27, 17, 55, tzinfo=timezone.utc), 100, 101, 99, 100, 1000)
    first_post = Bar(SYMBOL, datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc), 101, 102, 100, 101, 1000)

    assert _is_rth(last_rth) is True
    assert _is_postmarket(last_rth) is False
    assert _is_rth(first_post) is False
    assert _is_postmarket(first_post) is True


def test_live_intraday_snapshot_partitions_one_provider_call_without_freezing_future_reads(monkeypatch):
    rth = Bar(SYMBOL, datetime(2026, 6, 15, 19, 55, tzinfo=timezone.utc), 100, 101, 99, 100, 1000)
    post = Bar(SYMBOL, datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc), 110, 111, 109, 110, 2000)
    calls = []

    def _fetch(symbol, session_date):
        calls.append((symbol, session_date))
        return [rth, post]

    monkeypatch.setattr("tradebot.vendors.alpaca.fetch_intraday_bars", _fetch)
    market_data = LiveMarketData(SYMBOL, SESSION)

    snapshot = market_data.intraday_snapshot(SYMBOL, SESSION)
    assert snapshot.rth == (rth,)
    assert snapshot.postmarket == (post,)
    assert calls == [(SYMBOL, SESSION)]

    # The ordinary live accessor still fetches afresh. Existing RTH runner
    # instances live all day and must never freeze on the first snapshot.
    assert market_data.session_bars(SYMBOL, SESSION) == (rth,)
    assert len(calls) == 2

    with pytest.raises(ValueError, match="scoped"):
        market_data.intraday_snapshot(SYMBOL, date(2026, 6, 16))


def test_daily_bars_not_gated_by_intraday_cursor(cache_dir):
    md = ReplayMarketData(cache_dir, SYMBOL, SESSION)
    assert len(md.daily_bars(SYMBOL, 1)) == 1


def test_daily_bars_excludes_bars_on_or_after_session_date(tmp_path):
    """The cache holds the most recent daily bars as of fetch time, which
    for an old replay session includes bars from its future. daily_bars()
    must filter those out rather than leak them."""
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    _write_intraday_csv(tmp_path, SYMBOL, SESSION, [_bar_row(rth_open, 100)])
    daily_rows = [
        _bar_row(datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc), 90),
        _bar_row(datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc), 92),
        _bar_row(datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc), 95),  # == session date
        _bar_row(datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc), 99),  # after session date
    ]
    _write_daily_csv(tmp_path, SYMBOL, daily_rows)

    md = ReplayMarketData(tmp_path, SYMBOL, SESSION)
    visible_daily = md.daily_bars(SYMBOL, 10)

    assert len(visible_daily) == 2
    for bar in visible_daily:
        assert bar.ts.date() < SESSION


def test_wrong_symbol_or_session_raises(cache_dir):
    md = ReplayMarketData(cache_dir, SYMBOL, SESSION)
    with pytest.raises(ValueError):
        md.session_bars("OTHER", SESSION)
    with pytest.raises(ValueError):
        md.session_bars(SYMBOL, date(2020, 1, 1))


def test_write_bars_csv_round_trips_through_replay_market_data(tmp_path):
    """2026-08-12: write_bars_csv is the write-side counterpart to
    _read_bars, moved here from scripts/fetch_cache.py so
    tradebot.runner's close-time cache fetch can call it directly (no
    scripts/ import needed inside the container). A file it writes must
    be exactly what ReplayMarketData/backfill_marks() can read back --
    that's the whole point of the fix this exists for."""
    session_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar(symbol=SYMBOL, ts=session_open + timedelta(minutes=BAR_MINUTES * i), open=100 + i, high=100.5 + i, low=99.5 + i, close=100.1 + i, volume=1000 + i)
        for i in range(5)
    ]
    path = tmp_path / SYMBOL / f"intraday_{SESSION.isoformat()}.csv"
    write_bars_csv(path, bars)

    assert path.exists()
    md = ReplayMarketData(tmp_path, SYMBOL, SESSION)
    while md.advance():
        pass
    read_back = list(md.session_bars(SYMBOL, SESSION))

    assert len(read_back) == 5
    assert [b.close for b in read_back] == [b.close for b in bars]
    assert [b.ts for b in read_back] == [b.ts for b in bars]


def test_write_bars_csv_creates_parent_directories(tmp_path):
    path = tmp_path / "NEWSYMBOL" / f"intraday_{SESSION.isoformat()}.csv"
    assert not path.parent.exists()

    write_bars_csv(path, [Bar(symbol="NEWSYMBOL", ts=datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1)])

    assert path.exists()


# --------------------------------------------------------------------------
# Proposal 5c — plausibility floor (docs/open-awareness-proposals-2026-08.md)
# --------------------------------------------------------------------------


def _rth_bars(n: int, volume: int = 1000, symbol: str = SYMBOL) -> list[Bar]:
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    return [
        Bar(symbol=symbol, ts=rth_open + timedelta(minutes=BAR_MINUTES * i), open=100, high=100.5, low=99.5, close=100.1, volume=volume)
        for i in range(n)
    ]


def test_median_session_volume_none_with_no_history():
    assert median_session_volume([]) is None


def test_median_session_volume_odd_count_is_the_middle_value():
    assert median_session_volume([10.0, 30.0, 20.0]) == 20.0


def test_median_session_volume_even_count_averages_the_middle_two():
    assert median_session_volume([10.0, 20.0, 30.0, 40.0]) == 25.0


def test_implausible_session_reason_passes_a_healthy_session():
    bars = _rth_bars(78, volume=1000)  # 78,000 total, 78/78 expected bars
    assert implausible_session_reason(bars, median_volume=100_000.0, expected_bar_count=78) is None


def test_implausible_session_reason_skips_volume_check_with_no_reference():
    # 1 bar is still below the 50% bar-count floor even with n=1's tiny
    # expected count, so use an expected count this single bar satisfies.
    bars = _rth_bars(1, volume=1)
    assert implausible_session_reason(bars, median_volume=None, expected_bar_count=1) is None


def test_implausible_session_reason_rejects_below_the_volume_floor():
    # 78 bars * 100 volume = 7,800 total -- below 20% of a 100,000 median (20,000).
    bars = _rth_bars(78, volume=100)
    reason = implausible_session_reason(bars, median_volume=100_000.0, expected_bar_count=78)
    assert reason is not None and reason.startswith("implausible_volume:")


def test_implausible_session_reason_rejects_below_the_bar_count_floor():
    # 38 of 78 expected bars (~49%) -- the real IEX-thin-USO shape the
    # 50% threshold was calibrated against (docs/open-awareness-proposals-2026-08.md).
    bars = _rth_bars(38, volume=1_000_000)  # plenty of volume -- only bar count should trip
    reason = implausible_session_reason(bars, median_volume=100.0, expected_bar_count=78)
    assert reason is not None and reason.startswith("implausible_bar_count:")


def test_implausible_session_reason_calendar_aware_early_close_not_flagged():
    # A 13:00 ET early close (39 expected bars) with all 39 present must
    # never be flagged just because 39 < a regular day's 78.
    bars = _rth_bars(39, volume=1000)
    assert implausible_session_reason(bars, median_volume=None, expected_bar_count=39) is None


def test_filter_plausible_sessions_accepts_a_uniformly_healthy_run():
    sessions = [(date(2026, 6, d), _rth_bars(78, volume=1000)) for d in range(1, 6)]
    accepted, rejections = filter_plausible_sessions(sessions, lambda d: 78)
    assert len(accepted) == 5
    assert rejections == []


def test_filter_plausible_sessions_rejects_a_runt_without_dropping_the_others():
    healthy = [(date(2026, 6, d), _rth_bars(78, volume=1000)) for d in range(1, 5)]
    runt = (date(2026, 6, 5), _rth_bars(5, volume=5000))  # ample volume, far below the bar-count floor
    sessions = healthy + [runt]
    accepted, rejections = filter_plausible_sessions(sessions, lambda d: 78)
    assert len(accepted) == 4
    assert rejections == [(date(2026, 6, 5), rejections[0][1])]
    assert rejections[0][1].startswith("implausible_bar_count:")


def test_filter_plausible_sessions_median_excludes_a_prior_rejection():
    """A runt session must not drag the volume reference down for the
    sessions after it -- the median is computed from ACCEPTED sessions
    only, per filter_plausible_sessions' docstring."""
    healthy = [(date(2026, 6, d), _rth_bars(78, volume=100_000)) for d in range(1, 4)]
    runt = (date(2026, 6, 4), _rth_bars(78, volume=1))  # passes bar count, fails volume once a median exists
    candidate = (date(2026, 6, 5), _rth_bars(78, volume=25_000))  # 25% of the healthy median -- should PASS
    sessions = healthy + [runt, candidate]
    accepted, rejections = filter_plausible_sessions(sessions, lambda d: 78)
    assert len(accepted) == 4  # 3 healthy + candidate; runt rejected
    assert [d for d, _ in rejections] == [date(2026, 6, 4)]


def test_filter_plausible_sessions_window_caps_the_trailing_reference():
    """Only the trailing `window` accepted sessions feed the median -- a
    thin symbol's history from beyond that window can't keep a stale,
    unrepresentative reference alive forever."""
    old_thin = [(date(2026, 1, d), _rth_bars(78, volume=100)) for d in range(1, 4)]
    recent_healthy = [(date(2026, 6, d), _rth_bars(78, volume=100_000)) for d in range(1, 4)]
    candidate = (date(2026, 6, 10), _rth_bars(78, volume=25_000))  # implausible vs. old_thin, plausible vs. recent_healthy
    sessions = old_thin + recent_healthy + [candidate]
    accepted, rejections = filter_plausible_sessions(sessions, lambda d: 78, window=3)
    assert candidate[0] not in [d for d, _ in rejections]
    assert candidate[1] in accepted
