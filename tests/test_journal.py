"""Tests for tradebot.journal."""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Detection
from tradebot.journal import (
    MIN_HISTORY_SAMPLE,
    backfill_marks,
    cluster_id,
    connect,
    historical_performance,
    write_cluster,
)

SYMBOL = "TEST"
SESSION = date(2026, 6, 15)
FIELDNAMES = ["ts", "open", "high", "low", "close", "volume"]


def _detection(kind="level_break", score=4.0) -> Detection:
    return Detection(SYMBOL, kind, datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc), score, "headline", {"foo": "bar"})


def test_write_cluster_round_trips(tmp_path):
    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="level_break", headlines="broke prior_high", score=4.0, close=101.0, atr14=1.5,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )
    conn.commit()
    row = conn.execute("SELECT symbol, tier, score, code_version FROM detections").fetchone()
    assert row == (SYMBOL, "high", 4.0, "abc123")


def test_sub_threshold_detections_are_still_written_as_log_tier(tmp_path):
    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="range_expansion", headlines="minor range", score=0.3, close=101.0, atr14=1.5,
        trend="flat", detections=[_detection(score=0.3)], code_version_str="abc123",
    )
    conn.commit()
    tier = conn.execute("SELECT tier FROM detections").fetchone()[0]
    assert tier == "log"


def test_rewriting_the_same_cluster_upserts_instead_of_duplicating(tmp_path):
    conn = connect(tmp_path / "journal.db")
    kwargs = dict(
        session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="level_break", headlines="v1", score=2.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection(score=2.0)], code_version_str="abc123",
    )
    id1 = write_cluster(conn, **kwargs)
    kwargs["headlines"] = "v2"
    kwargs["score"] = 5.0
    id2 = write_cluster(conn, **kwargs)
    conn.commit()

    assert id1 == id2
    rows = conn.execute("SELECT headlines, score FROM detections").fetchall()
    assert rows == [("v2", 5.0)]


def test_cluster_id_is_deterministic():
    a = cluster_id("SPY", "2026-06-15", "2026-06-15T14:00:00+00:00", "gap")
    b = cluster_id("SPY", "2026-06-15", "2026-06-15T14:00:00+00:00", "gap")
    c = cluster_id("SPY", "2026-06-15", "2026-06-15T14:05:00+00:00", "gap")
    assert a == b
    assert a != c


def _write_bar_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _bar_row(ts: datetime, price: float) -> dict:
    return {"ts": ts.isoformat(), "open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1000}


def test_backfill_marks_fills_forward_prices_and_skips_missing_offsets(tmp_path):
    cache_dir = tmp_path / "cache"
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
    # 8 5-min bars covering 09:30-10:10 ET (40 minutes) — enough for +15/+30
    # but not +60 from a detection at the open.
    bars = [_bar_row(rth_open + timedelta(minutes=5 * i), 100 + i) for i in range(8)]
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", bars)
    _write_bar_csv(cache_dir / SYMBOL / "daily.csv", [_bar_row(rth_open - timedelta(days=1), 99)])

    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session=SESSION.isoformat(), symbol=SYMBOL, ts_utc=rth_open.isoformat(),
        kinds="gap", headlines="gapped up", score=2.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )
    conn.commit()

    written = backfill_marks(conn, SESSION, cache_dir=cache_dir, offsets_min=(15, 30, 60))
    marks = dict(conn.execute("SELECT offset_min, price FROM marks").fetchall())

    assert 15 in marks and 30 in marks
    assert 60 not in marks  # session doesn't extend that far — must not fabricate a price
    assert written == 2


def _write_cluster_with_mark(conn, kind, trend, close, price_at_30, ts_utc):
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc=ts_utc,
        kinds=kind, headlines="h", score=4.0, close=close, atr14=1.0,
        trend=trend, detections=[_detection(kind=kind)], code_version_str="abc",
    )
    conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, price_at_30))
    conn.commit()
    return detection_id


def test_historical_performance_computes_continuation_rate_and_avg_return(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # up-trend gap clusters at close=100: 3 continue up, 2 reverse down
    closes_marks = [105, 102, 98, 101, 97]
    for i, mark in enumerate(closes_marks):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )

    result = historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent")
    assert result is not None
    assert result.sample_size == 5
    assert result.continuation_rate == pytest.approx(0.6)  # 105,102,101 > 100; 98,97 < 100
    assert result.avg_return_pct == pytest.approx(0.6)  # mean of [+5,+2,-2,+1,-3] % = +0.6%
    assert result.offset_min == 30


def test_historical_performance_returns_none_below_min_sample(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert MIN_HISTORY_SAMPLE == 5
    for i in range(MIN_HISTORY_SAMPLE - 1):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=105,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )
    assert historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent") is None


def test_historical_performance_filters_by_kind_and_trend_and_excludes_self(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # 6 matching, so excluding one still leaves 5 (>= MIN_HISTORY_SAMPLE)
    matching_ids = [
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=105,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )
        for i in range(6)
    ]
    # wrong trend — must not count
    _write_cluster_with_mark(conn, kind="gap", trend="down", close=100.0, price_at_30=95, ts_utc=(base + timedelta(minutes=100)).isoformat())
    # wrong kind — must not count
    _write_cluster_with_mark(conn, kind="vwap_break", trend="up", close=100.0, price_at_30=105, ts_utc=(base + timedelta(minutes=105)).isoformat())

    result = historical_performance(conn, kind="gap", trend="up", exclude_id=matching_ids[0], lookback=20)
    assert result.sample_size == 5  # 6 matching, minus the excluded one
