"""Inline button handlers — the tap-to-journal path from HIGH alerts, plus
the button/typed-text steps of /start's onboarding flow. Each handler is
`(CallbackContext) -> CallbackReply`; the dispatcher always answers the
callback query (stopping the client-side spinner) and then applies
edit_text/send_text if set. See tradebot.telegram_bot.dispatcher.
"""
from __future__ import annotations

from datetime import timedelta

from tradebot.journal import get_contract_outcome
from tradebot.rendering import templates
from tradebot.rendering.fields import ts
from tradebot.telegram_bot import access, db, keyboards
from tradebot.telegram_bot.context import CallbackReply
from tradebot.telegram_bot.handlers import (
    BETA_PRICING_NOTICE,
    _RESUME_ONBOARDING_TEXT,
    _resolve_detection,
    finish_onboarding,
    log_took,
)


# -------------------------------------------------------------------- #
# Alert action buttons. Responses are confirmed via the toast (the
# callback_query payload doesn't reliably carry the original message
# text, so we don't try to rewrite the alert in place — that would risk
# corrupting its formatting) rather than editing the alert message itself.
# -------------------------------------------------------------------- #


def handle_took_button(ctx) -> CallbackReply:
    row = _resolve_detection(ctx, ctx.arg)
    if row is None:
        return CallbackReply(toast="Can't find that alert anymore.", show_alert=True)
    detection_id, symbol, kinds, tier, alert_ts_utc, close, no_trade, primary_kind, trend = row
    result = log_took(
        ctx, detection_id, symbol, primary_kind or kinds.split(",")[0], tier, alert_ts_utc,
        bool(no_trade), None, close, direction=trend,
    )
    if isinstance(result, str):
        return CallbackReply(toast=f"Already logged ({result}).", show_alert=False)
    return CallbackReply(
        toast="Logged as taken.",
        send_text="How were you feeling on this one? (optional)",
        send_keyboard=keyboards.mood_keyboard(result.id),
    )


def handle_mood_button(ctx) -> CallbackReply:
    """The optional one-tap mood prompt sent right after /took (or the "I
    took this" button). ctx.arg is "trade_id:mood" — the only callback in
    this module carrying two pieces of data, so it's parsed here rather
    than by the dispatcher's single-partition prefix routing."""
    trade_id, _, mood = ctx.arg.partition(":")
    trade = db.get_trade(ctx.users_conn, trade_id)
    if trade is None or trade.telegram_user_id != ctx.user.telegram_user_id:
        return CallbackReply(toast="Can't find that trade anymore.", show_alert=True)
    if mood not in db.MOOD_CHOICES:
        return CallbackReply(toast="Unrecognized option.", show_alert=True)
    db.set_trade_mood(ctx.users_conn, trade_id, mood)
    return CallbackReply(toast="Logged.", edit_text=f"Logged: {mood}.", edit_keyboard=None)


def handle_skip_button(ctx) -> CallbackReply:
    row = _resolve_detection(ctx, ctx.arg)
    if row is None:
        return CallbackReply(toast="Can't find that alert anymore.", show_alert=True)
    detection_id = row[0]
    if db.has_responded(ctx.users_conn, ctx.user.telegram_user_id, detection_id):
        return CallbackReply(toast="Already logged.", show_alert=False)
    db.record_alert_response(ctx.users_conn, ctx.user.telegram_user_id, detection_id, "skipped", ctx.now)
    return CallbackReply(toast="Logged as skipped.", show_alert=False)


_NO_TRADE_EXPLANATION = (
    "No tradable contract was found for this alert — either the options chain was "
    "unavailable or the nearest strike failed the liquidity filter (spread or open "
    "interest too thin)."
)


def handle_whynt_button(ctx) -> CallbackReply:
    row = _resolve_detection(ctx, ctx.arg)
    if row is None:
        return CallbackReply(toast="Can't find that alert anymore.", show_alert=True)
    no_trade = row[6]
    if no_trade is None:
        text = "NO TRADE tracking wasn't recorded for this alert."
    elif no_trade == 0:
        text = "This one actually had a tradable contract — see the Breakeven line on the alert."
    else:
        text = _NO_TRADE_EXPLANATION
    return CallbackReply(toast=text, show_alert=True)


def handle_outcome_button(ctx) -> CallbackReply:
    """'How'd it play out?' — the recommended contract's own outcome
    (profitable or not at each checkpoint, against OUR entry) plus its
    real day range (the most anyone could have made on it that day,
    independent of our entry timing). See
    tradebot.rendering.templates.render_contract_outcome."""
    row = _resolve_detection(ctx, ctx.arg)
    if row is None:
        return CallbackReply(toast="Can't find that alert anymore.", show_alert=True)
    detection_id = row[0]
    outcome = get_contract_outcome(ctx.journal_conn, detection_id)
    if outcome is None:
        return CallbackReply(toast="No tradable contract was selected for this alert.", show_alert=True)
    return CallbackReply(toast="Here you go.", send_text=templates.render_contract_outcome(outcome, ctx.now))


# -------------------------------------------------------------------- #
# Onboarding: risk ack -> timezone
# -------------------------------------------------------------------- #


def handle_ack_risk_button(ctx) -> CallbackReply:
    db.set_risk_ack(ctx.users_conn, ctx.user.telegram_user_id, ctx.now)
    db.set_onboarding_step(ctx.users_conn, ctx.user.telegram_user_id, "sensitivity")
    return CallbackReply(
        toast="Got it.",
        edit_text=f"{BETA_PRICING_NOTICE}\n\n{_RESUME_ONBOARDING_TEXT['sensitivity']}",
        edit_keyboard=keyboards.sensitivity_keyboard(),
    )


def handle_sensitivity_button(ctx) -> CallbackReply:
    sensitivity = ctx.arg
    db.set_alert_sensitivity(ctx.users_conn, ctx.user.telegram_user_id, sensitivity)
    db.set_onboarding_step(ctx.users_conn, ctx.user.telegram_user_id, "timezone")
    return CallbackReply(
        toast=f"{sensitivity.capitalize()}.",
        edit_text=_RESUME_ONBOARDING_TEXT["timezone"],
        edit_keyboard=keyboards.timezone_keyboard(),
    )


def handle_timezone_button(ctx) -> CallbackReply:
    tz_name = ctx.arg
    db.set_timezone(ctx.users_conn, ctx.user.telegram_user_id, tz_name)
    db.set_onboarding_step(ctx.users_conn, ctx.user.telegram_user_id, "speak_timing")
    return CallbackReply(
        toast=f"Timezone set to {tz_name}.",
        edit_text=_RESUME_ONBOARDING_TEXT["speak_timing"],
        edit_keyboard=keyboards.speak_timing_keyboard(),
    )


# ET clock-time equivalents of "outside 9:30 AM-4:00 PM ET" for each of
# keyboards.COMMON_TIMEZONES — all four are US zones that shift DST on
# the same calendar dates ET does, so a fixed hour offset from ET is
# exact, not approximate, for every one of them. "Market hours" in the
# onboarding flow means exactly this window; a user who wants a different
# window picks "Custom" and gets the typed quiet-hours prompt instead.
_MARKET_HOURS_QUIET_WINDOW = {
    "America/New_York": ("16:00", "09:30"),
    "America/Chicago": ("15:00", "08:30"),
    "America/Denver": ("14:00", "07:30"),
    "America/Los_Angeles": ("13:00", "06:30"),
}


def handle_speak_timing_button(ctx) -> CallbackReply:
    choice = ctx.arg
    if choice == "custom":
        db.set_onboarding_step(ctx.users_conn, ctx.user.telegram_user_id, "quiet_hours")
        return CallbackReply(
            toast="Got it.", edit_text=_RESUME_ONBOARDING_TEXT["quiet_hours"], edit_keyboard=None,
        )
    if choice == "always":
        db.set_quiet_hours(ctx.users_conn, ctx.user.telegram_user_id, None, None)
    elif choice == "market_hours":
        start, end = _MARKET_HOURS_QUIET_WINDOW.get(ctx.user.timezone, _MARKET_HOURS_QUIET_WINDOW["America/New_York"])
        db.set_quiet_hours(ctx.users_conn, ctx.user.telegram_user_id, start, end)
    else:
        return CallbackReply(toast="Unrecognized option.", show_alert=True)
    reply = finish_onboarding(ctx)
    return CallbackReply(toast="Done.", edit_text=reply.text, edit_keyboard=None)


# -------------------------------------------------------------------- #
# /pause buttons
# -------------------------------------------------------------------- #

_PAUSE_DURATIONS = {
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}


def handle_pause_button(ctx) -> CallbackReply:
    choice = ctx.arg
    if choice == "eod":
        # "rest of day" — session-scoped like /halt's personal stop, but
        # self-liftable via /resume (unlike a loss-limit lock).
        session_date = ctx.app.session_date_fn(ctx.now)
        db.set_session_halt(ctx.users_conn, ctx.user.telegram_user_id, session_date)
        return CallbackReply(toast="Paused for the rest of today.", edit_text="Paused for the rest of today's session. /resume to lift early.")
    duration = _PAUSE_DURATIONS.get(choice)
    if duration is None:
        return CallbackReply(toast="Unrecognized option.", show_alert=True)
    until = ctx.now + duration
    db.set_pause(ctx.users_conn, ctx.user.telegram_user_id, until, reason=f"user-requested {choice}")
    return CallbackReply(toast="Paused.", edit_text=f"Paused until {ts(until)}. /resume to lift early.")


# -------------------------------------------------------------------- #
# /watchlist buttons
# -------------------------------------------------------------------- #


def handle_watchlist_button(ctx) -> CallbackReply:
    if not access.can_access(ctx.user, "watchlist_edit"):
        return CallbackReply(toast="Editing isn't available on your plan — see /tiers.", show_alert=True)

    if ctx.arg == "save":
        active = db.get_watchlist(ctx.users_conn, ctx.user.telegram_user_id) or ctx.app.default_watchlist
        return CallbackReply(
            toast="Saved.",
            edit_text=f"<b>Your watchlist</b>\n\n{', '.join(active)}",
            edit_keyboard=None,
        )

    symbol = ctx.arg
    if symbol not in ctx.app.default_watchlist:
        return CallbackReply(toast="Not a supported symbol.", show_alert=True)
    now_selected = db.toggle_watchlist_symbol(ctx.users_conn, ctx.user.telegram_user_id, symbol)
    active = db.get_watchlist(ctx.users_conn, ctx.user.telegram_user_id) or ctx.app.default_watchlist
    return CallbackReply(
        toast=f"{symbol} {'added' if now_selected else 'removed'}.",
        edit_text="<b>Your watchlist</b>\n\nTap a symbol to toggle it, then Save.",
        edit_keyboard=keyboards.watchlist_keyboard(ctx.app.default_watchlist, set(active)),
    )


CALLBACK_HANDLERS = {
    "took": handle_took_button,
    "mood": handle_mood_button,
    "skip": handle_skip_button,
    "whynt": handle_whynt_button,
    "outcome": handle_outcome_button,
    "ack_risk": handle_ack_risk_button,
    "sens": handle_sensitivity_button,
    "tz": handle_timezone_button,
    "speak": handle_speak_timing_button,
    "pause": handle_pause_button,
    "wl": handle_watchlist_button,
}
