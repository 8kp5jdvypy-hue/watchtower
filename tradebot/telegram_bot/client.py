"""Thin Telegram Bot API client for the command layer. Shares the same
retry discipline as tradebot.alerts.TelegramAlerter (honor 429's
retry_after, back off on transient network errors, never crash the
dispatcher on a single blip) but exposes the calls the command layer
needs: editing a message (for the ack-then-edit SLA), answering callback
queries (button taps), long-polling updates, and sending documents
(CSV export).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

API_ROOT = "https://api.telegram.org/bot{token}/{method}"


class TelegramCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class SentMessage:
    chat_id: int
    message_id: int


class BotClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise TelegramCredentialsError("TELEGRAM_BOT_TOKEN is not set.")
        self.token = token

    def _url(self, method: str) -> str:
        return API_ROOT.format(token=self.token, method=method)

    def _call(self, method: str, payload: dict, max_retries: int = 5, base_delay: float = 1.0) -> dict:
        """POSTs to the Bot API. Retries on 429 (honoring Telegram's own
        retry_after) and on transient network failures. Raises on any
        other non-2xx response or once retries are exhausted — callers
        (the dispatcher's per-handler wrapper) are responsible for turning
        that into a logged error rather than crashing the poll loop."""
        for attempt in range(max_retries):
            try:
                resp = requests.post(self._url(method), json=payload, timeout=10)
            except requests.exceptions.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(base_delay * (2**attempt))
                continue

            if resp.status_code != 429 or attempt == max_retries - 1:
                resp.raise_for_status()
                return resp.json()["result"]
            delay = base_delay * (2**attempt)
            try:
                delay = resp.json().get("parameters", {}).get("retry_after", delay)
            except ValueError:
                pass
            time.sleep(delay)
        raise RuntimeError(f"unreachable: {method} exhausted retries without returning or raising")

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> SentMessage:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        return SentMessage(chat_id=result["chat"]["id"], message_id=result["message_id"])

    def edit_message(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None, show_alert: bool = False) -> None:
        payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text is not None:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str | None = None) -> SentMessage:
        data = {"chat_id": chat_id}
        if caption is not None:
            data["caption"] = caption
        files = {"document": (filename, content)}
        resp = requests.post(self._url("sendDocument"), data=data, files=files, timeout=30)
        resp.raise_for_status()
        result = resp.json()["result"]
        return SentMessage(chat_id=result["chat"]["id"], message_id=result["message_id"])

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(self._url("getUpdates"), params=params, timeout=timeout + 10)
        resp.raise_for_status()
        return resp.json()["result"]

    def get_my_commands(self) -> list[dict]:
        resp = requests.get(self._url("getMyCommands"), timeout=10)
        resp.raise_for_status()
        return resp.json()["result"]
