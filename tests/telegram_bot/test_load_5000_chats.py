"""Load test: 5,000 distinct chats, one HIGH-priority message each,
drained without ever tripping Telegram's documented rate limits (30
msgs/sec global, 1 msg/sec per chat — see worker.py's GLOBAL_RATE_*/
CHAT_RATE_* constants, which encode those same numbers).

This runs entirely in-process with a fake clock and a fake sender — no
real sleeping, no real subprocess, no real network — so 5,000 chats
drain in a couple of real seconds (SQLite I/O, not sleeping, is the
only real time spent) despite representing well over two real minutes
of simulated pacing (5000 / 30-per-second).

The fake sender is backed by an INDEPENDENT pair of token buckets (an
"oracle", using the same real-world capacity/refill constants as the
worker's own limiter, driven by the same fake clock) that stand in for
Telegram's actual server-side enforcement. If the worker's own
self-throttling ever let a send through that the oracle would have
rejected, that's exactly what "tripping the limit" means, and the test
fails. This is deliberately a different bucket instance than the one
WorkerCore uses internally — it exists to catch a wiring bug (e.g. a
send that bypasses the worker's own bucket accounting), not to
re-verify TokenBucket's arithmetic, which test_tokenbucket.py already
covers in isolation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.telegram_bot import db, outbox
from tradebot.telegram_bot.outbound import SendOutcome, SendResult
from tradebot.telegram_bot.tokenbucket import TokenBucket
from tradebot.telegram_bot.worker import (
    CHAT_RATE_CAPACITY,
    CHAT_RATE_PER_SECOND,
    GLOBAL_RATE_CAPACITY,
    GLOBAL_RATE_PER_SECOND,
    WorkerCore,
)

NUM_CHATS = 5_000


class FakeClock:
    """now_fn for WorkerCore: fast-forwarded by sleep_fn, never by real
    wall-clock time — this is what lets 5,000 chats' worth of simulated
    pacing run in a fraction of a real second."""

    def __init__(self, start: datetime):
        self.current = start
        self.total_slept = 0.0

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.total_slept += seconds


class RateLimitOracle:
    """Independent stand-in for Telegram's real server-side enforcement.
    Every violation recorded here means the worker sent faster than the
    real API would have allowed — see module docstring."""

    def __init__(self, clock: FakeClock):
        self._monotonic = lambda: clock.current.timestamp()
        self.global_bucket = TokenBucket(
            capacity=GLOBAL_RATE_CAPACITY, refill_per_second=GLOBAL_RATE_PER_SECOND, now_fn=self._monotonic
        )
        self._chat_buckets: dict[int, TokenBucket] = {}
        self.violations: list[tuple[str, int]] = []

    def _chat_bucket(self, chat_id: int) -> TokenBucket:
        bucket = self._chat_buckets.get(chat_id)
        if bucket is None:
            bucket = TokenBucket(
                capacity=CHAT_RATE_CAPACITY, refill_per_second=CHAT_RATE_PER_SECOND, now_fn=self._monotonic
            )
            self._chat_buckets[chat_id] = bucket
        return bucket

    def check(self, chat_id: int) -> bool:
        """Returns True if a real Telegram would have accepted this send
        right now. Consumes from both oracle buckets so back-to-back
        checks correctly reflect depletion, exactly like the real API's
        own accounting would."""
        global_ok = self.global_bucket.try_consume()
        chat_ok = self._chat_bucket(chat_id).try_consume()
        if not global_ok:
            self.violations.append(("global", chat_id))
        if global_ok and not chat_ok:
            self.violations.append(("chat", chat_id))
        if global_ok and chat_ok:
            return True
        if global_ok:  # consumed a global token for a check that failed on the chat side — give it back
            self.global_bucket.refund()
        return False


def _make_fake_sender(oracle: RateLimitOracle, delivered_log: list[int]):
    def sender(chat_id: int, text: str, reply_markup: dict | None) -> SendResult:
        oracle.check(chat_id)  # records a violation if the real API would have rejected this
        delivered_log.append(chat_id)
        return SendResult(outcome=SendOutcome.DELIVERED, message_id=len(delivered_log))

    return sender


def test_5000_chats_drain_without_ever_tripping_telegrams_rate_limits(tmp_path):
    conn = db.connect(tmp_path / "users.db")
    start = datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)
    recipients = [(chat_id, f"alert for chat {chat_id}", None) for chat_id in range(1, NUM_CHATS + 1)]
    inserted = outbox.enqueue_broadcast(conn, "load-test-alert", recipients, outbox.PRIORITY_HIGH, now=start)
    assert inserted == NUM_CHATS

    clock = FakeClock(start)
    oracle = RateLimitOracle(clock)
    delivered_log: list[int] = []

    worker = WorkerCore(
        conn=conn,
        sender=_make_fake_sender(oracle, delivered_log),
        now_fn=clock,
        sleep_fn=clock.advance,
        # Matched to GLOBAL_RATE_CAPACITY on purpose: a bigger batch just
        # means more claimed-then-immediately-released rows (each a
        # write) every pass once the global bucket runs dry, which is
        # pure SQLite commit overhead with no effect on what's being
        # tested here.
        batch_size=int(GLOBAL_RATE_CAPACITY),
    )

    worker.run_until_empty(max_passes=1_000_000)

    assert oracle.violations == [], f"{len(oracle.violations)} sends would have tripped Telegram's real limits"
    assert len(delivered_log) == NUM_CHATS
    assert len(set(delivered_log)) == NUM_CHATS  # every chat delivered to exactly once

    statuses = dict(conn.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status").fetchall())
    assert statuses == {"delivered": NUM_CHATS}

    # Sanity check on the simulation itself: draining 5,000 chats at a
    # real 30/sec global cap takes a bit under 3 real minutes of pacing —
    # if this drops near-zero, the test stopped actually exercising the
    # rate limiter and would pass vacuously.
    expected_minimum_seconds = (NUM_CHATS / GLOBAL_RATE_PER_SECOND) - GLOBAL_RATE_CAPACITY
    assert clock.total_slept >= expected_minimum_seconds * 0.9
