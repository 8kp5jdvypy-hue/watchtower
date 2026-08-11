"""Minimal production error visibility for the two frontends.

Every bug found on either app this project has gone through so far was
caught by a person manually watching a browser console during a design
pass -- nothing here today tells anyone when a real user's browser hits
an error. This is the smallest thing that closes that gap: one table,
one write path, one route, no vendor SDK (no Sentry/Bugsnag), same
"boring, first-party, SQLite-backed" discipline as tradebot.funnel_events
and tradebot.rate_limit.

Not a dashboard, not alerting -- just enough to answer "is anything
actually breaking in production" from a shell, which is more than
existed before this.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_MESSAGE_LEN = 500
MAX_STACK_LEN = 4000
MAX_URL_LEN = 500
MAX_USER_AGENT_LEN = 300


@dataclass(frozen=True)
class ClientError:
    id: int
    ts_utc: str
    message: str
    stack: str | None
    url: str | None
    user_agent: str | None
    account_id: str | None


def record_error(
    conn: sqlite3.Connection,
    message: str,
    stack: str | None = None,
    url: str | None = None,
    user_agent: str | None = None,
    account_id: str | None = None,
) -> bool:
    """Returns False (and writes nothing) for an empty message -- same
    "never let a public endpoint distinguish a bad request from a
    good one" discipline as tradebot.funnel_events.record_event."""
    message = (message or "").strip()
    if not message:
        return False
    conn.execute(
        "INSERT INTO client_errors (ts_utc, message, stack, url, user_agent, account_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            message[:MAX_MESSAGE_LEN],
            (stack or None) and stack[:MAX_STACK_LEN],
            (url or None) and url[:MAX_URL_LEN],
            (user_agent or None) and user_agent[:MAX_USER_AGENT_LEN],
            account_id,
        ),
    )
    conn.commit()
    return True


def recent_errors(conn: sqlite3.Connection, limit: int = 50) -> list[ClientError]:
    """Just enough of a read path to check from a shell that this is
    actually recording something real -- not a dashboard."""
    rows = conn.execute(
        "SELECT id, ts_utc, message, stack, url, user_agent, account_id "
        "FROM client_errors ORDER BY ts_utc DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [ClientError(*row) for row in rows]
