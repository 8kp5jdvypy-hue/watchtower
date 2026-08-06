"""Tests for tradebot.telegram_bot.dispatcher — routing, gating, rate
limiting, the ack-then-edit SLA, and the never-die-silently exception
wrapper. Uses a FakeClient instead of a real Telegram connection.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from tradebot.journal import connect as journal_connect
from tradebot.telegram_bot import commands, db, dispatcher as dispatcher_mod
from tradebot.telegram_bot.context import AppConfig, CallbackReply, Reply
from tradebot.telegram_bot.dispatcher import Dispatcher


@dataclass
class _SentMessage:
    chat_id: int
    message_id: int


class FakeClient:
    def __init__(self, commands_reply=None):
        self.sent = []
        self.edited = []
        self.answered = []
        self._next_id = 1000
        self._commands_reply = commands_reply

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        self._next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        return _SentMessage(chat_id=chat_id, message_id=self._next_id)

    def edit_message(self, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        self.edited.append((chat_id, message_id, text, reply_markup))

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answered.append((callback_query_id, text, show_alert))

    def send_document(self, chat_id, filename, content, caption=None):
        self._next_id += 1
        self.sent.append((chat_id, f"[doc:{filename}]", None))
        return _SentMessage(chat_id=chat_id, message_id=self._next_id)

    def get_my_commands(self):
        if self._commands_reply is not None:
            return self._commands_reply
        return [{"command": c, "description": d} for c, d in commands.COMMANDS]

    def get_updates(self, offset=None, timeout=30, allowed_updates=None):
        return []

    def get_me(self):
        return {"username": "TestBot"}

    def get_webhook_info(self):
        return {"url": "", "pending_update_count": 0}


def _app_config(channel_commands_enabled=False, allowed_user_ids=None):
    return AppConfig(
        admin_ids=frozenset(),
        default_watchlist=["TSLA"],
        stripe_portal_url=None,
        plans=[],
        support_contact="@support",
        market_is_open_fn=lambda now: True,
        session_date_fn=lambda now: now.date(),
        halt_file=__import__("pathlib").Path("/tmp/does_not_exist_HALT"),
        heartbeat_file=__import__("pathlib").Path("/tmp/does_not_exist_heartbeat.json"),
        channel_commands_enabled=channel_commands_enabled,
        allowed_user_ids=allowed_user_ids,
    )


def _build(client=None, handlers=None, callback_handlers=None, onboarding_text_handlers=None, rate_limiter=None,
           channel_commands_enabled=False, allowed_user_ids=None):
    client = client or FakeClient()
    users_conn = db.connect(":memory:")
    journal_conn = journal_connect(":memory:")
    app_config = _app_config(channel_commands_enabled=channel_commands_enabled, allowed_user_ids=allowed_user_ids)
    d = Dispatcher(
        client=client, users_conn=users_conn, journal_conn=journal_conn, app_config=app_config,
        handlers=handlers or {}, callback_handlers=callback_handlers or {},
        onboarding_text_handlers=onboarding_text_handlers or {}, rate_limiter=rate_limiter,
    )
    return d, client


def _message_update(text, user_id=1, chat_id=1, chat_type="private", username="alice"):
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "username": username},
            "text": text,
        },
    }


def _callback_update(data, user_id=1, chat_id=1, message_id=42, username="alice"):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cq1",
            "data": data,
            "from": {"id": user_id, "username": username},
            "message": {"chat": {"id": chat_id, "type": "private"}, "message_id": message_id},
        },
    }


def _channel_post_update(text, chat_id=-1001234567890, edited=False):
    # Real Telegram channel_post/edited_channel_post payloads carry NO
    # `from` field at all — this is the shape that broke the old dispatcher.
    key = "edited_channel_post" if edited else "channel_post"
    return {
        "update_id": 1,
        key: {"chat": {"id": chat_id, "type": "channel"}, "text": text},
    }


# ---------------------------------------------------------------------- #
# Startup check
# ---------------------------------------------------------------------- #


class _StopTest(BaseException):
    """Deliberately not an Exception subclass, so it escapes run_forever's
    `except Exception` handler and lets the test break out of the
    otherwise-infinite polling loop."""


def test_run_forever_survives_a_get_updates_failure_instead_of_dying(monkeypatch):
    # Regression test: a transient get_updates() failure (network blip, or
    # a 409 from Telegram when something else briefly polls the same bot
    # token) used to propagate straight out of run_forever() and kill the
    # whole dispatcher process.
    monkeypatch.setattr(dispatcher_mod.time, "sleep", lambda s: None)

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def get_updates(self, offset=None, timeout=30, allowed_updates=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated 409 conflict")
            raise _StopTest()

    client = FlakyClient()
    d, _ = _build(client=client)
    with pytest.raises(_StopTest):
        d.run_forever()
    assert client.calls == 2  # first failure was caught and retried, not fatal


def test_startup_check_passes_when_registry_matches_botfather():
    d, client = _build(client=FakeClient())
    d.startup_check()  # must not raise


def test_startup_check_fails_loudly_on_drift():
    drifted = [{"command": c, "description": desc} for c, desc in commands.COMMANDS[:-1]]  # drop /halt
    d, client = _build(client=FakeClient(commands_reply=drifted))
    with pytest.raises(commands.CommandDriftError):
        d.startup_check()


# ---------------------------------------------------------------------- #
# Unknown command
# ---------------------------------------------------------------------- #


def test_unknown_command_points_to_help():
    d, client = _build(handlers={"help": lambda ctx: Reply(text="help text")})
    d.process_updates_once([_message_update("/frobnicate")])
    assert len(client.sent) == 1
    chat_id, text, _ = client.sent[0]
    assert "/help" in text
    assert "frobnicate" in text


# ---------------------------------------------------------------------- #
# Group vs DM gating
# ---------------------------------------------------------------------- #


def test_group_chat_blocks_a_personal_command():
    d, client = _build(handlers={"took": lambda ctx: Reply(text="logged")})
    d.process_updates_once([_message_update("/took abc123", chat_type="group")])
    assert len(client.sent) == 1
    assert "DM me" in client.sent[0][1]


@pytest.mark.parametrize("allowed_command", sorted(commands.GROUP_ALLOWED))
def test_group_chat_allows_the_group_allowed_commands(allowed_command):
    d, client = _build(handlers={allowed_command: lambda ctx: Reply(text="ok-from-group")})
    d.process_updates_once([_message_update(f"/{allowed_command}", chat_type="group")])
    assert len(client.sent) == 1
    assert client.sent[0][1] == "ok-from-group"


def test_private_chat_allows_every_command():
    d, client = _build(handlers={"took": lambda ctx: Reply(text="logged")})
    d.process_updates_once([_message_update("/took abc123", chat_type="private")])
    assert client.sent == [(1, "logged", None)]


# ---------------------------------------------------------------------- #
# Rate limiting
# ---------------------------------------------------------------------- #


def test_rate_limit_blocks_after_max_per_window():
    limiter = dispatcher_mod.RateLimiter(max_per_window=2, window_seconds=60)
    d, client = _build(handlers={"help": lambda ctx: Reply(text="help")}, rate_limiter=limiter)

    d.process_updates_once([_message_update("/help")])
    d.process_updates_once([_message_update("/help")])
    d.process_updates_once([_message_update("/help")])  # 3rd within the window — blocked

    assert len(client.sent) == 3
    assert client.sent[0][1] == "help text" or client.sent[0][1] == "help"
    assert "limit" in client.sent[2][1].lower()


# ---------------------------------------------------------------------- #
# Ack-then-edit SLA
# ---------------------------------------------------------------------- #


def test_fast_handler_sends_directly_no_ack():
    d, client = _build(handlers={"help": lambda ctx: Reply(text="quick")})
    d.process_updates_once([_message_update("/help")])
    assert client.sent == [(1, "quick", None)]
    assert client.edited == []


def test_slow_handler_gets_an_ack_then_an_edit(monkeypatch):
    monkeypatch.setattr(dispatcher_mod, "ACK_THRESHOLD_SECONDS", 0.05)

    def slow_handler(ctx):
        time.sleep(0.2)
        return Reply(text="finally done")

    d, client = _build(handlers={"help": slow_handler})
    d.process_updates_once([_message_update("/help")])

    assert client.sent == [(1, dispatcher_mod.ACK_TEXT, None)]
    assert len(client.edited) == 1
    chat_id, message_id, text, _ = client.edited[0]
    assert text == "finally done"


# ---------------------------------------------------------------------- #
# Exceptions never die silently
# ---------------------------------------------------------------------- #


def test_handler_exception_replies_with_an_error_id_not_a_crash():
    def broken_handler(ctx):
        raise ValueError("boom")

    d, client = _build(handlers={"help": broken_handler})
    d.process_updates_once([_message_update("/help")])  # must not raise

    assert len(client.sent) == 1
    text = client.sent[0][1]
    assert "ref" in text.lower()


def test_callback_exception_answers_with_an_error_id():
    def broken_callback(ctx):
        raise ValueError("boom")

    d, client = _build(callback_handlers={"took": broken_callback})
    d.process_updates_once([_callback_update("took:abc123")])

    assert len(client.answered) == 1
    cq_id, text, show_alert = client.answered[0]
    assert "ref" in text.lower()
    assert show_alert is True


def test_unknown_callback_prefix_answers_gracefully():
    d, client = _build(callback_handlers={})
    d.process_updates_once([_callback_update("mystery:xyz")])
    assert len(client.answered) == 1
    assert client.answered[0][2] is True  # show_alert


def test_callback_edits_the_message_when_the_handler_asks_to():
    def edits(ctx):
        return CallbackReply(toast="ok", edit_text="new text", edit_keyboard=None)

    d, client = _build(callback_handlers={"ack_risk": edits})
    d.process_updates_once([_callback_update("ack_risk:")])
    assert client.edited == [(1, 42, "new text", None)]


def test_callback_send_text_carries_its_keyboard():
    """The mood prompt after /took (see tradebot.telegram_bot.callbacks)
    is a follow-up message with its own keyboard, not an edit to the
    original alert — send_keyboard must actually reach send_message."""
    keyboard = {"inline_keyboard": [[{"text": "Calm", "callback_data": "mood:t1:calm"}]]}

    def sends_a_followup(ctx):
        return CallbackReply(toast="ok", send_text="How were you feeling?", send_keyboard=keyboard)

    d, client = _build(callback_handlers={"took": sends_a_followup})
    d.process_updates_once([_callback_update("took:abc123")])
    assert client.sent == [(1, "How were you feeling?", keyboard)]


# ---------------------------------------------------------------------- #
# Channel posts — no `from` field at all. This is the exact bug report:
# commands typed into the channel got no response.
# ---------------------------------------------------------------------- #


def test_channel_post_is_ignored_by_default():
    # channel_commands_enabled defaults to False — a fresh deploy behaves
    # exactly like before this fix: silent, no crash, no reply.
    calls = []
    d, client = _build(handlers={"status": lambda ctx: calls.append(ctx) or Reply(text="ok")})
    d.process_updates_once([_channel_post_update("/status")])
    assert calls == []
    assert client.sent == []


def test_channel_post_status_with_no_from_user_does_not_raise_and_gets_a_reply():
    # The literal regression case from the bug report: a /status channel_post
    # with no from_user must be handled without raising anywhere in the stack.
    d, client = _build(handlers={"status": lambda ctx: Reply(text="all good")}, channel_commands_enabled=True)
    d.process_updates_once([_channel_post_update("/status")])  # must not raise
    assert client.sent == [(-1001234567890, "all good", None)]


def test_channel_post_handler_receives_user_none():
    seen = {}

    def capture(ctx):
        seen["user"] = ctx.user
        seen["chat_type"] = ctx.chat_type
        return Reply(text="ok")

    d, client = _build(handlers={"status": capture}, channel_commands_enabled=True)
    d.process_updates_once([_channel_post_update("/status")])
    assert seen["user"] is None
    assert seen["chat_type"] == "channel"


def test_channel_post_blocks_a_mutating_command_even_when_enabled():
    calls = []
    d, client = _build(handlers={"took": lambda ctx: calls.append(ctx) or Reply(text="logged")}, channel_commands_enabled=True)
    d.process_updates_once([_channel_post_update("/took abc123")])
    assert calls == []
    assert len(client.sent) == 1
    assert "DM me" in client.sent[0][1]


def test_channel_post_allows_read_only_commands_when_enabled():
    for cmd in sorted(commands.CHANNEL_ALLOWED):
        d, client = _build(handlers={cmd: lambda ctx: Reply(text=f"ok-{ctx.chat_type}")}, channel_commands_enabled=True)
        d.process_updates_once([_channel_post_update(f"/{cmd}")])
        assert client.sent == [(-1001234567890, "ok-channel", None)], cmd


def test_edited_channel_post_is_routed_the_same_as_channel_post():
    d, client = _build(handlers={"status": lambda ctx: Reply(text="ok")}, channel_commands_enabled=True)
    d.process_updates_once([_channel_post_update("/status", edited=True)])
    assert client.sent == [(-1001234567890, "ok", None)]


def test_channel_post_unknown_command_stays_silent():
    d, client = _build(handlers={"status": lambda ctx: Reply(text="ok")}, channel_commands_enabled=True)
    d.process_updates_once([_channel_post_update("/frobnicate")])
    assert client.sent == []


def test_channel_post_non_command_text_is_ignored():
    d, client = _build(handlers={"status": lambda ctx: Reply(text="ok")}, channel_commands_enabled=True)
    d.process_updates_once([_channel_post_update("just posting an alert, not a command")])
    assert client.sent == []


# ---------------------------------------------------------------------- #
# ALLOWED_USER_IDS — opt-in allowlist for DM/group commands
# ---------------------------------------------------------------------- #


def test_allowed_user_ids_unset_means_unrestricted():
    d, client = _build(handlers={"help": lambda ctx: Reply(text="hi")}, allowed_user_ids=None)
    d.process_updates_once([_message_update("/help", user_id=12345, chat_id=12345)])
    assert client.sent == [(12345, "hi", None)]


def test_allowed_user_ids_rejects_a_user_not_on_the_list():
    d, client = _build(handlers={"help": lambda ctx: Reply(text="hi")}, allowed_user_ids=frozenset({1}))
    d.process_updates_once([_message_update("/help", user_id=999)])
    assert len(client.sent) == 1
    assert "invite-only" in client.sent[0][1].lower()


def test_allowed_user_ids_allows_a_listed_user():
    d, client = _build(handlers={"help": lambda ctx: Reply(text="hi")}, allowed_user_ids=frozenset({999}))
    d.process_updates_once([_message_update("/help", user_id=999, chat_id=999)])
    assert client.sent == [(999, "hi", None)]


def test_allowed_user_ids_rejects_callback_queries_too():
    d, client = _build(callback_handlers={"took": lambda ctx: None}, allowed_user_ids=frozenset({1}))
    d.process_updates_once([_callback_update("took:abc", user_id=999)])
    assert len(client.answered) == 1
    assert client.answered[0][2] is True  # show_alert
    assert "invite-only" in client.answered[0][1].lower()
