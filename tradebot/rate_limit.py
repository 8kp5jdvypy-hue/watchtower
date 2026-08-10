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


def allow(conn: sqlite3.Connection, key: str, limit: int, window_seconds: int, now: datetime | None = None) -> bool:
    """Returns True and records this call if `key` is under `limit`
    calls within the current `window_seconds`-wide window; returns
    False (and still doesn't record it) once the limit is hit. Callers
    decide what "not allowed" means for their endpoint -- this never
    raises and never distinguishes "limit hit" from any other reason
    to say no, on purpose."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - _RETENTION).isoformat()
    conn.execute("DELETE FROM rate_limit_counters WHERE window_start < ?", (cutoff,))

    window_start = _window_start(now, window_seconds)
    row = conn.execute(
        "SELECT count FROM rate_limit_counters WHERE bucket_key = ? AND window_start = ?",
        (key, window_start),
    ).fetchone()
    count = row[0] if row else 0
    if count >= limit:
        conn.commit()  # keep the prune above even when denying
        return False

    conn.execute(
        "INSERT INTO rate_limit_counters (bucket_key, window_start, count) VALUES (?, ?, 1) "
        "ON CONFLICT(bucket_key, window_start) DO UPDATE SET count = count + 1",
        (key, window_start),
    )
    conn.commit()
    return True
