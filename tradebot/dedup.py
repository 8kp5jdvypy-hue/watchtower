"""Cross-time duplicate detection: recognizes a second cluster on the
same symbol shortly after a prior one as a continuation of the same
developing event, not a brand-new one. Deliberately narrow — one
rolling-window DB lookup, not a full state machine. See tradebot.events
for the same "DB-reading predicate, not a pure detector" shape this
follows: it answers exactly one question for runner.py — "is this
cluster a fresh watch, or a confirmed repeat of something recent?"

Today's same-bar clustering (detectors.score_cluster) already merges
every detector kind firing on the SAME bar into one cluster. This module
covers the gap that leaves open: a DIFFERENT detector kind (or the same
kind, re-triggered after retreating and crossing again) firing on the
same symbol a few bars later still produced a brand-new, separately
alerted cluster. Only HIGH/MEDIUM-tier clusters anchor a window — LOG
tier is sub-threshold noise that shouldn't suppress anything."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

# Placeholders — need a replay-based frequency analysis (how often does a
# *different* detector kind fire on the same symbol within 15/30/60min of
# a prior HIGH/MEDIUM cluster, and at what score gap does a second firing
# look like real escalation vs. noise) before trusting these as tuned
# values. See the implementation plan's "open decisions" section.
DEDUP_WINDOW_MINUTES = 30
ESCALATION_SCORE_DELTA = 2.0


class LifecycleState(str, Enum):
    WATCH = "watch"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class DedupResult:
    lifecycle_state: LifecycleState
    related_detection_id: str | None
    is_escalation: bool  # only meaningful when lifecycle_state == CONFIRMED


def find_recent_anchor(
    conn, symbol: str, before: datetime, window_minutes: int = DEDUP_WINDOW_MINUTES
) -> tuple[str, float] | None:
    """Most recent HIGH/MEDIUM-tier cluster for `symbol` strictly before
    `before`, within window_minutes. Returns (id, score) or None."""
    window_start = (before - timedelta(minutes=window_minutes)).isoformat()
    row = conn.execute(
        "SELECT id, score FROM detections WHERE symbol = ? AND ts_utc < ? AND ts_utc >= ? "
        "AND tier IN ('high', 'medium') ORDER BY ts_utc DESC LIMIT 1",
        (symbol, before.isoformat(), window_start),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def evaluate_dedup(
    conn,
    symbol: str,
    ts: datetime,
    score: float,
    window_minutes: int = DEDUP_WINDOW_MINUTES,
    escalation_delta: float = ESCALATION_SCORE_DELTA,
) -> DedupResult:
    """WATCH if there's no recent anchor — the normal case, behaves
    exactly as before this module existed. CONFIRMED if there is one:
    is_escalation says whether this new cluster's score materially beats
    the anchor's (a real escalation, worth its own alert) or is just a
    repeat (worth suppressing as a duplicate — see runner.py's
    SUPPRESS_DUPLICATE wiring)."""
    anchor = find_recent_anchor(conn, symbol, ts, window_minutes)
    if anchor is None:
        return DedupResult(LifecycleState.WATCH, None, False)
    anchor_id, anchor_score = anchor
    return DedupResult(LifecycleState.CONFIRMED, anchor_id, score >= anchor_score + escalation_delta)
