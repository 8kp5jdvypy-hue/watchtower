"""Unified Perch identity: accounts, linked identities, magic-link auth.

Schema lives in tradebot.telegram_bot.db (the `accounts`, `linked_identities`,
and `magic_link_tokens` tables in users.db) — same database, same
connect(), no new file to keep in sync. This module is the query/logic
layer on top, the same split db.py already uses for `User`.

Why this exists: today, `telegram_user_id` IS a user's identity — see
db.User. That's fine as long as Telegram is the only surface. The moment
a web dashboard or mobile app exists, something has to be the thing a
person "is" across all of them. That's an `Account` row here. A Telegram
user, once linked, is one `linked_identities` row pointing at an
`Account` — not a second, competing notion of identity.

`migrate_existing_telegram_users` is what makes today's Telegram users
land on this model with zero action from them: it gives everyone with no
linked identity yet exactly one new account, carrying over their
existing `plan`/`founding_member` state from `users` so nobody's status
changes.
"""
from __future__ import annotations

import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MAGIC_LINK_TTL_MINUTES = 15
TELEGRAM_PROVIDER = "telegram"

# Unauthenticated abuse surface: /auth/magic-link/request sends a real
# email to whatever address it's given, with no auth of its own — without
# a limit, it can be used to mail-bomb an arbitrary inbox or burn the
# Resend send quota. DB-backed (not an in-memory counter) so the limit
# holds across every gunicorn worker process, not just one.
MAGIC_LINK_RATE_LIMIT_WINDOW_MINUTES = 15
MAGIC_LINK_RATE_LIMIT_MAX_REQUESTS = 3


@dataclass(frozen=True)
class Account:
    id: str
    email: str | None
    plan: str
    founding_member: bool
    created_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_account(row) -> Account:
    account_id, email, plan, founding_member, created_at = row
    return Account(id=account_id, email=email, plan=plan, founding_member=bool(founding_member), created_at=created_at)


def get_account(conn: sqlite3.Connection, account_id: str) -> Account | None:
    row = conn.execute(
        "SELECT id, email, plan, founding_member, created_at FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return _row_to_account(row) if row else None


def get_account_by_email(conn: sqlite3.Connection, email: str) -> Account | None:
    row = conn.execute(
        "SELECT id, email, plan, founding_member, created_at FROM accounts WHERE email = ?", (email,)
    ).fetchone()
    return _row_to_account(row) if row else None


def get_account_for_identity(conn: sqlite3.Connection, provider: str, provider_user_id: str) -> Account | None:
    row = conn.execute(
        """
        SELECT a.id, a.email, a.plan, a.founding_member, a.created_at
        FROM accounts a JOIN linked_identities li ON li.account_id = a.id
        WHERE li.provider = ? AND li.provider_user_id = ?
        """,
        (provider, provider_user_id),
    ).fetchone()
    return _row_to_account(row) if row else None


def create_account(
    conn: sqlite3.Connection, email: str | None = None, plan: str = "beta", founding_member: bool = False,
) -> Account:
    """Not idempotent on email by itself — call get_account_by_email first
    if "find or create" is what's wanted (see get_or_create_account_for_email).
    A bare create_account is for the migration path, where a fresh,
    email-less account is always the right thing to make."""
    account_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO accounts (id, email, plan, founding_member, created_at) VALUES (?, ?, ?, ?, ?)",
        (account_id, email, plan, int(founding_member), _now_iso()),
    )
    conn.commit()
    return get_account(conn, account_id)


def get_or_create_account_for_email(conn: sqlite3.Connection, email: str) -> Account:
    existing = get_account_by_email(conn, email)
    if existing is not None:
        return existing
    return create_account(conn, email=email)


def link_identity(conn: sqlite3.Connection, account_id: str, provider: str, provider_user_id: str) -> None:
    """Idempotent: linking the same (provider, provider_user_id) again is
    a no-op rather than an error — a handler doesn't need to check first."""
    conn.execute(
        "INSERT OR IGNORE INTO linked_identities (account_id, provider, provider_user_id, linked_at) "
        "VALUES (?, ?, ?, ?)",
        (account_id, provider, provider_user_id, _now_iso()),
    )
    conn.commit()


def is_magic_link_rate_limited(conn: sqlite3.Connection, email: str, now: datetime) -> bool:
    """True once `email` has already requested
    MAGIC_LINK_RATE_LIMIT_MAX_REQUESTS or more links within the last
    MAGIC_LINK_RATE_LIMIT_WINDOW_MINUTES. Counts every request in the
    window regardless of whether that token was later consumed or has
    since expired — what's being limited is send volume, not valid-token
    count."""
    window_start = (now - timedelta(minutes=MAGIC_LINK_RATE_LIMIT_WINDOW_MINUTES)).isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM magic_link_tokens WHERE email = ? AND created_at >= ?",
        (email, window_start),
    ).fetchone()[0]
    return count >= MAGIC_LINK_RATE_LIMIT_MAX_REQUESTS


def create_magic_link_token(conn: sqlite3.Connection, email: str, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
    conn.execute(
        "INSERT INTO magic_link_tokens (token, email, created_at, expires_at, consumed_at) VALUES (?, ?, ?, ?, NULL)",
        (token, email, now.isoformat(), expires_at.isoformat()),
    )
    conn.commit()
    return token


def verify_magic_link_token(conn: sqlite3.Connection, token: str, now: datetime) -> str | None:
    """Consumes the token on success (single-use). Returns the email it
    was issued for, or None if the token is unknown, already used, or
    expired — callers don't need to distinguish which."""
    row = conn.execute(
        "SELECT email, expires_at, consumed_at FROM magic_link_tokens WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    email, expires_at, consumed_at = row
    if consumed_at is not None:
        return None
    if now > datetime.fromisoformat(expires_at):
        return None
    conn.execute("UPDATE magic_link_tokens SET consumed_at = ? WHERE token = ?", (now.isoformat(), token))
    conn.commit()
    return email


def migrate_existing_telegram_users(conn: sqlite3.Connection) -> int:
    """Gives every Telegram user with no linked identity yet exactly one
    new, email-less account, carrying over their current plan/
    founding_member so migrating is invisible to them. Safe to call on
    every startup — a user who already has a linked identity (from a
    prior run of this function, or from linking some other way) is
    skipped, so this never creates a second account for the same person.
    Returns the number of accounts created."""
    rows = conn.execute("SELECT telegram_user_id, plan, founding_member FROM users").fetchall()
    created = 0
    for telegram_user_id, plan, founding_member in rows:
        provider_user_id = str(telegram_user_id)
        if get_account_for_identity(conn, TELEGRAM_PROVIDER, provider_user_id) is not None:
            continue
        account = create_account(conn, email=None, plan=plan, founding_member=bool(founding_member))
        link_identity(conn, account.id, TELEGRAM_PROVIDER, provider_user_id)
        created += 1
    return created
