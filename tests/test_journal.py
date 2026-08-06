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
    hour_performance,
    iv_rank,
    pending_contract_backfills,
    record_contract_forward_mid,
    record_contract_selection,
    record_iv_sample,
    tier_performance,
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


def test_backfill_marks_default_offsets_include_5min(tmp_path):
    cache_dir = tmp_path / "cache"
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
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

    backfill_marks(conn, SESSION, cache_dir=cache_dir)  # default offsets, no override
    marks = dict(conn.execute("SELECT offset_min, price FROM marks").fetchall())

    assert 5 in marks
    assert marks[5] == 100  # first bar's own close — matches the ts_utc=rth_open convention above


def _write_cluster_with_mark(conn, kind, trend, close, price_at_30, ts_utc, score=4.0):
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc=ts_utc,
        kinds=kind, headlines="h", score=score, close=close, atr14=1.0,
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


def test_historical_performance_excludes_news_driven_rows_from_the_sample(tmp_path):
    """Continuation stats are built on technical-setup history and don't
    transfer to an event-driven move — see tradebot.events module
    docstring. A news_driven=1 row must never contaminate another
    detection's Similar Setups sample, even though it otherwise matches
    kind/trend."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # 5 clean-technical, continuing up-trend gaps
    for i, mark in enumerate([105, 102, 101, 103, 104]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )
    # A 6th, news-driven row that reversed hard — must not pull the sample down
    news_id = _write_cluster_with_mark(
        conn, kind="gap", trend="up", close=100.0, price_at_30=50,
        ts_utc=(base + timedelta(minutes=100)).isoformat(),
    )
    conn.execute("UPDATE detections SET news_driven=1 WHERE id=?", (news_id,))
    conn.commit()

    result = historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent", lookback=20)
    assert result.sample_size == 5  # the news-driven row is excluded, not just down-weighted
    assert result.continuation_rate == pytest.approx(1.0)  # all 5 clean rows continued


def test_tier_performance_groups_by_tier_and_computes_real_stats(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)

    # 5 HIGH-tier (score=5.0), all up-trend, all continue (mark > close)
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    # 5 MEDIUM-tier (score=2.0), mixed outcomes: 2 continue, 3 reverse
    for i, mark in enumerate([101, 102, 99, 98, 97]):
        _write_cluster_with_mark(
            conn, kind="level_break", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=100 + 5 * i)).isoformat(), score=2.0,
        )

    result = tier_performance(conn)
    assert result["high"].sample_size == 5
    assert result["high"].continuation_rate == pytest.approx(1.0)
    assert result["medium"].sample_size == 5
    assert result["medium"].continuation_rate == pytest.approx(0.4)  # 2/5 continued


def test_tier_performance_omits_tiers_below_min_sample(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert MIN_HISTORY_SAMPLE == 5
    for i in range(MIN_HISTORY_SAMPLE - 1):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=105,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    result = tier_performance(conn)
    assert "high" not in result


def test_hour_performance_groups_by_et_hour(tmp_path):
    conn = connect(tmp_path / "journal.db")
    # 14:00 ET == 18:00 UTC in EDT (summer)
    base_14et = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base_14et + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    result = hour_performance(conn, tier="high")
    assert 14 in result
    assert result[14].sample_size == 5
    assert result[14].continuation_rate == pytest.approx(1.0)
    assert result[14].hour_et == 14


def test_hour_performance_omits_hours_below_min_sample(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base_14et = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    for i in range(MIN_HISTORY_SAMPLE - 1):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=105,
            ts_utc=(base_14et + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    result = hour_performance(conn, tier="high")
    assert 14 not in result


def test_hour_performance_tier_none_includes_all_non_log_tiers(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base_14et = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base_14et + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    for i, mark in enumerate([101, 102, 99]):
        _write_cluster_with_mark(
            conn, kind="level_break", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base_14et + timedelta(minutes=25 + 5 * i)).isoformat(), score=2.0,
        )
    scoped = hour_performance(conn, tier="high")
    combined = hour_performance(conn, tier=None)
    assert scoped[14].sample_size == 5
    assert combined[14].sample_size == 8


def test_historical_performance_normalizes_avg_return_by_each_rows_own_atr14(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # each row's own atr14 differs — avg_return_atr must use each row's own,
    # not a single borrowed value, to be a real "typical move in ATR" figure
    rows = [(100.0, 102.0, 2.0), (100.0, 101.0, 0.5), (100.0, 103.0, 1.0), (100.0, 104.0, 4.0), (100.0, 99.0, 1.0)]
    for i, (close, mark, atr14) in enumerate(rows):
        detection_id = write_cluster(
            conn, session="2026-06-15", symbol=SYMBOL, ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            kinds="gap", headlines="h", score=4.0, close=close, atr14=atr14,
            trend="up", detections=[_detection(kind="gap")], code_version_str="abc",
        )
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, mark))
    conn.commit()

    result = historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent")
    expected = sum(abs(m - c) / a for c, m, a in rows) / len(rows)
    assert result.avg_return_atr == pytest.approx(expected)


def test_historical_performance_avg_return_atr_is_none_when_no_row_has_atr14(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i in range(MIN_HISTORY_SAMPLE):
        detection_id = write_cluster(
            conn, session="2026-06-15", symbol=SYMBOL, ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            kinds="gap", headlines="h", score=4.0, close=100.0, atr14=None,
            trend="up", detections=[_detection(kind="gap")], code_version_str="abc",
        )
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, 105))
    conn.commit()
    result = historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent")
    assert result.avg_return_atr is None


def test_iv_rank_computes_a_real_rank_once_enough_history_exists(tmp_path):
    conn = connect(tmp_path / "journal.db")
    for i, iv in enumerate([0.20, 0.30, 0.40, 0.50, 0.60]):
        record_iv_sample(conn, "GOOGL", date(2026, 6, 10 + i), iv)
    rank, sample = iv_rank(conn, "GOOGL", current_iv=0.55)
    assert sample == 5
    assert rank == pytest.approx((0.55 - 0.20) / (0.60 - 0.20) * 100)


def test_iv_rank_none_with_no_history():
    conn = connect(":memory:")
    rank, sample = iv_rank(conn, "GOOGL", current_iv=0.30)
    assert rank is None and sample == 0


def test_iv_rank_none_on_a_degenerate_range_never_divides_by_zero(tmp_path):
    conn = connect(tmp_path / "journal.db")
    for i in range(5):
        record_iv_sample(conn, "GOOGL", date(2026, 6, 10 + i), 0.30)  # identical every day
    rank, sample = iv_rank(conn, "GOOGL", current_iv=0.30)
    assert rank is None and sample == 5


def test_record_iv_sample_upserts_one_row_per_symbol_per_session(tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_iv_sample(conn, "GOOGL", date(2026, 6, 10), 0.30)
    record_iv_sample(conn, "GOOGL", date(2026, 6, 10), 0.35)  # same day, re-run
    rows = conn.execute("SELECT iv FROM iv_history WHERE symbol = ? AND session = ?", ("GOOGL", "2026-06-10")).fetchall()
    assert rows == [(0.35,)]


def test_contract_selection_round_trips_and_forward_mid_backfill(tmp_path):
    conn = connect(tmp_path / "journal.db")
    entry_ts = datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc)
    record_contract_selection(
        conn, "det1", symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13,
        delta=-0.47, entry_mid=4.20, entry_ts=entry_ts,
    )
    row = conn.execute("SELECT symbol, right, strike, dte, delta, entry_mid FROM contract_selections WHERE detection_id = ?", ("det1",)).fetchone()
    assert row == ("GOOGL", "put", 365.0, 13, -0.47, 4.20)

    pending = pending_contract_backfills(conn, entry_ts + timedelta(minutes=31), offset_min=30)
    assert pending == [("det1", "GOOGL", "put", 365.0, "2026-08-14")]

    record_contract_forward_mid(conn, "det1", 30, 4.05)
    still_pending = pending_contract_backfills(conn, entry_ts + timedelta(minutes=31), offset_min=30)
    assert still_pending == []
    mid_30 = conn.execute("SELECT mid_30m FROM contract_selections WHERE detection_id = ?", ("det1",)).fetchone()[0]
    assert mid_30 == 4.05


def test_record_contract_selection_is_idempotent_on_rerun(tmp_path):
    conn = connect(tmp_path / "journal.db")
    entry_ts = datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc)
    record_contract_selection(conn, "det1", symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13, delta=-0.47, entry_mid=4.20, entry_ts=entry_ts)
    record_contract_selection(conn, "det1", symbol="GOOGL", right="put", strike=370.0, expiry=date(2026, 8, 14), dte=13, delta=-0.30, entry_mid=6.00, entry_ts=entry_ts)
    count = conn.execute("SELECT COUNT(*) FROM contract_selections").fetchone()[0]
    assert count == 1
    strike = conn.execute("SELECT strike FROM contract_selections WHERE detection_id = ?", ("det1",)).fetchone()[0]
    assert strike == 370.0
