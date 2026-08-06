"""Fans a HIGH alert out to every eligible subscriber's own DM, with the
tap-to-journal buttons attached. This is the piece that makes /pause,
/watchlist, and /limits actually mean something — see db.list_subscribers_
for_symbol for the eligibility rule (onboarded, risk-acked, not paused,
not locked, not session-halted, symbol in their watchlist).

A single failed DM (blocked bot, deleted account, network blip) never
stops the rest of the fan-out — same discipline as tradebot.alerts:
one bad send should never take the others down with it.
"""
from __future__ import annotations

import logging
from datetime import datetime

from tradebot.telegram_bot import db, keyboards

logger = logging.getLogger("watchtower.telegram_bot")


def make_subscriber_hook(client, users_conn, session_date_fn, default_watchlist):
    """Returns a `(cluster, text) -> None` callable suitable for
    runner.process_new_bar's subscriber_hook parameter, closing over the
    live bot client and the app's user database."""

    def hook(cluster, text: str) -> None:
        now = datetime.fromisoformat(cluster.ts_utc)
        session_date = session_date_fn(now)
        subscribers = db.list_subscribers_for_symbol(users_conn, cluster.symbol, session_date, now, default_watchlist)
        keyboard = keyboards.alert_actions_keyboard(cluster.id)
        for user in subscribers:
            try:
                client.send_message(user.chat_id, text, keyboard)
            except Exception:
                logger.exception("failed to DM HIGH alert to user_id=%s", user.telegram_user_id)

    return hook
