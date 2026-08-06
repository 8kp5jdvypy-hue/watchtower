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

# channel_post/edited_channel_post: the bot posting alerts into a channel
# where it's admin also means people can type commands INTO that channel —
# those arrive as this update type, not `message`, and carry no `from`
# field (no user identity). Telegram's default (when allowed_updates is
# omitted) already includes channel_post, so leaving this off was never
# actually why they were dropped — they were dropped because
# _handle_update() below didn't look for the key at all. Being explicit
# here is still worth doing: it survives a future Telegram default change,
# and it's the config this module now logs on startup so that's visible.
ALLOWED_UPDATES = ["message", "callback_query", "channel_post", "edited_channel_post"]

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
        logger.info("polling with allowed_updates=%s", ALLOWED_UPDATES)
        while True:
            try:
                updates = self.client.get_updates(offset=self._offset, timeout=30, allowed_updates=ALLOWED_UPDATES)
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

    def _is_allowed_user(self, user_id: int) -> bool:
        """ALLOWED_USER_IDS is opt-in: unset/empty means unrestricted
        (matches the existing ADMIN_TELEGRAM_IDS precedent) — set it to
        lock the bot down to specific Telegram user IDs only."""
        return not self.app.allowed_user_ids or user_id in self.app.allowed_user_ids

    @staticmethod
    def _extract_identity(payload: dict) -> tuple:
        """(chat_id, user_id_or_None, is_channel) — the one place that
        reads a `chat`/`from` pair, so every caller handles a missing
        `from` (channel posts have none) the same way instead of each
        re-deriving it and risking a KeyError."""
        chat = payload["chat"]
        chat_id = chat["id"]
        is_channel = chat.get("type") == "channel"
        from_user = payload.get("from")
        user_id = from_user["id"] if from_user else None
        return chat_id, user_id, is_channel

    @staticmethod
    def _describe_update(update: dict) -> tuple:
        for key in ("message", "edited_message", "channel_post", "edited_channel_post", "callback_query"):
            if key in update:
                payload = update[key]
                chat = (payload.get("message") or {}).get("chat") if key == "callback_query" else payload.get("chat")
                return key, (chat or {}).get("type")
        return "unknown", None

    def _safe_handle_update(self, update: dict) -> None:
        try:
            self._handle_update(update)
        except Exception:
            logger.exception("unhandled error routing update: %r", update)

    def _handle_update(self, update: dict) -> None:
        update_type, chat_type = self._describe_update(update)
        logger.debug("inbound update: type=%s chat_type=%s", update_type, chat_type)

        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "channel_post" in update:
            self._handle_channel_post(update["channel_post"])
        elif "edited_channel_post" in update:
            self._handle_channel_post(update["edited_channel_post"])
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

        if not self._is_allowed_user(user_id):
            self._reply(chat_id, "This bot is invite-only right now — your account isn't on the allowed list.")
            return

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

    # ---------------------------------------------------------------- #
    # Channel posts — no `from` field, so no user identity at all. Off
    # by default (channel_commands_enabled=False); when on, only the
    # read-only CHANNEL_ALLOWED commands are reachable, and no per-user
    # DB row is ever created for a channel post — see context.HandlerContext
    # .user, which is Optional exactly for this case.
    # ---------------------------------------------------------------- #

    def _handle_channel_post(self, post: dict) -> None:
        chat_id, user_id, _is_channel = self._extract_identity(post)
        text = post.get("text", "").strip()

        if not self.app.channel_commands_enabled:
            return  # feature is off — exactly today's (silent) behavior

        if not text.startswith("/"):
            return  # not a command attempt — most channel posts are the bot's own alerts

        command, args = self._parse_command(text)

        if command not in commands.COMMAND_NAMES:
            return  # avoid replying to every non-command post in an active channel

        if command not in commands.CHANNEL_ALLOWED:
            self._reply(chat_id, f"/{command} isn't available in a channel post (no user identity to scope it to) — DM me instead.")
            return

        now = datetime.now(timezone.utc)
        # No per-user identity to rate-limit on — use the chat itself as the key.
        if not self.rate_limiter.allow(chat_id, now):
            return

        handler = self.handlers[command]
        with self._handler_lock:
            ctx = HandlerContext(
                client=self.client, users_conn=self.users_conn, journal_conn=self.journal_conn,
                user=None, chat_id=chat_id, chat_type="channel", args=args, now=now, app=self.app,
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

        if not self._is_allowed_user(user_id):
            self._answer_callback(cq_id, "This bot is invite-only right now.", True)
            return

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
            try:
                self.client.send_message(chat_id, result.send_text, result.send_keyboard)
            except Exception:
                logger.exception("failed to send follow-up message after callback")

    def _answer_callback(self, cq_id: str, text: str | None, show_alert: bool) -> None:
        try:
            self.client.answer_callback_query(cq_id, text, show_alert)
        except Exception:
            logger.exception("failed to answer callback query")
