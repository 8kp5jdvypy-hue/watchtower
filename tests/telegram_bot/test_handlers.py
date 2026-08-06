"""One test per command handler, plus the mid-session limit-raise queueing
rule exercised through the actual /limits handler (not just the db layer).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradebot.detectors import Detection
from tradebot.journal import connect as journal_connect
from tradebot.journal import set_no_trade, write_cluster
from tradebot.telegram_bot import db, handlers
from tradebot.telegram_bot.context import AppConfig, HandlerContext

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


def _app(market_open=True, bot_username=None):
    return AppConfig(
        admin_ids=frozenset({999}),
        default_watchlist=["SPY", "QQQ", "TSLA"],
        stripe_portal_url=None,
        plans=[("Free", "$0", "basic alerts"), ("Pro", "$29/mo", "custom watchlist")],
        support_contact="@support",
        market_is_open_fn=lambda now: market_open,
        session_date_fn=lambda now: now.date(),
        halt_file=Path("/tmp/watchtower_test_HALT_handlers"),
        heartbeat_file=Path("/tmp/watchtower_test_heartbeat_handlers.json"),
        bot_username=bot_username,
    )


def _setup(user_id=1, onboarded=True, admin=False, tier="free", market_open=True):
    users_conn = db.connect(":memory:")
    journal_conn = journal_connect(":memory:")
    db.get_or_create_user(users_conn, user_id, user_id, "alice")
    if onboarded:
        db.mark_onboarded(users_conn, user_id, NOW)
        db.set_risk_ack(users_conn, user_id, NOW)
    if admin:
        db.set_admin(users_conn, user_id, True)
    if tier != "free":
        users_conn.execute("UPDATE users SET tier = ? WHERE telegram_user_id = ?", (tier, user_id))
        users_conn.commit()
    return users_conn, journal_conn


def _ctx(users_conn, journal_conn, user_id=1, args=None, now=NOW, market_open=True, chat_type="private", bot_username=None):
    return HandlerContext(
        client=None, users_conn=users_conn, journal_conn=journal_conn, user=db.get_user(users_conn, user_id),
        chat_id=user_id, chat_type=chat_type, args=args or [], now=now,
        app=_app(market_open=market_open, bot_username=bot_username),
    )


def _write_high_alert(journal_conn, symbol="TSLA", no_trade=False, session=None):
    session = session or NOW.date().isoformat()
    detection_id = write_cluster(
        journal_conn, session=session, symbol=symbol, ts_utc=NOW.isoformat(), kinds="gap,level_break",
        headlines="h", score=5.0, close=250.0, atr14=2.0, trend="up",
        detections=[Detection(symbol, "gap", NOW, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    set_no_trade(journal_conn, detection_id, no_trade)
    journal_conn.commit()
    return detection_id


# ---------------------------------------------------------------------- #
# /start
# ---------------------------------------------------------------------- #


def test_start_shows_track_record_and_risk_ack_before_asking_anything():
    users_conn, journal_conn = _setup(onboarded=False)
    reply = handlers.handle_start(_ctx(users_conn, journal_conn))
    assert "track record" in reply.text.lower() or "not enough history" in reply.text.lower()
    assert reply.keyboard is not None
    user = db.get_user(users_conn, 1)
    assert user.onboarding_step == "risk_ack"
    assert not user.is_onboarded  # nothing was asked/set yet beyond showing the prompt


def test_start_is_idempotent_for_an_already_onboarded_user():
    users_conn, journal_conn = _setup(onboarded=True)
    db.set_timezone(users_conn, 1, "America/Chicago")
    reply = handlers.handle_start(_ctx(users_conn, journal_conn))
    assert "already set up" in reply.text.lower()
    assert db.get_user(users_conn, 1).timezone == "America/Chicago"  # untouched


# ---------------------------------------------------------------------- #
# /status
# ---------------------------------------------------------------------- #


def test_status_reports_market_state_and_users_own_lock_state():
    users_conn, journal_conn = _setup()
    db.set_pause(users_conn, 1, NOW + timedelta(hours=1), "manual")
    reply = handlers.handle_status(_ctx(users_conn, journal_conn))
    assert "paused" in reply.text.lower()
    assert "market" in reply.text.lower() or "live" in reply.text.lower() or "closed" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /performance
# ---------------------------------------------------------------------- #


def test_performance_reads_only_from_the_journal_and_handles_no_history():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_performance(_ctx(users_conn, journal_conn))
    assert "not enough" in reply.text.lower()


def test_performance_reports_real_numbers_once_history_exists():
    users_conn, journal_conn = _setup()
    base = NOW
    for i in range(6):
        did = write_cluster(
            journal_conn, session=base.date().isoformat(), symbol="TSLA", ts_utc=(base + timedelta(minutes=i)).isoformat(),
            kinds="level_break", headlines="h", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection("TSLA", "level_break", base, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        journal_conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, 101))
        set_no_trade(journal_conn, did, False)
    journal_conn.commit()
    reply = handlers.handle_performance(_ctx(users_conn, journal_conn))
    assert "hit rate" in reply.text.lower()
    assert "100.00%" in reply.text  # every one of these synthetic rows continued


# ---------------------------------------------------------------------- #
# /me
# ---------------------------------------------------------------------- #


def test_me_reports_not_enough_data_for_a_fresh_user():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_me(_ctx(users_conn, journal_conn))
    assert "not enough" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /took
# ---------------------------------------------------------------------- #


def test_took_logs_a_trade_and_rejects_double_logging():
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    reply = handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "2", "5.10"]))
    assert "logged" in reply.text.lower()
    trades = db.list_trades(users_conn, 1)
    assert len(trades) == 1 and trades[0].contracts == 2.0 and trades[0].entry_price == 5.10

    reply2 = handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id]))
    assert "already logged" in reply2.text.lower()
    assert len(db.list_trades(users_conn, 1)) == 1


def test_took_rejects_an_unknown_alert_id():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_took(_ctx(users_conn, journal_conn, args=["doesnotexist"]))
    assert "don't recognize" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /closed
# ---------------------------------------------------------------------- #


def test_closed_computes_pnl_on_the_most_recent_open_trade():
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "10.00"]))
    reply = handlers.handle_closed(_ctx(users_conn, journal_conn, args=["11.00", "confident"]))
    assert "+10.00%" in reply.text
    trade = db.list_trades(users_conn, 1)[0]
    assert trade.status == "closed" and trade.emotional_tag == "confident"


def test_closed_with_no_open_trade_gives_a_usage_hint():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_closed(_ctx(users_conn, journal_conn))
    assert "no open trade" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /limits — including the mid-session raise-is-queued rule
# ---------------------------------------------------------------------- #


def test_limits_with_no_args_shows_current_values():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn))
    assert "not set" in reply.text.lower()


def test_limits_decrease_applies_immediately_during_market_hours():
    users_conn, journal_conn = _setup()
    handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "5"], market_open=True))
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "2"], market_open=True))
    assert "effective immediately" in reply.text.lower()
    assert db.get_user(users_conn, 1).max_trades_per_day == 2


def test_limits_increase_mid_session_is_queued_with_an_explanation():
    users_conn, journal_conn = _setup()
    handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "5"], market_open=False))
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "10"], market_open=True))
    assert "queued" in reply.text.lower()
    assert "for exactly this moment" in reply.text.lower()
    assert db.get_user(users_conn, 1).max_trades_per_day == 5  # unchanged until next session


# ---------------------------------------------------------------------- #
# /pause, /resume
# ---------------------------------------------------------------------- #


def test_pause_shows_duration_buttons():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_pause(_ctx(users_conn, journal_conn))
    assert reply.keyboard is not None


def test_resume_lifts_a_pause():
    users_conn, journal_conn = _setup()
    db.set_pause(users_conn, 1, NOW + timedelta(hours=1), "manual")
    reply = handlers.handle_resume(_ctx(users_conn, journal_conn))
    assert "back on" in reply.text.lower()
    assert not db.get_user(users_conn, 1).is_paused(NOW)


def test_resume_refuses_when_locked_and_says_when_it_clears():
    users_conn, journal_conn = _setup()
    db.set_lock(users_conn, 1, NOW + timedelta(hours=3), "daily loss limit hit")
    reply = handlers.handle_resume(_ctx(users_conn, journal_conn))
    assert "can't" in reply.text.lower()
    assert "locked until" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /watchlist
# ---------------------------------------------------------------------- #


def test_watchlist_free_tier_cannot_edit():
    users_conn, journal_conn = _setup(tier="free")
    reply = handlers.handle_watchlist(_ctx(users_conn, journal_conn))
    assert reply.keyboard is None
    assert "paid" in reply.text.lower()


def test_watchlist_paid_tier_gets_an_editable_keyboard():
    users_conn, journal_conn = _setup(tier="pro")
    reply = handlers.handle_watchlist(_ctx(users_conn, journal_conn))
    assert reply.keyboard is not None


# ---------------------------------------------------------------------- #
# /events
# ---------------------------------------------------------------------- #


def test_events_says_nothing_loaded_rather_than_fabricating():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_events(_ctx(users_conn, journal_conn))
    assert "no events loaded" in reply.text.lower()


def test_events_lists_what_was_actually_loaded():
    users_conn, journal_conn = _setup()
    db.add_event(users_conn, NOW.date(), "earnings", "before the open", symbol="TSLA")
    reply = handlers.handle_events(_ctx(users_conn, journal_conn))
    assert "TSLA" in reply.text and "before the open" in reply.text


# ---------------------------------------------------------------------- #
# /tiers
# ---------------------------------------------------------------------- #


def test_tiers_lists_plans_and_notes_billing_not_configured():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_tiers(_ctx(users_conn, journal_conn))
    assert "Pro" in reply.text
    assert "billing isn't configured" in reply.text.lower()
    assert reply.keyboard is None  # no portal url configured


# ---------------------------------------------------------------------- #
# /export
# ---------------------------------------------------------------------- #


def test_export_with_no_trades_says_so():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_export(_ctx(users_conn, journal_conn))
    assert reply.document is None
    assert "nothing to export" in reply.text.lower()


def test_export_delivers_a_csv_document():
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "10.00"]))
    reply = handlers.handle_export(_ctx(users_conn, journal_conn))
    assert reply.document is not None
    filename, content = reply.document
    assert filename.endswith(".csv")
    assert b"TSLA" in content


# ---------------------------------------------------------------------- #
# /help
# ---------------------------------------------------------------------- #


def test_help_lists_commands_and_a_gambling_resource_line():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_help(_ctx(users_conn, journal_conn))
    assert "/status" in reply.text and "/took" in reply.text
    assert "ncpgambling.org" in reply.text.lower()


def test_help_in_a_group_points_people_to_dm_the_bot():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_help(_ctx(users_conn, journal_conn, chat_type="group", bot_username="KestrelBot"))
    assert "@KestrelBot" in reply.text
    assert "/start" in reply.text
    assert "DM" in reply.text


def test_help_in_a_group_without_a_known_username_still_says_dm_me():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_help(_ctx(users_conn, journal_conn, chat_type="group", bot_username=None))
    assert "DM me" in reply.text


def test_help_in_dm_for_an_unonboarded_user_nudges_toward_start():
    users_conn, journal_conn = _setup(onboarded=False)
    reply = handlers.handle_help(_ctx(users_conn, journal_conn, chat_type="private"))
    assert "New here" in reply.text and "/start" in reply.text


def test_help_in_dm_for_an_onboarded_user_has_no_setup_nag():
    users_conn, journal_conn = _setup(onboarded=True)
    reply = handlers.handle_help(_ctx(users_conn, journal_conn, chat_type="private"))
    assert "New here" not in reply.text
    assert "DM " not in reply.text


# ---------------------------------------------------------------------- #
# /halt
# ---------------------------------------------------------------------- #


def test_halt_as_regular_user_only_stops_their_own_session():
    users_conn, journal_conn = _setup(admin=False)
    reply = handlers.handle_halt(_ctx(users_conn, journal_conn))
    assert "your alerts" in reply.text.lower()
    assert db.get_user(users_conn, 1).is_halted_for_session(NOW.date())
    assert not _app().halt_file.exists()


def test_halt_as_admin_engages_a_global_halt():
    users_conn, journal_conn = _setup(user_id=999, admin=True)
    app = _app()
    if app.halt_file.exists():
        app.halt_file.unlink()
    ctx = HandlerContext(
        client=None, users_conn=users_conn, journal_conn=journal_conn, user=db.get_user(users_conn, 999),
        chat_id=999, chat_type="private", args=[], now=NOW, app=app,
    )
    reply = handlers.handle_halt(ctx)
    assert "global halt" in reply.text.lower()
    assert app.halt_file.exists()
    app.halt_file.unlink()
