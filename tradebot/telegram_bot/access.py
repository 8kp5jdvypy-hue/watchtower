"""Single seam for feature gating. Everything is free during beta —
can_access always returns True — but every feature check in the codebase
routes through this function, never a direct `user.plan == ...` check of
its own. When billing arrives, this file is the only thing that changes;
no caller needs to be touched.

db.User.plan is recorded from day one specifically so that whenever this
stops being a stub, grandfathering existing (founding_member) users is
reading history that was accurate from the start, not reconstructed after
the fact. Don't read it here yet — there is nothing to gate.
"""
from __future__ import annotations

from tradebot.telegram_bot.db import User


def can_access(user: User | None, feature: str) -> bool:
    return True
