"""Storage for the /v1 (iOS app) surface: bearer tokens and per-account
watchlists. Lives in users.db next to accounts/linked_identities, but
the tables are created here rather than in tradebot.telegram_bot.db so
the Telegram sunset (docs/telegram-sunset-plan-2026-09.md, M4) can
delete that package without touching anything the app depends on.

Tokens are opaque random strings; only their SHA-256 is stored. Access
tokens are short-lived and stateless to the client; refresh tokens
rotate on every use and belong to a *family* (one sign-in). Presenting
a refresh token that was already rotated is treated as theft and
revokes the whole family -- the standard rotation-with-reuse-detection
scheme, which the app's single-flight refresh (perchClient.ts
rotateTokens) is built to cooperate with.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'access' | 'refresh'
    account_id TEXT NOT NULL,
    family_id TEXT NOT NULL,         -- one sign-in; logout/reuse revokes the family
    device_name TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_family ON api_tokens(family_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_account ON api_tokens(account_id);

CREATE TABLE IF NOT EXISTS v1_watchlists (
    account_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    items_json TEXT NOT NULL,        -- [{"symbol": "SPY", "notificationsEnabled": true}, ...]
    radar_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

ACCESS_TTL = timedelta(minutes=30)
REFRESH_TTL = timedelta(days=30)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str


@dataclass(frozen=True)
class TokenRecord:
    kind: str
    account_id: str
    family_id: str
    expires_at: datetime
    revoked: bool


def issue_pair(
    conn: sqlite3.Connection, account_id: str, *, now: datetime, device_name: str | None, family_id: str | None = None
) -> TokenPair:
    family_id = family_id or uuid.uuid4().hex
    access = secrets.token_urlsafe(32)   # 43 chars
    refresh = secrets.token_urlsafe(48)  # 64 chars; schema wants 32..512
    access_exp = now + ACCESS_TTL
    conn.executemany(
        "INSERT INTO api_tokens (token_hash, kind, account_id, family_id, device_name, created_at, expires_at, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            (_hash(access), "access", account_id, family_id, device_name, _iso(now), _iso(access_exp)),
            (_hash(refresh), "refresh", account_id, family_id, device_name, _iso(now), _iso(now + REFRESH_TTL)),
        ],
    )
    conn.commit()
    return TokenPair(access_token=access, access_expires_at=access_exp, refresh_token=refresh)


def lookup(conn: sqlite3.Connection, token: str) -> TokenRecord | None:
    row = conn.execute(
        "SELECT kind, account_id, family_id, expires_at, revoked_at FROM api_tokens WHERE token_hash = ?",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    kind, account_id, family_id, expires_at, revoked_at = row
    return TokenRecord(
        kind=kind, account_id=account_id, family_id=family_id,
        expires_at=datetime.fromisoformat(expires_at), revoked=revoked_at is not None,
    )


def revoke_family(conn: sqlite3.Connection, family_id: str, *, now: datetime) -> None:
    conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL", (_iso(now), family_id)
    )
    conn.commit()


def rotate(conn: sqlite3.Connection, refresh_token: str, *, now: datetime) -> TokenPair | None:
    """Exchange a live refresh token for a new pair in the same family.
    None means 401: unknown, expired, wrong kind, or already used --
    the last of which also revokes the family (reuse = theft)."""
    rec = lookup(conn, refresh_token)
    if rec is None or rec.kind != "refresh":
        return None
    if rec.revoked:
        revoke_family(conn, rec.family_id, now=now)
        return None
    if now >= rec.expires_at:
        return None
    conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ?", (_iso(now), _hash(refresh_token)))
    # Old access tokens of this family die with the refresh token they came with.
    conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE family_id = ? AND kind = 'access' AND revoked_at IS NULL",
        (_iso(now), rec.family_id),
    )
    conn.commit()
    return issue_pair(conn, rec.account_id, now=now, device_name=None, family_id=rec.family_id)


def prune(conn: sqlite3.Connection, *, now: datetime, keep_revoked_for: timedelta = timedelta(days=7)) -> None:
    """Expired rows are dead weight; revoked rows are kept a week so reuse
    detection still fires for a stolen refresh token presented late."""
    conn.execute("DELETE FROM api_tokens WHERE expires_at < ?", (_iso(now - keep_revoked_for),))
    conn.commit()


# ---- watchlists ----------------------------------------------------------

@dataclass(frozen=True)
class StoredWatchlist:
    version: int
    items: list[dict]
    radar_enabled: bool
    updated_at: datetime


def get_watchlist(conn: sqlite3.Connection, account_id: str, *, now: datetime) -> StoredWatchlist:
    row = conn.execute(
        "SELECT version, items_json, radar_enabled, updated_at FROM v1_watchlists WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return StoredWatchlist(version=0, items=[], radar_enabled=False, updated_at=now)
    version, items_json, radar_enabled, updated_at = row
    return StoredWatchlist(
        version=int(version), items=json.loads(items_json), radar_enabled=bool(radar_enabled),
        updated_at=datetime.fromisoformat(updated_at),
    )


def replace_watchlist(
    conn: sqlite3.Connection, account_id: str, *, expected_version: int, items: list[dict],
    radar_enabled: bool | None, now: datetime,
) -> StoredWatchlist | None:
    """Optimistic concurrency: the caller must present the version it
    last saw. None means conflict (409) -- the app re-reads and retries,
    never silently overwrites another device's change."""
    current = get_watchlist(conn, account_id, now=now)
    if expected_version != current.version:
        return None
    radar = current.radar_enabled if radar_enabled is None else radar_enabled
    conn.execute(
        "INSERT INTO v1_watchlists (account_id, version, items_json, radar_enabled, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET version = excluded.version, items_json = excluded.items_json, "
        "radar_enabled = excluded.radar_enabled, updated_at = excluded.updated_at",
        (account_id, current.version + 1, json.dumps(items), int(radar), _iso(now)),
    )
    conn.commit()
    return get_watchlist(conn, account_id, now=now)
