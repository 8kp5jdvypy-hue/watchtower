"""The apns-worker process: drains push_outbox to Apple.

    python -m tradebot.push.worker            # run
    python -m tradebot.push.worker --health   # docker healthcheck probe

Unconfigured (no APNS_* env) it idles, writing a heartbeat with
status 'disabled' so the container reads healthy and the rest of the
stack is untouched -- the same kill-switch shape postmarket-discovery
uses. Configured, each tick: reclaim stale leases, claim a batch, send,
record delivered / retry / unsubscribed (410 also deactivates the
device). Lease + idempotent enqueue = at-least-once with no duplicates
across restarts. Heartbeat every tick; SIGTERM stops cleanly.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from tradebot.push import apns, store

logger = logging.getLogger("watchtower.push.worker")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HEARTBEAT_FILE = REPO_ROOT / "data" / "push_worker_heartbeat.json"
TICK_SECONDS = 2.0
IDLE_SECONDS = 30.0
BATCH = 50


def _write_heartbeat(path: Path, payload: dict) -> None:
    from tradebot.postmarket_shadow import write_heartbeat_atomic

    write_heartbeat_atomic(path, {"ts_utc": datetime.now(timezone.utc).isoformat(), **payload})


class WorkerCore:
    """One tick of work, testable with a fake sender."""

    def __init__(self, conn, sender, *, worker_id: str | None = None, now_fn=lambda: datetime.now(timezone.utc)) -> None:
        self.conn, self.sender, self.now_fn = conn, sender, now_fn
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    def tick(self) -> dict[str, int]:
        now = self.now_fn()
        counts = {"reclaimed": store.reclaim_stale_in_flight(self.conn, now), "delivered": 0, "retried": 0, "failed": 0, "unsubscribed": 0}
        for row in store.claim_ready_batch(self.conn, self.worker_id, BATCH, now):
            device = store.get_device(self.conn, row.device_id)
            if device is None or not device.active:
                store.mark_unsubscribed(self.conn, row.id, "device inactive")
                counts["unsubscribed"] += 1
                continue
            result = self.sender.send(device_token=device.token, environment=device.apns_environment, payload=row.payload, collapse_id=row.collapse_id)
            if result.ok:
                store.mark_delivered(self.conn, row.id, result.apns_id, self.now_fn())
                counts["delivered"] += 1
            elif result.unregister:
                store.revoke_device_token(self.conn, device.id, f"apns:{result.reason}", now=self.now_fn())
                store.mark_unsubscribed(self.conn, row.id, f"apns:{result.reason}")
                counts["unsubscribed"] += 1
            elif result.retry:
                store.mark_retry(self.conn, row.id, row.attempts, f"apns:{result.status}:{result.reason}", self.now_fn())
                counts["retried"] += 1
            else:
                # Permanent per-request failure (bad payload, topic mismatch…): no retry.
                store.mark_retry(self.conn, row.id, store.MAX_ATTEMPTS, f"apns:{result.status}:{result.reason}", self.now_fn())
                counts["failed"] += 1
        return counts


def _install_sigterm() -> threading.Event:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return stop


def health(heartbeat: Path, max_age: float) -> int:
    try:
        payload = json.loads(heartbeat.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["ts_utc"])).total_seconds()
    except (OSError, ValueError, KeyError):
        print("push worker: no heartbeat")
        return 1
    if age > max_age:
        print(f"push worker: heartbeat stale ({int(age)}s)")
        return 1
    print(f"push worker: {payload.get('status', 'unknown')} ({int(age)}s)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--heartbeat", default=str(HEARTBEAT_FILE))
    parser.add_argument("--max-age", type=float, default=120.0)
    parser.add_argument("--once", action="store_true", help="one tick then exit (tests / ops)")
    args = parser.parse_args(argv)
    heartbeat = Path(args.heartbeat)
    if args.health:
        return health(heartbeat, args.max_age)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from tradebot.telegram_bot.db import connect as users_connect  # users.db lives with the accounts; see push/__init__

    conn = users_connect()
    store.ensure_schema(conn)
    stop = _install_sigterm()

    if not apns.configured():
        logger.info("push worker: APNS_* not configured; idling with status=disabled")
        while not stop.is_set():
            _write_heartbeat(heartbeat, {"status": "disabled"})
            if args.once:
                return 0
            stop.wait(IDLE_SECONDS)
        return 0

    sender = apns.ApnsClient.from_env()
    core = WorkerCore(conn, sender)
    logger.info("push worker: configured, worker_id=%s", core.worker_id)
    try:
        while not stop.is_set():
            counts = core.tick()
            _write_heartbeat(heartbeat, {"status": "running", **counts})
            if any(counts[k] for k in ("delivered", "retried", "failed", "unsubscribed")):
                logger.info("push_tick %s", counts)
            if args.once:
                return 0
            stop.wait(TICK_SECONDS)
    finally:
        sender.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
