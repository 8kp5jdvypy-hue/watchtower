"""Shared types passed into every handler and callback. Split out from
dispatcher.py/handlers.py so neither module has to import the other just
to see these — handlers.py only needs the shapes, dispatcher.py owns the
routing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from tradebot.telegram_bot.db import User


@dataclass(frozen=True)
class AppConfig:
    admin_ids: frozenset
    default_watchlist: list
    stripe_portal_url: str | None
    plans: list  # list of (name, price_str, description) for /tiers
    support_contact: str
    market_is_open_fn: Callable[[datetime], bool]
    session_date_fn: Callable[[datetime], date]
    halt_file: Path
    heartbeat_file: Path
    high_tier_daily_cap: int = 8
    bar_minutes: int = 5
    bot_name: str = "Kestrel"
    bot_username: str | None = None


@dataclass
class Reply:
    text: str
    keyboard: dict | None = None
    document: tuple | None = None  # (filename: str, content: bytes)


@dataclass
class HandlerContext:
    client: object  # BotClient — typed loosely to avoid an import cycle in tests using fakes
    users_conn: object
    journal_conn: object
    user: User
    chat_id: int
    chat_type: str
    args: list
    now: datetime
    app: AppConfig


@dataclass
class CallbackContext:
    client: object
    users_conn: object
    journal_conn: object
    user: User
    chat_id: int
    message_id: int
    arg: str
    now: datetime
    app: AppConfig


@dataclass
class CallbackReply:
    toast: str | None = None
    show_alert: bool = False
    edit_text: str | None = None
    edit_keyboard: dict | None = None
    send_text: str | None = None
    send_keyboard: dict | None = None
