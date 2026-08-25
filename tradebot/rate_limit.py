"""Fixed-window rate limiting, backed by SQLite -- not an in-memory
dict, because the API runs under gunicorn (see docker-compose.yml),
which means multiple worker processes each with their own memory. An
in-memory counter would give each worker its own independent limit,
silently multiplying the real limit by however many workers are
running. SQLite is already the one shared source of truth every other
piece of state in this codebase goes through (accounts, magic-link
tokens, funnel events) -- this is the same discipline, not a new one.

Fixed-window, not sliding-window or token-bucket: simpler to reason
about and implement in three lines of SQL, and "at most N per clock-
aligned window" is more than precise enough for what this guards
today (email-request spam and junk analytics writes) -- neither is
rate-sensitive the way a login-attempt lockout would be, where a
window-boundary burst actually matters.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

# Counter rows are tiny and self-limiting in number (one per active
# bucket_key per window), but nothing deletes an old window on its own
# -- prune anything older than this on every check rather than running
# a separate cleanup job, since expected volume (a beta product) makes
# that cheap.
_RETENTION = timedelta(hours=6)


def _window_start(now: datetime, window_seconds: int) -> str:
    epoch_seconds = int(now.timestamp())
    floored = epoch_seconds - (epoch_seconds % window_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc).isoformat()


def allow_all(
    conn: sqlite3.Connection,
    buckets: Sequence[tuple[str, int, int]],
    now: datetime | None = None,
) -> bool:
    """Atomically admits and records every ``(key, limit, window)``.

    A request governed by both a principal and an IP limit must never
    leave one durable counter behind when the other limit denies it.
    ``BEGIN IMMEDIATE`` serializes the read/check/write transition
    across gunicorn workers; either every bucket advances once or none
    does. Pruning still commits on a denial, matching ``allow``'s
    historical retention behavior.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - _RETENTION).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM rate_limit_counters WHERE window_start < ?", (cutoff,))
        active_buckets = []
        for key, limit, window_seconds in buckets:
            window_start = _window_start(now, window_seconds)
            row = conn.execute(
                "SELECT count FROM rate_limit_counters WHERE bucket_key = ? AND window_start = ?",
                (key, window_start),
            ).fetchone()
            count = row[0] if row else 0
            if count >= limit:
                conn.commit()  # keep the prune above even when denying
                return False
            active_buckets.append((key, window_start))

        for key, window_start in active_buckets:
            conn.execute(
                "INSERT INTO rate_limit_counters (bucket_key, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT(bucket_key, window_start) DO UPDATE SET count = count + 1",
                (key, window_start),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def allow(conn: sqlite3.Connection, key: str, limit: int, window_seconds: int, now: datetime | None = None) -> bool:
    """Single-bucket convenience wrapper around :func:`allow_all`."""
    return allow_all(conn, [(key, limit, window_seconds)], now=now)
