"""Single seam for feature gating. Everything is free during beta —
can_access always returns True — but every feature check in the codebase
routes through this function, never a direct `user.plan == ...` check of
its own. When gating turns on for real, this function's body is the only
thing that changes to `return limits_for(resolve_plan(conn, user)).xxx`;
no caller needs to be touched. See the plan doc's note that FREE must
stay genuinely useful until the product has proven its value — that's
the business decision this stub currently encodes, not a placeholder
for something unfinished.

resolve_plan reads plan via the user's linked account (tradebot.
accounts) rather than db.User.plan directly, because plan is
account-scoped now, not Telegram-scoped: a founding-member grant made
from the web dashboard must be visible to a Telegram command too. It
falls back to the legacy `users.plan`/`founding_member` columns if no
linked account exists yet — shouldn't happen once
accounts.migrate_existing_telegram_users has run at startup, but a
feature check must never fail a Telegram command over missing plumbing.
"""
from __future__ import annotations

import sqlite3

from tradebot import accounts
from tradebot.telegram_bot.db import User


def resolve_plan(conn: sqlite3.Connection, user: User) -> str:
    account = accounts.get_account_for_identity(conn, accounts.TELEGRAM_PROVIDER, str(user.telegram_user_id))
    return account.plan if account is not None else user.plan


def can_access(user: User | None, feature: str) -> bool:
    return True
