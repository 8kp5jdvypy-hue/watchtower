"""Tests for tradebot.telegram_bot.delivery — fanning a HIGH alert out to
eligible subscribers only, via the outbox (see outbox.py) rather than a
direct send. tradebot.telegram_bot.worker is what actually delivers;
this module's job is only picking recipients and enqueueing correctly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.alerts import Cluster
from tradebot.telegram_bot import db, outbox
from tradebot.telegram_bot.delivery import make_subscriber_hook

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


def _cluster(symbol="TSLA") -> Cluster:
    return Cluster(
        id="abc123", ts_utc=NOW.isoformat(), session=NOW.date().isoformat(), symbol=symbol,
        kinds="gap", headlines="h", primary_headline="h", score=5.0, tier="high",
        close=250.0, atr14=2.0, trend="up", code_version="v1",
    )


def _onboarded_subscriber(conn, user_id, symbol_watchlist=None):
    db.get_or_create_user(conn, user_id, user_id, f"user{user_id}")
    db.mark_onboarded(conn, user_id, NOW)
    db.set_risk_ack(conn, user_id, NOW)
    if symbol_watchlist:
        db.set_watchlist(conn, user_id, symbol_watchlist)


def _outbox_rows(conn, alert_id):
    return conn.execute(
        "SELECT chat_id, text, reply_markup_json, priority, status FROM outbox WHERE alert_id = ? ORDER BY chat_id",
        (alert_id,),
    ).fetchall()


def test_fans_out_to_every_eligible_subscriber_with_buttons_attached():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    _onboarded_subscriber(conn, 2)
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA", "SPY"])

    hook(_cluster(), "<b>alert text</b>")

    rows = _outbox_rows(conn, "abc123")
    assert [r[0] for r in rows] == [1, 2]
    for chat_id, text, markup_json, priority, status in rows:
        assert text == "<b>alert text</b>"
        assert priority == outbox.PRIORITY_HIGH
        assert status == "pending"
        assert "took:abc123" in markup_json


def test_excludes_a_paused_subscriber():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_pause(conn, 1, NOW + timedelta(hours=1), "manual")
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_excludes_subscribers_whose_custom_watchlist_omits_the_symbol():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1, symbol_watchlist=["QQQ"])
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA", "QQQ"])

    hook(_cluster(symbol="TSLA"), "text")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_no_eligible_subscribers_enqueues_nothing_not_even_an_empty_alert_id_row():
    conn = db.connect(":memory:")
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])
    hook(_cluster(), "text")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_a_second_call_for_the_same_alert_does_not_duplicate_rows():
    """The idempotency key is (alert_id, chat_id) — re-invoking the hook
    for the same cluster (e.g. after a runner.py restart re-processing
    the same bar) must not double-enqueue."""
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text")
    hook(_cluster(), "text (a second, different render)")

    rows = _outbox_rows(conn, "abc123")
    assert len(rows) == 1
    assert rows[0][1] == "text"  # the FIRST enqueue wins, exactly like outbox.enqueue_broadcast elsewhere


# ---------------------------------------------------------------------- #
# Position sizing — a personal follow-up, enqueued separately from the
# shared alert text (see tradebot.rendering.templates.render_position_size).
# ---------------------------------------------------------------------- #


def test_sends_a_position_size_followup_when_sizing_is_configured():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 50_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 2.0)  # $1,000 budget
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "<b>alert text</b>", 2.00)  # $2.00 entry mid -> $200/contract -> 5 contracts

    alert_rows = _outbox_rows(conn, "abc123")
    sizing_rows = _outbox_rows(conn, "abc123:sizing")
    assert len(alert_rows) == 1 and alert_rows[0][1] == "<b>alert text</b>"
    assert len(sizing_rows) == 1
    assert sizing_rows[0][3] == outbox.PRIORITY_HIGH  # same urgency as the alert it follows
    assert "Max contracts: 5" in sizing_rows[0][1]


def test_position_size_math_matches_the_configured_budget():
    """$10,000 account, 1% risk = $100 budget; $2.00 entry mid = $200/contract
    full-loss — that's under a full contract, so this should be the
    'exceeds your risk limit' case, not a rounded-down suggestion."""
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 10_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 1.0)
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", 2.00)

    sizing_text = _outbox_rows(conn, "abc123:sizing")[0][1]
    assert "position exceeds your risk limit — skip." in sizing_text


def test_position_size_suggests_a_real_contract_count_within_budget():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 50_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 2.0)  # $1,000 budget
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", 2.00)  # $200/contract -> 5 contracts, $1,000 at risk

    sizing_text = _outbox_rows(conn, "abc123:sizing")[0][1]
    assert "Max contracts: 5" in sizing_text
    assert "$1,000.00" in sizing_text


def test_no_position_size_followup_when_sizing_is_not_configured():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)  # account_size/risk_per_trade_pct left unset
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", 2.00)
    assert _outbox_rows(conn, "abc123:sizing") == []
    assert len(_outbox_rows(conn, "abc123")) == 1  # the alert itself still went out


def test_no_position_size_followup_on_a_no_trade_alert():
    """entry_mid is None on a NO TRADE — nothing to size, even for a
    subscriber who has sizing configured."""
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 10_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 1.0)
    hook = make_subscriber_hook(conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", None)
    assert _outbox_rows(conn, "abc123:sizing") == []
