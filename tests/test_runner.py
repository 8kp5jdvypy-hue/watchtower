"""Tests for the pure/testable pieces of tradebot.runner.

The full run_replay()/run_live() loops are exercised via an actual
--replay-date run (see the session transcript), not unit tests — they're
integration-shaped (calendars, journaling, alerting all wired together).
These tests cover the pieces that are meaningfully testable in isolation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.alerts import AlertBudget, ConsoleAlerter, Decision
from tradebot.detectors import DailyAnchors, Detection
from tradebot.events import add_event_window
from tradebot.marketdata import Bar, Quote
from tradebot.journal import connect as journal_connect
import tradebot.runner as runner_mod
from tradebot.detectors import atr as compute_atr
from tradebot.runner import HeartbeatStats, evaluate_bar, is_halted_bar, is_stale, process_new_bar, session_bounds


def test_is_stale_true_past_the_threshold():
    latest_close = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    now = latest_close + timedelta(seconds=91)
    assert is_stale(latest_close, now, max_seconds=90) is True


def test_is_stale_false_within_the_threshold():
    latest_close = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    now = latest_close + timedelta(seconds=89)
    assert is_stale(latest_close, now, max_seconds=90) is False


def test_is_halted_bar_detects_zero_volume():
    ts = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    assert is_halted_bar(Bar("BE", ts, 10, 10, 10, 10, volume=0)) is True
    assert is_halted_bar(Bar("BE", ts, 10, 10.5, 9.5, 10.2, volume=100)) is False


def test_session_bounds_regular_day():
    open_ts, close_ts = session_bounds(date(2026, 7, 23))
    assert open_ts.hour == 13 and open_ts.minute == 30  # 09:30 ET in UTC (EDT)
    assert close_ts.hour == 20 and close_ts.minute == 0  # 16:00 ET in UTC (EDT)


def test_session_bounds_honors_early_close():
    # day after Thanksgiving 2026 — a known 13:00 ET early close
    open_ts, close_ts = session_bounds(date(2026, 11, 27))
    assert close_ts.hour == 18  # 13:00 ET in UTC (EST, UTC-5)


def test_session_bounds_rejects_non_trading_day():
    with pytest.raises(ValueError):
        session_bounds(date(2026, 7, 25))  # a Saturday


def test_heartbeat_stats_record_cluster_tracks_tier_and_suppression_counts():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    stats = HeartbeatStats(start_time=start, session_date=date(2026, 7, 23))
    stats.record_cluster("high", Decision.SEND)
    stats.record_cluster("high", Decision.SUPPRESS_COOLDOWN)
    stats.record_cluster("log", Decision.QUEUED_FOR_EOD)

    assert stats.tier_counts["high"] == 2
    assert stats.tier_counts["log"] == 1
    assert stats.suppression_counts["cooldown_active"] == 1
    # a plain SEND is not a suppression
    assert "send" not in stats.suppression_counts


def _high_tier_fixture():
    """A synthetic evaluate_bar() result scored well above TIER_HIGH, for
    exercising process_new_bar's SEND branch without needing to hand-craft
    real detector-triggering bars."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), 100.0, 100.5, 99.8, 100.2, volume=10_000)
    primary_detection = Detection("TSLA", "gap", bar.ts, 10.0, "a gap", {})
    result = {
        "ts": datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), "close": 100.2, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary_detection,
        "score": 10.0, "trend": "up", "detections": [primary_detection],
    }
    return anchors, bar, result


def test_process_new_bar_without_a_subscriber_hook_behaves_exactly_as_before(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)
    # no subscriber_hook passed — must not raise, and behaves like the pre-hook implementation
    assert stats.tier_counts["high"] == 1


def test_process_new_bar_calls_subscriber_hook_with_the_cluster_and_rendered_text_on_a_high_send(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    calls = []
    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=lambda cluster, text: calls.append((cluster, text)),
    )

    assert len(calls) == 1
    cluster, text = calls[0]
    assert cluster.symbol == "TSLA" and cluster.tier == "high"
    assert "TSLA" in text


def test_process_new_bar_swallows_a_subscriber_hook_exception_without_dropping_the_alert(monkeypatch):
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    def broken_hook(cluster, text):
        raise RuntimeError("simulated fan-out failure")

    process_new_bar(  # must not raise
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats,
        subscriber_hook=broken_hook,
    )
    assert any("fan-out failed" in e for e in stats.errors)
    row = conn.execute("SELECT alerted FROM detections").fetchone()
    assert row[0] == 1  # the alert itself still went out despite the hook blowing up


def test_evaluate_bar_cluster_atr14_matches_the_primary_detectors_own_atr():
    """Regression test for a real production bug: an alert showed ATR(14)
    as two different numbers in one message — the headline (built from a
    detector's own window) and the stats block (built from
    evaluate_bar's independently-recomputed atr(bars)) could disagree,
    because range_expansion scores against atr(bars[:-1]) (deliberately
    excluding the current, possibly-anomalous bar) while evaluate_bar used
    to compute atr(bars) (including it) for cluster.atr14. On a genuinely
    wide current bar those two windows can diverge sharply. The fix reuses
    whatever ATR the primary/headline detector actually used instead of
    recomputing a second, independent number."""
    anchors = DailyAnchors(
        symbol="TSLA", session_date=date(2026, 7, 23), prior_close=100.0, prior_high=115.0, prior_low=85.0,
        opening_range_high=120.0, opening_range_low=80.0, opening_range_volume=10_000,
        swing_high=130.0, swing_low=70.0, avg_cum_volume_by_bar={},
    )
    base = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    bars = []
    price = 100.0
    for i in range(16):
        bars.append(Bar("TSLA", base + timedelta(minutes=5 * i), price, price + 0.1, price - 0.1, price + 0.05, volume=1_000))
        price += 0.05
    # The current bar has a huge range relative to the last 16 quiet bars —
    # this is exactly what triggers range_expansion, and exactly the
    # scenario where atr(bars) vs atr(bars[:-1]) diverge sharply.
    bars.append(Bar("TSLA", base + timedelta(minutes=80), 100.8, 115.0, 95.0, 101.0, volume=5_000))

    result = evaluate_bar("TSLA", bars, anchors)

    assert result["primary_kind"] == "range_expansion"
    assert "ATR(14)=0.20" in result["primary_headline"]

    primary_detection = result["detections"][0]
    assert result["atr14"] == primary_detection.context["atr14"]

    # Prove this is a real behavior change, not a vacuous assertion: the
    # old buggy computation (atr on the full window, including the huge
    # current bar) is a materially different number.
    old_buggy_value = compute_atr(bars)
    assert result["atr14"] != old_buggy_value
    assert abs(old_buggy_value - result["atr14"]) > 1.0  # ~1.6 vs ~0.2 in this fixture


def test_process_new_bar_guard_rejection_logs_error_and_emits_a_metric(monkeypatch, caplog, tmp_path):
    from tradebot import metrics as metrics_mod

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_path)

    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def bad_quote_fn(symbol):
        # crossed quote — bid > ask — must trip the guard
        return Quote(symbol=symbol, ts=bar.ts, bid=101.0, ask=100.0, last=100.2)

    def chain_fn(symbol, expiry):
        raise NotImplementedError

    with caplog.at_level("ERROR", logger="watchtower.runner"):
        process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, bad_quote_fn, chain_fn, stats)

    assert any("alert suppressed by data guard" in r.message and "rule=crossed_quote" in r.message for r in caplog.records)

    row = conn.execute("SELECT alerted, suppress_reason FROM detections").fetchone()
    assert row[0] == 0
    assert row[1].startswith("data_integrity_failed: crossed_quote")

    assert metrics_mod.read_all(metrics_path) == {"validator_rejection{rule=crossed_quote}": 1}


def test_process_new_bar_selects_a_contract_and_journals_it(monkeypatch):
    """End-to-end: a real chain_fn produces a real ContractSelection that
    reaches templates.render_high_alert and gets journaled — not a
    None-chain stub like the other process_new_bar tests above."""
    from tradebot.marketdata import OptionChain, OptionContract

    anchors, bar, result = _high_tier_fixture()
    result = {**result, "trend": "up"}  # bullish -> calls
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    # 100.2 spot, target delta 0.40-0.55 -> the 100-strike call at delta 0.50
    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)

    detection_id = conn.execute("SELECT id FROM detections").fetchone()[0]
    row = conn.execute(
        "SELECT symbol, right, strike, dte, delta, is_vertical FROM contract_selections WHERE detection_id = ?",
        (detection_id,),
    ).fetchone()
    assert row == ("TSLA", "call", 100.0, 8, 0.50, 0)

    no_trade_flag = conn.execute("SELECT no_trade FROM detections WHERE id = ?", (detection_id,)).fetchone()[0]
    assert no_trade_flag == 0  # a contract WAS selected

    iv_row = conn.execute("SELECT iv FROM iv_history WHERE symbol = ?", ("TSLA",)).fetchone()
    assert iv_row == (0.35,)


def test_process_new_bar_blackouts_a_contract_when_a_real_earnings_event_falls_before_expiry(monkeypatch):
    """End-to-end: runner.py's bound_earnings_check_fn now reads the real
    event_windows table (tradebot.events.has_earnings_before) instead of
    the old, always-empty telegram_bot.db events table — confirm the
    wiring actually blocks a trade, not just that has_earnings_before()
    works correctly in isolation."""
    from tradebot.marketdata import OptionChain, OptionContract

    anchors, bar, result = _high_tier_fixture()
    result = {**result, "trend": "up"}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    # earnings between today (2026-07-23) and the expiry the fixture below
    # will pick (2026-07-31, same DTE math as the test above)
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=datetime(2026, 7, 28, 13, 30, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc), severity="suppress", source="nasdaq_earnings",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    process_new_bar(conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors, quote_fn, chain_fn, stats)

    detection_id = conn.execute("SELECT id FROM detections").fetchone()[0]
    no_trade_flag = conn.execute("SELECT no_trade FROM detections WHERE id = ?", (detection_id,)).fetchone()[0]
    assert no_trade_flag == 1
    assert conn.execute("SELECT COUNT(*) FROM contract_selections").fetchone()[0] == 0


def _no_op_chain_fn(symbol, expiry):
    raise NotImplementedError


def _flat_quote_fn(bar):
    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)
    return quote_fn


def test_process_new_bar_suppresses_high_alert_inside_a_suppress_severity_event_window(monkeypatch):
    """News as suppression, not an alert source: a HIGH cluster whose bar
    close falls inside a 'suppress' severity event window must never be
    sent, and the journal must say why — see tradebot.events module
    docstring."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT alerted, suppress_reason, tier, news_driven FROM detections").fetchone()
    assert row[0] == 0  # never alerted
    assert row[1] == "news_blackout:8-K:material event"
    assert row[2] == "high"  # the journal's ground-truth tier is score-based and unaffected
    assert row[3] == 1
    assert stats.suppression_counts["news_blackout"] == 1


def test_process_new_bar_downgrades_high_alert_inside_a_downgrade_severity_event_window(monkeypatch):
    """A 'downgrade' severity window still gets a look — just batched into
    the medium digest instead of pushed immediately as HIGH."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="downgrade", source="test", detail="earnings day",
    )
    clock = {"t": bar.ts}
    budget = AlertBudget(now=lambda: clock["t"])
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT alerted, suppress_reason, tier, news_driven FROM detections").fetchone()
    assert row[0] == 0  # not alerted immediately — queued for the medium digest instead
    assert row[1] is None  # queued, not suppressed
    assert row[2] == "high"  # ground-truth journal tier still reflects the real score
    assert row[3] == 1
    assert stats.tier_counts["medium"] == 1  # routed as medium from here on
    assert "high" not in stats.tier_counts

    # Prove it actually landed in the medium queue (not lost, not sent as
    # high) by crossing an hour boundary and popping the digest.
    clock["t"] = clock["t"] + timedelta(hours=1)
    digest = budget.pop_medium_digest_if_due()
    assert digest is not None and len(digest) == 1
    assert digest[0].symbol == "TSLA" and digest[0].tier == "medium"


def test_process_new_bar_event_window_suppression_does_not_burn_cap_or_cooldown(monkeypatch):
    """A blackout-suppressed HIGH alert must never reach AlertBudget.evaluate()
    at all — otherwise it would silently consume the daily HIGH cap or start
    the per-(symbol, kind) cooldown for an alert nobody ever saw, blocking a
    later, legitimate alert of the same kind."""
    anchors, bar, result = _high_tier_fixture()
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=result["ts"] - timedelta(minutes=5),
        end_utc=result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    # A second bar, same symbol/kind, at the SAME budget "now" — if the
    # first (suppressed) alert had wrongly started the cooldown, this one
    # would see zero elapsed time and get suppressed too. Its bar close is
    # past the event window's end (which runs to result["ts"] + 5min), so
    # only the cooldown could stop it.
    bar2 = Bar("TSLA", bar.ts + timedelta(minutes=10), 100.2, 100.7, 100.0, 100.4, volume=10_000)
    result2 = {**result, "ts": result["ts"] + timedelta(minutes=10)}
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: result2)

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar, bar2], anchors,
        _flat_quote_fn(bar2), _no_op_chain_fn, stats,
    )

    rows = conn.execute("SELECT alerted, suppress_reason FROM detections ORDER BY ts_utc").fetchall()
    assert len(rows) == 2
    assert rows[0] == (0, "news_blackout:8-K:material event")
    assert rows[1] == (1, None)  # sent normally — cooldown was never started by the blackout


def test_process_new_bar_tags_but_does_not_reroute_non_high_tiers_inside_an_event_window(monkeypatch):
    """Suppress/downgrade ROUTING only applies to HIGH — MEDIUM/LOG are
    already batched, so there's no immediate-publish race for a blackout
    window to protect against. But news_driven TAGGING applies to every
    tier: a medium-tier cluster overlapping a known event is still not a
    clean technical setup, and historical_performance() excludes it from
    the sample pool regardless of tier. See tradebot.events module
    docstring."""
    anchors, bar, result = _high_tier_fixture()
    medium_result = {**result, "score": 3.0}  # below HIGH threshold
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch: medium_result)

    conn = journal_connect(":memory:")
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=medium_result["ts"] - timedelta(minutes=5),
        end_utc=medium_result["ts"] + timedelta(minutes=5), severity="suppress", source="test", detail="material event",
    )
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=date(2026, 7, 23))

    process_new_bar(
        conn, budget, ConsoleAlerter(), "v1", "TSLA", date(2026, 7, 23), [bar], anchors,
        _flat_quote_fn(bar), _no_op_chain_fn, stats,
    )

    row = conn.execute("SELECT suppress_reason, tier, news_driven FROM detections").fetchone()
    assert row[0] is None  # normal medium routing (queued, not suppressed) — untouched by the event window
    assert row[1] == "medium"
    assert row[2] == 1  # tagged news-driven regardless of tier
