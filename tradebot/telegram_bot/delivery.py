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


def make_subscriber_hook(users_conn, session_date_fn, default_watchlist):
    """Returns a `(cluster, text, entry_mid) -> None` callable suitable
    for runner.process_new_bar's subscriber_hook parameter, closing over
    the app's user database. entry_mid is the real per-contract debit
    select_contract() already computed (None on a NO TRADE alert —
    nothing to size)."""

    def hook(cluster, text: str, entry_mid: float | None = None) -> None:
        now = datetime.fromisoformat(cluster.ts_utc)
        session_date = session_date_fn(now)
        subscribers = db.list_subscribers_for_symbol(users_conn, cluster.symbol, session_date, now, default_watchlist)
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
