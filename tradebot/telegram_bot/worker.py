"""The outbox delivery worker — the only thing that ever calls the
Telegram API for an outbound send. Everything else (runner.py via
tradebot.alerts.TelegramAlerter, tradebot.telegram_bot.delivery) persists
to the outbox (see outbox.py) and returns immediately.

WorkerCore is the testable core: an injectable clock, sleep function,
sender, and stop check, so tests drive thousands of iterations with a
fake clock instead of real wall-clock time and a fake Telegram instead
of the real API. main() wires up the real versions — real time, real
SIGTERM handling, the real Bot API, a real single-instance lock — for
actual deployment. See tradebot.telegram_bot.outbox's module docstring
for the crash-safety guarantee (and its one honest limitation) this
worker relies on.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tradebot.telegram_bot import db, outbox
from tradebot.telegram_bot.outbound import SendOutcome, SendResult, send_once
from tradebot.telegram_bot.tokenbucket import TokenBucket

logger = logging.getLogger("watchtower.outbox_worker")

# Telegram's documented bulk-notification guidance: no more than ~30
# messages/second across the whole bot, no more than ~1/second to any
# single chat. See https://core.telegram.org/bots/faq#broadcasting-to-users
GLOBAL_RATE_CAPACITY = 30.0
GLOBAL_RATE_PER_SECOND = 30.0
CHAT_RATE_CAPACITY = 1.0
CHAT_RATE_PER_SECOND = 1.0

DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_IDLE_SLEEP_SECONDS = 1.0

HEARTBEAT_STALE_SECONDS = 5 * 60
HEARTBEAT_CHECK_INTERVAL_SECONDS = 30
PAGE_REPEAT_INTERVAL_SECONDS = 15 * 60


@dataclass
class WorkerCore:
    conn: object  # sqlite3.Connection to users.db (holds both the outbox table and users)
    sender: Callable[[int, str, dict | None], SendResult]
    worker_id: str = field(default_factory=lambda: f"{os.uname().nodename}:{os.getpid()}")
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    sleep_fn: Callable[[float], None] = time.sleep
    random_fn: Callable[[], float] = random.random
    stop_check_fn: Callable[[], bool] = field(default=lambda: False)
    mark_unreachable_fn: Callable[[int, datetime], None] | None = None  # None -> looks the user up itself
    batch_size: int = DEFAULT_BATCH_SIZE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    idle_sleep_seconds: float = DEFAULT_IDLE_SLEEP_SECONDS
    global_rate_capacity: float = GLOBAL_RATE_CAPACITY
    global_rate_per_second: float = GLOBAL_RATE_PER_SECOND
    chat_rate_capacity: float = CHAT_RATE_CAPACITY
    chat_rate_per_second: float = CHAT_RATE_PER_SECOND
    # Heartbeat paging — see _maybe_page_on_stale_heartbeat. None disables it
    # entirely (used by tests that don't care about this concern).
    heartbeat_path: Path | None = None
    is_rth_fn: Callable[[datetime], bool] = field(default=lambda now: False)
    page_chat_id: int | None = None
    # Overridable so tests never touch the real data/incidents.jsonl —
    # None means "use tradebot.incidents' own default path."
    incidents_path: Path | None = None

    def __post_init__(self) -> None:
        self._global_bucket = TokenBucket(
            capacity=self.global_rate_capacity, refill_per_second=self.global_rate_per_second,
            now_fn=self._monotonic,
        )
        self._chat_buckets: dict[int, TokenBucket] = {}
        self._last_heartbeat_check: float = 0.0
        self._paged_at: datetime | None = None

    def _monotonic(self) -> float:
        return self.now_fn().timestamp()

    def _chat_bucket(self, chat_id: int) -> TokenBucket:
        bucket = self._chat_buckets.get(chat_id)
        if bucket is None:
            bucket = TokenBucket(
                capacity=self.chat_rate_capacity, refill_per_second=self.chat_rate_per_second, now_fn=self._monotonic,
            )
            self._chat_buckets[chat_id] = bucket
        return bucket

    def _log(self, event: str, row, **extra) -> None:
        logger.info(event, extra={"event": event, "alert_id": row.alert_id, "chat_id": row.chat_id, **extra})

    # ------------------------------------------------------------ #
    # One claim-and-attempt pass
    # ------------------------------------------------------------ #

    def run_once(self) -> bool:
        """Claims one priority-ordered batch and attempts each ready row,
        respecting rate limits. Returns True if at least one delivery was
        attempted (the caller uses this to decide whether to sleep before
        the next pass).

        claim_ready_batch leases the WHOLE batch upfront (see its
        docstring), but rate-limiting or a stop request can mean a
        specific row is never actually attempted THIS pass — every such
        row must be explicitly released back to pending before returning,
        or it would sit uselessly 'in_flight' until the lease timeout
        instead of being immediately eligible again next pass."""
        now = self.now_fn()
        batch = outbox.claim_ready_batch(self.conn, self.worker_id, self.batch_size, now)
        made_progress = False
        for i, row in enumerate(batch):
            if self.stop_check_fn():
                self._release_batch(batch[i:])
                break
            if not self._global_bucket.try_consume():
                self._release_batch(batch[i:])
                break  # global cap reached — nothing else in this batch can proceed either
            chat_bucket = self._chat_bucket(row.chat_id)
            if not chat_bucket.try_consume():
                self._global_bucket.refund()
                outbox.release_to_pending(self.conn, row.id)
                continue  # this chat is rate-limited right now — skip ahead, revisit later
            made_progress = True
            self._attempt(row)
        return made_progress

    def _release_batch(self, rows) -> None:
        for row in rows:
            outbox.release_to_pending(self.conn, row.id)

    def _attempt(self, row) -> None:
        result = self.sender(row.chat_id, row.text, row.reply_markup)
        now = self.now_fn()

        if result.outcome == SendOutcome.DELIVERED:
            outbox.mark_delivered(self.conn, row.id, now)
            self._log("delivered", row)

        elif result.outcome == SendOutcome.RATE_LIMITED:
            next_attempt = now + timedelta(seconds=result.retry_after)
            outbox.mark_retry(self.conn, row.id, next_attempt, error=result.error)
            self._log("retry_rate_limited", row, retry_after=result.retry_after)

        elif result.outcome == SendOutcome.UNREACHABLE:
            self._mark_unreachable(row.chat_id, now)
            outbox.mark_unsubscribed(self.conn, row.id, error=result.error)
            self._log("auto_unsubscribed", row, error=result.error)

        elif result.outcome == SendOutcome.RETRYABLE_ERROR:
            attempts = row.attempts + 1
            if attempts >= self.max_attempts:
                outbox.mark_failed(self.conn, row.id, error=result.error)
                self._log("failed_permanently", row, attempts=attempts, error=result.error)
            else:
                delay = self._backoff_with_jitter(attempts)
                outbox.mark_retry(self.conn, row.id, now + timedelta(seconds=delay), error=result.error)
                self._log("retry_backoff", row, attempts=attempts, delay=delay, error=result.error)

        else:  # PERMANENT_ERROR
            outbox.mark_failed(self.conn, row.id, error=result.error)
            self._log("failed_permanent_error", row, error=result.error)

    def _mark_unreachable(self, chat_id: int, now: datetime) -> None:
        if self.mark_unreachable_fn is not None:
            self.mark_unreachable_fn(chat_id, now)
            return
        user = db.get_user_by_chat_id(self.conn, chat_id)
        if user is not None:
            db.mark_telegram_unreachable(self.conn, user.telegram_user_id, now)

    def _backoff_with_jitter(self, attempts: int) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**attempts)) — spreads
        retries out instead of every failed row waking up at the exact
        same instant and re-triggering the same rate limit."""
        ceiling = min(self.backoff_cap_seconds, self.backoff_base_seconds * (2 ** attempts))
        return self.random_fn() * ceiling

    # ------------------------------------------------------------ #
    # Heartbeat paging — a deadman switch on the SCANNER process
    # (runner.py), not on this worker. Bypasses the outbox entirely:
    # a page is best-effort-immediate, never queued behind other traffic.
    # ------------------------------------------------------------ #

    def _maybe_page_on_stale_heartbeat(self, now: datetime) -> None:
        if self.heartbeat_path is None or self.page_chat_id is None:
            return
        now_s = now.timestamp()
        if now_s - self._last_heartbeat_check < HEARTBEAT_CHECK_INTERVAL_SECONDS:
            return
        self._last_heartbeat_check = now_s

        if not self.is_rth_fn(now):
            return

        from tradebot import incidents
        from tradebot.telegram_bot.heartbeat import read_heartbeat

        hb = read_heartbeat(self.heartbeat_path)
        if hb is None:
            return  # nothing to judge staleness against — e.g. right at startup
        last = datetime.fromisoformat(hb["ts_utc"])
        staleness = (now - last).total_seconds()

        if staleness <= HEARTBEAT_STALE_SECONDS:
            self._paged_at = None  # recovered — a fresh page is allowed if it goes stale again
            incidents.close_incident("heartbeat_stale", now, path=self.incidents_path)
            return

        minutes = int(staleness // 60)
        incidents.open_incident(
            "heartbeat_stale", f"no scanner evaluation in {minutes} minutes during RTH", now, path=self.incidents_path
        )
        if self._paged_at is not None and (now - self._paged_at).total_seconds() < PAGE_REPEAT_INTERVAL_SECONDS:
            return  # already paged recently — don't spam every loop pass while still stale

        text = f"<b>PAGE</b>\nNo scanner evaluation in {minutes} minutes during RTH. The market data feed or the scanner process may be down."
        result = self.sender(self.page_chat_id, text, None)
        logger.error(
            "heartbeat_page_sent" if result.outcome == SendOutcome.DELIVERED else "heartbeat_page_failed",
            extra={"event": "heartbeat_page", "staleness_seconds": staleness, "outcome": result.outcome.value},
        )
        self._paged_at = now

    # ------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------ #

    def run_forever(self) -> None:
        logger.info("worker starting", extra={"event": "worker_start", "worker_id": self.worker_id})
        while not self.stop_check_fn():
            now = self.now_fn()
            self._maybe_page_on_stale_heartbeat(now)
            made_progress = self.run_once()
            if not made_progress:
                self.sleep_fn(self.idle_sleep_seconds)
        logger.info("worker stopping", extra={"event": "worker_stop", "worker_id": self.worker_id})

    def run_until_empty(self, max_passes: int = 100_000) -> None:
        """Runs until there is nothing left to claim or retry-schedule
        into the near future — used by tests and the chaos/load tests'
        second ("recovery") run, where "run forever" would never return."""
        for _ in range(max_passes):
            if self.stop_check_fn():
                return
            now = self.now_fn()
            self._maybe_page_on_stale_heartbeat(now)
            made_progress = self.run_once()
            if not made_progress:
                pending = self.conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE status IN ('pending', 'in_flight')"
                ).fetchone()[0]
                if pending == 0:
                    return
                self.sleep_fn(self.idle_sleep_seconds)


# -------------------------------------------------------------------- #
# CLI — wires up real time, real signals, the real Bot API, and the
# single-instance lock. WorkerCore above is what's actually under test.
# -------------------------------------------------------------------- #

DEFAULT_LOCK_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "outbox_worker.lock"


def _make_real_sender(token: str) -> Callable[[int, str, dict | None], SendResult]:
    def sender(chat_id: int, text: str, reply_markup: dict | None) -> SendResult:
        return send_once(token, chat_id, text, reply_markup)

    return sender


def _install_sigterm_handler() -> threading.Event:
    """Graceful SIGTERM: set a flag the main loop checks between rows and
    between batches, rather than dying mid-send. A second SIGTERM (or
    SIGINT from Ctrl-C during manual testing) is handled the same way —
    Python's default handler chain still applies to everything else."""
    stop_event = threading.Event()

    def _handler(signum, frame) -> None:
        logger.info("received SIGTERM, stopping after the current batch", extra={"event": "sigterm_received"})
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    return stop_event


def _make_stop_check_fn(stop_event: threading.Event, halt_file: Path) -> Callable[[], bool]:
    def stop_check() -> bool:
        if stop_event.is_set():
            return True
        if halt_file.exists():
            return True
        if os.environ.get("WATCHTOWER_KILL_SWITCH"):
            return True
        return False

    return stop_check


def _is_rth(now: datetime) -> bool:
    from tradebot.runner import CALENDAR, ET

    session_date = now.astimezone(ET).date()
    if not CALENDAR.is_session(session_date):
        return False
    open_ts = CALENDAR.session_open(session_date).to_pydatetime()
    close_ts = CALENDAR.session_close(session_date).to_pydatetime()
    return open_ts <= now <= close_ts


def build_worker(users_db_path: Path | str | None = None) -> WorkerCore:
    from tradebot.runner import HALT_FILE, HEARTBEAT_FILE

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set — the worker has nothing to send with.")
    page_chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID")

    conn = db.connect(users_db_path) if users_db_path is not None else db.connect()
    stop_event = _install_sigterm_handler()

    return WorkerCore(
        conn=conn,
        sender=_make_real_sender(token),
        stop_check_fn=_make_stop_check_fn(stop_event, HALT_FILE),
        heartbeat_path=HEARTBEAT_FILE,
        is_rth_fn=_is_rth,
        page_chat_id=int(page_chat_id_raw) if page_chat_id_raw else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users-db", default=None, help="path to users.db (defaults to data/users.db)")
    parser.add_argument(
        "--once", action="store_true",
        help="drain everything currently pending/retry-scheduled, then exit — for testing, not production use",
    )
    parser.add_argument("--lock-path", default=str(DEFAULT_LOCK_PATH))
    args = parser.parse_args()

    from tradebot.telegram_bot.jsonlog import configure_json_logging
    from tradebot.telegram_bot.singleton import AlreadyRunningError, SingleInstanceLock

    configure_json_logging(logger)

    lock = SingleInstanceLock(Path(args.lock_path))
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        logger.error("startup aborted: %s", exc, extra={"event": "already_running"})
        raise SystemExit(1) from exc

    try:
        worker = build_worker(args.users_db)
        if args.once:
            worker.run_until_empty()
        else:
            worker.run_forever()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
