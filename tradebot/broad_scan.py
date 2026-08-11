"""Stage 1 of the two-stage market-wide scan: a cheap, coarse screen over
the FULL active universe (tradebot.universe), producing a short list of
candidates. Stage 2 — deep analysis — is the EXISTING detectors.py /
runner.py pipeline, unchanged, applied only to what this stage promotes.
Nothing here replaces or duplicates a real detector; this is the funnel
in front of it:

    universe (thousands)
        -> Stage 1 screen (this module, cheap ratios on daily-bar-level
           data, no ATR/anchors/options — that's Stage 2's job)
        -> promoted candidates (a short list)
        -> Stage 2: the real, unchanged detector suite, run only on those

Scoring here is deliberately NOT in ATR units and is NEVER shown to a
user or compared against a Stage 2 score — it exists purely to rank
"worth a real look" against other Stage 1 candidates in the same pass.
Reuses detectors.score_cluster's combination shape (strongest signal
plus partial credit for corroborating ones) for consistency with how the
rest of this project scores multiple simultaneous signals, not because
the two scores are on the same scale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from tradebot import metrics

# Cheap-screen thresholds — NOT the same thresholds as the real detectors
# (tradebot.detectors.TIER_HIGH/TIER_MEDIUM, rvol_spike's 3.0x, etc.),
# which operate on real intraday bars + ATR. These exist only to cut a
# universe of thousands down to a short list worth Stage 2's more
# expensive, real evaluation.
RVOL_THRESHOLD = 3.0  # matches detectors.rvol_spike's own spike_ratio default, for consistency
MOVE_PCT_THRESHOLD = 3.0
RANGE_PCT_THRESHOLD = 5.0
GAP_PCT_THRESHOLD = 3.0


@dataclass(frozen=True)
class Snapshot:
    """The cheapest useful per-symbol read for a Stage 1 pass — a single
    daily-bar-level snapshot, not an intraday series. `avg_volume` is a
    trailing historical average (e.g. 20-day), supplied by the caller;
    this module has no I/O of its own (same PURE-function discipline as
    tradebot.detectors — see CLAUDE.md)."""

    symbol: str
    open: float
    high: float
    low: float
    close: float
    prior_close: float
    volume: int
    avg_volume: float


@dataclass(frozen=True)
class CandidateScore:
    symbol: str
    score: float  # Stage 1 units only — never ATR, never shown to a user
    reasons: tuple[str, ...]  # which cheap checks fired, e.g. ("unusual_volume", "gap")


def screen_snapshot(snapshot: Snapshot) -> CandidateScore | None:
    """None if nothing cheap looks unusual — most of the universe, most
    of the time. Never fabricates a score for a symbol with no real
    baseline (avg_volume/prior_close <= 0)."""
    if snapshot.avg_volume <= 0 or snapshot.prior_close <= 0:
        return None

    ratios: dict[str, float] = {}

    rvol = snapshot.volume / snapshot.avg_volume
    if rvol >= RVOL_THRESHOLD:
        ratios["unusual_volume"] = rvol / RVOL_THRESHOLD

    move_pct = abs(snapshot.close - snapshot.prior_close) / snapshot.prior_close * 100
    if move_pct >= MOVE_PCT_THRESHOLD:
        ratios["price_acceleration"] = move_pct / MOVE_PCT_THRESHOLD

    range_pct = (snapshot.high - snapshot.low) / snapshot.prior_close * 100
    if range_pct >= RANGE_PCT_THRESHOLD:
        ratios["range_expansion"] = range_pct / RANGE_PCT_THRESHOLD

    gap_pct = abs(snapshot.open - snapshot.prior_close) / snapshot.prior_close * 100
    if gap_pct >= GAP_PCT_THRESHOLD:
        ratios["gap"] = gap_pct / GAP_PCT_THRESHOLD

    if not ratios:
        return None

    ordered = sorted(ratios.values(), reverse=True)
    score = ordered[0] + 0.25 * sum(ordered[1:])
    reasons = tuple(sorted(ratios, key=lambda k: ratios[k], reverse=True))
    return CandidateScore(symbol=snapshot.symbol, score=score, reasons=reasons)


def build_snapshots_from_daily_bars(bars_by_symbol: dict, min_history: int = 6) -> list[Snapshot]:
    """Turns bulk-fetched daily bars (see
    vendors.alpaca.fetch_daily_bars_bulk — one bulk call across the whole
    universe, not per symbol) into Stage 1 Snapshots. The LAST bar is
    "today" (open/high/low/close/volume so far); the bar before it is
    prior_close; avg_volume is the trailing mean of every OTHER cached
    bar (never including today's own still-forming volume in its own
    baseline). Skips a symbol with fewer than min_history bars rather
    than computing an average on too little history — same
    never-a-stat-on-too-little-data discipline as tradebot.journal."""
    snapshots = []
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < min_history:
            continue
        ordered = sorted(bars, key=lambda b: b.ts)
        last, prior = ordered[-1], ordered[-2]
        history = ordered[:-1]
        avg_volume = sum(b.volume for b in history) / len(history)
        snapshots.append(
            Snapshot(
                symbol=symbol, open=last.open, high=last.high, low=last.low, close=last.close,
                prior_close=prior.close, volume=last.volume, avg_volume=avg_volume,
            )
        )
    return snapshots


def promote_candidates(scores: Sequence[CandidateScore], threshold: float = 1.0) -> list[CandidateScore]:
    """Candidates worth Stage 2's real (expensive) evaluation — sorted
    strongest-first so a caller that caps how many it promotes (matching
    'higher coverage, same or better signal-to-noise,' not 'more
    alerts') keeps the best of them, not an arbitrary subset."""
    return sorted((c for c in scores if c.score >= threshold), key=lambda c: c.score, reverse=True)


def run_stage1_screen(
    snapshots: Sequence[Snapshot], threshold: float = 1.0, metrics_path=None,
) -> list[CandidateScore]:
    """The orchestration entrypoint: screen the whole batch, record real
    counters at every stage of the funnel (see module docstring's
    'thousands -> short list' shape), return the promoted candidates for
    Stage 2 to actually evaluate. Latency is accumulated as a running
    total (see metrics.increment's `amount`), not called once per
    symbol — 14,000 individual metric writes would itself be the hot-path
    cost this is supposed to avoid."""
    import time

    start = time.monotonic()
    metrics.increment("universe_symbols_monitored", path=metrics_path, amount=len(snapshots))

    scores = [c for c in (screen_snapshot(s) for s in snapshots) if c is not None]
    metrics.increment("universe_candidates_created", path=metrics_path, amount=len(scores))

    promoted = promote_candidates(scores, threshold=threshold)
    metrics.increment("universe_candidates_promoted", path=metrics_path, amount=len(promoted))

    elapsed_ms = (time.monotonic() - start) * 1000
    metrics.increment("universe_stage1_latency_ms_total", path=metrics_path, amount=int(elapsed_ms))
    metrics.increment("universe_stage1_runs", path=metrics_path)

    return promoted
