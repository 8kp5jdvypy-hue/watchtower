"""Direct tests for tradebot.telegram_bot.performance's arithmetic —
drawdown and losing-streak math is easy to get subtly wrong, so it's
worth pinning down with hand-computed expected values."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.detectors import Detection
from tradebot.journal import connect, set_news_driven, set_no_trade, write_cluster
from tradebot.telegram_bot import performance

BASE = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)


def _seed(conn, closes, marks, kinds, trends, no_trade_flags=None, news_driven_flags=None):
    for i in range(len(closes)):
        did = write_cluster(
            conn, session="2026-06-15", symbol="TEST", ts_utc=(BASE + timedelta(minutes=5 * i)).isoformat(),
            kinds=kinds[i], headlines="h", score=5.0, close=closes[i], atr14=1.0, trend=trends[i],
            detections=[Detection("TEST", kinds[i], BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, marks[i]))
        if no_trade_flags is not None and no_trade_flags[i] is not None:
            set_no_trade(conn, did, no_trade_flags[i])
        if news_driven_flags is not None and news_driven_flags[i] is not None:
            set_news_driven(conn, did, news_driven_flags[i])
    conn.commit()


def test_returns_none_below_min_history_sample():
    conn = connect(":memory:")
    _seed(conn, [100, 101, 102], [101, 102, 103], ["gap"] * 3, ["up"] * 3)
    assert performance.track_record(conn) is None


def test_hit_rate_avg_return_and_streak_hand_computed():
    conn = connect(":memory:")
    # signed returns (uptrend, so return = (mark-close)/close*100):
    # +1%, -1%, +1%, -1%, -1%, +1%   -> hits at idx 0,2,5 -> hit_rate 3/6=0.5
    closes = [100] * 6
    marks = [101, 99, 101, 99, 99, 101]
    _seed(conn, closes, marks, ["level_break"] * 6, ["up"] * 6)

    tr = performance.track_record(conn)
    assert tr.sample_size == 6
    assert tr.hit_rate == 0.5
    assert abs(tr.avg_return_pct - 0.0) < 1e-9
    # losses at idx 1, then idx 3,4 (back to back) -> longest streak is 2
    assert tr.longest_losing_streak == 2


def test_max_drawdown_on_a_monotonic_losing_sequence():
    conn = connect(":memory:")
    # six alerts, each losing exactly 1% in sequence -> cumulative curve
    # is 0, -1, -2, -3, -4, -5, -6 — peak stays at 0, trough at -6
    closes = [100] * 6
    marks = [99] * 6
    _seed(conn, closes, marks, ["level_break"] * 6, ["up"] * 6)
    tr = performance.track_record(conn)
    assert tr.longest_losing_streak == 6
    assert tr.max_drawdown_pct == -6.0


def test_news_vs_clean_technical_split_by_real_news_driven_flag():
    """The split reads the real news_driven column — set by runner.py from
    tradebot.events' actual event windows — not a "gap" kind heuristic. A
    "gap" detection with no overlapping event window is clean technical,
    and a non-gap detection that does overlap one is news-driven."""
    conn = connect(":memory:")
    kinds = ["gap"] * 5 + ["level_break"] * 5
    closes = [100] * 10
    marks = [101] * 10
    news_driven_flags = [False] * 5 + [True] * 5  # inverted vs kind, to prove kind isn't what's read
    _seed(conn, closes, marks, kinds, ["up"] * 10, news_driven_flags=news_driven_flags)
    tr = performance.track_record(conn)
    assert tr.news_driven is not None and tr.news_driven.sample_size == 5
    assert tr.clean_technical is not None and tr.clean_technical.sample_size == 5


def test_total_alerts_vs_no_trade_only_counts_tracked_rows():
    conn = connect(":memory:")
    closes = [100] * 6
    marks = [101] * 6
    no_trade_flags = [True, False, True, False, False, None]  # last one never tracked
    _seed(conn, closes, marks, ["gap"] * 6, ["up"] * 6, no_trade_flags=no_trade_flags)
    tr = performance.track_record(conn)
    assert tr.total_alerts == 6
    assert tr.no_trade_tracked_count == 5  # the untracked one is excluded
    assert tr.total_no_trade == 2
