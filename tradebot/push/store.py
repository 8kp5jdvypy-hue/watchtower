"""users.db tables for push: registered devices and the push outbox.

push_devices — one row per app installation (the app's installationId
is stable across token rotations; a re-register updates the row).
push_outbox  — one row per (alert, device), same lease/retry discipline
as tradebot.telegram_bot.outbox so M3 can compare the two channels
row for row: status pending → delivered | failed | unsubscribed.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_devices (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    installation_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    apns_environment TEXT NOT NULL,       -- 'sandbox' | 'production'
    token TEXT NOT NULL,                  -- APNs device token (hex)
    app_version TEXT,
    locale TEXT,
    time_zone TEXT,
    preferences_json TEXT NOT NULL,
    idempotency_key TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_devices_account ON push_devices(account_id);

CREATE TABLE IF NOT EXISTS push_outbox (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    collapse_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    leased_by TEXT,
    leased_at TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    apns_id TEXT,
    last_error TEXT,
    UNIQUE(alert_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_push_outbox_ready ON push_outbox(status, next_attempt_at);
"""

MAX_ATTEMPTS = 6
LEASE_TIMEOUT = timedelta(minutes=2)
DEFAULT_PREFERENCES = {
    "watchlistPriority": True,
    "radarDiscoveries": False,
    "corrections": True,
    "operational": True,
    "sessions": ["regular"],
    "paused": False,
    "quietStartLocal": None,
    "quietEndLocal": None,
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Device:
    id: str
    account_id: str
    installation_id: str
    platform: str
    apns_environment: str
    token: str
    app_version: str | None
    locale: str | None
    time_zone: str | None
    preferences: dict
    active: bool
    created_at: datetime
    updated_at: datetime


def _row_to_device(row) -> Device:
    (id_, account_id, installation_id, platform, env, token, app_version, locale, tz, prefs_json, active,
     created_at, updated_at) = row
    return Device(
        id=id_, account_id=account_id, installation_id=installation_id, platform=platform, apns_environment=env,
        token=token, app_version=app_version, locale=locale, time_zone=tz, preferences=json.loads(prefs_json),
        active=bool(active), created_at=datetime.fromisoformat(created_at), updated_at=datetime.fromisoformat(updated_at),
    )


_DEVICE_COLS = (
    "id, account_id, installation_id, platform, apns_environment, token, app_version, locale, time_zone, "
    "preferences_json, active, created_at, updated_at"
)


def register_device(
    conn: sqlite3.Connection, *, account_id: str, installation_id: str, platform: str, apns_environment: str,
    token: str, app_version: str | None, locale: str | None, time_zone: str | None, preferences: dict,
    idempotency_key: str | None, now: datetime | None = None,
) -> Device:
    """Idempotent on installation_id: the app re-registers on every
    launch and on token rotation, and the row is updated in place (the
    device id the app stored stays valid). A row previously revoked by
    APNs (410) is reactivated by a fresh registration -- the new token
    is the app telling us it is alive again."""
    now = now or _now()
    existing = conn.execute("SELECT id FROM push_devices WHERE installation_id = ?", (installation_id,)).fetchone()
    prefs = {**DEFAULT_PREFERENCES, **(preferences or {})}
    if existing:
        conn.execute(
            "UPDATE push_devices SET account_id = ?, platform = ?, apns_environment = ?, token = ?, app_version = ?, "
            "locale = ?, time_zone = ?, preferences_json = ?, idempotency_key = ?, active = 1, revoked_at = NULL, "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (account_id, platform, apns_environment, token, app_version, locale, time_zone, json.dumps(prefs),
             idempotency_key, _iso(now), existing[0]),
        )
        device_id = existing[0]
    else:
        device_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO push_devices (id, account_id, installation_id, platform, apns_environment, token, app_version, "
            "locale, time_zone, preferences_json, idempotency_key, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (device_id, account_id, installation_id, platform, apns_environment, token, app_version, locale, time_zone,
             json.dumps(prefs), idempotency_key, _iso(now), _iso(now)),
        )
    conn.commit()
    return get_device(conn, device_id)


def get_device(conn: sqlite3.Connection, device_id: str) -> Device | None:
    row = conn.execute(f"SELECT {_DEVICE_COLS} FROM push_devices WHERE id = ?", (device_id,)).fetchone()
    return _row_to_device(row) if row else None


def unregister_device(conn: sqlite3.Connection, device_id: str, account_id: str, *, now: datetime | None = None) -> bool:
    """Soft-delete, scoped to the owning account so one account cannot
    unregister another's device by guessing an id. True if a row changed."""
    now = now or _now()
    cur = conn.execute(
        "UPDATE push_devices SET active = 0, revoked_at = ?, updated_at = ? WHERE id = ? AND account_id = ? AND active = 1",
        (_iso(now), _iso(now), device_id, account_id),
    )
    conn.commit()
    return cur.rowcount > 0


def revoke_device_token(conn: sqlite3.Connection, device_id: str, reason: str, *, now: datetime | None = None) -> None:
    """APNs said the token is gone (410 Unregistered / 400 BadDeviceToken)."""
    now = now or _now()
    conn.execute(
        "UPDATE push_devices SET active = 0, revoked_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (_iso(now), reason[:200], _iso(now), device_id),
    )
    conn.commit()


def active_devices_for_accounts(conn: sqlite3.Connection, account_ids: list[str]) -> list[Device]:
    if not account_ids:
        return []
    rows = conn.execute(
        f"SELECT {_DEVICE_COLS} FROM push_devices WHERE active = 1 AND account_id IN ({','.join('?' * len(account_ids))})",
        account_ids,
    ).fetchall()
    return [_row_to_device(r) for r in rows]


def accounts_watching(conn: sqlite3.Connection, symbol: str) -> list[str]:
    """Accounts whose /v1 watchlist carries `symbol` with notifications on."""
    out: list[str] = []
    for account_id, items_json in conn.execute("SELECT account_id, items_json FROM v1_watchlists").fetchall():
        try:
            items = json.loads(items_json)
        except ValueError:
            continue
        if any(i.get("symbol") == symbol and i.get("notificationsEnabled", True) for i in items):
            out.append(account_id)
    return out


# ---- outbox ------------------------------------------------------------

@dataclass(frozen=True)
class PushRow:
    id: str
    alert_id: str
    device_id: str
    payload: dict
    collapse_id: str | None
    attempts: int


def enqueue(
    conn: sqlite3.Connection, alert_id: str, devices: list[Device], payload: dict, *, collapse_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """One row per device, one transaction, idempotent on (alert_id,
    device_id) -- a runner restart that re-fires the hook adds nothing."""
    now = now or _now()
    inserted = 0
    for d in devices:
        cur = conn.execute(
            "INSERT OR IGNORE INTO push_outbox (id, alert_id, device_id, payload_json, collapse_id, status, attempts, "
            "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (uuid.uuid4().hex, alert_id, d.id, json.dumps(payload), collapse_id, _iso(now), _iso(now)),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def reclaim_stale_in_flight(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute(
        "UPDATE push_outbox SET status = 'pending', leased_by = NULL, leased_at = NULL WHERE status = 'in_flight' AND leased_at < ?",
        (_iso(now - LEASE_TIMEOUT),),
    )
    conn.commit()
    return cur.rowcount


def claim_ready_batch(conn: sqlite3.Connection, worker_id: str, limit: int, now: datetime) -> list[PushRow]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT id, alert_id, device_id, payload_json, collapse_id, attempts FROM push_outbox "
            "WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY created_at LIMIT ?",
            (_iso(now), limit),
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                f"UPDATE push_outbox SET status = 'in_flight', leased_by = ?, leased_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                (worker_id, _iso(now), *ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [PushRow(id=r[0], alert_id=r[1], device_id=r[2], payload=json.loads(r[3]), collapse_id=r[4], attempts=r[5]) for r in rows]


def mark_delivered(conn: sqlite3.Connection, row_id: str, apns_id: str | None, when: datetime) -> None:
    conn.execute(
        "UPDATE push_outbox SET status = 'delivered', delivered_at = ?, apns_id = ?, attempts = attempts + 1, leased_by = NULL WHERE id = ?",
        (_iso(when), apns_id, row_id),
    )
    conn.commit()


def mark_retry(conn: sqlite3.Connection, row_id: str, attempts: int, error: str, now: datetime) -> None:
    """Exponential backoff; gives up (status failed) past MAX_ATTEMPTS."""
    next_attempts = attempts + 1
    if next_attempts >= MAX_ATTEMPTS:
        conn.execute(
            "UPDATE push_outbox SET status = 'failed', attempts = ?, last_error = ?, leased_by = NULL WHERE id = ?",
            (next_attempts, error[:300], row_id),
        )
    else:
        delay = timedelta(seconds=min(600, 5 * 2**next_attempts))
        conn.execute(
            "UPDATE push_outbox SET status = 'pending', attempts = ?, last_error = ?, next_attempt_at = ?, leased_by = NULL WHERE id = ?",
            (next_attempts, error[:300], _iso(now + delay), row_id),
        )
    conn.commit()


def mark_unsubscribed(conn: sqlite3.Connection, row_id: str, error: str) -> None:
    conn.execute(
        "UPDATE push_outbox SET status = 'unsubscribed', attempts = attempts + 1, last_error = ?, leased_by = NULL WHERE id = ?",
        (error[:300], row_id),
    )
    conn.commit()


def delivery_summary(conn: sqlite3.Connection, alert_id: str) -> dict[str, int]:
    """Per-alert status counts -- what M3's parity gate reads."""
    rows = conn.execute("SELECT status, COUNT(*) FROM push_outbox WHERE alert_id = ? GROUP BY status", (alert_id,)).fetchall()
    return {status: n for status, n in rows}
