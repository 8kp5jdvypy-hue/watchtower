"""Tests for tradebot.telegram_bot.outbox — the persist-before-delivery
outbox pattern: idempotent enqueue, priority-ordered claiming, and the
lease/reclaim cycle that makes a crashed worker's in-flight rows
recoverable rather than lost."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.telegram_bot import db, outbox

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


def _conn():
    return db.connect(":memory:")


def test_enqueue_broadcast_writes_one_row_per_recipient():
    conn = _conn()
    n = outbox.enqueue_broadcast(
        conn, "alert1", [(1, "hi user 1", None), (2, "hi user 2", None)], outbox.PRIORITY_HIGH, now=NOW,
    )
    assert n == 2
    rows = conn.execute("SELECT chat_id, text FROM outbox ORDER BY chat_id").fetchall()
    assert rows == [(1, "hi user 1"), (2, "hi user 2")]


def test_idempotency_key_is_alert_id_plus_chat_id():
    """Re-enqueueing the same (alert_id, chat_id) — e.g. a producer retry
    after a crash — is a safe no-op, never a duplicate row."""
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    second = outbox.enqueue_broadcast(conn, "alert1", [(1, "hi (retry)", None)], outbox.PRIORITY_HIGH, now=NOW)
    assert second == 0
    rows = conn.execute("SELECT text FROM outbox WHERE alert_id = 'alert1' AND chat_id = 1").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "hi"  # the ORIGINAL text — a retry never overwrites an already-enqueued row


def test_same_alert_id_different_chat_ids_are_independent_rows():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "a", None)], outbox.PRIORITY_HIGH, now=NOW)
    outbox.enqueue_broadcast(conn, "alert1", [(2, "b", None)], outbox.PRIORITY_HIGH, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE alert_id = 'alert1'").fetchone()[0] == 2


def test_enqueue_is_atomic_across_all_recipients_in_one_call():
    """A crash mid-fan-out can only happen BEFORE the single commit (so
    nothing is written) or AFTER it (so everything is) — there is no
    partially-enqueued state to observe from outside this function."""
    conn = _conn()
    recipients = [(i, f"msg {i}", None) for i in range(50)]
    n = outbox.enqueue_broadcast(conn, "alert1", recipients, outbox.PRIORITY_HIGH, now=NOW)
    assert n == 50
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 50


def test_enqueue_clips_text_over_the_telegram_char_cap():
    conn = _conn()
    long_text = "x" * 5000
    outbox.enqueue_broadcast(conn, "alert1", [(1, long_text, None)], outbox.PRIORITY_HIGH, now=NOW)
    stored = conn.execute("SELECT text FROM outbox").fetchone()[0]
    assert len(stored) <= outbox.TELEGRAM_MAX_TEXT_LEN
    assert stored.endswith("[truncated]")


def test_enqueue_does_not_clip_text_at_or_under_the_cap():
    conn = _conn()
    exact = "x" * outbox.TELEGRAM_MAX_TEXT_LEN
    outbox.enqueue_broadcast(conn, "alert1", [(1, exact, None)], outbox.PRIORITY_HIGH, now=NOW)
    stored = conn.execute("SELECT text FROM outbox").fetchone()[0]
    assert stored == exact


def test_enqueue_round_trips_the_reply_markup():
    conn = _conn()
    keyboard = {"inline_keyboard": [[{"text": "I took this", "callback_data": "took:abc"}]]}
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", keyboard)], outbox.PRIORITY_HIGH, now=NOW)
    rows = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)
    assert rows[0].reply_markup == keyboard


def test_claim_ready_batch_orders_high_before_normal_before_log():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "log1", [(1, "log", None)], outbox.PRIORITY_LOG, now=NOW)
    outbox.enqueue_broadcast(conn, "normal1", [(1, "normal", None)], outbox.PRIORITY_NORMAL, now=NOW)
    outbox.enqueue_broadcast(conn, "high1", [(1, "high", None)], outbox.PRIORITY_HIGH, now=NOW)

    claimed = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)
    assert [r.text for r in claimed] == ["high", "normal", "log"]


def test_claim_ready_batch_is_fifo_within_the_same_priority():
    conn = _conn()
    for i in range(3):
        outbox.enqueue_broadcast(
            conn, f"alert{i}", [(1, f"msg{i}", None)], outbox.PRIORITY_HIGH, now=NOW + timedelta(seconds=i),
        )
    claimed = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW + timedelta(seconds=10))
    assert [r.text for r in claimed] == ["msg0", "msg1", "msg2"]


def test_claim_ready_batch_respects_the_limit():
    conn = _conn()
    recipients = [(i, f"m{i}", None) for i in range(10)]
    outbox.enqueue_broadcast(conn, "alert1", recipients, outbox.PRIORITY_HIGH, now=NOW)
    claimed = outbox.claim_ready_batch(conn, "worker-1", limit=3, now=NOW)
    assert len(claimed) == 3


def test_claim_ready_batch_excludes_rows_not_yet_due():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "later", None)], outbox.PRIORITY_HIGH, now=NOW)
    # simulate a retry scheduled into the future
    row_id = conn.execute("SELECT id FROM outbox").fetchone()[0]
    outbox.mark_retry(conn, row_id, NOW + timedelta(minutes=5), error="simulated")
    claimed = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)
    assert claimed == []
    claimed_later = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW + timedelta(minutes=6))
    assert len(claimed_later) == 1


def test_claim_ready_batch_leases_the_row_so_it_is_not_claimed_twice():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    first = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)
    second = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)
    assert len(first) == 1
    assert second == []  # already in_flight, not pending


def test_release_to_pending_does_not_count_as_an_attempt():
    """Used when the worker leased a row but chose not to send it this
    pass (rate-limited) — must be immediately re-claimable, not treated
    as a failed send."""
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    row = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)[0]
    outbox.release_to_pending(conn, row.id)

    status_and_attempts = conn.execute("SELECT status, attempts FROM outbox WHERE id = ?", (row.id,)).fetchone()
    assert status_and_attempts == ("pending", 0)
    reclaimed = outbox.claim_ready_batch(conn, "worker-2", limit=10, now=NOW)  # immediately, no lease-timeout wait
    assert len(reclaimed) == 1


def test_mark_delivered_is_terminal_and_never_reclaimed():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    row = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)[0]
    outbox.mark_delivered(conn, row.id, NOW)
    much_later = NOW + timedelta(hours=1)
    assert outbox.claim_ready_batch(conn, "worker-1", limit=10, now=much_later) == []
    status = conn.execute("SELECT status FROM outbox WHERE id = ?", (row.id,)).fetchone()[0]
    assert status == "delivered"


def test_stale_in_flight_row_is_reclaimed_after_the_lease_timeout():
    """The core crash-recovery mechanism: a worker that leased a row and
    never came back (crashed) must not lose that row forever."""
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    claimed = outbox.claim_ready_batch(conn, "dead-worker", limit=10, now=NOW)
    assert len(claimed) == 1  # leased, but the "worker" now vanishes without confirming

    just_before_timeout = NOW + timedelta(seconds=outbox.LEASE_TIMEOUT_SECONDS - 5)
    assert outbox.claim_ready_batch(conn, "worker-2", limit=10, now=just_before_timeout) == []

    after_timeout = NOW + timedelta(seconds=outbox.LEASE_TIMEOUT_SECONDS + 5)
    reclaimed = outbox.claim_ready_batch(conn, "worker-2", limit=10, now=after_timeout)
    assert len(reclaimed) == 1
    assert reclaimed[0].id == claimed[0].id


def test_mark_retry_increments_attempts_and_reschedules():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    row = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)[0]
    outbox.mark_retry(conn, row.id, NOW + timedelta(seconds=30), error="429: rate limited")
    updated = conn.execute(
        "SELECT status, attempts, last_error FROM outbox WHERE id = ?", (row.id,)
    ).fetchone()
    assert updated == ("pending", 1, "429: rate limited")


def test_mark_failed_is_terminal_and_never_reclaimed():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    row = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)[0]
    outbox.mark_failed(conn, row.id, error="500 five times in a row")
    much_later = NOW + timedelta(hours=1)
    assert outbox.claim_ready_batch(conn, "worker-1", limit=10, now=much_later) == []
    status = conn.execute("SELECT status FROM outbox WHERE id = ?", (row.id,)).fetchone()[0]
    assert status == "failed"


def test_mark_unsubscribed_is_terminal_and_never_reclaimed():
    conn = _conn()
    outbox.enqueue_broadcast(conn, "alert1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    row = outbox.claim_ready_batch(conn, "worker-1", limit=10, now=NOW)[0]
    outbox.mark_unsubscribed(conn, row.id, error="403: bot was blocked by the user")
    much_later = NOW + timedelta(hours=1)
    assert outbox.claim_ready_batch(conn, "worker-1", limit=10, now=much_later) == []
    status = conn.execute("SELECT status FROM outbox WHERE id = ?", (row.id,)).fetchone()[0]
    assert status == "unsubscribed"
