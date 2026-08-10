"""One test per command handler, plus the mid-session limit-raise queueing
rule exercised through the actual /limits handler (not just the db layer).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradebot.detectors import Detection
from tradebot.events import add_event_window
from tradebot.journal import connect as journal_connect
from tradebot.journal import set_no_trade, write_cluster
from tradebot.telegram_bot import db, handlers
from tradebot.telegram_bot.context import AppConfig, HandlerContext

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)

# Every reply goes out with parse_mode=HTML (see client.BotClient.send_message)
# — a literal '<' in plain usage-hint text (e.g. "<max trades/day>") gets
# parsed as a bogus tag and Telegram rejects the WHOLE message with a 400,
# which the dispatcher just logs and swallows: the user gets nothing, no
# error shown. Caught live when /start's "limits" resume text 400'd in
# production. _ALLOWED_TAG_RE is the small whitelist of tags this bot
# actually renders; anything else must be written as &lt;/&gt;.
_ALLOWED_TAG_RE = re.compile(r"</?(b|i|code)>")


def _assert_html_safe(text: str) -> None:
    stripped = _ALLOWED_TAG_RE.sub("", text)
    assert "<" not in stripped and ">" not in stripped, f"unescaped angle bracket in Telegram HTML text: {text!r}"


def _app(market_open=True, bot_username=None, max_active_users=None):
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
        incidents_path=Path("/tmp/watchtower_test_incidents_handlers.jsonl"),
        bot_username=bot_username,
        max_active_users=max_active_users,
    )


def _setup(user_id=1, onboarded=True, admin=False, market_open=True):
    users_conn = db.connect(":memory:")
    journal_conn = journal_connect(":memory:")
    db.get_or_create_user(users_conn, user_id, user_id, "alice")
    if onboarded:
        db.mark_onboarded(users_conn, user_id, NOW)
        db.set_risk_ack(users_conn, user_id, NOW)
    if admin:
        db.set_admin(users_conn, user_id, True)
    return users_conn, journal_conn


def _ctx(users_conn, journal_conn, user_id=1, args=None, now=NOW, market_open=True, chat_type="private",
         bot_username=None, max_active_users=None):
    return HandlerContext(
        client=None, users_conn=users_conn, journal_conn=journal_conn, user=db.get_user(users_conn, user_id),
        chat_id=user_id, chat_type=chat_type, args=args or [], now=now,
        app=_app(market_open=market_open, bot_username=bot_username, max_active_users=max_active_users),
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
    assert "beta" in reply.text.lower()


def test_start_labels_beta_and_repositions_as_discipline_tool_not_a_proven_edge():
    users_conn, journal_conn = _setup(onboarded=False)
    reply = handlers.handle_start(_ctx(users_conn, journal_conn))
    assert "beta" in reply.text.lower()
    assert "discipline and journaling system" in reply.text.lower()
    assert "not a promise of" in reply.text.lower()
    assert "pause" in reply.text.lower()


def test_start_states_plainly_when_the_hit_rate_is_not_yet_significant():
    """The exact regression this whole feature exists for: a coin-flip
    hit rate must never be silently presented as an edge."""
    users_conn, journal_conn = _setup(onboarded=False)
    journal_conn2 = journal_connect(":memory:")
    base = NOW
    for i in range(20):
        did = write_cluster(
            journal_conn2, session=base.date().isoformat(), symbol="TSLA", ts_utc=(base + timedelta(minutes=i)).isoformat(),
            kinds="level_break", headlines="h", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection("TSLA", "level_break", base, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        # exactly 50/50 continuation -> hit_rate 0.5 -> z=0 -> not significant
        journal_conn2.execute(
            "INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)",
            (did, 101 if i % 2 == 0 else 99),
        )
    journal_conn2.commit()

    reply = handlers.handle_start(_ctx(users_conn, journal_conn2))
    assert "not yet statistically different from a coin flip" in reply.text.lower()
    assert "measured, real edge" not in reply.text.lower()


# --------------------------------------------------------------------------
# /start — capacity cap + waitlist
# --------------------------------------------------------------------------


def test_start_waitlists_a_brand_new_user_once_capacity_is_hit():
    users_conn, journal_conn = _setup(onboarded=False)
    db.get_or_create_user(users_conn, 998, 998, "already_onboarded")
    db.mark_onboarded(users_conn, 998, NOW)  # fills the one available slot

    reply = handlers.handle_start(_ctx(users_conn, journal_conn, max_active_users=1))
    assert "capacity" in reply.text.lower() and "waitlist" in reply.text.lower()
    user = db.get_user(users_conn, 1)
    assert user.waitlisted_at is not None
    assert user.onboarding_step is None  # never entered the onboarding flow


def test_start_does_not_waitlist_below_capacity():
    users_conn, journal_conn = _setup(onboarded=False)
    reply = handlers.handle_start(_ctx(users_conn, journal_conn, max_active_users=10))
    assert "waitlist" not in reply.text.lower()
    assert db.get_user(users_conn, 1).onboarding_step == "risk_ack"


def test_start_lets_a_waitlisted_user_in_once_a_slot_opens():
    users_conn, journal_conn = _setup(onboarded=False)
    db.set_waitlisted(users_conn, 1, NOW)

    still_full = handlers.handle_start(_ctx(users_conn, journal_conn, max_active_users=0))
    assert "waitlist" in still_full.text.lower()
    assert db.get_user(users_conn, 1).waitlisted_at is not None

    opened_up = handlers.handle_start(_ctx(users_conn, journal_conn, max_active_users=10))
    assert "waitlist" not in opened_up.text.lower()
    user = db.get_user(users_conn, 1)
    assert user.waitlisted_at is None
    assert user.onboarding_step == "risk_ack"


# --------------------------------------------------------------------------
# /start — risk ack transition includes the beta pricing notice
# --------------------------------------------------------------------------


def test_ack_risk_button_states_beta_pricing_before_timezone():
    from tradebot.telegram_bot import callbacks
    from tradebot.telegram_bot.context import CallbackContext

    users_conn, journal_conn = _setup(onboarded=False)
    ctx = CallbackContext(
        client=None, users_conn=users_conn, journal_conn=journal_conn, user=db.get_user(users_conn, 1),
        chat_id=1, message_id=1, arg="", now=NOW, app=_app(),
    )
    result = callbacks.handle_ack_risk_button(ctx)
    assert "free during beta" in result.edit_text.lower()
    assert "30 days" in result.edit_text.lower()
    assert "founding-member pricing" in result.edit_text.lower()
    assert "never free forever" not in result.edit_text.lower()  # no such promise is made
    assert result.edit_keyboard is not None  # still lands on the timezone picker


def test_completing_onboarding_sends_a_real_sample_alert():
    """The 'here's what you actually get' moment — a real historical
    alert, not a mockup, right when onboarding finishes (the 'custom'
    quiet-hours path, the last of the two ways onboarding can finish —
    see also callbacks.handle_speak_timing_button's 'always'/'market_hours'
    branches, covered in test_callbacks.py)."""
    users_conn, journal_conn = _setup(onboarded=False)
    db.set_onboarding_step(users_conn, 1, "quiet_hours")
    reply = handlers._handle_quiet_hours_text(_ctx(users_conn, journal_conn), "none")
    assert db.get_user(users_conn, 1).is_onboarded
    assert "you're set" in reply.text.lower()
    assert "what an alert actually looks like" in reply.text.lower()
    assert "not a mockup" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /status
# ---------------------------------------------------------------------- #


def test_status_reports_market_state_and_users_own_lock_state():
    users_conn, journal_conn = _setup()
    db.set_pause(users_conn, 1, NOW + timedelta(hours=1), "manual")
    reply = handlers.handle_status(_ctx(users_conn, journal_conn))
    assert "paused" in reply.text.lower()
    assert "market" in reply.text.lower() or "live" in reply.text.lower() or "closed" in reply.text.lower()
    assert "beta" in reply.text.lower()


def _write_heartbeat(path: Path, ts_utc: str) -> None:
    path.write_text(json.dumps({"ts_utc": ts_utc}))


def test_status_does_not_flag_stale_over_a_weekend_with_the_market_closed():
    """Regression: an old heartbeat is expected and correct while the
    market's closed (see runner.py's OFF_SESSION_IDLE_SECONDS — it
    doesn't write a heartbeat at all outside a trading session), not a
    fault worth alarming someone checking /status over the weekend."""
    users_conn, journal_conn = _setup()
    ctx = _ctx(users_conn, journal_conn, market_open=False)
    old_ts = (NOW - timedelta(days=2)).isoformat()
    _write_heartbeat(ctx.app.heartbeat_file, old_ts)
    reply = handlers.handle_status(ctx)
    assert "stale" not in reply.text.lower()


def test_status_still_flags_a_genuinely_stale_feed_during_market_hours():
    users_conn, journal_conn = _setup()
    ctx = _ctx(users_conn, journal_conn, market_open=True)
    old_ts = (NOW - timedelta(minutes=30)).isoformat()
    _write_heartbeat(ctx.app.heartbeat_file, old_ts)
    reply = handlers.handle_status(ctx)
    assert "stale" in reply.text.lower()


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
    assert "coin flip" in reply.text.lower()  # the significance verdict — n=6 is nowhere near proof either way


def test_performance_never_calls_a_coin_flip_hit_rate_a_measured_real_edge():
    """The exact regression: /performance must state the coin-flip
    verdict plainly, the same significance math as /start, not a rosier
    number with no context."""
    users_conn, journal_conn = _setup()
    base = NOW
    for i in range(20):
        did = write_cluster(
            journal_conn, session=base.date().isoformat(), symbol="TSLA", ts_utc=(base + timedelta(minutes=i)).isoformat(),
            kinds="level_break", headlines="h", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection("TSLA", "level_break", base, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        journal_conn.execute(
            "INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)",
            (did, 101 if i % 2 == 0 else 99),
        )
        set_no_trade(journal_conn, did, False)
    journal_conn.commit()
    reply = handlers.handle_performance(_ctx(users_conn, journal_conn))
    assert "not yet statistically different from a coin flip" in reply.text.lower()
    assert "measured, real edge" not in reply.text.lower()


# ---------------------------------------------------------------------- #
# /example
# ---------------------------------------------------------------------- #


def test_example_with_an_empty_journal_says_so_honestly():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_example(_ctx(users_conn, journal_conn))
    assert "no real win in the journal yet" in reply.text.lower()
    assert "no real day with enough tracked alerts" in reply.text.lower()


def test_example_shows_a_real_win_with_the_correct_option_side():
    users_conn, journal_conn = _setup()
    base = NOW
    for i in range(6):
        did = write_cluster(
            journal_conn, session=base.date().isoformat(), symbol="TSLA", ts_utc=(base + timedelta(minutes=5 * i)).isoformat(),
            kinds="gap", headlines="TSLA gapped up", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection("TSLA", "gap", base, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        journal_conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, 105))
    journal_conn.commit()

    reply = handlers.handle_example(_ctx(users_conn, journal_conn))
    assert "TSLA" in reply.text
    assert "calls favored" in reply.text.lower()
    assert "TSLA gapped up" in reply.text
    assert "hit rate" in reply.text.lower()
    assert "100.00%" in reply.text  # every seeded alert continued -> a real, if unrealistic, day rate
    assert "+100.00%" not in reply.text  # hit rate is a magnitude, not a signed move — no leading '+'
    assert "not typical" in reply.text.lower()
    assert "coin flip" in reply.text.lower()


# ---------------------------------------------------------------------- #
# /me
# ---------------------------------------------------------------------- #


def test_me_reports_not_enough_data_for_a_fresh_user():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_me(_ctx(users_conn, journal_conn))
    assert "not enough" in reply.text.lower()


def test_me_shows_the_headline_no_trade_comparison_and_new_dimensions():
    users_conn, journal_conn = _setup()
    for i, symbol in enumerate(["TSLA", "AAPL", "SPY", "QQQ", "NVDA"]):
        detection_id = _write_high_alert(journal_conn, symbol=symbol, session=NOW.date().isoformat())
        handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "5.00"]))
        trade = db.list_trades(users_conn, 1)[-1]
        db.log_closed(users_conn, trade.id, exit_price=5.50, closed_at=NOW)
    reply = handlers.handle_me(_ctx(users_conn, journal_conn))
    assert "After a NO TRADE gate" in reply.text
    assert "By direction:" in reply.text
    assert "By hold time:" in reply.text
    assert "Adherence:" in reply.text
    assert "Logging completeness:" in reply.text


def test_me_recap_reports_not_enough_data():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_me(_ctx(users_conn, journal_conn, args=["recap"]))
    assert "not enough" in reply.text.lower()


def test_me_recap_shows_leaks_for_the_current_month():
    users_conn, journal_conn = _setup()
    for i in range(db.MIN_STAT_SAMPLE):
        detection_id = _write_high_alert(journal_conn, symbol="TSLA", session=NOW.date().isoformat())
        handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "5.00"]))
        trade = db.list_trades(users_conn, 1)[-1]
        db.set_trade_mood(users_conn, trade.id, "calm")
        db.log_closed(users_conn, trade.id, exit_price=5.50, closed_at=NOW)
    for i in range(db.MIN_STAT_SAMPLE):
        detection_id = _write_high_alert(journal_conn, symbol="AAPL", session=NOW.date().isoformat())
        handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "5.00"]))
        trade = db.list_trades(users_conn, 1)[-1]
        db.set_trade_mood(users_conn, trade.id, "revenge")
        db.log_closed(users_conn, trade.id, exit_price=4.50, closed_at=NOW)
    reply = handlers.handle_me(_ctx(users_conn, journal_conn, args=["recap"]))
    assert f"Recap — {NOW.year:04d}-{NOW.month:02d}" in reply.text
    assert "revenge" in reply.text.lower()
    assert "'calm'" not in reply.text.lower()  # calm beat the month's average, not a leak


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


def test_took_auto_fills_direction_and_offers_a_mood_keyboard():
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)  # trend="up" per _write_high_alert
    reply = handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "2", "5.10"]))
    assert reply.keyboard is not None
    trade = db.list_trades(users_conn, 1)[0]
    assert trade.direction == "up"
    assert trade.kind == "gap"  # primary_kind fallback (first of "gap,level_break")


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
    assert trade.status == "closed" and trade.note == "confident"  # trailing /closed text is a free-text note


def test_closed_with_no_open_trade_gives_a_usage_hint():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_closed(_ctx(users_conn, journal_conn))
    assert "no open trade" in reply.text.lower()


def test_closed_by_short_price_is_never_misread_as_a_trade_id():
    """Regression: a short whole-dollar price like '5' must never be
    treated as a trade-id prefix lookup, even if some open trade's id
    happens to start with '5' — see handle_closed's len>=8 gate."""
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "4.00"]))
    reply = handlers.handle_closed(_ctx(users_conn, journal_conn, args=["5"]))
    assert "+25.00%" in reply.text  # (5-4)/4 -> treated as a price close, not a failed id lookup


def test_closed_by_an_all_digit_short_id_prefix_is_treated_as_an_id_not_a_price():
    """Regression: the 8-char short id prefix this bot shows (see
    log_took's result.id[:8]) is hex, so an all-digit one is possible —
    it must resolve as a trade-id prefix lookup, not get silently
    misread as an absurd price, the way a bare numeric first arg
    normally would be."""
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "4.00"]))
    trade = db.list_trades(users_conn, 1)[-1]
    # Force this trade's real (32-char) id to start with an all-digit
    # 8-char prefix, matching the exact edge case this fix is for.
    forced_id = "12345678" + trade.id[8:]
    users_conn.execute("UPDATE user_trades SET id = ? WHERE id = ?", (forced_id, trade.id))
    users_conn.commit()

    reply = handlers.handle_closed(_ctx(users_conn, journal_conn, args=["12345678", "5.00"]))
    assert "+25.00%" in reply.text
    assert db.get_trade(users_conn, forced_id).status == "closed"


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


def test_limits_confirmation_uses_a_friendly_label_not_the_raw_field_name():
    """Regression: confirmations must never leak internal snake_case
    column names like 'max_trades_per_day' to the user."""
    users_conn, journal_conn = _setup()
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "5"], market_open=True))
    assert "max trades/day" in reply.text.lower()
    assert "max_trades_per_day" not in reply.text
    assert "is now 5," in reply.text  # a plain count, not "5.0"

    loss_reply = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["loss", "200"], market_open=True))
    assert "max daily loss" in loss_reply.text.lower()
    assert "max_daily_loss" not in loss_reply.text
    assert "$200.00" in loss_reply.text  # money-formatted, matching the /limits summary view


def test_limits_queued_confirmation_uses_a_friendly_label_too():
    users_conn, journal_conn = _setup()
    handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "5"], market_open=False))
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "10"], market_open=True))
    assert "max trades/day" in reply.text.lower()
    assert "max_trades_per_day" not in reply.text


def test_limits_increase_mid_session_is_queued_with_an_explanation():
    users_conn, journal_conn = _setup()
    handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "5"], market_open=False))
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["trades", "10"], market_open=True))
    assert "queued" in reply.text.lower()
    assert "for exactly this moment" in reply.text.lower()
    assert db.get_user(users_conn, 1).max_trades_per_day == 5  # unchanged until next session


def test_limits_account_and_risk_apply_immediately_even_mid_session():
    """account/risk are sizing inputs, not protective caps — no queueing,
    unlike trades/loss/size above, even with the market open."""
    users_conn, journal_conn = _setup()
    r1 = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["account", "10000"], market_open=True))
    assert "effective immediately" in r1.text.lower()
    r2 = handlers.handle_limits(_ctx(users_conn, journal_conn, args=["risk", "1.5"], market_open=True))
    assert "effective immediately" in r2.text.lower()
    user = db.get_user(users_conn, 1)
    assert user.account_size == 10_000
    assert user.risk_per_trade_pct == 1.5


def test_limits_shows_position_sizing_section():
    users_conn, journal_conn = _setup()
    handlers.handle_limits(_ctx(users_conn, journal_conn, args=["account", "10000"]))
    handlers.handle_limits(_ctx(users_conn, journal_conn, args=["risk", "1"]))
    reply = handlers.handle_limits(_ctx(users_conn, journal_conn))
    assert "Position sizing" in reply.text
    assert "$10,000.00" in reply.text
    assert "1.00%" in reply.text


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


def test_watchlist_editing_is_available_to_everyone_during_beta():
    """No feature gating — see tradebot.telegram_bot.access.can_access,
    which always returns True right now."""
    users_conn, journal_conn = _setup()
    reply = handlers.handle_watchlist(_ctx(users_conn, journal_conn))
    assert reply.keyboard is not None


# ---------------------------------------------------------------------- #
# /events
# ---------------------------------------------------------------------- #


def test_events_says_nothing_loaded_rather_than_fabricating():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_events(_ctx(users_conn, journal_conn))
    assert "no known earnings, macro, or filing events" in reply.text.lower()


def test_events_lists_what_was_actually_loaded():
    """/events reads the real event_windows table (tradebot.events), the
    same source as runner.py's pre-open card — not the old, always-empty
    telegram_bot.db events table."""
    users_conn, journal_conn = _setup()
    add_event_window(
        journal_conn, symbol="TSLA", kind="earnings", start_utc=NOW, end_utc=NOW + timedelta(hours=1),
        severity="downgrade", source="test", detail="before the open",
    )
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


def test_tiers_shows_plan_founding_member_status_and_the_beta_notice():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_tiers(_ctx(users_conn, journal_conn))
    assert "beta" in reply.text.lower()
    assert "founding member" in reply.text.lower()
    assert "30 days" in reply.text.lower()


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


def test_export_includes_direction_and_note_columns():
    """Data is theirs — every field a trade can carry, including the new
    direction/note columns, must round-trip into the CSV."""
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "10.00"]))
    handlers.handle_closed(_ctx(users_conn, journal_conn, args=["11.00", "took", "half", "size"]))
    reply = handlers.handle_export(_ctx(users_conn, journal_conn))
    _, content = reply.document
    header = content.decode().splitlines()[0]
    assert "direction" in header and "note" in header
    assert b"up" in content
    assert b"took half size" in content


def test_export_neutralizes_a_note_that_looks_like_a_spreadsheet_formula():
    """Regression: a free-text note starting with =, +, -, or @ must not
    open as a live formula in Excel/Sheets — this export is meant to be
    shared, not just read back into this bot."""
    users_conn, journal_conn = _setup()
    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "10.00"]))
    handlers.handle_closed(_ctx(
        users_conn, journal_conn, args=["11.00", "=cmd|'/c", "calc'!A1"],
    ))
    reply = handlers.handle_export(_ctx(users_conn, journal_conn))
    _, content = reply.document
    assert b"'=cmd" in content  # neutralized with a leading apostrophe
    assert b",=cmd" not in content  # never written as a live formula cell


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
    reply = handlers.handle_help(_ctx(users_conn, journal_conn, chat_type="group", bot_username="PerchBot"))
    assert "@PerchBot" in reply.text
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
# /feedback
# ---------------------------------------------------------------------- #


def test_feedback_with_no_message_gives_a_usage_hint():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_feedback(_ctx(users_conn, journal_conn))
    assert "usage" in reply.text.lower()
    assert users_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_feedback_persists_the_message():
    users_conn, journal_conn = _setup()
    reply = handlers.handle_feedback(_ctx(users_conn, journal_conn, args=["the", "sizing", "math", "is", "great"]))
    assert "logged" in reply.text.lower() or "thanks" in reply.text.lower()
    row = users_conn.execute("SELECT telegram_user_id, message FROM feedback").fetchone()
    assert row == (1, "the sizing math is great")


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


# ---------------------------------------------------------------------- #
# Telegram HTML safety — regression coverage for the production incident
# above. Every usage-hint / error string that uses angle-bracket
# placeholder notation must escape it, or the real send silently 400s.
# ---------------------------------------------------------------------- #


def test_no_usage_hint_text_has_an_unescaped_angle_bracket():
    users_conn, journal_conn = _setup(onboarded=False)
    db.set_onboarding_step(users_conn, 1, "quiet_hours")
    _assert_html_safe(handlers.handle_start(_ctx(users_conn, journal_conn)).text)
    _assert_html_safe(handlers._handle_quiet_hours_text(_ctx(users_conn, journal_conn), "not a time range").text)

    users_conn, journal_conn = _setup()
    _assert_html_safe(handlers.handle_limits(_ctx(users_conn, journal_conn)).text)  # the /limits usage hint itself

    users_conn, journal_conn = _setup()
    _assert_html_safe(handlers.handle_took(_ctx(users_conn, journal_conn)).text)
    _assert_html_safe(handlers.handle_closed(_ctx(users_conn, journal_conn)).text)
    _assert_html_safe(handlers.handle_limits(_ctx(users_conn, journal_conn, args=["badfield", "1"])).text)
    _assert_html_safe(handlers.handle_feedback(_ctx(users_conn, journal_conn)).text)

    detection_id = _write_high_alert(journal_conn)
    handlers.handle_took(_ctx(users_conn, journal_conn, args=[detection_id, "1", "5.00"]))
    trade = db.list_trades(users_conn, 1)[-1]
    _assert_html_safe(handlers.handle_closed(_ctx(users_conn, journal_conn, args=[trade.id[:8]])).text)
