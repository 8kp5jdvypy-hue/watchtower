"""Chaos test: kill the outbox worker mid-broadcast with a real SIGKILL
(not a graceful stop — this specifically exercises the crash-recovery
path that graceful shutdown never touches) and confirm the outbox's
lease/reclaim mechanism loses nothing and never double-delivers to the
same chat.

This runs a REAL subprocess of `python -m tradebot.telegram_bot.worker`
against a real local HTTP server on loopback (standing in for Telegram
via TELEGRAM_API_ROOT) — not an in-process simulation. It's slower and
more elaborate than the rest of this test suite on purpose: a literal
kill -9 is the only way to honestly test what happens when the worker
gets no chance to clean up after itself.

See tradebot.telegram_bot.outbox's module docstring for the one
documented limitation this test is designed around: a message can be
double-delivered if the kill lands in the exact microseconds between
"Telegram accepted it" and "we recorded that" — this test's per-request
delay and kill timing are chosen to make that specific race exceedingly
unlikely to prove the point, but the assertions below tolerate at most
one such duplicate rather than pretending the race can't exist.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tradebot.telegram_bot import db, outbox

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NUM_RECIPIENTS = 20
PER_REQUEST_DELAY_SECONDS = 0.15  # slow enough that a kill mid-batch reliably lands mid-batch


class _FakeTelegramServer:
    """Records every sendMessage call it receives and answers 200 OK
    after an artificial delay — real HTTP, real loopback socket, so a
    real subprocess worker can talk to it exactly like the live API."""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.lock = threading.Lock()
        self.received: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass  # silence — the test doesn't need the HTTP access log

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                time.sleep(server.delay_seconds)
                with server.lock:
                    server.received.append(body)
                payload = json.dumps({"ok": True, "result": {"message_id": len(server.received)}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def received_count(self) -> int:
        with self.lock:
            return len(self.received)

    def received_chat_ids(self) -> list[int]:
        with self.lock:
            return [r["chat_id"] for r in self.received]


def _spawn_worker(users_db: Path, api_root: str, lock_path: Path, extra_env: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["TELEGRAM_BOT_TOKEN"] = "chaos-test-token"
    env["TELEGRAM_API_ROOT"] = api_root
    env.pop("TELEGRAM_CHAT_ID", None)  # paging isn't part of this test
    env.pop("WATCHTOWER_KILL_SWITCH", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [
            sys.executable, "-m", "tradebot.telegram_bot.worker",
            "--users-db", str(users_db), "--once", "--lock-path", str(lock_path),
        ],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def _wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_kill_worker_mid_broadcast_loses_and_duplicates_nothing(tmp_path):
    users_db = tmp_path / "users.db"
    conn = db.connect(users_db)
    recipients = [(1000 + i, f"alert to chat {1000 + i}", None) for i in range(NUM_RECIPIENTS)]
    outbox.enqueue_broadcast(conn, "chaos-alert-1", recipients, outbox.PRIORITY_HIGH)
    conn.close()  # the subprocess opens its own connection to the same file

    lock_path = tmp_path / "worker.lock"
    server = _FakeTelegramServer(delay_seconds=PER_REQUEST_DELAY_SECONDS)
    try:
        api_root = f"http://127.0.0.1:{server.port}"

        proc1 = _spawn_worker(users_db, api_root, lock_path, extra_env={"OUTBOX_LEASE_TIMEOUT_SECONDS": "2"})
        try:
            # Let it get partway through the batch, then crash it for real.
            got_partway = _wait_until(lambda: server.received_count() >= NUM_RECIPIENTS // 3, timeout=10)
            assert got_partway, "the first worker never even started sending — test setup is broken"
            time.sleep(PER_REQUEST_DELAY_SECONDS * 2)  # a little more progress before the kill
            proc1.send_signal(signal.SIGKILL)
            proc1.wait(timeout=10)
        finally:
            if proc1.poll() is None:
                proc1.kill()
                proc1.wait(timeout=10)

        delivered_before_kill = server.received_count()
        assert 0 < delivered_before_kill < NUM_RECIPIENTS, (
            f"expected a genuine mid-batch kill (some but not all delivered), got {delivered_before_kill}/{NUM_RECIPIENTS} "
            "— tune PER_REQUEST_DELAY_SECONDS or the wait/kill timing if this becomes flaky"
        )

        # Give the (short, test-only) lease timeout a moment to actually
        # pass in real time before the second worker starts.
        time.sleep(2.5)

        proc2 = _spawn_worker(users_db, api_root, lock_path, extra_env={"OUTBOX_LEASE_TIMEOUT_SECONDS": "2"})
        try:
            out, _ = proc2.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc2.kill()
            out, _ = proc2.communicate()
            raise AssertionError(f"second worker never drained the queue; output:\n{out.decode(errors='replace')}")

        assert proc2.returncode == 0, f"second worker exited nonzero; output:\n{out.decode(errors='replace')}"

        conn2 = db.connect(users_db)
        statuses = conn2.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status").fetchall()
        status_map = dict(statuses)
        conn2.close()

        # No losses: every single row ends up delivered.
        assert status_map.get("delivered") == NUM_RECIPIENTS, f"status breakdown: {status_map}"
        assert "pending" not in status_map and "in_flight" not in status_map

        # No (meaningful) duplicates: each chat_id was actually delivered
        # to at most once, except for at most ONE chat that could have
        # been caught in the documented accept-but-not-yet-recorded race
        # window right at the moment of the kill.
        chat_ids_sent = server.received_chat_ids()
        assert len(chat_ids_sent) >= NUM_RECIPIENTS  # never fewer messages sent than rows enqueued
        duplicate_count = len(chat_ids_sent) - len(set(chat_ids_sent))
        assert duplicate_count <= 1, f"expected at most one duplicate from the crash-boundary race, got {duplicate_count}: {chat_ids_sent}"
    finally:
        server.stop()
