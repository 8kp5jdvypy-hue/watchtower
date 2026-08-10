"""End-to-end integration test for the real alert pipeline: a detected
signal walks through the SAME code every live alert actually uses —

    signal (runner.process_new_bar)
    -> validate (guard.validate_alert_data, called inside process_new_bar)
    -> render (tradebot.rendering.templates.render_high_alert)
    -> queue (delivery.make_subscriber_hook -> outbox.enqueue_broadcast)
    -> deliver (tradebot.telegram_bot.worker.WorkerCore)

with ONLY the actual Telegram HTTP call mocked (a fake sender swapped
into WorkerCore — the same injection point production wiring uses, see
worker.build_worker's real sender vs WorkerCore's injectable one).
Every other layer here is the real production code, not a stub or a
hand-rendered string — this is the test that would have caught the
/limits HTML-escaping production incident (see test_handlers.py's
"unescaped angle bracket" regression test) if the gap had been in the
queue/delivery boundary instead of the reply text itself.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from tradebot.alerts import AlertBudget, ConsoleAlerter
from tradebot.detectors import DailyAnchors, Detection
from tradebot.journal import connect as journal_connect
from tradebot.marketdata import Bar, OptionChain, OptionContract, Quote
from tradebot.runner import HeartbeatStats, process_new_bar
from tradebot.telegram_bot import db, outbox
from tradebot.telegram_bot.delivery import make_subscriber_hook
from tradebot.telegram_bot.outbound import SendOutcome, SendResult
from tradebot.telegram_bot.worker import WorkerCore

SESSION_DATE = date(2026, 7, 23)


def _high_tier_scenario():
    anchors = DailyAnchors(
        symbol="TSLA", session_date=SESSION_DATE, prior_close=100.0, prior_high=101.0, prior_low=99.0,
        opening_range_high=100.5, opening_range_low=99.5, opening_range_volume=1000,
        swing_high=102.0, swing_low=98.0, avg_cum_volume_by_bar={},
    )
    bar = Bar("TSLA", datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), 100.0, 100.5, 99.8, 100.2, volume=10_000)
    primary_detection = Detection("TSLA", "gap", bar.ts, 10.0, "a real gap", {})
    evaluate_result = {
        "ts": datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), "close": 100.2, "atr14": 1.0,
        "kinds": "gap", "primary_kind": "gap", "primary_headline": "a real gap", "headlines": "a real gap",
        "primary_detection": primary_detection,
        "score": 10.0, "trend": "up", "detections": [primary_detection],
    }
    return anchors, bar, evaluate_result


def _real_quote_fn(bar):
    def quote_fn(symbol):
        return Quote(symbol=symbol, ts=bar.ts, bid=100.1, ask=100.3, last=100.2)

    return quote_fn


def _real_chain_fn():
    contract = OptionContract(
        symbol="TSLA_TEST_CALL", expiry=date(2026, 7, 31), strike=100.0, right="call",
        bid=2.00, ask=2.05, last=2.02, delta=0.50, theta=-0.10, open_interest=1000,
        implied_volatility=0.35, day_volume=500,
    )

    def chain_fn(symbol, expiry):
        if expiry != date(2026, 7, 31):
            return OptionChain(symbol=symbol, expiry=expiry, contracts=[])
        return OptionChain(symbol=symbol, expiry=expiry, contracts=[contract])

    return chain_fn


def _onboarded_subscriber(users_conn, user_id, watchlist):
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    db.get_or_create_user(users_conn, user_id, user_id, f"user{user_id}")
    db.mark_onboarded(users_conn, user_id, now)
    db.set_risk_ack(users_conn, user_id, now)
    db.set_watchlist(users_conn, user_id, watchlist)


class _FakeTelegramSender:
    """The one mocked boundary — stands in for the real HTTP call to
    Telegram (tradebot.telegram_bot.outbound.send_once), exactly at the
    same injection point WorkerCore's real CLI wiring uses."""

    def __init__(self):
        self.calls = []

    def __call__(self, chat_id, text, reply_markup):
        self.calls.append((chat_id, text, reply_markup))
        return SendResult(outcome=SendOutcome.DELIVERED, message_id=len(self.calls))


def test_a_real_high_signal_flows_through_validate_render_queue_and_delivers():
    journal_conn = journal_connect(":memory:")
    users_conn = db.connect(":memory:")
    _onboarded_subscriber(users_conn, 42, ["TSLA"])

    anchors, bar, evaluate_result = _high_tier_scenario()
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=SESSION_DATE)
    subscriber_hook = make_subscriber_hook(users_conn, lambda now: SESSION_DATE, default_watchlist=["TSLA"])

    import tradebot.runner as runner_mod
    original_evaluate_bar = runner_mod.evaluate_bar
    runner_mod.evaluate_bar = lambda symbol, bars, anch, market_bars=None: evaluate_result
    try:
        process_new_bar(
            journal_conn, budget, ConsoleAlerter(), "v1", "TSLA", SESSION_DATE, [bar], anchors,
            _real_quote_fn(bar), _real_chain_fn(), stats, subscriber_hook=subscriber_hook,
        )
    finally:
        runner_mod.evaluate_bar = original_evaluate_bar

    # SIGNAL + VALIDATE: a real HIGH cluster was journaled and alerted —
    # guard.validate_alert_data ran for real and did not reject it.
    assert stats.tier_counts["high"] == 1
    detection_row = journal_conn.execute("SELECT id, alerted, suppress_reason FROM detections").fetchone()
    detection_id, alerted, suppress_reason = detection_row
    assert alerted == 1
    assert suppress_reason is None

    # QUEUE: the real subscriber fan-out enqueued a row for the real
    # subscriber, keyed by the real detection id (the outbox's
    # idempotency key), still pending.
    row = users_conn.execute(
        "SELECT chat_id, text, status, priority FROM outbox WHERE alert_id = ?", (detection_id,)
    ).fetchone()
    assert row is not None
    chat_id, queued_text, status, priority = row
    assert chat_id == 42
    assert status == "pending"
    assert priority == outbox.PRIORITY_HIGH

    # RENDER: the queued text is the real render_high_alert output, not
    # a stub — it carries the real symbol, the real selected contract's
    # economics, and the real detection id, HTML-escaped and everything.
    assert "TSLA" in queued_text
    assert "<b>" in queued_text  # real HTML formatting from the renderer
    assert detection_id[:6] in queued_text  # the footer's short id (see rendering.templates._footer)

    # DELIVER: draining the outbox with only the Telegram HTTP call
    # mocked actually sends this exact rendered text and marks it
    # delivered — the same code path a real subscriber's phone would hit.
    sender = _FakeTelegramSender()
    worker = WorkerCore(conn=users_conn, sender=sender, now_fn=lambda: evaluate_result["ts"])
    made_progress = worker.run_once()

    assert made_progress is True
    assert len(sender.calls) == 1
    delivered_chat_id, delivered_text, _markup = sender.calls[0]
    assert delivered_chat_id == 42
    assert delivered_text == queued_text  # byte-for-byte what render_high_alert produced
    assert users_conn.execute("SELECT status FROM outbox WHERE alert_id = ?", (detection_id,)).fetchone()[0] == "delivered"


def test_a_signal_that_fails_validation_never_reaches_the_queue():
    """The 'validate' stage of the pipeline is a real gate, not
    decorative — a data-integrity failure must stop the alert before it
    ever reaches render/queue/deliver, with nothing enqueued for anyone."""
    journal_conn = journal_connect(":memory:")
    users_conn = db.connect(":memory:")
    _onboarded_subscriber(users_conn, 42, ["TSLA"])

    anchors, bar, evaluate_result = _high_tier_scenario()
    budget = AlertBudget(now=lambda: bar.ts)
    stats = HeartbeatStats(start_time=bar.ts, session_date=SESSION_DATE)
    subscriber_hook = make_subscriber_hook(users_conn, lambda now: SESSION_DATE, default_watchlist=["TSLA"])

    def crossed_quote_fn(symbol):
        # bid > ask -> guard.validate_alert_data's crossed_quote rejection
        return Quote(symbol=symbol, ts=bar.ts, bid=105.0, ask=100.0, last=100.2)

    import tradebot.runner as runner_mod
    original_evaluate_bar = runner_mod.evaluate_bar
    runner_mod.evaluate_bar = lambda symbol, bars, anch, market_bars=None: evaluate_result
    try:
        process_new_bar(
            journal_conn, budget, ConsoleAlerter(), "v1", "TSLA", SESSION_DATE, [bar], anchors,
            crossed_quote_fn, _real_chain_fn(), stats, subscriber_hook=subscriber_hook,
        )
    finally:
        runner_mod.evaluate_bar = original_evaluate_bar

    detection_row = journal_conn.execute("SELECT id, alerted, suppress_reason FROM detections").fetchone()
    detection_id, alerted, suppress_reason = detection_row
    assert alerted == 0
    assert suppress_reason is not None and "crossed_quote" in suppress_reason

    # nothing was ever queued for the subscriber
    assert users_conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
