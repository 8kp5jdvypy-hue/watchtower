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


# ---------------------------------------------------------------------- #
# Position sizing — a personal follow-up DM, never part of the shared
# alert text (see tradebot.rendering.templates.render_position_size).
# ---------------------------------------------------------------------- #


def test_sends_a_position_size_followup_when_sizing_is_configured():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 50_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 2.0)  # $1,000 budget
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "<b>alert text</b>", 2.00)  # $2.00 entry mid -> $200/contract -> 5 contracts

    assert len(client.sent) == 2
    alert_call, sizing_call = client.sent
    assert alert_call[1] == "<b>alert text</b>"
    assert "Max contracts: 5" in sizing_call[1]
    assert "$" in sizing_call[1]


def test_position_size_math_matches_the_configured_budget():
    """$10,000 account, 1% risk = $100 budget; $2.00 entry mid = $200/contract
    full-loss — that's under a full contract, so this should be the
    'exceeds your risk limit' case, not a rounded-down suggestion."""
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 10_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 1.0)
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", 2.00)

    _, sizing_text, _ = client.sent[1]
    assert "position exceeds your risk limit — skip." in sizing_text


def test_position_size_suggests_a_real_contract_count_within_budget():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 50_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 2.0)  # $1,000 budget
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", 2.00)  # $200/contract -> 5 contracts, $1,000 at risk

    _, sizing_text, _ = client.sent[1]
    assert "Max contracts: 5" in sizing_text
    assert "$1,000.00" in sizing_text


def test_no_position_size_followup_when_sizing_is_not_configured():
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)  # account_size/risk_per_trade_pct left unset
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", 2.00)
    assert len(client.sent) == 1  # just the alert, no sizing follow-up


def test_no_position_size_followup_on_a_no_trade_alert():
    """entry_mid is None on a NO TRADE — nothing to size, even for a
    subscriber who has sizing configured."""
    conn = db.connect(":memory:")
    _onboarded_subscriber(conn, 1)
    db.set_sizing_field(conn, 1, "account_size", 10_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 1.0)
    client = FakeClient()
    hook = make_subscriber_hook(client, conn, lambda now: now.date(), default_watchlist=["TSLA"])

    hook(_cluster(), "text", None)
    assert len(client.sent) == 1
