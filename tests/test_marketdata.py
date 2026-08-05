"""Tests for tradebot.marketdata.ReplayMarketData — the lookahead guard.

This is the most important test in the project: it proves that replaying
history can never leak a future bar to a detector. See CLAUDE.md.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.marketdata import ReplayMarketData

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
