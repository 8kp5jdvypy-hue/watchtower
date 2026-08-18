"""Direct tests for tradebot.telegram_bot.performance's arithmetic —
drawdown and losing-streak math is easy to get subtly wrong, so it's
worth pinning down with hand-computed expected values."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradebot.detectors import Detection
from tradebot.journal import connect, set_news_driven, set_no_trade, write_cluster
from tradebot.telegram_bot import db as users_db
from tradebot.telegram_bot import outbox
from tradebot.telegram_bot import performance

BASE = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)


def _seed(conn, closes, marks, kinds, trends, no_trade_flags=None, news_driven_flags=None, alerted_flags=None):
    for i in range(len(closes)):
        did = write_cluster(
            conn, session="2026-06-15", symbol="TEST", ts_utc=(BASE + timedelta(minutes=5 * i)).isoformat(),
            kinds=kinds[i], headlines="h", score=5.0, close=closes[i], atr14=1.0, trend=trends[i],
            detections=[Detection("TEST", kinds[i], BASE, 5.0, "h", {})], code_version_str="abc",
            alerted=alerted_flags[i] if alerted_flags is not None else True,
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
    # exactly 50% is z=0 by construction -> never significant
    assert tr.significance.z_score == 0.0
    assert tr.significance.is_significant is False


# ---------------------------------------------------------------------- #
# alerted_only (docs/phase4-proof-engine-proposal.md's "finding that
# changes the design"): default False must keep every existing caller
# (/performance, the Telegram weekly recap) computing over EVERY
# HIGH-tier detection, alerted or not -- these regression tests exist
# specifically to prove that adding the parameter didn't quietly change
# what /performance and the Telegram recap have always reported.
# ---------------------------------------------------------------------- #


def test_track_record_default_still_includes_unalerted_detections():
    """The exact regression this parameter must not cause: journaled-
    but-never-sent detections (suppressed, over budget, a replay run)
    must still count by default, same as before alerted_only existed."""
    conn = connect(":memory:")
    closes = [100] * 6
    marks = [101, 99, 101, 99, 99, 101]
    alerted_flags = [True, False, True, False, True, False]  # 3 of 6 actually sent
    _seed(conn, closes, marks, ["level_break"] * 6, ["up"] * 6, alerted_flags=alerted_flags)

    tr = performance.track_record(conn)  # alerted_only defaults to False

    assert tr.sample_size == 6  # all 6, not just the 3 alerted ones
    assert tr.hit_rate == 0.5


def test_track_record_alerted_only_excludes_unalerted_detections():
    conn = connect(":memory:")
    closes = [100] * 10
    marks = [101, 99, 101, 99, 99, 101, 99, 101, 99, 101]  # hits at idx 0,2,5,7,9
    # only the first 5 (hits at 0,2 -> 2/5) were actually sent; the rest
    # (hits at 5,7,9 -> 3/5) were journaled but never alerted
    alerted_flags = [True] * 5 + [False] * 5
    _seed(conn, closes, marks, ["level_break"] * 10, ["up"] * 10, alerted_flags=alerted_flags)

    tr = performance.track_record(conn, alerted_only=True)

    assert tr.sample_size == 5
    assert tr.hit_rate == pytest.approx(2 / 5)


def test_weekly_recap_default_still_includes_unalerted_detections():
    conn = connect(":memory:")
    closes = [100] * 6
    marks = [101, 99, 101, 99, 99, 101]
    alerted_flags = [True, False, True, False, True, False]
    _seed(conn, closes, marks, ["level_break"] * 6, ["up"] * 6, alerted_flags=alerted_flags)
    week_start = BASE.date().isoformat()
    week_end = (BASE.date() + timedelta(days=7)).isoformat()

    wr = performance.weekly_recap(conn, week_start, week_end)

    assert wr.sample_size == 6


def test_weekly_recap_alerted_only_excludes_unalerted_detections():
    conn = connect(":memory:")
    closes = [100] * 6
    marks = [101, 99, 101, 99, 99, 101]
    alerted_flags = [True, True, True, False, False, False]
    _seed(conn, closes, marks, ["level_break"] * 6, ["up"] * 6, alerted_flags=alerted_flags)
    week_start = BASE.date().isoformat()
    week_end = (BASE.date() + timedelta(days=7)).isoformat()

    wr = performance.weekly_recap(conn, week_start, week_end, alerted_only=True)

    assert wr.sample_size == 3


# ---------------------------------------------------------------------- #
# Statistical significance — see performance.significance_check. This is
# what stops the welcome text / pinned message from ever calling a
# coin-flip hit rate "a measured, real edge."
# ---------------------------------------------------------------------- #


def test_hit_rate_z_score_is_zero_at_exactly_the_baseline():
    assert performance.hit_rate_z_score(0.5, 1000) == 0.0


def test_hit_rate_z_score_is_symmetric_around_baseline():
    above = performance.hit_rate_z_score(0.55, 400)
    below = performance.hit_rate_z_score(0.45, 400)
    assert above > 0 and below < 0
    assert abs(above + below) < 1e-9


def test_hit_rate_z_score_zero_sample_size_does_not_divide_by_zero():
    assert performance.hit_rate_z_score(0.5, 0) == 0.0


def test_a_near_coin_flip_at_real_world_sample_size_is_not_significant():
    """This is the exact regression case: 49.57% over n=466 (this
    project's actual live numbers when this check was added) must not
    read as significant — this is what makes the coin-flip finding real,
    not a one-off in the current data."""
    check = performance.significance_check(0.4957, 466)
    assert check.is_significant is False
    assert abs(check.z_score) < 1.0  # nowhere close to the ~1.96 threshold


def test_a_strong_effect_at_a_large_sample_is_significant():
    check = performance.significance_check(0.60, 1000)
    assert check.is_significant is True
    assert check.z_score > performance.Z_95_TWO_SIDED


def test_required_sample_size_matches_the_standard_formula_by_hand():
    # ((1.96 + 0.84)^2 * 0.25) / 0.05^2 ~= 784
    n = performance.required_sample_size(0.55)
    assert 780 <= n <= 790


def test_required_sample_size_rejects_a_target_equal_to_baseline():
    with pytest.raises(ValueError):
        performance.required_sample_size(0.5)


def test_n_needed_for_meaningful_edge_does_not_depend_on_current_sample():
    """A fixed target ('how much would it take'), not a projection off
    today's noisy observed effect — see SignificanceCheck's docstring."""
    small = performance.significance_check(0.51, 20)
    large = performance.significance_check(0.51, 4000)
    assert small.n_needed_for_meaningful_edge == large.n_needed_for_meaningful_edge


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


# ---------------------------------------------------------------------- #
# Weekly recap — see tradebot.rendering.templates.render_weekly_recap.
# Unlike track_record(), this never returns None: a thin week still gets
# a recap, just one that says the sample is too thin for a rate.
# ---------------------------------------------------------------------- #


def _seed_on(conn, ts, close, mark, kind="gap", trend="up", symbol="TEST"):
    did = write_cluster(
        conn, session=ts.date().isoformat(), symbol=symbol, ts_utc=ts.isoformat(),
        kinds=kind, headlines="h", score=5.0, close=close, atr14=1.0, trend=trend,
        detections=[Detection(symbol, kind, ts, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, mark))
    conn.commit()
    return did


def test_weekly_recap_scopes_strictly_to_the_week_window():
    conn = connect(":memory:")
    week_start = datetime(2026, 8, 3, tzinfo=timezone.utc)  # Monday
    week_end = datetime(2026, 8, 10, tzinfo=timezone.utc)  # next Monday, exclusive
    for i in range(5):  # inside the week: all hits
        _seed_on(conn, week_start + timedelta(days=i, hours=1), 100, 101)
    _seed_on(conn, week_start - timedelta(hours=1), 100, 101)  # just before -> excluded
    _seed_on(conn, week_end, 100, 101)  # exactly at week_end -> excluded (exclusive)

    recap = performance.weekly_recap(conn, week_start.isoformat(), week_end.isoformat())
    assert recap.sample_size == 5
    assert recap.hit_rate == 1.0


def test_weekly_recap_never_returns_none_even_with_too_few_alerts():
    conn = connect(":memory:")
    week_start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    week_end = datetime(2026, 8, 10, tzinfo=timezone.utc)
    _seed_on(conn, week_start + timedelta(hours=1), 100, 101)  # only 1 -> below MIN_HISTORY_SAMPLE

    recap = performance.weekly_recap(conn, week_start.isoformat(), week_end.isoformat())
    assert recap is not None
    assert recap.sample_size == 1
    assert recap.hit_rate is None
    assert recap.significance is None


def test_weekly_recap_with_zero_alerts_still_returns_a_recap():
    conn = connect(":memory:")
    recap = performance.weekly_recap(conn, "2026-08-03T00:00:00+00:00", "2026-08-10T00:00:00+00:00")
    assert recap.sample_size == 0
    assert recap.total_alerts == 0
    assert recap.hit_rate is None


def test_weekly_recap_computes_a_bad_week_exactly_like_a_good_one():
    """The structural guarantee behind 'publishes bad weeks as
    prominently as good ones': the SAME function, SAME fields, SAME
    significance math regardless of sign."""
    conn = connect(":memory:")
    week_start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    week_end = datetime(2026, 8, 10, tzinfo=timezone.utc)
    for i in range(6):  # all losses
        _seed_on(conn, week_start + timedelta(days=1, hours=i), 100, 99)

    recap = performance.weekly_recap(conn, week_start.isoformat(), week_end.isoformat())
    assert recap.hit_rate == 0.0
    assert recap.avg_return_pct < 0
    assert recap.significance is not None  # computed exactly the same as a winning week would be


# ---------------------------------------------------------------------- #
# /example — random_real_win / random_real_day_hit_rate. Randomness is
# WHICH real record gets picked, never a generated result — these tests
# use a seeded rng so the "random" choice is deterministic to assert on.
# ---------------------------------------------------------------------- #


class _FixedChoiceRng:
    """Picks a specific item by identity/position rather than truly at
    random — deterministic tests without needing to seed real random.Random
    and hope for a particular draw."""

    def __init__(self, index=0):
        self.index = index

    def choice(self, seq):
        return seq[self.index]


def test_random_real_win_only_ever_returns_a_real_positive_return():
    conn = connect(":memory:")
    # a real win (up, continued) and a real loss (up, reversed) in the same journal
    _seed_on(conn, BASE, 100, 101, trend="up")  # win: +1%
    _seed_on(conn, BASE + timedelta(minutes=5), 100, 99, trend="up")  # loss: -1%

    win = performance.random_real_win(conn)
    assert win is not None
    assert win.return_pct > 0
    assert win.trend == "up"


def test_random_real_win_none_when_the_journal_has_no_win_yet():
    conn = connect(":memory:")
    _seed_on(conn, BASE, 100, 99, trend="up")  # only a loss on record
    assert performance.random_real_win(conn) is None


def test_random_real_win_picks_among_multiple_real_wins():
    conn = connect(":memory:")
    _seed_on(conn, BASE, 100, 101, trend="up", symbol="AAA")
    _seed_on(conn, BASE + timedelta(minutes=5), 100, 102, trend="up", symbol="BBB")

    # notable_percentile=0 -> the whole pool, not just the top slice —
    # isolates "does choice actually vary" from the filtering behavior,
    # which gets its own test below.
    first = performance.random_real_win(conn, rng=_FixedChoiceRng(0), notable_percentile=0)
    second = performance.random_real_win(conn, rng=_FixedChoiceRng(1), notable_percentile=0)
    assert {first.symbol, second.symbol} == {"AAA", "BBB"}


def test_random_real_win_restricts_to_the_notable_top_slice_by_default():
    """Regression for the actual point of the feature: most real wins
    are small (this journal's real median is under 1%), so a uniform
    sample mostly shows unremarkable ones. The default pool must be
    restricted to genuinely bigger real wins, not just any real win —
    see performance.random_real_win's docstring for why disclosing this
    (render_example's "notable, not typical" framing) makes it honest
    curation rather than a silent cherry-pick."""
    conn = connect(":memory:")
    # 9 small real wins (+0.1%) and 1 much bigger real win (+5%) — with
    # notable_percentile=90 (the default), only the top ~10% should ever
    # be eligible, i.e. only the +5% one.
    for i in range(9):
        _seed_on(conn, BASE + timedelta(minutes=5 * i), 100.0, 100.1, trend="up", symbol=f"SMALL{i}")
    _seed_on(conn, BASE + timedelta(minutes=100), 100.0, 105.0, trend="up", symbol="BIG")

    for _ in range(10):
        win = performance.random_real_win(conn)
        assert win.symbol == "BIG"


def test_random_real_day_hit_rate_requires_min_sample_per_day():
    conn = connect(":memory:")
    # only 2 tracked alerts this session -> below MIN_HISTORY_SAMPLE (5)
    _seed_on(conn, BASE, 100, 101)
    _seed_on(conn, BASE + timedelta(minutes=5), 100, 99)
    assert performance.random_real_day_hit_rate(conn) is None


def test_random_real_day_hit_rate_on_a_real_session_with_enough_samples():
    conn = connect(":memory:")
    for i in range(6):
        _seed_on(conn, BASE + timedelta(minutes=5 * i), 100, 101 if i < 4 else 99)
    day = performance.random_real_day_hit_rate(conn)
    assert day is not None
    assert day.sample_size == 6
    assert day.hit_rate == pytest.approx(4 / 6)
    assert day.session == BASE.date().isoformat()


# ---------------------------------------------------------------------- #
# public_alert_history (docs/phase4-proof-engine-proposal.md, Part A/B).
# Reads two databases: journal.db for detections/marks, users.db's
# outbox for the real, verifiable send timestamp -- these tests seed
# both, since the whole point of this function is the join between them.
# ---------------------------------------------------------------------- #


def _seed_delivered(users_conn, alert_id: str, when: datetime, chat_id: int = 12345) -> None:
    outbox.enqueue_broadcast(users_conn, alert_id, [(chat_id, "text", None)], outbox.PRIORITY_HIGH, now=when)
    row_id = users_conn.execute("SELECT id FROM outbox WHERE alert_id = ? AND chat_id = ?", (alert_id, chat_id)).fetchone()[0]
    outbox.mark_delivered(users_conn, row_id, when)


def test_public_alert_history_excludes_alerts_with_no_delivered_outbox_row():
    """The core append-only property: alerted=1 alone (a replay run, or
    a send still in flight) is not enough to appear -- only a real,
    confirmed delivery is."""
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    did = write_cluster(
        jconn, session="2026-06-15", symbol="TEST", ts_utc=BASE.isoformat(), kinds="gap", headlines="h",
        score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("TEST", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    jconn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, 101.0))
    jconn.commit()
    # no outbox row enqueued at all for this alert_id

    rows = performance.public_alert_history(jconn, uconn)
    assert rows == []


def test_public_alert_history_excludes_unalerted_detections_even_if_outbox_somehow_has_a_row():
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    did = write_cluster(
        jconn, session="2026-06-15", symbol="TEST", ts_utc=BASE.isoformat(), kinds="gap", headlines="h",
        score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("TEST", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=False,
    )
    jconn.commit()
    _seed_delivered(uconn, did, BASE)

    assert performance.public_alert_history(jconn, uconn) == []


def test_public_alert_history_includes_a_real_delivered_alert_with_the_outbox_timestamp():
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    did = write_cluster(
        jconn, session="2026-06-15", symbol="TEST", ts_utc=BASE.isoformat(), kinds="gap", headlines="TEST gapped up",
        score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("TEST", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    jconn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, 101.0))
    jconn.commit()
    delivered_at = BASE + timedelta(seconds=3)
    _seed_delivered(uconn, did, delivered_at)

    rows = performance.public_alert_history(jconn, uconn)

    assert len(rows) == 1
    row = rows[0]
    assert row.detection_id == did
    assert row.symbol == "TEST"
    assert row.headline == "TEST gapped up"
    assert row.sent_at == delivered_at.isoformat()  # the real outbox time, not detection ts_utc
    assert row.tracked is True
    assert row.return_pct == pytest.approx(1.0)  # (101-100)/100*100, uptrend
    assert row.origin == "watchlist"


def test_public_alert_history_pending_row_shown_before_grading_not_hidden():
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    did = write_cluster(
        jconn, session="2026-06-15", symbol="TEST", ts_utc=BASE.isoformat(), kinds="gap", headlines="h",
        score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("TEST", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    jconn.commit()  # no mark written yet -- not graded
    _seed_delivered(uconn, did, BASE)

    rows = performance.public_alert_history(jconn, uconn)

    assert len(rows) == 1
    assert rows[0].tracked is False
    assert rows[0].return_pct is None


def test_public_alert_history_uses_the_earliest_delivery_across_multiple_recipients():
    """An alert_id can have multiple outbox rows (ops channel + N
    subscriber DMs, see delivery.make_subscriber_hook) -- sent_at is the
    earliest real delivery across all of them, not an arbitrary one."""
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    did = write_cluster(
        jconn, session="2026-06-15", symbol="TEST", ts_utc=BASE.isoformat(), kinds="gap", headlines="h",
        score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("TEST", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    jconn.commit()
    _seed_delivered(uconn, did, BASE + timedelta(seconds=5), chat_id=111)  # ops channel, delivered later
    _seed_delivered(uconn, did, BASE + timedelta(seconds=1), chat_id=222)  # a subscriber, delivered first

    rows = performance.public_alert_history(jconn, uconn)

    assert rows[0].sent_at == (BASE + timedelta(seconds=1)).isoformat()


def test_public_alert_history_sorted_newest_sent_first():
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    ids = []
    for i in range(3):
        did = write_cluster(
            jconn, session="2026-06-15", symbol=f"SYM{i}", ts_utc=(BASE + timedelta(minutes=i)).isoformat(),
            kinds="gap", headlines="h", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection(f"SYM{i}", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        ids.append(did)
    jconn.commit()
    # deliver out of detection order
    _seed_delivered(uconn, ids[0], BASE + timedelta(minutes=10))
    _seed_delivered(uconn, ids[1], BASE + timedelta(minutes=30))
    _seed_delivered(uconn, ids[2], BASE + timedelta(minutes=20))

    rows = performance.public_alert_history(jconn, uconn)

    assert [r.symbol for r in rows] == ["SYM1", "SYM2", "SYM0"]


def test_public_alert_history_respects_since_until_and_limit():
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    for i in range(5):
        ts = BASE + timedelta(days=i)
        did = write_cluster(
            jconn, session="2026-06-15", symbol=f"SYM{i}", ts_utc=ts.isoformat(),
            kinds="gap", headlines="h", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection(f"SYM{i}", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        jconn.commit()
        _seed_delivered(uconn, did, ts)

    since = (BASE + timedelta(days=1)).isoformat()
    until = (BASE + timedelta(days=4)).isoformat()
    rows = performance.public_alert_history(jconn, uconn, since=since, until=until)
    assert {r.symbol for r in rows} == {"SYM1", "SYM2", "SYM3"}

    limited = performance.public_alert_history(jconn, uconn, limit=2)
    assert len(limited) == 2
    assert limited[0].symbol == "SYM4"  # newest sent first, still respected under limit


def test_public_alert_history_origin_defaults_to_watchlist_and_screening_is_visible():
    jconn = connect(":memory:")
    uconn = users_db.connect(":memory:")
    screening_id = write_cluster(
        jconn, session="2026-06-15", symbol="RADAR", ts_utc=BASE.isoformat(), kinds="gap", headlines="h",
        score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("RADAR", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
        origin="screening",
    )
    jconn.commit()
    _seed_delivered(uconn, screening_id, BASE)

    rows = performance.public_alert_history(jconn, uconn)

    assert len(rows) == 1
    assert rows[0].origin == "screening"
