"""Tests for tradebot.telegram_bot.delivery — fanning a HIGH alert out to
eligible subscribers only, and not letting one bad DM kill the rest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.alerts import Cluster
from tradebot.telegram_bot import db
from tradebot.telegram_bot.delivery import make_subscriber_hook

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


def _cluster(symbol="TSLA") -> Cluster:
    return Cluster(
        id="abc123", ts_utc=NOW.isoformat(), session=NOW.date().isoformat(), symbol=symbol,
        kinds="gap", headlines="h", primary_headline="h", score=5.0, tier="high",
        close=250.0, atr14=2.0, trend="up", code_version="v1",
    )


class FakeClient:
    def __init__(self, fail_for_chat_id=None):
        self.sent = []
        self.fail_for_chat_id = fail_for_chat_id

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        if chat_id == self.fail_for_chat_id:
            raise RuntimeError("simulated Telegram failure")
        self.sent.append((chat_id, text, reply_markup))


def _onboarded_subscriber(conn, user_id, symbol_watchlist=None):
    db.get_or_create_user(conn, user_id, user_id, f"user{user_id}")
    db.mark_onboarded(conn, user_id, NOW)
    db.set_risk_ack(conn, user_id, NOW)
    if symbol_watchlist:
        db.set_watchlist(conn, user_id, symbol_watchlist)


def test_fans_out_to_every_eligible_subscriber_with_buttons_attached():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    _onboarded_subscriber(conn, 2)
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA", "SPY"])

    hook(_cluster(), "<b>alert text</b>")

    assert {c for c, _, _ in client.sent} == {1, 2}
    for _, text, keyboard in client.sent:
        assert text == "<b>alert text</b>"
        assert keyboard is not None
        assert "took:abc123" in str(keyboard)


def test_excludes_a_paused_subscriber():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_pause(conn, 1, NOW + timedelta(hours=1), "manual")
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text")
    assert client.sent == []


def test_excludes_subscribers_whose_custom_watchlist_omits_the_symbol():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1, symbol_watchlist=["QQQ"])
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA", "QQQ"])

    hook(_cluster(symbol="TSLA"), "text")
    assert client.sent == []


def test_one_failed_dm_does_not_block_the_others():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    _onboarded_subscriber(conn, 2)
    client = FakeClient(fail_for_chat_id=1)
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text")  # must not raise
    assert [c for c, _, _ in client.sent] == [2]
