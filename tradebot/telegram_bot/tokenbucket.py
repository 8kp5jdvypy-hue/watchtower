"""Token-bucket rate limiting for outbound Telegram sends — see
tradebot.telegram_bot.worker, which keeps one global bucket (Telegram's
whole-bot cap) and one bucket per chat_id (Telegram's per-chat cap).

Injectable clock (`now_fn`) rather than a hard dependency on
time.monotonic — the load test drives thousands of simulated chats
through a fast-forwarding fake clock instead of burning real wall-clock
seconds waiting on refill.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TokenBucket:
    capacity: float
    refill_per_second: float
    now_fn: Callable[[], float] = field(default=time.monotonic)
    tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self._last_refill = self.now_fn()

    def _refill(self) -> None:
        now = self.now_fn()
        elapsed = max(0.0, now - self._last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self._last_refill = now

    def try_consume(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def seconds_until_available(self, n: float = 1.0) -> float:
        """0.0 if a call to try_consume(n) would succeed right now."""
        self._refill()
        if self.tokens >= n:
            return 0.0
        return (n - self.tokens) / self.refill_per_second

    def refund(self, n: float = 1.0) -> None:
        """Gives back a token that was consumed speculatively but never
        actually used — see worker.py's global-then-per-chat check order,
        where a per-chat miss refunds the global token it provisionally
        took. Never refills past capacity."""
        self.tokens = min(self.capacity, self.tokens + n)
