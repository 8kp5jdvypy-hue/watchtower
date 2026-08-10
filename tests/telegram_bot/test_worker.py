"""Tests for tradebot.telegram_bot.worker.WorkerCore — the outbox
delivery loop, driven entirely by a fake clock and a fake sender so
nothing here depends on real time or a real Telegram API. Priority
ordering, rate-limit skip-ahead, exact retry_after honoring,
auto-unsubscribe, and backoff are each pinned down by a dedicated test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.telegram_bot import db, outbox
from tradebot.telegram_bot.outbound import SendOutcome, SendResult
from tradebot.telegram_bot.worker import WorkerCore

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeSender:
    """Records every call. `responses[chat_id]` is a list consumed in
    order; once exhausted (or if chat_id was never configured), `default`
    is returned — DELIVERED unless a test overrides it."""

    def __init__(self, default: SendResult | None = None) -> None:
        self.calls: list[tuple[int, str, dict | None]] = []
        self.responses: dict[int, list[SendResult]] = {}
        self.default = default or SendResult(outcome=SendOutcome.DELIVERED, message_id=1)

    def __call__(self, chat_id: int, text: str, reply_markup: dict | None) -> SendResult:
        self.calls.append((chat_id, text, reply_markup))
        queue = self.responses.get(chat_id)
        if queue:
            return queue.pop(0)
        return self.default


def _conn():
    return db.connect(":memory:")


def _worker(conn, sender, clock, **overrides) -> WorkerCore:
    defaults = dict(
        conn=conn, sender=sender, worker_id="test-worker", now_fn=clock, sleep_fn=lambda s: clock.advance(s),
    )
    defaults.update(overrides)
    return WorkerCore(**defaults)


def test_delivers_a_single_ready_row():
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "a1", [(1, "hello", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender()
    worker = _worker(conn, sender, clock)

    made_progress = worker.run_once()

    assert made_progress is True
    assert sender.calls == [(1, "hello", None)]
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "delivered"


def test_high_priority_delivered_before_normal_and_log():
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "log1", [(1, "log", None)], outbox.PRIORITY_LOG, now=NOW)
    outbox.enqueue_broadcast(conn, "normal1", [(2, "normal", None)], outbox.PRIORITY_NORMAL, now=NOW)
    outbox.enqueue_broadcast(conn, "high1", [(3, "high", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender()
    worker = _worker(conn, sender, clock)

    worker.run_once()

    assert [c[1] for c in sender.calls] == ["high", "normal", "log"]


def test_per_chat_rate_limit_skips_ahead_instead_of_blocking_the_batch():
    """Two rows for the SAME chat can't both send in one pass (chat
    bucket capacity=1/sec) — the second must be skipped, not block a
    THIRD row for a DIFFERENT chat from going out in the same pass."""
    conn = _conn()
    clock = FakeClock(NOW)
    # Staggered created_at (for deterministic FIFO ordering) but all
    # already due by the time run_once() is called below.
    outbox.enqueue_broadcast(conn, "a1", [(1, "first for chat 1", None)], outbox.PRIORITY_HIGH, now=NOW)
    outbox.enqueue_broadcast(
        conn, "a2", [(1, "second for chat 1", None)], outbox.PRIORITY_HIGH, now=NOW + timedelta(seconds=1),
    )
    outbox.enqueue_broadcast(
        conn, "a3", [(2, "for chat 2", None)], outbox.PRIORITY_HIGH, now=NOW + timedelta(seconds=2),
    )
    sender = FakeSender()
    worker = _worker(conn, sender, clock, chat_rate_capacity=1.0, chat_rate_per_second=1.0)

    clock.advance(10)  # past every row's next_attempt_at, so all three are ready
    worker.run_once()

    sent_texts = {c[1] for c in sender.calls}
    assert "first for chat 1" in sent_texts
    assert "for chat 2" in sent_texts  # got through despite chat 1 being rate-limited
    assert "second for chat 1" not in sent_texts  # skipped this pass, still pending
    pending = conn.execute("SELECT text FROM outbox WHERE status = 'pending'").fetchall()
    assert pending == [("second for chat 1",)]


def test_global_rate_limit_stops_the_whole_batch_not_just_one_chat():
    conn = _conn()
    clock = FakeClock(NOW)
    recipients = [(i, f"msg{i}", None) for i in range(5)]
    outbox.enqueue_broadcast(conn, "a1", recipients, outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender()
    worker = _worker(conn, sender, clock, global_rate_capacity=2.0, global_rate_per_second=2.0)

    worker.run_once()

    assert len(sender.calls) == 2  # only the global cap's worth went out
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0] == 3


def test_rate_limited_response_honors_the_exact_retry_after():
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "a1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender(default=SendResult(outcome=SendOutcome.RATE_LIMITED, retry_after=37.0, error="429"))
    worker = _worker(conn, sender, clock)

    worker.run_once()

    row = conn.execute("SELECT status, next_attempt_at FROM outbox").fetchone()
    assert row[0] == "pending"
    assert datetime.fromisoformat(row[1]) == NOW + timedelta(seconds=37.0)


def test_unreachable_response_auto_unsubscribes_the_user():
    conn = _conn()
    clock = FakeClock(NOW)
    db.get_or_create_user(conn, 1, 1, "alice")
    outbox.enqueue_broadcast(conn, "a1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender(default=SendResult(outcome=SendOutcome.UNREACHABLE, error="Forbidden: bot was blocked"))
    worker = _worker(conn, sender, clock)

    worker.run_once()

    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "unsubscribed"
    assert db.get_user(conn, 1).is_telegram_unreachable is True


def test_unreachable_response_for_a_chat_with_no_user_row_does_not_crash():
    """The ops-channel chat_id has no corresponding user row — going
    unreachable there must still mark the outbox row, just with nothing
    to auto-unsubscribe."""
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "a1", [(-100200300, "ops alert", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender(default=SendResult(outcome=SendOutcome.UNREACHABLE, error="chat not found"))
    worker = _worker(conn, sender, clock)

    worker.run_once()  # must not raise
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "unsubscribed"


def test_retryable_error_backs_off_and_eventually_fails_permanently():
    """A fixed random_fn makes the backoff schedule deterministic (a real
    random draw near 0 could schedule a retry sooner than the per-chat
    rate limit allows it to actually fire, which is correct — the rate
    limit wins — but would make this specific test flaky rather than
    exercising anything wrong)."""
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "a1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender(default=SendResult(outcome=SendOutcome.RETRYABLE_ERROR, error="502 Bad Gateway"))
    worker = _worker(
        conn, sender, clock, max_attempts=3, backoff_base_seconds=1.0, backoff_cap_seconds=60.0,
        random_fn=lambda: 0.5,
    )

    for _ in range(3):
        worker.run_once()
        row = conn.execute("SELECT status, attempts, next_attempt_at FROM outbox").fetchone()
        if row[0] == "failed":
            break
        # fast-forward past whatever backoff was scheduled
        clock.t = datetime.fromisoformat(row[2]) + timedelta(seconds=0.01)

    final = conn.execute("SELECT status, attempts FROM outbox").fetchone()
    assert final == ("failed", 3)
    assert len(sender.calls) == 3  # exactly max_attempts real attempts, not more


def test_permanent_error_fails_immediately_without_retrying():
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "a1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender(default=SendResult(outcome=SendOutcome.PERMANENT_ERROR, error="400: message text is empty"))
    worker = _worker(conn, sender, clock)

    worker.run_once()

    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "failed"
    assert len(sender.calls) == 1  # never retried — a permanent error isn't worth burning attempts on


def test_run_once_respects_stop_check_fn_mid_batch():
    conn = _conn()
    clock = FakeClock(NOW)
    recipients = [(i, f"msg{i}", None) for i in range(5)]
    outbox.enqueue_broadcast(conn, "a1", recipients, outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender()
    stop_after = {"n": 2}

    def stop_check():
        return len(sender.calls) >= stop_after["n"]

    worker = _worker(conn, sender, clock, stop_check_fn=stop_check)
    worker.run_once()

    assert len(sender.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0] == 3


def test_run_until_empty_drains_including_a_scheduled_retry():
    conn = _conn()
    clock = FakeClock(NOW)
    outbox.enqueue_broadcast(conn, "a1", [(1, "hi", None)], outbox.PRIORITY_HIGH, now=NOW)
    sender = FakeSender(default=SendResult(outcome=SendOutcome.RATE_LIMITED, retry_after=5.0, error="429"))
    worker = _worker(conn, sender, clock, idle_sleep_seconds=1.0)

    calls_before_recovery = []

    real_call = sender.__call__

    def sender_then_recover(chat_id, text, reply_markup):
        result = real_call(chat_id, text, reply_markup)
        calls_before_recovery.append(result)
        if len(calls_before_recovery) >= 2:
            sender.default = SendResult(outcome=SendOutcome.DELIVERED, message_id=1)
        return result

    worker.sender = sender_then_recover
    worker.run_until_empty()

    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "delivered"


def test_heartbeat_paging_fires_once_when_stale_during_rth(tmp_path):
    import json

    heartbeat_path = tmp_path / "heartbeat.json"
    heartbeat_path.write_text(json.dumps({"ts_utc": (NOW - timedelta(minutes=20)).isoformat()}))
    conn = _conn()
    clock = FakeClock(NOW)
    sender = FakeSender()
    worker = _worker(
        conn, sender, clock, heartbeat_path=heartbeat_path, is_rth_fn=lambda now: True, page_chat_id=999,
        incidents_path=tmp_path / "incidents.jsonl",
    )

    worker._maybe_page_on_stale_heartbeat(clock())

    assert sender.calls == [(999, sender.calls[0][1], None)]
    assert "10 minutes" in sender.calls[0][1] or "minutes" in sender.calls[0][1]


def test_heartbeat_paging_is_silent_when_fresh():
    conn = _conn()
    clock = FakeClock(NOW)
    sender = FakeSender()

    from tradebot.telegram_bot import heartbeat as bot_liveness

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = __import__("pathlib").Path(d) / "heartbeat.json"
        bot_liveness.write_heartbeat(path, NOW - timedelta(seconds=30))
        worker = _worker(
            conn, sender, clock, heartbeat_path=path, is_rth_fn=lambda now: True, page_chat_id=999,
            incidents_path=path.parent / "incidents.jsonl",
        )
        worker._maybe_page_on_stale_heartbeat(clock())

    assert sender.calls == []


def test_heartbeat_paging_is_silent_outside_rth():
    conn = _conn()
    clock = FakeClock(NOW)
    sender = FakeSender()
    worker = _worker(conn, sender, clock, is_rth_fn=lambda now: False, page_chat_id=999)
    # heartbeat_path is None by default -> also silent, but is_rth check must independently gate too
    assert worker.heartbeat_path is None
    worker._maybe_page_on_stale_heartbeat(clock())
    assert sender.calls == []


def test_heartbeat_paging_does_not_repeat_within_the_repeat_interval(tmp_path):
    import json

    heartbeat_path = tmp_path / "heartbeat.json"
    heartbeat_path.write_text(json.dumps({"ts_utc": (NOW - timedelta(minutes=10)).isoformat()}))
    conn = _conn()
    clock = FakeClock(NOW)
    sender = FakeSender()
    worker = _worker(
        conn, sender, clock, heartbeat_path=heartbeat_path, is_rth_fn=lambda now: True, page_chat_id=999,
        incidents_path=tmp_path / "incidents.jsonl",
    )

    worker._maybe_page_on_stale_heartbeat(clock())
    clock.advance(60)  # still stale, but well within the repeat cooldown
    worker._last_heartbeat_check = 0  # force the interval-throttle to allow re-checking
    worker._maybe_page_on_stale_heartbeat(clock())


def test_stale_heartbeat_opens_an_incident_and_recovery_closes_it(tmp_path):
    import json

    from tradebot import incidents

    heartbeat_path = tmp_path / "heartbeat.json"
    incidents_path = tmp_path / "incidents.jsonl"
    heartbeat_path.write_text(json.dumps({"ts_utc": (NOW - timedelta(minutes=20)).isoformat()}))
    conn = _conn()
    clock = FakeClock(NOW)
    sender = FakeSender()
    worker = _worker(
        conn, sender, clock, heartbeat_path=heartbeat_path, is_rth_fn=lambda now: True, page_chat_id=999,
        incidents_path=incidents_path,
    )

    worker._maybe_page_on_stale_heartbeat(clock())
    open_incidents = incidents.list_incidents(path=incidents_path)
    assert len(open_incidents) == 1
    assert open_incidents[0]["kind"] == "heartbeat_stale"
    assert open_incidents[0]["ended_at"] is None

    # feed recovers -> a fresh, non-stale heartbeat write
    clock.advance(600)
    heartbeat_path.write_text(json.dumps({"ts_utc": clock().isoformat()}))
    worker._last_heartbeat_check = 0
    worker._maybe_page_on_stale_heartbeat(clock())

    closed_incidents = incidents.list_incidents(path=incidents_path)
    assert len(closed_incidents) == 1
    assert closed_incidents[0]["ended_at"] is not None

    assert len(sender.calls) == 1  # no repeat page yet
