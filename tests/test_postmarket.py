"""Postmarket shadow evaluator: real reactions plus hostile data shapes."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.detectors import Bar
from tradebot.postmarket import (
    OUTCOME_AWAITING_PERSISTENCE,
    OUTCOME_BAR_GAP,
    OUTCOME_BELOW_MOVE,
    OUTCOME_BELOW_NOTIONAL,
    OUTCOME_CANDIDATE,
    OUTCOME_DUPLICATE_TIMESTAMP,
    OUTCOME_MALFORMED_BAR,
    OUTCOME_NO_COMPLETED_POSTMARKET_BAR,
    OUTCOME_NO_RTH_CLOSE,
    OUTCOME_OUT_OF_ORDER,
    OUTCOME_STALE,
    OUTCOME_UNSTABLE_PRINT,
    OUTCOME_ZERO_VOLUME,
    OBSERVER_VERSION,
    connect,
    evaluate_earnings_reaction,
    fetch_error_evaluation,
    record_shadow_tick,
)


SESSION = date(2026, 8, 26)
SESSION_CLOSE = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def test_postmarket_connection_waits_through_brief_writer_overlap(tmp_path):
    conn = connect(tmp_path / "postmarket.db")

    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000


def _bar(ts, close, *, symbol="TEST", volume=1_000_000, open_=None, high=None, low=None):
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    return Bar(symbol, ts, open_, high, low, close, volume)


def _rth(close=100.0, *, symbol="TEST"):
    return [_bar(SESSION_CLOSE - timedelta(minutes=5), close, symbol=symbol)]


def _post(closes, *, volumes=None, start=SESSION_CLOSE, symbol="TEST"):
    volumes = volumes or [1_000_000] * len(closes)
    return [
        _bar(
            start + timedelta(minutes=5 * i), close,
            symbol=symbol, volume=volumes[i],
        )
        for i, close in enumerate(closes)
    ]


def _evaluate(
    closes, *, symbol="TEST", rth_close=100.0, now=None, volumes=None, bars=None,
):
    bars = _post(closes, volumes=volumes, symbol=symbol) if bars is None else bars
    now = now or SESSION_CLOSE + timedelta(minutes=5 * len(bars))
    return evaluate_earnings_reaction(
        symbol, SESSION, _rth(rth_close, symbol=symbol), bars,
        session_close=SESSION_CLOSE, now=now,
    )


@pytest.mark.parametrize(
    "rth_close, closes",
    [
        (206.11, [223.50, 228.00]),  # CRM: first real bar was +8.44%
        (134.87, [153.00, 158.00]),  # OKTA: first real bar was +13.44%
        (189.42, [205.00, 211.12]),  # CRWD: delayed move, eventually +11.46%
        (100.00, [90.00, 88.00]),    # negative earnings reaction
    ],
)
def test_persistent_liquid_reactions_become_candidates(rth_close, closes):
    result = _evaluate(closes, rth_close=rth_close)
    assert result.outcome == OUTCOME_CANDIDATE
    assert abs(result.move_pct) >= 8
    assert result.persistence_bars == 2
    assert result.cumulative_notional >= 100_000


def test_quiet_report_stays_below_move():
    result = _evaluate([103.0, 104.0])
    assert result.outcome == OUTCOME_BELOW_MOVE


def test_one_completed_spike_waits_for_persistence():
    result = _evaluate([110.0])
    assert result.outcome == OUTCOME_AWAITING_PERSISTENCE
    assert result.persistence_bars == 1


def test_forming_second_bar_is_never_used():
    bars = _post([110.0, 112.0])
    result = _evaluate(
        [], bars=bars, now=SESSION_CLOSE + timedelta(minutes=8),
    )
    assert result.outcome == OUTCOME_AWAITING_PERSISTENCE
    assert result.bar.ts == SESSION_CLOSE


def test_single_print_then_reversal_is_not_a_candidate():
    result = _evaluate([120.0, 101.0])
    assert result.outcome == OUTCOME_BELOW_MOVE


def test_zero_volume_in_persistence_window_is_rejected():
    result = _evaluate([110.0, 111.0], volumes=[1_000_000, 0])
    assert result.outcome == OUTCOME_ZERO_VOLUME


def test_gapped_postmarket_series_is_rejected():
    bars = [
        _bar(SESSION_CLOSE, 110.0),
        _bar(SESSION_CLOSE + timedelta(minutes=10), 111.0),
    ]
    result = _evaluate([], bars=bars, now=SESSION_CLOSE + timedelta(minutes=15))
    assert result.outcome == OUTCOME_BAR_GAP


def test_earlier_sparse_trading_does_not_poison_two_fresh_consecutive_bars():
    bars = [
        _bar(SESSION_CLOSE, 101.0),
        _bar(SESSION_CLOSE + timedelta(minutes=20), 110.0),
        _bar(SESSION_CLOSE + timedelta(minutes=25), 111.0),
    ]
    result = _evaluate([], bars=bars, now=SESSION_CLOSE + timedelta(minutes=30))
    assert result.outcome == OUTCOME_CANDIDATE


def test_duplicate_timestamp_is_rejected():
    bars = [_bar(SESSION_CLOSE, 110.0), _bar(SESSION_CLOSE, 111.0)]
    result = _evaluate([], bars=bars, now=SESSION_CLOSE + timedelta(minutes=5))
    assert result.outcome == OUTCOME_DUPLICATE_TIMESTAMP


def test_out_of_order_series_is_rejected():
    bars = [_bar(SESSION_CLOSE + timedelta(minutes=5), 111.0), _bar(SESSION_CLOSE, 110.0)]
    result = _evaluate([], bars=bars, now=SESSION_CLOSE + timedelta(minutes=10))
    assert result.outcome == OUTCOME_OUT_OF_ORDER


def test_stale_latest_bar_is_rejected():
    result = _evaluate(
        [110.0, 111.0], now=SESSION_CLOSE + timedelta(minutes=18),
    )
    assert result.outcome == OUTCOME_STALE


def test_missing_exact_rth_close_is_rejected():
    wrong_rth = [_bar(SESSION_CLOSE - timedelta(minutes=10), 100.0)]
    result = evaluate_earnings_reaction(
        "TEST", SESSION, wrong_rth, _post([110.0, 111.0]),
        session_close=SESSION_CLOSE, now=SESSION_CLOSE + timedelta(minutes=10),
    )
    assert result.outcome == OUTCOME_NO_RTH_CLOSE


def test_no_completed_postmarket_bar_is_explicit():
    result = _evaluate(
        [], bars=_post([110.0]), now=SESSION_CLOSE + timedelta(minutes=4),
    )
    assert result.outcome == OUTCOME_NO_COMPLETED_POSTMARKET_BAR


def test_malformed_ohlc_is_rejected():
    bars = [
        _bar(SESSION_CLOSE, 110.0),
        _bar(SESSION_CLOSE + timedelta(minutes=5), 111.0, high=100.0),
    ]
    result = _evaluate([], bars=bars, now=SESSION_CLOSE + timedelta(minutes=10))
    assert result.outcome == OUTCOME_MALFORMED_BAR


def test_unstable_consecutive_prints_are_rejected():
    result = _evaluate([110.0, 125.0])
    assert result.outcome == OUTCOME_UNSTABLE_PRINT


def test_thin_reaction_is_rejected_by_notional():
    result = _evaluate([110.0, 111.0], volumes=[10, 10])
    assert result.outcome == OUTCOME_BELOW_NOTIONAL


def test_shadow_tick_is_complete_append_only_and_candidate_deduplicated(tmp_path):
    conn = connect(tmp_path / "postmarket.db")
    candidate = _evaluate([110.0, 111.0])
    quiet = _evaluate([101.0, 102.0], symbol="QUIET")
    tick_time = SESSION_CLOSE + timedelta(minutes=10)

    tick1, new1 = record_shadow_tick(
        conn, [candidate, quiet], session=SESSION, tick_utc=tick_time,
        completed_utc=tick_time + timedelta(milliseconds=20),
        run_id="run-1", run_mode="shadow", code_version="abc123",
        data_feed="sip", scheduled_symbols=2, latency_ms=20,
    )
    tick2, new2 = record_shadow_tick(
        conn, [candidate, quiet], session=SESSION,
        tick_utc=tick_time + timedelta(minutes=1), run_id="run-1",
        completed_utc=tick_time + timedelta(minutes=1, milliseconds=18),
        run_mode="shadow", code_version="abc123", data_feed="sip",
        scheduled_symbols=2, latency_ms=18,
    )

    assert tick2 > tick1
    assert (new1, new2) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM postmarket_observations").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM postmarket_candidates").fetchone()[0] == 1
    assert conn.execute(
        "SELECT invariant_ok, candidate_observations, new_candidates FROM postmarket_ticks WHERE tick_id=?",
        (tick1,),
    ).fetchone() == (1, 1, 1)
    assert conn.execute(
        "SELECT data_feed,market_data_provider,bar_timeframe,catalyst_source "
        "FROM postmarket_observations WHERE tick_id=? AND symbol='TEST'",
        (tick1,),
    ).fetchone() == ("sip", "alpaca", "5Min", "nasdaq_earnings")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_ticks SET invariant_ok=0")


def test_fetch_error_is_recordable_and_breaks_conservation_only_if_missing(tmp_path):
    conn = connect(tmp_path / "postmarket.db")
    evaluation = fetch_error_evaluation("CRM", SESSION, RuntimeError("vendor down"))
    tick_id, new = record_shadow_tick(
        conn, [evaluation], session=SESSION, tick_utc=SESSION_CLOSE,
        completed_utc=SESSION_CLOSE,
        run_id="run-1", run_mode="shadow", code_version="abc123",
        data_feed="sip", scheduled_symbols=1,
    )
    assert new == 0
    assert conn.execute(
        "SELECT invariant_ok,error_count FROM postmarket_ticks WHERE tick_id=?", (tick_id,)
    ).fetchone() == (1, 1)
    assert "RuntimeError: vendor down" in conn.execute(
        "SELECT reason FROM postmarket_observations WHERE tick_id=?", (tick_id,)
    ).fetchone()[0]


def test_missing_evaluation_is_persisted_as_failed_conservation(tmp_path):
    conn = connect(tmp_path / "postmarket.db")
    tick_id, _ = record_shadow_tick(
        conn, [_evaluate([101.0, 102.0])], session=SESSION,
        tick_utc=SESSION_CLOSE, completed_utc=SESSION_CLOSE,
        run_id="run-1", run_mode="shadow",
        code_version="abc123", data_feed="sip", scheduled_symbols=2,
    )
    assert conn.execute(
        "SELECT invariant_ok,scheduled_symbols,evaluated_symbols "
        "FROM postmarket_ticks WHERE tick_id=?",
        (tick_id,),
    ).fetchone() == (0, 2, 1)


def test_failed_tick_rolls_back_candidates_atomically(tmp_path):
    conn = connect(tmp_path / "postmarket.db")
    first = _evaluate([110.0, 111.0], symbol="FIRST")
    invalid = replace(_evaluate([110.0, 111.0], symbol="INVALID"), direction=None)

    with pytest.raises(ValueError, match="candidate evaluation is incomplete"):
        record_shadow_tick(
            conn, [first, invalid], session=SESSION, tick_utc=SESSION_CLOSE,
            completed_utc=SESSION_CLOSE,
            run_id="run-1", run_mode="shadow", code_version="abc123",
            data_feed="sip", scheduled_symbols=2,
        )

    assert conn.execute("SELECT COUNT(*) FROM postmarket_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM postmarket_ticks").fetchone()[0] == 0


def test_observer_version_is_explicit():
    assert OBSERVER_VERSION == 1
