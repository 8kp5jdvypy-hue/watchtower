"""Tests for tradebot.analytics."""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.analytics import (
    FIVE_MIN_OFFSET,
    NEXT_DAY_CLOSE_OFFSET_MIN,
    backfill_five_minute_marks,
    backfill_next_day_marks,
    compute_excursion,
    signal_frequency,
    signal_quality_report,
)
from tradebot.detectors import Detection
from tradebot.journal import connect, write_cluster

SYMBOL = "TEST"
SESSION = date(2026, 6, 15)
NEXT_SESSION = date(2026, 6, 16)
FIELDNAMES = ["ts", "open", "high", "low", "close", "volume"]
RTH_OPEN = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _detection(kind="level_break", score=4.0) -> Detection:
    return Detection(SYMBOL, kind, RTH_OPEN, score, "headline", {"foo": "bar"})


def _write_bar_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _bar_row(ts: datetime, price: float) -> dict:
    return {"ts": ts.isoformat(), "open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1000}


def _write_cluster(conn, *, ts_utc, close, trend="up", symbol=SYMBOL, kind="level_break", score=4.0) -> str:
    return write_cluster(
        conn, session=SESSION.isoformat(), symbol=symbol, ts_utc=ts_utc, kinds=kind, headlines="h",
        score=score, close=close, atr14=1.0, trend=trend, detections=[_detection(kind=kind, score=score)],
        code_version_str="abc123", primary_kind=kind,
    )


# --------------------------------------------------------------------------
# compute_excursion
# --------------------------------------------------------------------------


def test_compute_excursion_clean_uptrend_has_zero_mae(tmp_path):
    """Price only goes up after the detection — MAE (adverse excursion)
    must clamp to 0, never report a fabricated negative drawdown."""
    cache_dir = tmp_path / "cache"
    bars = [_bar_row(RTH_OPEN + timedelta(minutes=5 * i), 100 + i) for i in range(5)]  # 100..104
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", bars)

    conn = connect(tmp_path / "journal.db")
    detection_ts = (RTH_OPEN + timedelta(minutes=5)).isoformat()  # bar0's own close — the triggering bar
    detection_id = _write_cluster(conn, ts_utc=detection_ts, close=100.0)
    conn.commit()

    result = compute_excursion(conn, detection_id, cache_dir=cache_dir, horizon_minutes=60)

    assert result.bars_examined == 4  # bar0 (the trigger) is excluded; bars 1-4 are "after"
    assert result.mfe_pct == pytest.approx(4.5)  # (104.5 - 100) / 100
    assert result.mae_pct == 0.0  # price never dropped below entry — no adverse excursion, not a negative one
    assert result.time_to_mfe_minutes == pytest.approx(20.0)  # bar4 closes 20m after the detection


def test_compute_excursion_captures_a_real_pullback(tmp_path):
    cache_dir = tmp_path / "cache"
    prices = [100, 99, 98, 103]  # entry, then a dip, then a recovery past the entry
    bars = [_bar_row(RTH_OPEN + timedelta(minutes=5 * i), p) for i, p in enumerate(prices)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", bars)

    conn = connect(tmp_path / "journal.db")
    detection_ts = (RTH_OPEN + timedelta(minutes=5)).isoformat()
    detection_id = _write_cluster(conn, ts_utc=detection_ts, close=100.0)
    conn.commit()

    result = compute_excursion(conn, detection_id, cache_dir=cache_dir, horizon_minutes=60)

    assert result.mfe_pct == pytest.approx(3.5)  # (103.5 - 100) / 100
    assert result.mae_pct == pytest.approx(2.5)  # (100 - 97.5) / 100 — the real dip, not smoothed away
    assert result.time_to_mfe_minutes == pytest.approx(15.0)


def test_compute_excursion_returns_none_when_horizon_excludes_every_bar(tmp_path):
    cache_dir = tmp_path / "cache"
    bars = [_bar_row(RTH_OPEN + timedelta(minutes=5 * i), 100 + i) for i in range(5)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", bars)

    conn = connect(tmp_path / "journal.db")
    detection_id = _write_cluster(conn, ts_utc=(RTH_OPEN + timedelta(minutes=5)).isoformat(), close=100.0)
    conn.commit()

    assert compute_excursion(conn, detection_id, cache_dir=cache_dir, horizon_minutes=1) is None


def test_compute_excursion_returns_none_for_an_unknown_detection(tmp_path):
    conn = connect(tmp_path / "journal.db")
    assert compute_excursion(conn, "does-not-exist", cache_dir=tmp_path / "cache") is None


# --------------------------------------------------------------------------
# signal_quality_report
# --------------------------------------------------------------------------


def test_signal_quality_report_none_below_min_sample(tmp_path):
    conn = connect(tmp_path / "journal.db")
    for i in range(4):  # one short of MIN_HISTORY_SAMPLE (5)
        ts = (RTH_OPEN + timedelta(minutes=5 * i)).isoformat()
        detection_id = _write_cluster(conn, ts_utc=ts, close=100.0)
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, 105)", (detection_id,))
    conn.commit()

    assert signal_quality_report(conn, kind="level_break", cache_dir=tmp_path / "cache") is None


def test_signal_quality_report_computes_hit_rate_and_false_positive_as_complements(tmp_path):
    conn = connect(tmp_path / "journal.db")
    prices = [110, 105, 95, 102, 98, 101]  # 4 hits (>close), 2 misses, close=100 for all
    for i, price in enumerate(prices):
        ts = (RTH_OPEN + timedelta(minutes=5 * i)).isoformat()
        detection_id = _write_cluster(conn, ts_utc=ts, close=100.0)
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, price))
    conn.commit()

    report = signal_quality_report(conn, kind="level_break", tier="high", cache_dir=tmp_path / "cache")

    assert report.sample_size == 6
    assert report.hit_rate == pytest.approx(4 / 6)
    assert report.false_positive_rate == pytest.approx(1 - report.hit_rate)
    assert report.avg_return_pct == pytest.approx(11 / 6)
    assert report.median_return_pct == pytest.approx(1.5)
    # No cache dir with real bars was provided — excursion stats must
    # degrade to "not computed," never a fabricated average.
    assert report.excursion_sample_size == 0
    assert report.avg_mfe_pct is None
    assert report.avg_mae_pct is None


def test_signal_quality_report_filters_by_symbol_and_kind(tmp_path):
    conn = connect(tmp_path / "journal.db")
    for i in range(6):
        ts = (RTH_OPEN + timedelta(minutes=5 * i)).isoformat()
        detection_id = _write_cluster(conn, ts_utc=ts, close=100.0, symbol="TEST", kind="level_break")
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, 105)", (detection_id,))
    # A handful of a different symbol/kind that must never leak into the filtered report.
    for i in range(6):
        ts = (RTH_OPEN + timedelta(minutes=5 * i)).isoformat()
        detection_id = _write_cluster(conn, ts_utc=ts, close=50.0, symbol="OTHER", kind="gap")
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, 40)", (detection_id,))
    conn.commit()

    report = signal_quality_report(conn, symbol="TEST", kind="level_break", cache_dir=tmp_path / "cache")
    assert report.sample_size == 6
    assert report.hit_rate == 1.0

    other = signal_quality_report(conn, symbol="OTHER", kind="gap", cache_dir=tmp_path / "cache")
    assert other.sample_size == 6
    assert other.hit_rate == 0.0


# --------------------------------------------------------------------------
# signal_frequency
# --------------------------------------------------------------------------


def test_signal_frequency_counts_per_session_with_filters(tmp_path):
    conn = connect(tmp_path / "journal.db")
    _write_cluster(conn, ts_utc=RTH_OPEN.isoformat(), close=100.0, kind="level_break")
    _write_cluster(conn, ts_utc=(RTH_OPEN + timedelta(minutes=5)).isoformat(), close=100.0, kind="level_break")
    _write_cluster(conn, ts_utc=(RTH_OPEN + timedelta(minutes=10)).isoformat(), close=100.0, kind="gap")
    conn.commit()

    assert signal_frequency(conn) == {SESSION.isoformat(): 3}
    assert signal_frequency(conn, kind="level_break") == {SESSION.isoformat(): 2}
    assert signal_frequency(conn, kind="gap") == {SESSION.isoformat(): 1}
    assert signal_frequency(conn, kind="rvol_spike") == {}  # no matches — absent, not a fabricated zero


# --------------------------------------------------------------------------
# Additive backfills
# --------------------------------------------------------------------------


def test_backfill_five_minute_marks_adds_the_offset(tmp_path):
    cache_dir = tmp_path / "cache"
    bars = [_bar_row(RTH_OPEN + timedelta(minutes=5 * i), 100 + i) for i in range(8)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", bars)
    _write_bar_csv(cache_dir / SYMBOL / "daily.csv", [_bar_row(RTH_OPEN - timedelta(days=1), 99)])

    conn = connect(tmp_path / "journal.db")
    detection_id = _write_cluster(conn, ts_utc=RTH_OPEN.isoformat(), close=100.0)
    conn.commit()

    written = backfill_five_minute_marks(conn, SESSION, cache_dir=cache_dir)
    marks = dict(conn.execute("SELECT offset_min, price FROM marks WHERE detection_id = ?", (detection_id,)).fetchall())

    # journal.backfill_marks() always also writes the unconditional close
    # mark alongside whatever offsets_min was passed — see its own
    # docstring — so the +5m checkpoint plus the close mark is 2 rows.
    assert written == 2
    assert marks[FIVE_MIN_OFFSET] == 100  # bar0's own close time is RTH_OPEN + 5m, so it qualifies (>=)


def test_backfill_next_day_marks_uses_the_next_cached_sessions_close(tmp_path):
    cache_dir = tmp_path / "cache"
    today_bars = [_bar_row(RTH_OPEN + timedelta(minutes=5 * i), 100 + i) for i in range(4)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", today_bars)
    next_open = RTH_OPEN + timedelta(days=1)
    next_bars = [_bar_row(next_open + timedelta(minutes=5 * i), 200 + i) for i in range(4)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{NEXT_SESSION.isoformat()}.csv", next_bars)

    conn = connect(tmp_path / "journal.db")
    detection_id = _write_cluster(conn, ts_utc=RTH_OPEN.isoformat(), close=100.0)
    conn.commit()

    written = backfill_next_day_marks(conn, SESSION, cache_dir=cache_dir)
    price = conn.execute(
        "SELECT price FROM marks WHERE detection_id = ? AND offset_min = ?", (detection_id, NEXT_DAY_CLOSE_OFFSET_MIN)
    ).fetchone()[0]

    assert written == 1
    assert price == 203  # next session's real last close (200 + 3)


def test_backfill_next_day_marks_skips_when_no_later_session_is_cached(tmp_path):
    """Never fabricates a next-day mark just because none exists yet."""
    cache_dir = tmp_path / "cache"
    today_bars = [_bar_row(RTH_OPEN + timedelta(minutes=5 * i), 100 + i) for i in range(4)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", today_bars)

    conn = connect(tmp_path / "journal.db")
    detection_id = _write_cluster(conn, ts_utc=RTH_OPEN.isoformat(), close=100.0)
    conn.commit()

    written = backfill_next_day_marks(conn, SESSION, cache_dir=cache_dir)
    row = conn.execute(
        "SELECT price FROM marks WHERE detection_id = ? AND offset_min = ?", (detection_id, NEXT_DAY_CLOSE_OFFSET_MIN)
    ).fetchone()

    assert written == 0
    assert row is None
