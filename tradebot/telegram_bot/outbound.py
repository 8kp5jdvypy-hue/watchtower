"""A single Telegram sendMessage attempt, classified into an outcome the
worker can act on — never retries internally (unlike client.BotClient,
which loops until success or exhaustion). The worker owns all retry
scheduling itself (via the outbox's next_attempt_at), because a
synchronous retry loop here would block the worker from getting to the
NEXT, possibly higher-priority, ready row while this one sleeps.

API_ROOT is overridable via the TELEGRAM_API_ROOT env var so tests (and
the chaos/load tests specifically) can point a real subprocess worker at
a local fake server on loopback instead of the real Telegram API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

import requests

DEFAULT_API_ROOT = "https://api.telegram.org"


def _api_root() -> str:
    return os.environ.get("TELEGRAM_API_ROOT", DEFAULT_API_ROOT)


class SendOutcome(Enum):
    DELIVERED = "delivered"
    RATE_LIMITED = "rate_limited"       # 429 — retry_after is exact, from Telegram itself
    UNREACHABLE = "unreachable"          # 403 Forbidden, or 400 "chat not found" — terminal, auto-unsubscribe
    RETRYABLE_ERROR = "retryable_error"  # 5xx or a network-level failure — backoff+jitter
    PERMANENT_ERROR = "permanent_error"  # any other 4xx — our own bug, not worth retrying blindly


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    retry_after: float | None = None   # set only for RATE_LIMITED
    error: str | None = None           # set for anything but DELIVERED
    message_id: int | None = None      # set only for DELIVERED


_UNREACHABLE_MARKERS = ("bot was blocked by the user", "user is deactivated", "chat not found", "kicked")


def _classify_error_description(status_code: int, description: str) -> SendOutcome:
    lowered = description.lower()
    if status_code == 403 or any(marker in lowered for marker in _UNREACHABLE_MARKERS):
        return SendOutcome.UNREACHABLE
    if status_code >= 500:
        return SendOutcome.RETRYABLE_ERROR
    return SendOutcome.PERMANENT_ERROR


def send_once(token: str, chat_id: int, text: str, reply_markup: dict | None = None, timeout: float = 10.0) -> SendResult:
    """Exactly one HTTP attempt. Network-level failures (timeout,
    connection error, DNS) are RETRYABLE_ERROR — indistinguishable from a
    5xx in terms of what the worker should do about them."""
    url = f"{_api_root()}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return SendResult(outcome=SendOutcome.RETRYABLE_ERROR, error=f"network error: {exc}")

    try:
        body = resp.json()
    except ValueError:
        # A non-JSON response (e.g. an upstream proxy's HTML error page)
        # on a 5xx is still just "the server is unwell" — retryable. On
        # anything else it's unexpected enough to treat as permanent
        # rather than retry forever against a response we can't parse.
        outcome = SendOutcome.RETRYABLE_ERROR if resp.status_code >= 500 else SendOutcome.PERMANENT_ERROR
        return SendResult(outcome=outcome, error=f"non-JSON response, status={resp.status_code}")

    if resp.status_code == 200 and body.get("ok"):
        return SendResult(outcome=SendOutcome.DELIVERED, message_id=body["result"]["message_id"])

    description = body.get("description", f"HTTP {resp.status_code}")
    if resp.status_code == 429:
        retry_after = body.get("parameters", {}).get("retry_after", 1)
        return SendResult(outcome=SendOutcome.RATE_LIMITED, retry_after=float(retry_after), error=description)

    outcome = _classify_error_description(resp.status_code, description)
    return SendResult(outcome=outcome, error=description)
