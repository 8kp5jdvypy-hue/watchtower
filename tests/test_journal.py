"""Tests for tradebot.journal."""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Detection
from tradebot.journal import (
    CLOSE_MARK_OFFSET_MIN,
    MIN_HISTORY_SAMPLE,
    backfill_marks,
    cluster_id,
    code_version,
    connect,
    detected_symbols_for_session,
    historical_performance,
    hour_performance,
    iv_rank,
    kind_performance,
    get_contract_outcome,
    pending_contract_backfills,
    pending_contract_close_backfills,
    pending_contract_day_range_backfills,
    record_contract_day_range,
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


def test_write_cluster_data_feed_and_origin_round_trip_and_default(tmp_path):
    """data_feed defaults to None (pre-Decision-B callers/tests never pass
    it -- see write_cluster's docstring); origin defaults to 'watchlist'
    (every caller before broad_scan promotion existed). Both round-trip
    when a caller does pass them."""
    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="level_break", headlines="h", score=4.0, close=101.0, atr14=1.5,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )
    write_cluster(
        conn, session="2026-06-15", symbol="AAPL", ts_utc="2026-06-15T14:05:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=101.0, atr14=1.5,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc123",
        data_feed="sip", origin="screening",
    )
    conn.commit()
    default_row = conn.execute("SELECT data_feed, origin FROM detections WHERE symbol = ?", (SYMBOL,)).fetchone()
    assert default_row == (None, "watchlist")
    explicit_row = conn.execute("SELECT data_feed, origin FROM detections WHERE symbol = 'AAPL'").fetchone()
    assert explicit_row == ("sip", "screening")


def test_write_cluster_stores_primary_kind_and_freezes_symbol_class_at_write_time(tmp_path):
    """symbol_class (deep/thin, see tradebot.config.liquidity_class) is
    derived and frozen here, not looked up later — a historical row keeps
    reporting what was true when the alert fired even if the watchlist's
    classification changes in the future."""
    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session="2026-06-15", symbol="AAPL", ts_utc="2026-06-15T14:00:00+00:00",
        kinds="level_break,rvol_spike", headlines="h", score=4.0, close=101.0, atr14=1.5,
        trend="up", detections=[_detection()], code_version_str="abc123", primary_kind="rvol_spike",
    )
    write_cluster(
        conn, session="2026-06-15", symbol="BE", ts_utc="2026-06-15T14:05:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=5.0, atr14=0.3,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc123", primary_kind="gap",
    )
    conn.commit()
    rows = dict(conn.execute("SELECT symbol, symbol_class FROM detections").fetchall())
    assert rows == {"AAPL": "deep", "BE": "thin"}
    primary_kind = conn.execute("SELECT primary_kind FROM detections WHERE symbol = 'AAPL'").fetchone()[0]
    assert primary_kind == "rvol_spike"  # the primary, not the full multi-kind list


def test_suppress_category_lifecycle_state_and_related_detection_id_round_trip(tmp_path):
    """These three columns are written via UPDATE after write_cluster (same
    pattern as suppress_reason itself) — this just confirms the columns
    exist and round-trip, and that a fresh row leaves them NULL until
    something updates them, same discipline as primary_kind/symbol_class
    on rows written before those columns existed."""
    conn = connect(tmp_path / "journal.db")
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="level_break", headlines="h", score=4.0, close=101.0, atr14=1.5,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )
    conn.commit()
    row = conn.execute(
        "SELECT suppress_category, lifecycle_state, related_detection_id FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    assert row == (None, None, None)

    conn.execute(
        "UPDATE detections SET suppress_category=?, lifecycle_state=?, related_detection_id=? WHERE id=?",
        ("duplicate", "confirmed", "some-other-id", detection_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT suppress_category, lifecycle_state, related_detection_id FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    assert row == ("duplicate", "confirmed", "some-other-id")


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


def test_set_news_driven_records_kind_and_severity(tmp_path):
    from tradebot.journal import set_news_driven

    conn = connect(tmp_path / "journal.db")
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc", primary_kind="gap",
    )
    conn.commit()
    set_news_driven(conn, detection_id, True, kind="earnings", severity="suppress")
    row = conn.execute(
        "SELECT news_driven, event_kind, event_severity FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()
    assert row == (1, "earnings", "suppress")


def test_set_extreme_mover_records_gap_pct_and_verified_volume(tmp_path):
    from tradebot.journal import set_extreme_mover

    conn = connect(tmp_path / "journal.db")
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc", primary_kind="gap",
    )
    conn.commit()
    set_extreme_mover(conn, detection_id, gap_pct=0.488, verified_volume=412_345)
    row = conn.execute(
        "SELECT extreme_mover, extreme_mover_gap_pct, extreme_mover_volume FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    assert row == (1, 0.488, 412_345)


def test_extreme_mover_stays_null_when_never_set(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc", primary_kind="gap",
    )
    conn.commit()
    row = conn.execute(
        "SELECT extreme_mover, extreme_mover_gap_pct, extreme_mover_volume FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    assert row == (None, None, None)


def test_cluster_id_is_deterministic():
    a = cluster_id("SPY", "2026-06-15", "2026-06-15T14:00:00+00:00", "gap")
    b = cluster_id("SPY", "2026-06-15", "2026-06-15T14:00:00+00:00", "gap")
    c = cluster_id("SPY", "2026-06-15", "2026-06-15T14:05:00+00:00", "gap")
    assert a == b
    assert a != c


def test_code_version_prefers_git_sha_env_var(monkeypatch):
    """2026-08-12: the deployed container has no .git, so the subprocess
    fallback always returned 'unknown' in production -- GIT_SHA is now
    baked in at build time (Dockerfile ARG/ENV, docker-compose.yml's
    build.args) and must be checked first."""
    monkeypatch.setenv("GIT_SHA", "abc1234")
    assert code_version() == "abc1234"


def test_code_version_falls_back_to_git_subprocess_when_env_var_unset_or_unknown(monkeypatch):
    """Local dev: GIT_SHA is normally unset, but a real .git directory
    is -- must still resolve a real hash, not silently prefer a literal
    'unknown' over a real subprocess result."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    version = code_version()
    assert version != ""  # this repo has a real .git; some real value comes back

    monkeypatch.setenv("GIT_SHA", "unknown")  # the Dockerfile's own default value
    assert code_version() == version  # must fall through to the same real subprocess result, not "unknown"


def test_detected_symbols_for_session_covers_watchlist_and_screening_alike(tmp_path):
    """2026-08-12: the manual emergency backfill only covered WATCHLIST,
    leaving screening-origin detections with 0 marks. This is the single
    source of truth the permanent fix (runner.py's close-time cache
    fetch) and backfill_marks() itself both use, so their symbol scope
    can never drift apart."""
    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session=SESSION.isoformat(), symbol="AAPL", ts_utc="2026-06-15T14:00:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc", origin="watchlist",
    )
    write_cluster(
        conn, session=SESSION.isoformat(), symbol="FDRX", ts_utc="2026-06-15T14:05:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=10.0, atr14=0.5,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc", origin="screening",
    )
    # A different session -- must not appear.
    write_cluster(
        conn, session="2026-06-16", symbol="TSLA", ts_utc="2026-06-16T14:00:00+00:00",
        kinds="gap", headlines="h", score=4.0, close=200.0, atr14=2.0,
        trend="up", detections=[_detection(kind="gap")], code_version_str="abc",
    )
    conn.commit()

    assert detected_symbols_for_session(conn, SESSION) == ["AAPL", "FDRX"]


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
    assert CLOSE_MARK_OFFSET_MIN in marks  # the close mark is unconditional, regardless of offsets_min
    assert marks[CLOSE_MARK_OFFSET_MIN] == 107  # the session's real last bar close (100 + 7)


def test_backfill_marks_logs_an_error_when_the_intraday_cache_file_is_absent(tmp_path, caplog):
    """2026-08-12 incident shape: the session's own intraday cache file
    never existed at backfill time (daily.csv is present -- only the
    session file is missing). Pre-fix, this produced 0 marks with zero
    signal anywhere that anything was wrong. Written 0 is still correct
    behavior here (never fabricate a price) -- what must change is that
    it's no longer silent."""
    cache_dir = tmp_path / "cache"
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    _write_bar_csv(cache_dir / SYMBOL / "daily.csv", [_bar_row(rth_open - timedelta(days=1), 99)])
    # No intraday_{SESSION}.csv written at all.

    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session=SESSION.isoformat(), symbol=SYMBOL, ts_utc=rth_open.isoformat(),
        kinds="gap", headlines="gapped up", score=2.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )
    conn.commit()

    with caplog.at_level("ERROR", logger="watchtower.journal"):
        written = backfill_marks(conn, SESSION, cache_dir=cache_dir)

    assert written == 0  # correct -- there is genuinely nothing to write
    assert len(caplog.records) == 1  # but it must not be silent
    assert caplog.records[0].levelname == "ERROR"
    assert SYMBOL in caplog.records[0].message
    assert SESSION.isoformat() in caplog.records[0].message


def test_backfill_marks_does_not_log_when_the_cache_file_exists_but_has_nothing_new(tmp_path, caplog):
    """The ordinary, non-incident quiet case -- a real cache file that
    genuinely has no bars past a detection's own timestamp -- must stay
    quiet. Only a missing FILE is the incident shape; an empty result
    from a real file is normal, expected behavior."""
    cache_dir = tmp_path / "cache"
    rth_open = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    # Real file, but its one bar is AT the detection's own timestamp --
    # nothing forward of it, so every offset legitimately finds nothing.
    _write_bar_csv(cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv", [_bar_row(rth_open, 100)])
    _write_bar_csv(cache_dir / SYMBOL / "daily.csv", [_bar_row(rth_open - timedelta(days=1), 99)])

    conn = connect(tmp_path / "journal.db")
    write_cluster(
        conn, session=SESSION.isoformat(), symbol=SYMBOL, ts_utc=rth_open.isoformat(),
        kinds="gap", headlines="gapped up", score=2.0, close=100.0, atr14=1.0,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )
    conn.commit()

    with caplog.at_level("ERROR", logger="watchtower.journal"):
        backfill_marks(conn, SESSION, cache_dir=cache_dir)

    assert caplog.records == []


def test_backfill_marks_default_offsets_are_15_30_60_and_close(tmp_path):
    """15m/30m/60m/close is the fixed, non-curated outcome checkpoint set
    — every published alert gets exactly these, automatically."""
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

    assert set(marks) == {15, 30, CLOSE_MARK_OFFSET_MIN}  # 60 isn't reached, 5 is no longer a checkpoint
    assert marks[15] == 102  # first bar closing at/after rth_open + 15m


def _write_cluster_with_mark(conn, kind, trend, close, price_at_30, ts_utc, score=4.0, data_feed="iex", origin="watchlist"):
    detection_id = write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc=ts_utc,
        kinds=kind, headlines="h", score=score, close=close, atr14=1.0,
        trend=trend, detections=[_detection(kind=kind)], code_version_str="abc",
        primary_kind=kind, data_feed=data_feed, origin=origin,
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


def test_historical_performance_avg_return_pct_is_trend_signed_not_raw(tmp_path):
    """Regression for the 2026-08-12 sign-convention bug: avg_return_pct
    must be flipped to the detection's own trend, same convention
    tier_performance()/kind_performance() already use -- a down-trend
    continuation (price fell further) must report POSITIVE here, not the
    raw negative price change. Mirrors the up-trend test above with the
    same numbers so the fix's effect is directly comparable: same 5
    marks, opposite trend, and continuation_rate/avg_return_pct both
    invert relative to it."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # down-trend gap clusters at close=100: 2 continue down, 3 reverse up
    # (the same raw marks as the up-trend test, opposite trend call)
    closes_marks = [105, 102, 98, 101, 97]
    for i, mark in enumerate(closes_marks):
        _write_cluster_with_mark(
            conn, kind="gap", trend="down", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )

    result = historical_performance(conn, kind="gap", trend="down", exclude_id="nonexistent")
    assert result is not None
    assert result.continuation_rate == pytest.approx(0.4)  # 98,97 < 100 continue the down call; 105,102,101 reverse
    # Only 2 of 5 continued the down call (matches continuation_rate=0.4
    # above) -- signed to "down", the per-row returns are
    # [-5,-2,+2,-1,+3]% (each raw % negated), mean -0.6%. Same magnitude
    # as the up-trend test's +0.6% (same 5 raw marks), opposite sign --
    # this trend's net effect really was negative, and now says so
    # consistently with tier_performance()'s own convention for the
    # identical rows, instead of disagreeing with it.
    assert result.avg_return_pct == pytest.approx(-0.6)


def test_historical_performance_avg_return_atr_is_signed_and_agrees_with_pct(tmp_path):
    """Regression for design review H5: avg_return_atr was an abs() mean
    (always positive), so the UI could print "avg follow-through 0.07%
    (~-0.15x ATR)" -- the same statistic with two opposite signs. It now
    uses the SAME signed, trend-flipped per-entry convention as
    avg_return_pct, so the two figures always agree in sign. atr14=1.0
    in the helper, so the ATR mean is just the trend-signed mean point
    move."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    closes_marks = [105, 102, 98, 101, 97]
    for trend, expected_atr_mean in (("up", 0.6), ("down", -0.6)):
        kind = f"gap_{trend}"
        for i, mark in enumerate(closes_marks):
            _write_cluster_with_mark(
                conn, kind=kind, trend=trend, close=100.0, price_at_30=mark,
                ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            )
        result = historical_performance(conn, kind=kind, trend=trend, exclude_id="nonexistent")
        assert result is not None
        # signed mean: [+5,+2,-2,+1,-3]/1.0 for up, negated for down
        assert result.avg_return_atr == pytest.approx(expected_atr_mean)
        # the H5 invariant itself: both representations share a sign
        assert (result.avg_return_atr > 0) == (result.avg_return_pct > 0)


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


def test_historical_performance_matches_primary_kind_exactly_not_as_a_kinds_substring(tmp_path):
    """Regression: the old filter was `d.kinds LIKE '%kind%'` against the
    full multi-detector kinds string, so a cluster where 'gap' fired as a
    SECONDARY detector (not the primary/headline one) would still count
    as a 'gap' setup. Real primary_kind must be an exact match instead."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i in range(MIN_HISTORY_SAMPLE):
        detection_id = write_cluster(
            conn, session="2026-06-15", symbol=SYMBOL, ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            kinds="gap,level_break", headlines="h", score=4.0, close=100.0, atr14=1.0,
            trend="up", detections=[_detection(kind="gap")], code_version_str="abc",
            primary_kind="level_break",  # gap fired too, but wasn't primary
            data_feed="iex",
        )
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, 105))
    conn.commit()
    assert historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent") is None
    result = historical_performance(conn, kind="level_break", trend="up", exclude_id="nonexistent")
    assert result is not None and result.sample_size == MIN_HISTORY_SAMPLE


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


def test_historical_performance_excludes_a_different_feed_and_screening_origin(tmp_path):
    """Same CURRENT_FEED_FILTER_SQL discipline as tier_performance() --
    the modal's Similar Setups sample must draw from the same population
    those stats do, not a separately-filtered one."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i, mark in enumerate([105, 102, 101, 103, 104]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(hours=1, minutes=5 * i)).isoformat(), data_feed="sip", origin="watchlist",
        )
    # old-feed row: must not count even though kind/trend match
    _write_cluster_with_mark(
        conn, kind="gap", trend="up", close=100.0, price_at_30=999,
        ts_utc=base.isoformat(), data_feed="iex", origin="watchlist",
    )
    # screening-origin row on the current feed: must not count either
    _write_cluster_with_mark(
        conn, kind="gap", trend="up", close=100.0, price_at_30=999,
        ts_utc=(base + timedelta(hours=1, minutes=100)).isoformat(), data_feed="sip", origin="screening",
    )

    result = historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent", lookback=20)
    assert result.sample_size == 5


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


def test_tier_performance_excludes_rows_from_a_different_data_feed(tmp_path):
    """Decision B, Option 1 (docs/sip-migration-proposal.md): only the
    CURRENTLY-live feed's rows count -- 'current' meaning the most recent
    row's own data_feed, not a hardcoded value, so this stays correct
    whichever feed happens to be live."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # 5 old IEX-era rows -- would clear MIN_HISTORY_SAMPLE on their own
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(), score=5.0, data_feed="iex",
        )
    # 5 newer SIP-era rows, mixed outcomes, written later (later ts_utc) --
    # this is what "current feed" resolves to.
    for i, mark in enumerate([101, 99, 102, 98, 97]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(hours=1, minutes=5 * i)).isoformat(), score=5.0, data_feed="sip",
        )

    result = tier_performance(conn)
    assert result["high"].sample_size == 5  # only the SIP rows
    assert result["high"].continuation_rate == pytest.approx(0.4)  # 101,102 > 100; 99,98,97 < 100


def test_tier_performance_excludes_pre_migration_rows_with_null_data_feed(tmp_path):
    """Every row written before this column existed has data_feed IS NULL,
    which must never match the current feed value -- see
    CURRENT_FEED_FILTER_SQL's docstring ('resets to n=0' is intentional,
    not a bug)."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        detection_id = write_cluster(
            conn, session="2026-06-15", symbol=SYMBOL, ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            kinds="gap", headlines="h", score=5.0, close=100.0, atr14=1.0,
            trend="up", detections=[_detection(kind="gap")], code_version_str="abc", primary_kind="gap",
            # data_feed intentionally omitted -- simulates a pre-migration row.
        )
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, mark))
    conn.commit()
    assert tier_performance(conn) == {}


def test_tier_performance_excludes_broad_scan_promoted_screening_symbols(tmp_path):
    """docs/broad-scan-honesty-proposal.md finding (b): a symbol promoted
    in by broad_scan for one session isn't part of the watchlist's
    historical track record."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(), score=5.0, origin="watchlist",
        )
    for i, mark in enumerate([50, 40, 60, 45, 55]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(hours=1, minutes=5 * i)).isoformat(), score=5.0, origin="screening",
        )

    result = tier_performance(conn)
    assert result["high"].sample_size == 5  # only the watchlist rows
    assert result["high"].continuation_rate == pytest.approx(1.0)  # all 5 watchlist rows continued


def test_kind_performance_groups_by_kind_and_computes_real_stats(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)

    # 5 "gap", all up-trend, all continue (mark > close)
    for i, mark in enumerate([105, 106, 104, 103, 107]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    # 5 "level_break", mixed outcomes: 2 continue, 3 reverse
    for i, mark in enumerate([101, 102, 99, 98, 97]):
        _write_cluster_with_mark(
            conn, kind="level_break", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=100 + 5 * i)).isoformat(), score=2.0,
        )

    result = kind_performance(conn)
    assert result["gap"].sample_size == 5
    assert result["gap"].continuation_rate == pytest.approx(1.0)
    assert result["level_break"].sample_size == 5
    assert result["level_break"].continuation_rate == pytest.approx(0.4)  # 2/5 continued


def test_kind_performance_omits_kinds_below_min_sample(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert MIN_HISTORY_SAMPLE == 5
    for i in range(MIN_HISTORY_SAMPLE - 1):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=105,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(), score=5.0,
        )
    result = kind_performance(conn)
    assert "gap" not in result


def test_kind_performance_excludes_news_driven_rows_and_reports_the_count(tmp_path):
    """Mirrors test_historical_performance_excludes_news_driven_rows_from_
    the_sample -- kind_performance() and the modal's per-signal stats must
    report on the same population (see kind_performance()'s docstring)."""
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i, mark in enumerate([105, 102, 101, 103, 104]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )
    news_ids = [
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=100 + 5 * i)).isoformat(),
        )
        for i, mark in enumerate([50, 40])
    ]
    for news_id in news_ids:
        conn.execute("UPDATE detections SET news_driven=1 WHERE id=?", (news_id,))
    conn.commit()

    result = kind_performance(conn)
    assert result["gap"].sample_size == 5  # news-driven rows excluded, not just down-weighted
    assert result["gap"].excluded_news_driven == 2


def test_kind_performance_computes_median_and_mean_separately(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    # signed returns (all up-trend, so signed == raw %): -1, -1, -1, -1, +20 --
    # a single outlier that pulls the mean up hard without moving the median.
    for i, mark in enumerate([99, 99, 99, 99, 120]):
        _write_cluster_with_mark(
            conn, kind="gap", trend="up", close=100.0, price_at_30=mark,
            ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
        )
    result = kind_performance(conn)
    assert result["gap"].avg_return_pct == pytest.approx(3.2)  # mean of [-1,-1,-1,-1,+20]
    assert result["gap"].median_return_pct == pytest.approx(-1.0)  # median of the same set


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
            trend="up", detections=[_detection(kind="gap")], code_version_str="abc", primary_kind="gap",
            data_feed="iex",
        )
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (detection_id, mark))
    conn.commit()

    result = historical_performance(conn, kind="gap", trend="up", exclude_id="nonexistent")
    # signed convention (see the H5 sign-agreement test above): up-trend
    # rows contribute (mark - close)/atr14 as-is, so the 100->99 reversal
    # counts NEGATIVE instead of abs()'s +1.0
    expected = sum((m - c) / a for c, m, a in rows) / len(rows)
    assert result.avg_return_atr == pytest.approx(expected)


def test_historical_performance_avg_return_atr_is_none_when_no_row_has_atr14(tmp_path):
    conn = connect(tmp_path / "journal.db")
    base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    for i in range(MIN_HISTORY_SAMPLE):
        detection_id = write_cluster(
            conn, session="2026-06-15", symbol=SYMBOL, ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            kinds="gap", headlines="h", score=4.0, close=100.0, atr14=None,
            trend="up", detections=[_detection(kind="gap")], code_version_str="abc", primary_kind="gap",
            data_feed="iex",
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
    detection_id = write_cluster(
        conn, session="2026-07-23", symbol="GOOGL", ts_utc=entry_ts.isoformat(),
        kinds="level_break", headlines="h", score=10.0, close=366.0, atr14=1.5,
        trend="down", detections=[_detection(kind="level_break")], code_version_str="abc", primary_kind="level_break",
    )
    conn.commit()
    record_contract_selection(
        conn, detection_id, symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13,
        delta=-0.47, entry_mid=4.20, entry_ts=entry_ts,
    )
    row = conn.execute("SELECT symbol, right, strike, dte, delta, entry_mid FROM contract_selections WHERE detection_id = ?", (detection_id,)).fetchone()
    assert row == ("GOOGL", "put", 365.0, 13, -0.47, 4.20)

    pending = pending_contract_backfills(conn, entry_ts + timedelta(minutes=31), offset_min=30)
    assert pending == [(detection_id, "GOOGL", "put", 365.0, "2026-08-14", 0, None)]

    record_contract_forward_mid(conn, detection_id, 30, 4.05)
    still_pending = pending_contract_backfills(conn, entry_ts + timedelta(minutes=31), offset_min=30)
    assert still_pending == []
    mid_30 = conn.execute("SELECT mid_30m FROM contract_selections WHERE detection_id = ?", (detection_id,)).fetchone()[0]
    assert mid_30 == 4.05

    # close mid: due once the session has ended, not at a fixed offset
    close_pending = pending_contract_close_backfills(conn, date(2026, 7, 23))
    assert close_pending == [(detection_id, "GOOGL", "put", 365.0, "2026-08-14", 0, None)]
    record_contract_forward_mid(conn, detection_id, CLOSE_MARK_OFFSET_MIN, 3.90)
    assert pending_contract_close_backfills(conn, date(2026, 7, 23)) == []
    mid_close = conn.execute("SELECT mid_close FROM contract_selections WHERE detection_id = ?", (detection_id,)).fetchone()[0]
    assert mid_close == 3.90


def test_record_contract_selection_is_idempotent_on_rerun(tmp_path):
    conn = connect(tmp_path / "journal.db")
    entry_ts = datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc)
    record_contract_selection(conn, "det1", symbol="GOOGL", right="put", strike=365.0, expiry=date(2026, 8, 14), dte=13, delta=-0.47, entry_mid=4.20, entry_ts=entry_ts)
    record_contract_selection(conn, "det1", symbol="GOOGL", right="put", strike=370.0, expiry=date(2026, 8, 14), dte=13, delta=-0.30, entry_mid=6.00, entry_ts=entry_ts)
    count = conn.execute("SELECT COUNT(*) FROM contract_selections").fetchone()[0]
    assert count == 1
    strike = conn.execute("SELECT strike FROM contract_selections WHERE detection_id = ?", ("det1",)).fetchone()[0]
    assert strike == 370.0


# ---------------------------------------------------------------------- #
# Contract day range — see runner.backfill_contract_day_ranges /
# templates.render_contract_outcome for how this gets fetched and shown.
# ---------------------------------------------------------------------- #


def test_record_and_fetch_contract_day_range(tmp_path):
    conn = connect(tmp_path / "journal.db")
    entry_ts = datetime(2026, 4, 8, 16, 5, tzinfo=timezone.utc)
    detection_id = write_cluster(
        conn, session="2026-04-08", symbol="META", ts_utc=entry_ts.isoformat(),
        kinds="vwap_break", headlines="h", score=4.37, close=599.82, atr14=1.815,
        trend="up", detections=[_detection(kind="vwap_break")], code_version_str="abc", primary_kind="vwap_break",
    )
    conn.commit()
    record_contract_selection(
        conn, detection_id, symbol="META", right="call", strike=600.0, expiry=date(2026, 4, 17), dte=9,
        delta=0.45, entry_mid=2.96, entry_ts=entry_ts,
    )

    pending = pending_contract_day_range_backfills(conn, date(2026, 4, 8))
    assert pending == [(detection_id, "META", "call", 600.0, "2026-04-17")]

    record_contract_day_range(conn, detection_id, 1.43, 3.90)
    assert pending_contract_day_range_backfills(conn, date(2026, 4, 8)) == []

    outcome = get_contract_outcome(conn, detection_id)
    assert outcome.day_low == 1.43
    assert outcome.day_high == 3.90
    assert outcome.entry_mid == 2.96
    assert outcome.mid_30m is None  # never fabricated — not backfilled in this test


def test_pending_contract_day_range_backfills_excludes_verticals(tmp_path):
    """A spread's day range isn't the sum of its legs' independent
    ranges — see the function's own docstring for why this is a
    deliberate exclusion, not a gap."""
    conn = connect(tmp_path / "journal.db")
    entry_ts = datetime(2026, 4, 8, 16, 5, tzinfo=timezone.utc)
    detection_id = write_cluster(
        conn, session="2026-04-08", symbol="META", ts_utc=entry_ts.isoformat(),
        kinds="vwap_break", headlines="h", score=4.37, close=599.82, atr14=1.815,
        trend="up", detections=[_detection(kind="vwap_break")], code_version_str="abc", primary_kind="vwap_break",
    )
    conn.commit()
    record_contract_selection(
        conn, detection_id, symbol="META", right="call", strike=600.0, expiry=date(2026, 4, 17), dte=9,
        delta=0.45, entry_mid=1.50, entry_ts=entry_ts, is_vertical=True, short_strike=610.0, short_delta=0.20,
    )
    assert pending_contract_day_range_backfills(conn, date(2026, 4, 8)) == []


def test_get_contract_outcome_returns_none_without_a_selection(tmp_path):
    conn = connect(tmp_path / "journal.db")
    assert get_contract_outcome(conn, "nonexistent") is None
