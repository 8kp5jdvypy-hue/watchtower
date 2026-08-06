"""Alert delivery and budgeting.

Message rendering lives in tradebot.formatting (templates.py + fields.py)
— this module only decides WHETHER and WHEN to send (AlertBudget) and
HOW to deliver (ConsoleAlerter / TelegramAlerter). See CLAUDE.md: live
alerting is opt-in, default log-only.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

import requests


@dataclass(frozen=True)
class Cluster:
    """A journaled detection cluster, as needed to render an alert. Field
    names mirror the journal's detections table, plus primary_headline —
    the highest-scoring constituent detection's own headline, used as the
    alert's one-sentence rationale (not the full semicolon-chained list)."""

    id: str
    ts_utc: str
    session: str
    symbol: str
    kinds: str
    headlines: str
    primary_headline: str
    score: float
    tier: str
    close: float
    atr14: float | None
    trend: str
    code_version: str


class ConsoleAlerter:
    """Default, log-only alerter — used whenever --live is absent."""

    def send(self, text: str) -> None:
        print(f"\n--- ALERT (console, not sent) ---\n{text}\n")


class TelegramCredentialsError(RuntimeError):
    pass


class TelegramAlerter:
    """Pushes to Telegram. Only used with --live."""

    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not self.token or not self.chat_id:
            raise TelegramCredentialsError(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set in the environment. "
                "Set them before running with --live."
            )

    def send(self, text: str, max_retries: int = 5, base_delay: float = 1.0) -> None:
        """Retries on both a 429 (rate limit — honors Telegram's own
        retry_after hint) and transient network failures (timeouts,
        connection errors). A single rate limit or network blip must
        never crash the runner mid-session — see CLAUDE.md: per-iteration
        exceptions get logged and the loop continues, but that's only a
        safety net; routine transient failures shouldn't need it."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, json=payload, timeout=10)
            except requests.exceptions.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(base_delay * (2**attempt))
                continue

            if resp.status_code != 429 or attempt == max_retries - 1:
                resp.raise_for_status()
                return
            delay = base_delay * (2**attempt)
            try:
                delay = resp.json().get("parameters", {}).get("retry_after", delay)
            except ValueError:
                pass
            time.sleep(delay)


class Decision(str, Enum):
    SEND = "send"
    CAP_REACHED_NOTICE = "daily_cap_reached_notice"
    SUPPRESS_CAP = "daily_cap_reached"
    SUPPRESS_COOLDOWN = "cooldown_active"
    QUEUED_FOR_DIGEST = "queued_for_hourly_digest"
    QUEUED_FOR_EOD = "queued_for_eod_summary"


@dataclass
class AlertBudget:
    """Decides whether a cluster is pushed immediately, batched, or
    suppressed. Takes an injectable clock (`now`) so tests never need real
    time or sleeps.

    - HIGH tier: pushed immediately, subject to a daily cap and a
      per-(symbol, detector_kind) cooldown.
    - MEDIUM tier: batched into one digest per hour.
    - LOG tier: batched into one end-of-day summary only.
    """

    now: Callable[[], datetime]
    max_high_per_day: int = 8
    cooldown_minutes: int = 45

    _current_day: object = field(default=None, init=False, repr=False)
    _high_sent_today: list = field(default_factory=list, init=False, repr=False)
    _last_sent_by_key: dict = field(default_factory=dict, init=False, repr=False)
    _cap_notice_sent: bool = field(default=False, init=False, repr=False)
    _medium_queue: list = field(default_factory=list, init=False, repr=False)
    _log_queue: list = field(default_factory=list, init=False, repr=False)
    _last_digest_hour_slot: object = field(default=None, init=False, repr=False)

    def _reset_if_new_day(self) -> None:
        now = self.now()
        today = now.date()
        if self._current_day != today:
            self._current_day = today
            self._high_sent_today.clear()
            self._last_sent_by_key.clear()
            self._cap_notice_sent = False
            self._medium_queue.clear()
            self._log_queue.clear()
            # Start on the current hour slot, not None — otherwise the
            # first pop_medium_digest_if_due() call would see "no slot
            # recorded yet" as a fresh hour and release immediately, even
            # if nothing has actually crossed an hour boundary.
            self._last_digest_hour_slot = now.replace(minute=0, second=0, microsecond=0)

    def evaluate(self, cluster: Cluster) -> Decision:
        """Decide what happens to this cluster. Callers should record the
        Decision's value as suppress_reason in the journal whenever it
        isn't Decision.SEND."""
        self._reset_if_new_day()

        if cluster.tier == "log":
            self._log_queue.append(cluster)
            return Decision.QUEUED_FOR_EOD

        if cluster.tier == "medium":
            self._medium_queue.append(cluster)
            return Decision.QUEUED_FOR_DIGEST

        # tier == "high"
        if len(self._high_sent_today) >= self.max_high_per_day:
            if not self._cap_notice_sent:
                self._cap_notice_sent = True
                return Decision.CAP_REACHED_NOTICE
            return Decision.SUPPRESS_CAP

        now = self.now()
        kinds = cluster.kinds.split(",")
        for kind in kinds:
            last_sent = self._last_sent_by_key.get((cluster.symbol, kind))
            if last_sent is not None and (now - last_sent) < timedelta(minutes=self.cooldown_minutes):
                return Decision.SUPPRESS_COOLDOWN

        self._high_sent_today.append(now)
        for kind in kinds:
            self._last_sent_by_key[(cluster.symbol, kind)] = now
        return Decision.SEND

    def pop_medium_digest_if_due(self) -> list[Cluster] | None:
        """Returns the accumulated medium-tier queue once per clock-hour
        boundary, or None if not due yet / nothing queued."""
        self._reset_if_new_day()
        now = self.now()
        current_slot = now.replace(minute=0, second=0, microsecond=0)
        if self._last_digest_hour_slot == current_slot:
            return None
        self._last_digest_hour_slot = current_slot
        if not self._medium_queue:
            return None
        digest, self._medium_queue = self._medium_queue, []
        return digest

    def pop_log_summary(self) -> list[Cluster]:
        """Drains and returns the log-tier queue. Callers decide when it's
        end-of-day; this just hands back whatever accumulated."""
        summary, self._log_queue = self._log_queue, []
        return summary
