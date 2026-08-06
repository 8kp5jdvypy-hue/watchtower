"""Outbox pattern for outbound Telegram sends: producers (runner.py via
tradebot.alerts.TelegramAlerter, and tradebot.telegram_bot.delivery)
persist a row here and return immediately; tradebot.telegram_bot.worker
is the only thing that ever calls the Telegram API, on its own schedule,
respecting priority and rate limits. See db.py's SCHEMA for the table.

Crash safety: a producer's enqueue_broadcast() call is one SQLite
transaction — either every recipient's row is written, or (on a crash
before commit) none are. UNIQUE(alert_id, chat_id) makes a re-run of
that same enqueue call (e.g. after a producer restart) a safe no-op,
never a duplicate.

The worker's lease/reclaim cycle (claim_ready_batch -> mark_in_flight ->
mark_delivered/retry/failed/unsubscribed) makes losing a row impossible:
anything not yet marked delivered is either still 'pending' or an
'in_flight' row whose lease has expired, and reclaim_stale_in_flight()
puts expired leases back to 'pending'. What it can NOT make impossible
is a duplicate delivery in the single specific case where the worker
process is killed in the microseconds between "Telegram accepted the
message" and "our own commit recording that" — no idempotency key exists
on Telegram's sendMessage API to close that window from our side. This
module makes that window as small as possible (mark_delivered is the
very next statement after a successful API response, nothing else runs
between them) and treats it as an accepted, documented at-least-once
edge case rather than a claimed impossibility — see the chaos test for
what's actually verified.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PRIORITY_HIGH = 0    # HIGH alerts, system notices (halt, stale data, cap reached) — latency is the product
PRIORITY_NORMAL = 1  # medium digests, morning briefing, pre-open card, position-size follow-ups
PRIORITY_LOG = 2      # end-of-day log summary, heartbeat — least time-sensitive

# Telegram's real hard cap on a text message, in characters — sending
# over this gets the whole message rejected with a 400, not truncated
# for you. Approximated as len(text) rather than true UTF-16 code units
# (Telegram's actual unit): every message this project renders is
# ASCII/Latin plus HTML entities, so the two never diverge in practice,
# but a message built from raw non-BMP user input elsewhere could in
# principle differ — noted rather than silently assumed away.
TELEGRAM_MAX_TEXT_LEN = 4096
_TRUNCATION_MARKER = "\n\n[truncated]"

# An in_flight row whose lease is older than this is assumed to belong
# to a dead worker (crashed, killed, never got to mark_delivered/retry)
# and is put back to pending for another attempt — see
# reclaim_stale_in_flight(). Comfortably longer than any single Telegram
# API call should ever take (client.py's own timeout is 10s).
#
# Overridable via OUTBOX_LEASE_TIMEOUT_SECONDS so the chaos test (a real
# subprocess, real SIGKILL) doesn't have to wait out a real 60 seconds
# before its second worker run can reclaim what the killed one leased.
LEASE_TIMEOUT_SECONDS = 60


def _lease_timeout_seconds() -> float:
    return float(os.environ.get("OUTBOX_LEASE_TIMEOUT_SECONDS", LEASE_TIMEOUT_SECONDS))


@dataclass(frozen=True)
class OutboxRow:
    id: str
    alert_id: str
    chat_id: int
    priority: int
    text: str
    reply_markup: dict | None
    status: str
    attempts: int
    next_attempt_at: str
    created_at: str


_COLUMNS = (
    "id, alert_id, chat_id, priority, text, reply_markup_json, status, attempts, next_attempt_at, created_at"
)


def _row_to_outbox_row(row) -> OutboxRow:
    id_, alert_id, chat_id, priority, text, markup_json, status, attempts, next_attempt_at, created_at = row
    return OutboxRow(
        id=id_, alert_id=alert_id, chat_id=chat_id, priority=priority, text=text,
        reply_markup=json.loads(markup_json) if markup_json else None,
        status=status, attempts=attempts, next_attempt_at=next_attempt_at, created_at=created_at,
    )


def _clip(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_TEXT_LEN:
        return text
    keep = TELEGRAM_MAX_TEXT_LEN - len(_TRUNCATION_MARKER)
    return text[:keep] + _TRUNCATION_MARKER


def enqueue_broadcast(
    conn: sqlite3.Connection,
    alert_id: str,
    recipients: list[tuple[int, str, dict | None]],
    priority: int,
    now: datetime | None = None,
) -> int:
    """recipients: [(chat_id, text, reply_markup), ...]. Writes every
    recipient's row in ONE transaction — atomic fan-out, see module
    docstring. Returns the number of NEW rows actually inserted (a
    re-run against the same alert_id inserts 0, via UNIQUE(alert_id,
    chat_id) + INSERT OR IGNORE, not an error)."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    inserted = 0
    for chat_id, text, reply_markup in recipients:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO outbox
                (id, alert_id, chat_id, priority, text, reply_markup_json, status, attempts,
                 next_attempt_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                uuid.uuid4().hex, alert_id, chat_id, priority, _clip(text),
                json.dumps(reply_markup) if reply_markup is not None else None,
                now_iso, now_iso,
            ),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return inserted


def claim_ready_batch(conn: sqlite3.Connection, worker_id: str, limit: int, now: datetime) -> list[OutboxRow]:
    """Reclaims stale in_flight rows, then leases up to `limit` ready
    rows (pending, due) in priority order — HIGH before digests, FIFO
    within a priority. Select-then-update in one transaction; safe
    without SELECT...FOR UPDATE-style locking because of the single-
    instance guarantee (tradebot.telegram_bot.singleton) — there is
    never a second worker to race against."""
    reclaim_stale_in_flight(conn, now)
    now_iso = now.isoformat()
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM outbox WHERE status = 'pending' AND next_attempt_at <= ? "
        "ORDER BY priority ASC, created_at ASC LIMIT ?",
        (now_iso, limit),
    ).fetchall()
    claimed = [_row_to_outbox_row(r) for r in rows]
    if claimed:
        conn.executemany(
            "UPDATE outbox SET status = 'in_flight', leased_by = ?, leased_at = ? WHERE id = ?",
            [(worker_id, now_iso, r.id) for r in claimed],
        )
        conn.commit()
    return claimed


def reclaim_stale_in_flight(conn: sqlite3.Connection, now: datetime) -> int:
    """Rows leased by a worker that never came back (crashed, killed)
    go back to pending. Returns the number reclaimed."""
    cutoff = (now - timedelta(seconds=_lease_timeout_seconds())).isoformat()
    cur = conn.execute(
        "UPDATE outbox SET status = 'pending', leased_by = NULL, leased_at = NULL "
        "WHERE status = 'in_flight' AND leased_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def mark_delivered(conn: sqlite3.Connection, row_id: str, when: datetime) -> None:
    conn.execute(
        "UPDATE outbox SET status = 'delivered', delivered_at = ? WHERE id = ?", (when.isoformat(), row_id)
    )
    conn.commit()


def mark_retry(conn: sqlite3.Connection, row_id: str, next_attempt_at: datetime, error: str) -> None:
    """Puts the row back to pending with a new due time (honoring
    Telegram's exact retry_after on a 429, or our own backoff+jitter on
    a 5xx/network error — see tradebot.telegram_bot.worker) and bumps
    the attempt counter for max-attempts accounting."""
    conn.execute(
        "UPDATE outbox SET status = 'pending', attempts = attempts + 1, next_attempt_at = ?, "
        "leased_by = NULL, leased_at = NULL, last_error = ? WHERE id = ?",
        (next_attempt_at.isoformat(), error, row_id),
    )
    conn.commit()


def release_to_pending(conn: sqlite3.Connection, row_id: str) -> None:
    """Puts a leased row back to pending WITHOUT counting it as an
    attempt — used when the worker claimed a batch but then chose not to
    send a specific row THIS pass (rate-limited, or a graceful stop mid-
    batch), as opposed to mark_retry, which is only for an actual send
    that failed. Without this, a skipped row would sit 'in_flight' doing
    nothing until reclaim_stale_in_flight's lease timeout, needlessly
    delaying a row that was never even attempted."""
    conn.execute(
        "UPDATE outbox SET status = 'pending', leased_by = NULL, leased_at = NULL WHERE id = ?", (row_id,)
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, row_id: str, error: str) -> None:
    """Terminal: max retries exhausted on a retryable error, or a
    permanent (non-retryable) error like a malformed request. Not
    reclaimed, not retried again — visible for ops review via the
    outbox table itself."""
    conn.execute(
        "UPDATE outbox SET status = 'failed', attempts = attempts + 1, last_error = ?, "
        "leased_by = NULL, leased_at = NULL WHERE id = ?",
        (error, row_id),
    )
    conn.commit()


def mark_unsubscribed(conn: sqlite3.Connection, row_id: str, error: str) -> None:
    """Terminal: Forbidden or ChatNotFound — see
    db.mark_telegram_unreachable, which the worker also calls for the
    owning user so future alerts stop trying this chat at all."""
    conn.execute(
        "UPDATE outbox SET status = 'unsubscribed', last_error = ?, leased_by = NULL, leased_at = NULL "
        "WHERE id = ?",
        (error, row_id),
    )
    conn.commit()
