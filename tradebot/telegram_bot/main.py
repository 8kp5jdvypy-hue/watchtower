#!/usr/bin/env python3
"""Entrypoint for the command dispatcher — the process that long-polls
Telegram for commands and button taps. Runs independently from
`python -m tradebot.runner --live` (the market scanner loop): they're
separate long-running processes that only share data/users.db and
data/journal.db on disk. See tradebot.telegram_bot.delivery for how the
scanner process fans HIGH alerts out to subscribers without needing to
talk to this process directly.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tradebot.config import WATCHLIST
from tradebot.journal import connect as journal_connect
from tradebot.runner import CALENDAR, ET, HALT_FILE, HEARTBEAT_FILE
from tradebot.telegram_bot import callbacks, commands, db, handlers
from tradebot.telegram_bot.client import BotClient
from tradebot.telegram_bot.context import AppConfig
from tradebot.telegram_bot.dispatcher import Dispatcher

logger = logging.getLogger("watchtower.telegram_bot")


def _market_is_open(now: datetime) -> bool:
    session_date = now.astimezone(ET).date()
    if not CALENDAR.is_session(session_date):
        return False
    open_ts = CALENDAR.session_open(session_date).to_pydatetime()
    close_ts = CALENDAR.session_close(session_date).to_pydatetime()
    return open_ts <= now <= close_ts


def _session_date(now: datetime) -> date:
    return now.astimezone(ET).date()


def _parse_id_list(env_var: str) -> frozenset:
    raw = os.environ.get(env_var, "")
    return frozenset(int(x) for x in raw.split(",") if x.strip())


def _parse_bool(env_var: str, default: bool = False) -> bool:
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_app_config(bot_username: str | None = None) -> AppConfig:
    return AppConfig(
        admin_ids=_parse_id_list("ADMIN_TELEGRAM_IDS"),
        default_watchlist=list(WATCHLIST),
        stripe_portal_url=os.environ.get("STRIPE_PORTAL_URL") or None,
        plans=[],  # populate once real Stripe products exist — see /tiers
        support_contact=os.environ.get("SUPPORT_CONTACT", "@support"),
        market_is_open_fn=_market_is_open,
        session_date_fn=_session_date,
        halt_file=HALT_FILE,
        heartbeat_file=HEARTBEAT_FILE,
        bot_username=bot_username,
        allowed_user_ids=_parse_id_list("ALLOWED_USER_IDS") or None,
        channel_commands_enabled=_parse_bool("CHANNEL_COMMANDS_ENABLED", default=False),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync-commands", action="store_true",
        help="push tradebot/telegram_bot/commands.py's registry to BotFather via setMyCommands before "
        "starting, instead of only verifying it matches. Use this once after intentionally changing the "
        "command list — the startup drift check stays fail-loudly for everything else.",
    )
    args = parser.parse_args()

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    client = BotClient(token)

    if args.sync_commands:
        logger.info("--sync-commands: pushing local command registry to BotFather via setMyCommands")
        client.set_my_commands(commands.COMMANDS)

    users_conn = db.connect()
    journal_conn = journal_connect(check_same_thread=False)
    bot_username = client.get_me().get("username")
    app_config = build_app_config(bot_username)

    logger.info(
        "config: allowed_user_ids=%s channel_commands_enabled=%s admin_ids=%s",
        sorted(app_config.allowed_user_ids) if app_config.allowed_user_ids else "unrestricted",
        app_config.channel_commands_enabled,
        sorted(app_config.admin_ids) or "none",
    )

    dispatcher = Dispatcher(
        client=client,
        users_conn=users_conn,
        journal_conn=journal_conn,
        app_config=app_config,
        handlers=handlers.HANDLERS,
        callback_handlers=callbacks.CALLBACK_HANDLERS,
        onboarding_text_handlers=handlers.ONBOARDING_TEXT_STEPS,
    )
    dispatcher.run_forever()


if __name__ == "__main__":
    main()
