"""Enqueues a HIGH alert to every eligible subscriber's own DM (with the
tap-to-journal buttons attached) via the outbox — see
tradebot.telegram_bot.worker, the only thing that actually calls the
Telegram API. This is the piece that makes /pause, /watchlist, and
/limits actually mean something — see db.list_subscribers_for_symbol for
the eligibility rule (onboarded, risk-acked, not paused, not locked, not
session-halted, symbol in their watchlist, not confirmed unreachable).

Also where position sizing gets attached: the shared alert render (see
tradebot.rendering.templates.render_high_alert) has no per-user context,
so a subscriber with account_size and risk_per_trade_pct configured
(see /limits) gets a follow-up enqueued with their own max-contracts
figure — never baked into the one shared render everyone else also sees.

Enqueueing is a single DB transaction for the whole fan-out (see
outbox.enqueue_broadcast) — a crash here loses or duplicates nothing;
delivery failures (blocked bot, deleted account, network blip) are the
worker's problem, not this module's.
"""
from __future__ import annotations

from datetime import datetime

from tradebot.costs import position_size
from tradebot.rendering.templates import render_position_size
from tradebot.telegram_bot import db, keyboards, outbox

# 'quiet' sensitivity (see db.ALERT_SENSITIVITIES / onboarding's "how much
# signal do you want?" step) raises a subscriber's own HIGH-tier floor
# above the global tier threshold (detectors.TIER_HIGH, 3.8) — a real,
# stricter cutoff, not a cosmetic label. 'balanced' and 'aggressive' both
# see every real HIGH alert (score is already >= TIER_HIGH by
# construction for anything reaching this hook at all); 'aggressive'
# additionally gets the hourly MEDIUM digest personally — see
# personal_medium_fanout below.
QUIET_SENSITIVITY_MIN_SCORE = 5.5


def make_subscriber_hook(users_conn, session_date_fn, default_watchlist):
    """Returns a `(cluster, text, entry_mid) -> None` callable suitable
    for runner.process_new_bar's subscriber_hook parameter, closing over
    the app's user database. entry_mid is the real per-contract debit
    select_contract() already computed (None on a NO TRADE alert —
    nothing to size)."""

    def hook(cluster, text: str, entry_mid: float | None = None) -> None:
        now = datetime.fromisoformat(cluster.ts_utc)
        session_date = session_date_fn(now)
        subscribers = [
            user for user in db.list_subscribers_for_symbol(users_conn, cluster.symbol, session_date, now, default_watchlist)
            if user.alert_sensitivity != "quiet" or cluster.score >= QUIET_SENSITIVITY_MIN_SCORE
        ]
        keyboard = keyboards.alert_actions_keyboard(cluster.id)

        alert_recipients = [(user.chat_id, text, keyboard) for user in subscribers]
        if alert_recipients:
            outbox.enqueue_broadcast(users_conn, cluster.id, alert_recipients, outbox.PRIORITY_HIGH, now=now)

        if entry_mid is None:
            return
        sizing_recipients = []
        for user in subscribers:
            if not (user.account_size and user.risk_per_trade_pct):
                continue
            size = position_size(entry_mid, user.account_size, user.risk_per_trade_pct)
            sizing_recipients.append((user.chat_id, render_position_size(size, now), None))
        if sizing_recipients:
            # A distinct alert_id (not cluster.id, already used above for
            # the alert itself — the outbox's idempotency key is per
            # alert_id+chat_id, so reusing cluster.id here would collide
            # with the alert row and the sizing follow-up would never
            # get enqueued for the same chat).
            outbox.enqueue_broadcast(
                users_conn, f"{cluster.id}:sizing", sizing_recipients, outbox.PRIORITY_HIGH, now=now,
            )

    return hook


def make_medium_fanout_fn(users_conn, session_date_fn, default_watchlist):
    """Returns a `(clusters, text, when) -> None` callable for
    runner.send_medium_digest_if_due's personal_fanout_fn parameter.

    Personally forwards the SAME hourly digest text the ops channel
    already receives — no separate enrichment, since MEDIUM clusters
    never get the guard/contract-selection/historical-performance
    treatment HIGH ones do (see runner.process_new_bar) — to every
    'aggressive'-sensitivity subscriber whose watchlist overlaps at
    least one symbol in this digest. Reuses db.list_subscribers_for_symbol
    (the same eligibility rule as HIGH: onboarded, risk-acked, not
    paused/locked/session-halted, not in quiet hours, not unreachable)
    rather than a second copy of it."""

    def fanout(clusters, text: str, when: datetime) -> None:
        session_date = session_date_fn(when)
        symbols = {c.symbol for c in clusters}
        recipients: dict[int, int] = {}  # telegram_user_id -> chat_id, deduped across symbols
        for symbol in symbols:
            for user in db.list_subscribers_for_symbol(users_conn, symbol, session_date, when, default_watchlist):
                if user.alert_sensitivity == "aggressive":
                    recipients[user.telegram_user_id] = user.chat_id
        if not recipients:
            return
        alert_id = f"digest:{when.isoformat()}"
        outbox.enqueue_broadcast(
            users_conn, alert_id, [(chat_id, text, None) for chat_id in recipients.values()],
            outbox.PRIORITY_NORMAL, now=when,
        )

    return fanout
