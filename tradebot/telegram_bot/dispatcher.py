"""The long-polling command dispatcher: routes text commands and button
taps to handlers, and enforces every cross-cutting rule from the spec —
the 2s ack SLA, group-vs-DM gating, the per-user rate limit, and the
"never die silently" exception wrapper. Handlers themselves (in
handlers.py/callbacks.py) stay free of this plumbing.

Concurrency note: SQLite connections aren't safe for concurrent access
from multiple threads without external serialization. Updates are
dispatched to a thread pool so one slow handler can't block another
user's ack, but handler *execution* is serialized behind a single lock —
correctness over throughput. For this bot's scale that's the right
trade: a queued handler still gets its "still working on it" ack on time
even if it has to wait its turn to actually run.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from tradebot.telegram_bot import commands, db
from tradebot.telegram_bot.context import CallbackContext, CallbackReply, HandlerContext, Reply
from tradebot.telegram_bot.ratelimit import RateLimiter

ACK_THRESHOLD_SECONDS = 1.5
ACK_TEXT = "⏳ One sec…"

logger = logging.getLogger("watchtower.telegram_bot")


class Dispatcher:
    def __init__(
        self,
        client,
        users_conn,
        journal_conn,
        app_config,
        handlers: dict,
        callback_handlers: dict,
        onboarding_text_handlers: dict | None = None,
        rate_limiter: RateLimiter | None = None,
        max_workers: int = 8,
    ) -> None:
        self.client = client
        self.users_conn = users_conn
        self.journal_conn = journal_conn
        self.app = app_config
        self.handlers = handlers
        self.callback_handlers = callback_handlers
        self.onboarding_text_handlers = onboarding_text_handlers or {}
        self.rate_limiter = rate_limiter or RateLimiter()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._handler_lock = threading.RLock()
        self._offset: int | None = None

    # ---------------------------------------------------------------- #
    # Startup
    # ---------------------------------------------------------------- #

    def startup_check(self) -> None:
        """Fails loudly (raises) if the code's command list and
        BotFather's registered commands have drifted apart. Callers
        should let this exception propagate to process exit — see
        run_forever()."""
        commands.verify_commands_match_botfather(self.client)
        logger.info("startup check passed: command registry matches BotFather")

    # ---------------------------------------------------------------- #
    # Main loop
    # ---------------------------------------------------------------- #

    def run_forever(self) -> None:
        self.startup_check()
        while True:
            try:
                updates = self.client.get_updates(offset=self._offset, timeout=30)
            except Exception:
                # A single network blip or a 409 (another process already
                # long-polling this same bot token) must never take the
                # whole dispatcher down — log it, back off briefly, and
                # keep polling. Same discipline as TelegramAlerter.send()
                # and the scanner's per-symbol loop: transient failures
                # get logged and the loop continues.
                logger.exception("get_updates failed; backing off before retrying")
                time.sleep(5)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                self._executor.submit(self._safe_handle_update, update)

    def process_updates_once(self, updates: list) -> None:
        """Test/CLI hook — processes a fixed batch synchronously instead
        of long-polling forever."""
        futures = [self._executor.submit(self._safe_handle_update, u) for u in updates]
        for f in futures:
            f.result()

    def _get_user(self, user_id: int, chat_id: int, username: str | None) -> db.User:
        user = db.get_or_create_user(self.users_conn, user_id, chat_id, username)
        should_be_admin = user_id in self.app.admin_ids
        if should_be_admin != user.is_admin:
            db.set_admin(self.users_conn, user_id, should_be_admin)
            user = db.get_user(self.users_conn, user_id)
        return user

    def _safe_handle_update(self, update: dict) -> None:
        try:
            self._handle_update(update)
        except Exception:
            logger.exception("unhandled error routing update: %r", update)

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update and "text" in update["message"]:
            self._handle_message(update["message"])
        # other update types (edited_message, non-text media, etc.) are ignored

    # ---------------------------------------------------------------- #
    # Text commands
    # ---------------------------------------------------------------- #

    @staticmethod
    def _parse_command(text: str) -> tuple:
        parts = text.strip().split()
        command = parts[0][1:].split("@")[0].lower()
        return command, parts[1:]

    def _handle_message(self, message: dict) -> None:
        """Everything from here through the handler call is serialized on
        _handler_lock — see the module docstring. This matters beyond just
        the handler itself: _get_user() writes to users_conn, and SQLite
        connections aren't safe to touch from multiple threads at once
        even with check_same_thread=False — letting that race (as an
        earlier version of this method did) is a real crash, not a
        theoretical one."""
        chat = message["chat"]
        chat_id = chat["id"]
        chat_type = chat["type"]
        from_user = message["from"]
        user_id = from_user["id"]
        username = from_user.get("username")
        text = message["text"].strip()
        now = datetime.now(timezone.utc)

        with self._handler_lock:
            user = self._get_user(user_id, chat_id, username)

            if not self.rate_limiter.allow(user_id, now):
                retry = self.rate_limiter.retry_after_seconds(user_id, now)
                self._reply(chat_id, f"Slow down — you've hit the {self.rate_limiter.max_per_window}/min command limit. Try again in {retry:.0f}s.")
                return

            if not text.startswith("/"):
                self._maybe_handle_onboarding_text(user, chat_id, chat_type, text, now)
                return

            command, args = self._parse_command(text)

            if command not in commands.COMMAND_NAMES:
                self._reply(chat_id, f"I don't know /{command}. Send /help for the full list.")
                return

            if chat_type != "private" and command not in commands.GROUP_ALLOWED:
                self._reply(chat_id, "DM me for that one — it can touch your personal data, so it only works in a private chat.")
                return

            handler = self.handlers[command]
            ctx = HandlerContext(
                client=self.client, users_conn=self.users_conn, journal_conn=self.journal_conn,
                user=user, chat_id=chat_id, chat_type=chat_type, args=args, now=now, app=self.app,
            )
            self._run_with_ack(chat_id, lambda: handler(ctx))

    def _maybe_handle_onboarding_text(self, user: db.User, chat_id: int, chat_type: str, text: str, now: datetime) -> None:
        handler = self.onboarding_text_handlers.get(user.onboarding_step)
        if handler is None:
            return  # not a command, and we're not mid-onboarding waiting on free text — nothing to say
        ctx = HandlerContext(
            client=self.client, users_conn=self.users_conn, journal_conn=self.journal_conn,
            user=user, chat_id=chat_id, chat_type=chat_type, args=text.split(), now=now, app=self.app,
        )
        self._run_with_ack(chat_id, lambda: handler(ctx, text))

    def _run_with_ack(self, chat_id: int, run_handler) -> None:
        """Runs run_handler() under the serialization lock. If it hasn't
        finished within ACK_THRESHOLD_SECONDS, sends an immediate ack and
        edits it with the real result once done — see module docstring."""
        done = threading.Event()
        ack_holder: dict = {}

        def send_ack_if_slow() -> None:
            if not done.wait(ACK_THRESHOLD_SECONDS):
                try:
                    ack_holder["msg"] = self.client.send_message(chat_id, ACK_TEXT)
                except Exception:
                    logger.exception("failed to send ack message")

        watcher = threading.Thread(target=send_ack_if_slow, daemon=True)
        watcher.start()

        try:
            with self._handler_lock:
                reply = run_handler()
        except Exception:
            error_id = uuid.uuid4().hex[:8]
            logger.exception("handler error ref=%s", error_id)
            reply = Reply(text=f"Something broke on my end (ref {error_id}). It's been logged — try again shortly.")
        finally:
            done.set()
        watcher.join(timeout=ACK_THRESHOLD_SECONDS + 2)

        if reply is None:
            reply = Reply(text="Done.")

        if "msg" in ack_holder:
            try:
                self.client.edit_message(chat_id, ack_holder["msg"].message_id, reply.text, reply.keyboard)
            except Exception:
                logger.exception("failed to edit ack message with final reply")
        else:
            self._send_reply(chat_id, reply)

    def _send_reply(self, chat_id: int, reply: Reply) -> None:
        try:
            if reply.document is not None:
                filename, content = reply.document
                self.client.send_document(chat_id, filename, content, caption=reply.text)
            else:
                self.client.send_message(chat_id, reply.text, reply.keyboard)
        except Exception:
            logger.exception("failed to send reply")

    def _reply(self, chat_id: int, text: str) -> None:
        try:
            self.client.send_message(chat_id, text)
        except Exception:
            logger.exception("failed to send short-circuit reply")

    # ---------------------------------------------------------------- #
    # Callback queries (inline button taps)
    # ---------------------------------------------------------------- #

    def _handle_callback(self, callback_query: dict) -> None:
        cq_id = callback_query["id"]
        data = callback_query.get("data", "")
        from_user = callback_query["from"]
        user_id = from_user["id"]
        username = from_user.get("username")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        now = datetime.now(timezone.utc)

        with self._handler_lock:
            user = self._get_user(user_id, chat_id if chat_id is not None else user_id, username)

            if not self.rate_limiter.allow(user_id, now):
                self._answer_callback(cq_id, "Slow down a moment.", False)
                return

            prefix, _, arg = data.partition(":")
            handler = self.callback_handlers.get(prefix)
            if handler is None:
                self._answer_callback(cq_id, "That button doesn't do anything anymore.", True)
                return

            ctx = CallbackContext(
                client=self.client, users_conn=self.users_conn, journal_conn=self.journal_conn,
                user=user, chat_id=chat_id, message_id=message_id, arg=arg, now=now, app=self.app,
            )
            try:
                result = handler(ctx)
            except Exception:
                error_id = uuid.uuid4().hex[:8]
                logger.exception("callback error ref=%s", error_id)
                result = CallbackReply(toast=f"Something broke (ref {error_id}).", show_alert=True)

        self._answer_callback(cq_id, result.toast, result.show_alert)
        if result.edit_text is not None and chat_id is not None and message_id is not None:
            try:
                self.client.edit_message(chat_id, message_id, result.edit_text, result.edit_keyboard)
            except Exception:
                logger.exception("failed to edit message after callback")
        if result.send_text is not None and chat_id is not None:
            self._reply(chat_id, result.send_text)

    def _answer_callback(self, cq_id: str, text: str | None, show_alert: bool) -> None:
        try:
            self.client.answer_callback_query(cq_id, text, show_alert)
        except Exception:
            logger.exception("failed to answer callback query")
