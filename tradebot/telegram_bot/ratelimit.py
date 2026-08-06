"""Per-user sliding-window rate limit: 20 commands/min, then a cooldown
notice instead of a handler run. In-memory only — this is a single
dispatcher process, so there's nothing to persist across restarts, and a
restart clearing everyone's counter is a fine failure mode.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta


class RateLimiter:
    def __init__(self, max_per_window: int = 20, window_seconds: float = 60.0) -> None:
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self._hits: dict[int, deque] = defaultdict(deque)

    def _prune(self, user_id: int, now: datetime) -> deque:
        hits = self._hits[user_id]
        cutoff = now - timedelta(seconds=self.window_seconds)
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def allow(self, user_id: int, now: datetime) -> bool:
        """Records the attempt as a hit only if it's allowed — a blocked
        command shouldn't itself extend the cooldown further."""
        hits = self._prune(user_id, now)
        if len(hits) >= self.max_per_window:
            return False
        hits.append(now)
        return True

    def retry_after_seconds(self, user_id: int, now: datetime) -> float:
        hits = self._prune(user_id, now)
        if not hits:
            return 0.0
        return max(0.0, self.window_seconds - (now - hits[0]).total_seconds())
