"""One function per command, matching tradebot.telegram_bot.commands.COMMANDS
exactly. Every handler is `(HandlerContext) -> Reply` — plain data in,
plain data out — so tests never need a real Telegram connection; see
tests/telegram_bot/test_handlers.py.

Free-text onboarding steps (quiet hours, risk limits) are `(ctx, text) ->
Reply` instead, registered in ONBOARDING_TEXT_STEPS and dispatched by
Dispatcher._maybe_handle_onboarding_text whenever the user's
onboarding_step expects typed input rather than a button tap.
"""
from __future__ import annotations

import csv
import html
import io
from datetime import datetime

from tradebot.events import events_for_date
from tradebot.rendering.fields import money, pct, qty, rate, ts
from tradebot.rendering.templates import render_pre_open_card
from tradebot.telegram_bot import access, db, keyboards, performance
from tradebot.telegram_bot.context import HandlerContext, Reply


def _parse_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fmt_bucket(bucket) -> str:
    if bucket is None:
        return "not enough samples yet"
    sign = "+" if bucket.avg_pnl_pct >= 0 else ""
    return f"{rate(bucket.win_rate * 100)} win, {sign}{bucket.avg_pnl_pct:.2f}% avg (n={qty(bucket.n)})"


def _fmt_risk_pct(value: float | None) -> str:
    """Unlike fields.pct (always signed — meaningful for a directional
    P/L move), a configured risk-per-trade % is a magnitude, not a signed
    change — a leading '+' would read as "gained 1%", not "risking 1%"."""
    return f"{value:.2f}%" if value is not None else "not set"


# -------------------------------------------------------------------- #
# /start — onboarding. Real track record first, explicit risk ack,
# then timezone (buttons) -> quiet hours (typed) -> limits (typed).
# -------------------------------------------------------------------- #


def _significance_verdict_line(tr, verbose: bool = True) -> str:
    """The one line that stops this text from ever implying a proven
    edge when the numbers don't back it up. Computed fresh from
    performance.significance_check every time this renders — never a
    fixed claim written into the copy by hand, so it can't quietly go
    stale as more sessions accumulate. verbose=False (onboarding) keeps
    the same real verdict but drops the "how much more data" elaboration
    that /performance (verbose=True, the deep-dive command) still shows
    in full — shorter is fine at the door, the substance isn't cut."""
    sig = tr.significance
    if sig.is_significant:
        direction = "better than" if tr.hit_rate > 0.5 else "worse than"
        line = f"That hit rate is currently statistically {direction} a coin flip (z={sig.z_score:.2f})"
        if not verbose:
            return line + "."
        return line + " — still provisional, and past performance doesn't guarantee anything going forward."
    if not verbose:
        return f"Not yet statistically different from a coin flip at this sample size (z={sig.z_score:.2f})."
    return (
        f"At this sample size, that hit rate is NOT yet statistically different from a coin flip "
        f"(z={sig.z_score:.2f}) — treat every number above as unproven, not a track record to trade on. "
        f"Roughly {qty(sig.n_needed_for_meaningful_edge)} alerts would be needed to confirm even a modest "
        f"real edge one way or the other."
    )


def _track_record_and_risk_text(ctx: HandlerContext) -> str:
    tr = performance.track_record(ctx.journal_conn, tier="high")
    lines = [
        f"<b>Welcome to {html.escape(ctx.app.bot_name)} — BETA.</b>",
        "",
        "A discipline and journaling system on top of a technical alert feed — not a promise of "
        "an edge. Free during beta.",
        "",
        "Your real track record, no cherry-picking:",
    ]
    if tr is None:
        lines.append("Not enough history yet for real numbers.")
    else:
        lines += [
            "",
            f"HIGH tier, last {qty(tr.sample_size)} alerts @ +{tr.offset_min}m:",
            f"  Hit rate: {rate(tr.hit_rate * 100)}   Avg move: {pct(tr.avg_return_pct)}",
            f"  Longest losing streak: {qty(tr.longest_losing_streak)} in a row",
            f"  Worst drawdown: {pct(tr.max_drawdown_pct)}",
            "",
            _significance_verdict_line(tr, verbose=False),
        ]
    lines += [
        "",
        "It reports patterns and what actually happened after — not advice, and losses happen. "
        "Beta also means alerts might pause here and there for fixes.",
        "",
        "Tap below to continue.",
    ]
    return "\n".join(lines)


def _settings_summary(ctx: HandlerContext) -> str:
    user = ctx.user
    lines = [
        f"<b>{html.escape(ctx.app.bot_name)} · BETA</b> — you're already set up.",
        "",
        f"Timezone: {html.escape(user.timezone)}",
        f"Quiet hours: {user.quiet_hours_start}–{user.quiet_hours_end}" if user.quiet_hours_start else "Quiet hours: none set",
        f"Max trades/day: {user.max_trades_per_day if user.max_trades_per_day is not None else 'not set'}",
        f"Max daily loss: {money(user.max_daily_loss) if user.max_daily_loss is not None else 'not set'}",
        f"Max position size: {user.max_position_size if user.max_position_size is not None else 'not set'}",
        "",
        "Change any of this with /limits, /watchlist, or /pause. This won't wipe or reset anything.",
    ]
    return "\n".join(lines)


_RESUME_ONBOARDING_TEXT = {
    "sensitivity": "How much signal do you want?",
    "timezone": "Pick your timezone:",
    "speak_timing": "When should Perch speak?",
    "quiet_hours": "What are your quiet hours (no alerts)? Reply like 22:00-06:00, or 'none'.",
}

# Shown once, right after the risk ack tap and before timezone setup — see
# handle_ack_risk_button. Plain, no hedging: free now, notice before any
# change, existing users protected if it ever happens. Never "free
# forever" — that's a promise nobody asked this bot to make.
BETA_PRICING_NOTICE = (
    "Free during beta. If this ever becomes paid, you'll get 30 days' notice "
    "and founding-member pricing — nothing changes without warning."
)


def _waitlist_text(ctx: HandlerContext, position: int) -> str:
    cap = ctx.app.max_active_users
    return (
        f"<b>{html.escape(ctx.app.bot_name)}</b> is at capacity right now ({qty(cap)} active users) — "
        f"you're #{qty(position)} on the waitlist. I'll message you the moment a spot opens. No action needed."
    )


def _is_over_capacity(ctx: HandlerContext) -> bool:
    cap = ctx.app.max_active_users
    if cap is None:
        return False
    return db.count_active_users(ctx.users_conn) >= cap


def _begin_onboarding(ctx: HandlerContext) -> Reply:
    db.set_onboarding_step(ctx.users_conn, ctx.user.telegram_user_id, "risk_ack")
    return Reply(text=_track_record_and_risk_text(ctx), keyboard=keyboards.risk_ack_keyboard())


def handle_start(ctx: HandlerContext) -> Reply:
    user = ctx.user
    if user.is_onboarded:
        return Reply(text=_settings_summary(ctx))

    if user.waitlisted_at is not None:
        if _is_over_capacity(ctx):
            position = db.waitlist_position(ctx.users_conn, user.telegram_user_id)
            return Reply(text=_waitlist_text(ctx, position))
        db.clear_waitlist(ctx.users_conn, user.telegram_user_id)  # a spot opened up — let them in
        return _begin_onboarding(ctx)

    if user.onboarding_step is None:
        if _is_over_capacity(ctx):
            db.set_waitlisted(ctx.users_conn, user.telegram_user_id, ctx.now)
            position = db.waitlist_position(ctx.users_conn, user.telegram_user_id)
            return Reply(text=_waitlist_text(ctx, position))
        return _begin_onboarding(ctx)

    if user.onboarding_step == "risk_ack":
        return Reply(text=_track_record_and_risk_text(ctx), keyboard=keyboards.risk_ack_keyboard())
    if user.onboarding_step == "sensitivity":
        return Reply(text=_RESUME_ONBOARDING_TEXT["sensitivity"], keyboard=keyboards.sensitivity_keyboard())
    if user.onboarding_step == "timezone":
        return Reply(text=_RESUME_ONBOARDING_TEXT["timezone"], keyboard=keyboards.timezone_keyboard())
    if user.onboarding_step == "speak_timing":
        return Reply(text=_RESUME_ONBOARDING_TEXT["speak_timing"], keyboard=keyboards.speak_timing_keyboard())
    return Reply(text=_RESUME_ONBOARDING_TEXT.get(user.onboarding_step, "Let's pick this back up — reply to continue."))


def finish_onboarding(ctx: HandlerContext) -> Reply:
    """The shared last step of onboarding, reached from either branch of
    'when should Perch speak?' (always/market hours finish immediately;
    custom finishes here once the typed quiet-hours reply is read — see
    _handle_quiet_hours_text). Real numeric risk limits (max trades/day,
    daily loss $, position size) are deliberately NOT asked here anymore
    — see /limits, already a complete, always-available command; keeping
    onboarding itself to what the spec calls "progressive personalization"
    means not front-loading every setting before someone has seen a
    single alert."""
    db.set_onboarding_step(ctx.users_conn, ctx.user.telegram_user_id, None)
    db.mark_onboarded(ctx.users_conn, ctx.user.telegram_user_id, ctx.now)
    from tradebot.rendering.templates import render_sample_alert

    return Reply(
        text="You're set. Alerts will respect your quiet hours and sensitivity from now on.\n"
        "/limits to set daily loss and trade caps, /watchlist to customize your symbols, /help any time.\n"
        "\n"
        f"{render_sample_alert()}"
    )


def _handle_quiet_hours_text(ctx: HandlerContext, text: str) -> Reply:
    raw = text.strip().lower()
    if raw in ("none", "no", "n/a", "skip"):
        db.set_quiet_hours(ctx.users_conn, ctx.user.telegram_user_id, None, None)
    else:
        parts = raw.split("-")
        if len(parts) != 2 or not all(_looks_like_time(p) for p in parts):
            return Reply(text="Couldn't read that. Reply like 22:00-06:00, or 'none'.")
        db.set_quiet_hours(ctx.users_conn, ctx.user.telegram_user_id, parts[0].strip(), parts[1].strip())
    return finish_onboarding(ctx)


def _looks_like_time(value: str) -> bool:
    value = value.strip()
    if ":" not in value:
        return False
    hh, _, mm = value.partition(":")
    return hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59


ONBOARDING_TEXT_STEPS = {
    "quiet_hours": _handle_quiet_hours_text,
}


# -------------------------------------------------------------------- #
# /status
# -------------------------------------------------------------------- #


def handle_status(ctx: HandlerContext) -> Reply:
    now = ctx.now
    session_date = ctx.app.session_date_fn(now)
    market_open = ctx.app.market_is_open_fn(now)
    globally_halted = ctx.app.halt_file.exists()

    from tradebot.telegram_bot import heartbeat as bot_liveness

    hb = bot_liveness.read_heartbeat(ctx.app.heartbeat_file)
    if hb is None:
        feed_line = "no heartbeat recorded yet — the live scanner may not be running"
    else:
        age = (now - datetime.fromisoformat(hb["ts_utc"])).total_seconds()
        # Staleness only means something while the market is actually
        # open — an old heartbeat over a weekend/holiday (or overnight)
        # is the scanner correctly idling, not a fault. See runner.py's
        # OFF_SESSION_IDLE_SECONDS: run_live() itself doesn't write a
        # heartbeat at all outside a trading session.
        stale = market_open and age > ctx.app.bar_minutes * 60 * 2
        feed_line = f"last loop {int(age)}s ago" + (" — looks STALE" if stale else "")

    fired_today = ctx.journal_conn.execute(
        "SELECT COUNT(*) FROM detections WHERE tier='high' AND alerted=1 AND session=?", (session_date.isoformat(),)
    ).fetchone()[0]
    cooldowns_today = ctx.journal_conn.execute(
        "SELECT COUNT(*) FROM detections WHERE tier='high' AND suppress_reason='cooldown_active' AND session=?",
        (session_date.isoformat(),),
    ).fetchone()[0]

    header = "🛑 Globally halted" if globally_halted else ("🟢 Live" if market_open else "⚪ Market closed")
    lines = [
        f"<b>{header} · BETA</b>",
        f"Data feed: {feed_line}",
        f"HIGH alerts today: {qty(fired_today)}/{qty(ctx.app.high_tier_daily_cap)}",
        f"Cooldown suppressions today: {qty(cooldowns_today)}",
    ]
    if ctx.user is None:
        pass  # channel_post — no per-user identity, so no personal "You: ..." line to show
    elif ctx.user.is_locked(now):
        lines.append(f"You: locked until {ts(datetime.fromisoformat(ctx.user.locked_until))} — {ctx.user.lock_reason}")
    elif ctx.user.is_paused(now):
        lines.append(f"You: paused until {ts(datetime.fromisoformat(ctx.user.paused_until))}")
    elif ctx.user.is_halted_for_session(session_date):
        lines.append("You: alerts halted for the rest of today's session")
    else:
        lines.append("You: active, receiving alerts")
    return Reply(text="\n".join(lines))


# -------------------------------------------------------------------- #
# /performance — journal only
# -------------------------------------------------------------------- #


def handle_performance(ctx: HandlerContext) -> Reply:
    tr = performance.track_record(ctx.journal_conn, tier="high")
    if tr is None:
        return Reply(text="Not enough journaled history yet for a real track record. Check back after a few more sessions.")
    lines = [
        f"<b>HIGH tier track record</b> · @ +{tr.offset_min}m, n={qty(tr.sample_size)}",
        "",
        f"Hit rate: {rate(tr.hit_rate * 100)}",
        f"Avg move: {pct(tr.avg_return_pct)}",
        f"Longest losing streak: {qty(tr.longest_losing_streak)} in a row",
        f"Worst hypothetical drawdown: {pct(tr.max_drawdown_pct)} (equal-weighted, back-to-back — not real compounded P/L)",
        "",
        _significance_verdict_line(tr),
        "",
        f"News-driven: {tr.news_driven and rate(tr.news_driven.hit_rate * 100) + f' hit, {pct(tr.news_driven.avg_return_pct)} avg (n={qty(tr.news_driven.sample_size)})' or 'not enough samples yet'}",
        f"Clean technical: {tr.clean_technical and rate(tr.clean_technical.hit_rate * 100) + f' hit, {pct(tr.clean_technical.avg_return_pct)} avg (n={qty(tr.clean_technical.sample_size)})' or 'not enough samples yet'}",
        "",
    ]
    if tr.no_trade_tracked_count:
        lines.append(
            f"Alerts sent: {qty(tr.total_alerts)} · NO TRADE (no tradable contract): "
            f"{qty(tr.total_no_trade)} of {qty(tr.no_trade_tracked_count)} tracked"
        )
    else:
        lines.append(f"Alerts sent: {qty(tr.total_alerts)} · NO TRADE tracking not available for this history yet")
    return Reply(text="\n".join(lines))


# -------------------------------------------------------------------- #
# /example — a real win and a real day's hit rate, randomly picked from
# the journal on every call. See performance.random_real_win /
# random_real_day_hit_rate's module comment for why this is a random
# SELECTION among real records, never a generated result.
# -------------------------------------------------------------------- #


def handle_example(ctx: HandlerContext) -> Reply:
    from tradebot.rendering.templates import render_example

    win = performance.random_real_win(ctx.journal_conn)
    day = performance.random_real_day_hit_rate(ctx.journal_conn)
    return Reply(text=render_example(win, day, ctx.now))


# -------------------------------------------------------------------- #
# /me — personal stats
# -------------------------------------------------------------------- #


def _fmt_recap_leak(leak: dict) -> str:
    sign = "+" if leak["avg_pnl_pct"] >= 0 else ""
    return f"  {html.escape(leak['label'])}: {sign}{leak['avg_pnl_pct']:.2f}% avg (n={qty(leak['n'])}, {leak['gap_pct']:.2f}pp below your month)"


def _handle_me_recap(ctx: HandlerContext) -> Reply:
    """/me recap [YYYY-MM] — the monthly digest: real numbers plus the 3
    biggest leaks, stated plainly. Defaults to the current calendar month
    to date."""
    if len(ctx.args) > 1 and "-" in ctx.args[1]:
        try:
            year, month = (int(p) for p in ctx.args[1].split("-", 1))
        except ValueError:
            return Reply(text="Usage: /me recap [YYYY-MM]")
    else:
        year, month = ctx.now.year, ctx.now.month

    recap = db.monthly_recap(ctx.users_conn, ctx.user.telegram_user_id, year, month)
    if recap is None:
        return Reply(text=f"Not enough closed trades in {year:04d}-{month:02d} yet (need {db.MIN_STAT_SAMPLE}+) for a real recap.")

    sign = "+" if recap["overall_avg_pnl_pct"] >= 0 else ""
    lines = [
        f"<b>Recap — {year:04d}-{month:02d}</b>", "",
        f"{qty(recap['trade_count'])} closed trades · {pct(recap['win_rate'] * 100)} win rate · "
        f"{sign}{recap['overall_avg_pnl_pct']:.2f}% avg",
    ]
    if recap["leaks"]:
        lines.append("")
        lines.append("Your biggest leaks this month:")
        for leak in recap["leaks"]:
            lines.append(_fmt_recap_leak(leak))
    else:
        lines.append("")
        lines.append("No bucket underperformed your own average enough to call out — nothing stands out as a leak this month.")
    return Reply(text="\n".join(lines))


def handle_me(ctx: HandlerContext) -> Reply:
    if ctx.args and ctx.args[0].lower() == "recap":
        return _handle_me_recap(ctx)

    stats = db.personal_stats(ctx.users_conn, ctx.user.telegram_user_id)
    if stats["overall"] is None:
        return Reply(text=f"Not enough closed trades yet (need {db.MIN_STAT_SAMPLE}+) — log outcomes with /took and /closed to build this out.")

    lines = ["<b>Your stats</b>", "", f"Overall: {_fmt_bucket(stats['overall'])}", ""]

    # The headline number: what overriding an explicit NO TRADE actually costs.
    lines.append("After a NO TRADE gate: " + _fmt_bucket(stats["no_trade_comparison"]["after_no_trade"]))
    lines.append("Normal entries: " + _fmt_bucket(stats["no_trade_comparison"]["normal"]))

    lines.append("")
    # "By detector"/"By symbol" can legitimately be empty (a manually
    # logged trade with no detection_id has no kind) — skip the header
    # entirely rather than printing it with nothing underneath.
    if stats["by_detector"]:
        lines.append("By detector:")
        for kind, val in stats["by_detector"].items():
            lines.append(f"  {html.escape(kind)}: {_fmt_bucket(val)}")

    if stats["by_symbol"]:
        lines.append("By symbol:")
        for symbol, val in stats["by_symbol"].items():
            lines.append(f"  {html.escape(symbol)}: {_fmt_bucket(val)}")

    lines.append("By direction:")
    for direction, val in stats["by_direction"].items():
        lines.append(f"  {html.escape(direction)}: {_fmt_bucket(val)}")

    lines.append("By hold time:")
    for bucket_label, val in stats["by_hold_time"].items():
        lines.append(f"  {html.escape(bucket_label)}: {_fmt_bucket(val)}")

    lines.append("")
    lines.append("Taken within 2min (the chase test):")
    lines.append(f"  within 2min: {_fmt_bucket(stats['fast_vs_slow']['within_2min'])}")
    lines.append(f"  later: {_fmt_bucket(stats['fast_vs_slow']['later'])}")

    if stats["pnl_by_tag"]:
        lines.append("")
        lines.append("By mood at entry:")
        for tag, val in stats["pnl_by_tag"].items():
            lines.append(f"  {html.escape(tag)}: {_fmt_bucket(val)}")

    if stats["adherence_score"] is not None:
        lines.append("")
        lines.append(
            f"Adherence: {pct(stats['adherence_score'] * 100)} of your trades came from a HIGH alert, "
            f"inside the rules, not improvised (n={qty(stats['total_trades'])})"
        )

    if stats["logging_completeness"] is not None:
        lines.append(
            f"Logging completeness: {pct(stats['logging_completeness'] * 100)} of alerts you responded to "
            f"were logged through to a real outcome (n={qty(stats['total_alerts_responded'])})"
        )
        if stats["open_trades"]:
            lines.append(f"({qty(stats['open_trades'])} still open — /closed to wrap them up)")

    lines.append("")
    lines.append("/me recap — this month's 3 biggest leaks · /export — your journal as CSV")

    return Reply(text="\n".join(lines))


# -------------------------------------------------------------------- #
# /took
# -------------------------------------------------------------------- #


def _resolve_detection(ctx: HandlerContext, alert_id: str):
    return ctx.journal_conn.execute(
        "SELECT id, symbol, kinds, tier, ts_utc, close, no_trade, primary_kind, trend "
        "FROM detections WHERE id = ? OR id LIKE ?",
        (alert_id, f"{alert_id}%"),
    ).fetchone()


def log_took(ctx: HandlerContext, detection_id: str, symbol: str, kind: str, tier: str, alert_ts_utc: str,
             after_no_trade: bool, contracts: float | None, entry_price: float | None,
             direction: str | None = None) -> db.Trade | str:
    """Shared by the typed /took handler and the "I took this" button —
    returns the Trade on success, or a short reason string if one already
    exists for this alert. Every field here is auto-filled from the
    alert's own journaled context — nothing the user has to re-type."""
    existing = db.get_open_trade_for_alert(ctx.users_conn, ctx.user.telegram_user_id, detection_id)
    if existing is not None:
        return f"already logged as taken ({ts(datetime.fromisoformat(existing.taken_at))})"
    trade = db.log_took(
        ctx.users_conn, ctx.user.telegram_user_id, detection_id=detection_id, symbol=symbol, kind=kind,
        tier=tier, direction=direction, alert_ts_utc=alert_ts_utc, taken_at=ctx.now, after_no_trade=after_no_trade,
        contracts=contracts, entry_price=entry_price,
    )
    if not db.has_responded(ctx.users_conn, ctx.user.telegram_user_id, detection_id):
        db.record_alert_response(ctx.users_conn, ctx.user.telegram_user_id, detection_id, "took", ctx.now)
    return trade


def handle_took(ctx: HandlerContext) -> Reply:
    if not ctx.args:
        return Reply(text="Usage: /took &lt;alert_id&gt; [contracts] [entry price]\nTip: tap \"I took this\" on the alert instead — no typing needed.")
    alert_id = ctx.args[0]
    row = _resolve_detection(ctx, alert_id)
    if row is None:
        return Reply(text=f"I don't recognize alert id {html.escape(alert_id)} — check the short id in the alert's footer.")
    detection_id, symbol, kinds, tier, alert_ts_utc, close, no_trade, primary_kind, trend = row
    contracts = _parse_float(ctx.args[1]) if len(ctx.args) > 1 else None
    entry = _parse_float(ctx.args[2]) if len(ctx.args) > 2 else close

    result = log_took(
        ctx, detection_id, symbol, primary_kind or kinds.split(",")[0], tier, alert_ts_utc,
        bool(no_trade), contracts, entry, direction=trend,
    )
    if isinstance(result, str):
        return Reply(text=f"{result.capitalize()}. Use /closed to log the exit.")
    extra = f" · {qty(contracts)} contracts" if contracts else ""
    text = (
        f"Logged: {symbol} · entry {money(entry) if entry else '—'}{extra}. /closed {result.id[:8]} when you're out.\n"
        "How were you feeling on this one? (optional)"
    )
    return Reply(text=text, keyboard=keyboards.mood_keyboard(result.id))


# -------------------------------------------------------------------- #
# /closed
# -------------------------------------------------------------------- #


def _find_open_trade_by_prefix(ctx: HandlerContext, prefix: str) -> db.Trade | None:
    rows = ctx.users_conn.execute(
        "SELECT id FROM user_trades WHERE telegram_user_id = ? AND id LIKE ? AND status = 'open'",
        (ctx.user.telegram_user_id, f"{prefix}%"),
    ).fetchall()
    if len(rows) == 1:
        return db.get_trade(ctx.users_conn, rows[0][0])
    return None


def handle_closed(ctx: HandlerContext) -> Reply:
    if not ctx.args:
        open_trade = db.most_recent_open_trade(ctx.users_conn, ctx.user.telegram_user_id)
        if open_trade is None:
            return Reply(text="No open trade to close. Usage: /closed [trade_id] &lt;exit price&gt; [note]")
        return Reply(text=f"Usage: /closed &lt;exit price&gt; [note] — closes your open {open_trade.symbol} from {ts(datetime.fromisoformat(open_trade.taken_at))}.")

    first = ctx.args[0]
    # Check for a real trade-id match FIRST: the 8-char short ids this
    # bot shows (see log_took's result.id[:8]) are hex, so an all-digit
    # one (e.g. "12345678") is possible and would otherwise get misread
    # as a price instead of an id. The prefix search is gated to
    # len >= 8 (this bot never shows anything shorter) so a short,
    # ordinary whole-dollar price like "5" can't spuriously collide with
    # some open trade's id happening to start with "5" — hex prefixes as
    # short as one character match far too easily to use for this.
    trade_by_id = db.get_trade(ctx.users_conn, first)
    if trade_by_id is None and len(first) >= 8:
        trade_by_id = _find_open_trade_by_prefix(ctx, first)
    if trade_by_id is not None and trade_by_id.telegram_user_id != ctx.user.telegram_user_id:
        trade_by_id = None

    if trade_by_id is not None:
        trade = trade_by_id
        if len(ctx.args) < 2:
            return Reply(text="Usage: /closed &lt;trade_id&gt; &lt;exit price&gt; [note]")
        exit_price = _parse_float(ctx.args[1])
        if exit_price is None:
            return Reply(text=f"Couldn't read {html.escape(ctx.args[1])} as a price.")
        note = " ".join(ctx.args[2:]) or None
    else:
        maybe_price = _parse_float(first)
        if maybe_price is not None:
            trade = db.most_recent_open_trade(ctx.users_conn, ctx.user.telegram_user_id)
            if trade is None:
                return Reply(text="No open trade to close.")
            exit_price = maybe_price
            note = " ".join(ctx.args[1:]) or None
        else:
            return Reply(text=f"I don't recognize trade id {html.escape(first)}.")

    if trade.status == "closed":
        return Reply(text="That trade's already closed.")

    closed = db.log_closed(ctx.users_conn, trade.id, exit_price=exit_price, closed_at=ctx.now, note=note)
    sign = "+" if (closed.pnl_pct or 0) >= 0 else ""
    pnl_text = f"{sign}{closed.pnl_pct:.2f}%" if closed.pnl_pct is not None else "n/a (no entry price on file)"
    return Reply(text=f"Closed {closed.symbol}: {pnl_text} (entry {money(closed.entry_price) if closed.entry_price else '—'} → exit {money(exit_price)}).")


# -------------------------------------------------------------------- #
# /limits
# -------------------------------------------------------------------- #

LIMIT_ALIASES = {
    "trades": "max_trades_per_day", "max_trades_per_day": "max_trades_per_day",
    "loss": "max_daily_loss", "max_daily_loss": "max_daily_loss",
    "size": "max_position_size", "max_position_size": "max_position_size",
    "account": "account_size", "account_size": "account_size",
    "risk": "risk_per_trade_pct", "risk_per_trade_pct": "risk_per_trade_pct",
}

# What a confirmation message calls each field — never the raw snake_case
# column name, and formatted the same way the no-args /limits summary
# already does (money for dollar fields, a plain count for trades).
LIMIT_FRIENDLY_LABELS = {
    "max_trades_per_day": "max trades/day",
    "max_daily_loss": "max daily loss",
    "max_position_size": "max position size",
}


def _fmt_limit_value(field: str, value: float) -> str:
    if field == "max_trades_per_day":
        return qty(int(value))
    if field == "max_daily_loss":
        return money(value)
    return qty(value) if value == int(value) else f"{value:g}"


def handle_limits(ctx: HandlerContext) -> Reply:
    session_date = ctx.app.session_date_fn(ctx.now)
    db.apply_pending_limits_if_due(ctx.users_conn, ctx.user.telegram_user_id, session_date)
    user = db.get_user(ctx.users_conn, ctx.user.telegram_user_id)

    if not ctx.args:
        lines = [
            "<b>Your limits</b>",
            f"Max trades/day: {user.max_trades_per_day if user.max_trades_per_day is not None else 'not set'}",
            f"Max daily loss: {money(user.max_daily_loss) if user.max_daily_loss is not None else 'not set'}",
            f"Max position size: {user.max_position_size if user.max_position_size is not None else 'not set'}",
            "",
            "<b>Position sizing</b> (shown on every HIGH alert once both are set)",
            f"Account size: {money(user.account_size) if user.account_size is not None else 'not set'}",
            f"Risk per trade: {_fmt_risk_pct(user.risk_per_trade_pct)}",
        ]
        if user.pending_limits:
            lines.append("")
            lines.append("Queued for next session:")
            for p in user.pending_limits:
                lines.append(f"  {p['field']} → {p['value']}")
        lines.append("")
        lines.append("Set with: /limits trades 3   /limits loss 200   /limits size 5")
        lines.append("Sizing: /limits account 10000   /limits risk 1")
        return Reply(text="\n".join(lines))

    if len(ctx.args) < 2:
        return Reply(text="Usage: /limits &lt;trades|loss|size|account|risk&gt; &lt;value&gt;")

    field = LIMIT_ALIASES.get(ctx.args[0].lower())
    if field is None:
        return Reply(text="Unknown limit — use trades, loss, size, account, or risk.")
    value = _parse_float(ctx.args[1])
    if value is None or value <= 0:
        return Reply(text=f"Couldn't read {html.escape(ctx.args[1])} as a positive number.")

    if field in db.SIZING_FIELDS:
        db.set_sizing_field(ctx.users_conn, ctx.user.telegram_user_id, field, value)
        if field == "account_size":
            return Reply(text=f"Done — account size is now {money(value)}, effective immediately.")
        return Reply(text=f"Done — risk per trade is now {_fmt_risk_pct(value)}, effective immediately.")

    market_open = ctx.app.market_is_open_fn(ctx.now)
    result = db.apply_limit_change(ctx.users_conn, ctx.user.telegram_user_id, field, value, now=ctx.now, market_is_open=market_open)
    label = LIMIT_FRIENDLY_LABELS[field]
    formatted_value = _fmt_limit_value(field, value)
    if result == "queued":
        return Reply(text=f"Noted — but you set this limit for exactly this moment. {label} → {formatted_value} is queued and takes effect next session, not mid-day.")
    return Reply(text=f"Done — {label} is now {formatted_value}, effective immediately.")


# -------------------------------------------------------------------- #
# /pause, /resume
# -------------------------------------------------------------------- #


def handle_pause(ctx: HandlerContext) -> Reply:
    if ctx.user.is_locked(ctx.now):
        return Reply(text=f"You're already locked until {ts(datetime.fromisoformat(ctx.user.locked_until))} ({ctx.user.lock_reason}) — pausing won't change anything.")
    return Reply(text="Pause alerts for how long?", keyboard=keyboards.pause_keyboard())


def handle_resume(ctx: HandlerContext) -> Reply:
    if ctx.user.is_locked(ctx.now):
        return Reply(text=f"Can't — you're locked until {ts(datetime.fromisoformat(ctx.user.locked_until))} ({ctx.user.lock_reason}). That doesn't lift early.")
    session_date = ctx.app.session_date_fn(ctx.now)
    was_paused = ctx.user.is_paused(ctx.now)
    was_halted = ctx.user.is_halted_for_session(session_date)
    if not was_paused and not was_halted:
        return Reply(text="You weren't paused.")
    db.clear_pause(ctx.users_conn, ctx.user.telegram_user_id)
    db.clear_session_halt(ctx.users_conn, ctx.user.telegram_user_id)
    return Reply(text="Alerts back on.")


# -------------------------------------------------------------------- #
# /watchlist
# -------------------------------------------------------------------- #


def handle_watchlist(ctx: HandlerContext) -> Reply:
    custom = db.get_watchlist(ctx.users_conn, ctx.user.telegram_user_id)
    active = custom or ctx.app.default_watchlist
    lines = [f"<b>Your watchlist</b> ({'custom' if custom else 'default'})", "", ", ".join(active)]
    if not access.can_access(ctx.user, "watchlist_edit"):
        lines.append("")
        lines.append("Editing isn't available on your plan — see /tiers.")
        return Reply(text="\n".join(lines))
    lines.append("")
    lines.append("Tap a symbol to toggle it, then Save. (Deselecting everything reverts you to the default list.)")
    return Reply(text="\n".join(lines), keyboard=keyboards.watchlist_keyboard(ctx.app.default_watchlist, set(active)))


# -------------------------------------------------------------------- #
# /events
# -------------------------------------------------------------------- #


def handle_events(ctx: HandlerContext) -> Reply:
    """Same content and rendering as the pre-open card runner.py sends at
    session start (tradebot.events.events_for_date over the real
    event_windows table) — one query path, not two copies that could
    drift, same discipline as /performance (see
    tradebot.telegram_bot.performance's module docstring)."""
    session_date = ctx.app.session_date_fn(ctx.now)
    events = events_for_date(ctx.journal_conn, session_date)
    return Reply(text=render_pre_open_card(events, session_date, ctx.now))


# -------------------------------------------------------------------- #
# /tiers
# -------------------------------------------------------------------- #


def handle_tiers(ctx: HandlerContext) -> Reply:
    plan = access.resolve_plan(ctx.users_conn, ctx.user)
    plan_line = f"Your plan: {html.escape(plan)}"
    if ctx.user.founding_member:
        plan_line += " (founding member)"
    lines = ["<b>Plans</b>", "", plan_line, "", BETA_PRICING_NOTICE]
    if ctx.app.plans:
        lines.append("")
        for name, price, desc in ctx.app.plans:
            lines.append(f"{html.escape(name)} — {html.escape(price)}: {html.escape(desc)}")
    else:
        lines.append("")
        lines.append("Plan details aren't configured yet.")
    if ctx.app.stripe_portal_url is None:
        lines.append("")
        lines.append(f"Billing isn't configured yet — contact {html.escape(ctx.app.support_contact)} to change plans for now.")
    keyboard = keyboards.tiers_keyboard(ctx.app.stripe_portal_url)
    return Reply(text="\n".join(lines), keyboard=keyboard)


# -------------------------------------------------------------------- #
# /export
# -------------------------------------------------------------------- #

_EXPORT_COLUMNS = [
    "id", "symbol", "kind", "tier", "direction", "alert_ts_utc", "taken_at", "reaction_seconds", "after_no_trade",
    "contracts", "entry_price", "exit_price", "closed_at", "pnl_pct", "status", "emotional_tag", "note",
]

_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralizes formula injection (a free-text `note` starting with =,
    +, -, or @ opens as a live formula in Excel/Sheets, not text) by
    prefixing with a literal apostrophe — the standard mitigation, and
    this data is meant to be shared (this is a "yours to keep" export),
    so it has to be safe to open in a spreadsheet, not just safe here."""
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value


def handle_export(ctx: HandlerContext) -> Reply:
    trades = db.list_trades(ctx.users_conn, ctx.user.telegram_user_id)
    if not trades:
        return Reply(text="No logged trades yet — nothing to export. Your data is always available here whenever you have some.")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_COLUMNS)
    for t in trades:
        writer.writerow([_csv_safe(getattr(t, col)) for col in _EXPORT_COLUMNS])
    content = buf.getvalue().encode("utf-8")
    filename = f"journal_{ctx.user.telegram_user_id}_{ctx.now.date().isoformat()}.csv"
    return Reply(text=f"{len(trades)} trade(s).", document=(filename, content))


# -------------------------------------------------------------------- #
# /help
# -------------------------------------------------------------------- #


def _dm_setup_note(ctx: HandlerContext) -> str | None:
    """Most of what this bot does only works in a DM (see GROUP_ALLOWED) —
    /help is one of the few commands that runs in both places, so it's the
    natural spot to point people at DMing the bot to actually set up."""
    who = f"@{ctx.app.bot_username}" if ctx.app.bot_username else "me"
    if ctx.chat_type != "private":
        return f"👉 DM {who} and send /start to set up personalized alerts, limits, and your watchlist — most commands only work there."
    if not ctx.user.is_onboarded:
        return "👉 New here? Send /start to set up personalized alerts, limits, and your watchlist."
    return None


def handle_help(ctx: HandlerContext) -> Reply:
    lines = [
        f"<b>{html.escape(ctx.app.bot_name)} commands</b>",
        "",
        "<b>Account</b>",
        "/status — bot & your state",
        "/performance — real track record",
        "/example — a real win and a real day's hit rate, picked fresh each time",
        "/me — your personal stats (or /me recap for this month's biggest leaks)",
        "",
        "<b>Journaling</b>",
        "/took &lt;alert_id&gt; — log a trade (or tap the alert's button)",
        "/closed &lt;exit price&gt; [note] — log an exit",
        "/export — your journal as CSV, yours to keep",
        "",
        "<b>Controls</b>",
        "/limits — daily loss/trade caps, plus account size &amp; risk per trade for position sizing",
        "/pause, /resume — mute/unmute alerts",
        "/watchlist — your symbols",
        "/halt — emergency stop",
        "",
        "<b>Info</b>",
        "/events — today's calendar",
        "/tiers — plans &amp; billing",
        "/feedback &lt;message&gt; — tell us what's broken or missing, one tap",
        "",
        f"Support: {html.escape(ctx.app.support_contact)}",
        "",
        "If trading ever stops feeling like a choice, the National Council on Problem Gambling "
        "(1-800-522-4700, ncpgambling.org) is free, confidential, and available any time — no judgment.",
    ]
    note = _dm_setup_note(ctx)
    if note:
        lines.insert(2, "")
        lines.insert(2, note)
    return Reply(text="\n".join(lines))


# -------------------------------------------------------------------- #
# /feedback — one tap from anywhere. During a free beta, this is the
# only thing being collected instead of revenue.
# -------------------------------------------------------------------- #


def handle_feedback(ctx: HandlerContext) -> Reply:
    message = " ".join(ctx.args).strip()
    if not message:
        return Reply(text="Usage: /feedback &lt;message&gt; — tell us what's broken, missing, or working well. Goes straight to us, one line, no reply needed.")
    db.add_feedback(ctx.users_conn, ctx.user.telegram_user_id, message, ctx.now)
    return Reply(text="Got it — logged. Thanks.")


# -------------------------------------------------------------------- #
# /halt
# -------------------------------------------------------------------- #


def handle_halt(ctx: HandlerContext) -> Reply:
    if ctx.user.is_admin:
        from tradebot import incidents

        ctx.app.halt_file.parent.mkdir(parents=True, exist_ok=True)
        ctx.app.halt_file.write_text(f"halted by {ctx.user.telegram_user_id} at {ctx.now.isoformat()}\n")
        incidents.open_incident(
            "halt", f"global halt by admin {ctx.user.telegram_user_id}", ctx.now, path=ctx.app.incidents_path
        )
        return Reply(text="🛑 Global halt engaged — all publishing stops for everyone. Remove data/HALT to resume.")
    session_date = ctx.app.session_date_fn(ctx.now)
    db.set_session_halt(ctx.users_conn, ctx.user.telegram_user_id, session_date)
    return Reply(text="Stopped your alerts for the rest of today's session. /resume brings them back early if you change your mind.")


HANDLERS = {
    "start": handle_start,
    "status": handle_status,
    "performance": handle_performance,
    "example": handle_example,
    "me": handle_me,
    "took": handle_took,
    "closed": handle_closed,
    "limits": handle_limits,
    "pause": handle_pause,
    "resume": handle_resume,
    "watchlist": handle_watchlist,
    "events": handle_events,
    "tiers": handle_tiers,
    "export": handle_export,
    "help": handle_help,
    "halt": handle_halt,
    "feedback": handle_feedback,
}
