"""Tests for the inline-button handlers — the tap-to-journal path and
onboarding button steps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradebot.detectors import Detection
from tradebot.journal import connect as journal_connect
from tradebot.journal import set_no_trade, write_cluster
from tradebot.telegram_bot import callbacks, db
from tradebot.telegram_bot.context import AppConfig, CallbackContext

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


def _app():
    return AppConfig(
        admin_ids=frozenset(), default_watchlist=["SPY", "QQQ", "TSLA"], stripe_portal_url=None, plans=[],
        support_contact="@support", market_is_open_fn=lambda now: True, session_date_fn=lambda now: now.date(),
        halt_file=Path("/tmp/watchtower_test_HALT_cb"), heartbeat_file=Path("/tmp/watchtower_test_hb_cb.json"),
    )


def _setup(tier="free"):
    users_conn = db.connect(":memory:")
    journal_conn = journal_connect(":memory:")
    db.get_or_create_user(users_conn, 1, 1, "alice")
    db.mark_onboarded(users_conn, 1, NOW)
    db.set_risk_ack(users_conn, 1, NOW)
    if tier != "free":
        users_conn.execute("UPDATE users SET tier = ? WHERE telegram_user_id = ?", (tier, 1))
        users_conn.commit()
    return users_conn, journal_conn


def _ctx(users_conn, journal_conn, arg, now=NOW):
    return CallbackContext(
        client=None, users_conn=users_conn, journal_conn=journal_conn, user=db.get_user(users_conn, 1),
        chat_id=1, message_id=42, arg=arg, now=now, app=_app(),
    )


def _write_alert(journal_conn, no_trade=False):
    did = write_cluster(
        journal_conn, session=NOW.date().isoformat(), symbol="TSLA", ts_utc=NOW.isoformat(),
        kinds="gap,level_break", headlines="h", score=5.0, close=250.0, atr14=2.0, trend="up",
        detections=[Detection("TSLA", "gap", NOW, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    set_no_trade(journal_conn, did, no_trade)
    journal_conn.commit()
    return did


def test_took_button_logs_and_dedupes():
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn)
    r1 = callbacks.handle_took_button(_ctx(users_conn, journal_conn, detection_id))
    assert "logged" in r1.toast.lower()
    assert len(db.list_trades(users_conn, 1)) == 1

    r2 = callbacks.handle_took_button(_ctx(users_conn, journal_conn, detection_id))
    assert "already logged" in r2.toast.lower()
    assert len(db.list_trades(users_conn, 1)) == 1


def test_took_button_unknown_alert():
    users_conn, journal_conn = _setup()
    r = callbacks.handle_took_button(_ctx(users_conn, journal_conn, "nope"))
    assert r.show_alert is True


def test_took_button_auto_fills_direction_and_prompts_for_mood():
    """Full alert context, auto-filled — nothing typed. The mood prompt is
    a separate follow-up message (send_text/send_keyboard), not an edit
    to the alert itself, so the alert's own formatting is never touched."""
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn)
    r = callbacks.handle_took_button(_ctx(users_conn, journal_conn, detection_id))
    assert r.send_text is not None
    assert r.send_keyboard is not None
    trade = db.list_trades(users_conn, 1)[0]
    assert trade.direction == "up"  # from the alert's own trend, not typed
    assert trade.kind == "gap"  # primary_kind, not the full "gap,level_break" list


def test_mood_button_sets_the_emotional_tag():
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn)
    callbacks.handle_took_button(_ctx(users_conn, journal_conn, detection_id))
    trade = db.list_trades(users_conn, 1)[0]

    r = callbacks.handle_mood_button(_ctx(users_conn, journal_conn, f"{trade.id}:rushed"))
    assert "logged" in r.toast.lower()
    assert r.edit_keyboard is None  # the one-shot prompt's keyboard is removed after a tap
    updated = db.get_trade(users_conn, trade.id)
    assert updated.emotional_tag == "rushed"


def test_mood_button_rejects_an_unrecognized_mood():
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn)
    callbacks.handle_took_button(_ctx(users_conn, journal_conn, detection_id))
    trade = db.list_trades(users_conn, 1)[0]

    r = callbacks.handle_mood_button(_ctx(users_conn, journal_conn, f"{trade.id}:ecstatic"))
    assert r.show_alert is True
    assert db.get_trade(users_conn, trade.id).emotional_tag is None


def test_mood_button_unknown_trade():
    users_conn, journal_conn = _setup()
    r = callbacks.handle_mood_button(_ctx(users_conn, journal_conn, "nonexistent:calm"))
    assert r.show_alert is True


def test_skip_button_records_a_response_without_creating_a_trade():
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn)
    r = callbacks.handle_skip_button(_ctx(users_conn, journal_conn, detection_id))
    assert "skipped" in r.toast.lower()
    assert db.has_responded(users_conn, 1, detection_id)
    assert db.list_trades(users_conn, 1) == []


def test_whynt_button_explains_a_no_trade_alert():
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn, no_trade=True)
    r = callbacks.handle_whynt_button(_ctx(users_conn, journal_conn, detection_id))
    assert r.show_alert is True
    assert "no tradable contract" in r.toast.lower()


def test_whynt_button_on_an_alert_that_did_have_a_contract():
    users_conn, journal_conn = _setup()
    detection_id = _write_alert(journal_conn, no_trade=False)
    r = callbacks.handle_whynt_button(_ctx(users_conn, journal_conn, detection_id))
    assert "did have" in r.toast.lower() or "actually had" in r.toast.lower()


def test_ack_risk_button_advances_to_timezone_step():
    users_conn, journal_conn = _setup()
    db.set_onboarding_step(users_conn, 1, "risk_ack")
    r = callbacks.handle_ack_risk_button(_ctx(users_conn, journal_conn, ""))
    assert db.get_user(users_conn, 1).risk_ack_at is not None
    assert db.get_user(users_conn, 1).onboarding_step == "timezone"
    assert r.edit_keyboard is not None


def test_timezone_button_sets_timezone_and_advances_to_quiet_hours():
    users_conn, journal_conn = _setup()
    db.set_onboarding_step(users_conn, 1, "timezone")
    r = callbacks.handle_timezone_button(_ctx(users_conn, journal_conn, "America/Chicago"))
    user = db.get_user(users_conn, 1)
    assert user.timezone == "America/Chicago"
    assert user.onboarding_step == "quiet_hours"
    assert r.edit_keyboard is None


def test_pause_button_30m_sets_a_pause_and_confirms_when_it_lifts():
    users_conn, journal_conn = _setup()
    r = callbacks.handle_pause_button(_ctx(users_conn, journal_conn, "30m"))
    user = db.get_user(users_conn, 1)
    assert user.is_paused(NOW)
    assert not user.is_paused(NOW + timedelta(hours=1))
    assert "lift early" in r.edit_text.lower()


def test_pause_button_eod_halts_for_the_session():
    users_conn, journal_conn = _setup()
    r = callbacks.handle_pause_button(_ctx(users_conn, journal_conn, "eod"))
    assert db.get_user(users_conn, 1).is_halted_for_session(NOW.date())


def test_watchlist_button_blocked_for_free_tier():
    users_conn, journal_conn = _setup(tier="free")
    r = callbacks.handle_watchlist_button(_ctx(users_conn, journal_conn, "TSLA"))
    assert r.show_alert is True
    assert db.get_watchlist(users_conn, 1) is None


def test_watchlist_button_toggles_for_paid_tier():
    users_conn, journal_conn = _setup(tier="pro")
    r1 = callbacks.handle_watchlist_button(_ctx(users_conn, journal_conn, "TSLA"))
    assert "added" in r1.toast.lower()
    assert db.get_watchlist(users_conn, 1) == ["TSLA"]

    r2 = callbacks.handle_watchlist_button(_ctx(users_conn, journal_conn, "TSLA"))
    assert "removed" in r2.toast.lower()
    assert db.get_watchlist(users_conn, 1) is None


def test_watchlist_save_button_confirms_current_selection():
    users_conn, journal_conn = _setup(tier="pro")
    callbacks.handle_watchlist_button(_ctx(users_conn, journal_conn, "TSLA"))
    r = callbacks.handle_watchlist_button(_ctx(users_conn, journal_conn, "save"))
    assert "saved" in r.toast.lower()
    assert "TSLA" in r.edit_text
