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
from datetime import date
from typing import Sequence
from zoneinfo import ZoneInfo

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

# Was build_snapshots_from_daily_bars' inline default; named so it can be
# recorded alongside the others in a screening tick's thresholds. Same
# value, same behavior — it is promoted to a constant only because
# "which history floor produced this INSUFFICIENT_HISTORY row" is a
# question the record has to be able to answer.
MIN_HISTORY_BARS = 6

# Bump when the MEANING of this screen changes: any threshold above, the
# score combination in screen_snapshot, MIN_HISTORY_BARS, or the
# promotion rule. Rows carrying different screen_versions are not
# comparable to each other, and a query that compares across sessions
# should filter on it.
#
# Deliberately hand-bumped rather than hashed from the constants: a hash
# can't be forgotten but also can't be reasoned about — two rows would
# differ with no way to tell whether the difference mattered. The safety
# net against a forgotten bump is that every tick records the thresholds
# it actually applied, so the ground truth is in the data either way.
SCREEN_VERSION = 2

ET = ZoneInfo("America/New_York")


def screen_thresholds() -> dict:
    """The constants this screen applies, as recorded on every tick.
    Read straight off the module — never a second copy that could drift
    from what screen_snapshot actually compares against."""
    return {
        "rvol": RVOL_THRESHOLD,
        "move_pct": MOVE_PCT_THRESHOLD,
        "range_pct": RANGE_PCT_THRESHOLD,
        "gap_pct": GAP_PCT_THRESHOLD,
        "min_history_bars": MIN_HISTORY_BARS,
    }


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


def build_snapshots_from_daily_bars(
    bars_by_symbol: dict,
    min_history: int = MIN_HISTORY_BARS,
    *,
    session_date: date | None = None,
) -> list[Snapshot]:
    """Turns bulk-fetched daily bars (see
    vendors.alpaca.fetch_daily_bars_bulk — one bulk call across the whole
    universe, not per symbol) into Stage 1 Snapshots. The LAST bar is
    "today" (open/high/low/close/volume so far); the bar before it is
    prior_close; avg_volume is the trailing mean of every OTHER cached
    bar (never including today's own still-forming volume in its own
    baseline). Skips a symbol with fewer than min_history bars rather
    than computing an average on too little history — same
    never-a-stat-on-too-little-data discipline as tradebot.journal.

    When session_date is supplied (the live runner always supplies it),
    a symbol is also skipped unless its newest daily bar belongs to that
    US-market session.  This prevents a premarket/prior-session daily bar
    from being treated as "today" and carried into intraday Stage 2.
    Keeping the argument optional preserves the pure helper's historical
    and ad-hoc use cases, where the caller may intentionally screen an
    older fixture without claiming it is live data."""
    snapshots = []
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < min_history:
            continue
        ordered = sorted(bars, key=lambda b: b.ts)
        if session_date is not None and ordered[-1].ts.astimezone(ET).date() != session_date:
            continue
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


# --------------------------------------------------------------------------
# Stage 1 observability — classifying what happened to each symbol.
#
# PURE (CLAUDE.md): data in, rows out. No I/O, no clock read, no globals.
# Persisting these is tradebot.universe.record_screening_tick's job; this
# only decides what the rows say.
#
# Every outcome below maps onto one specific, already-existing code path.
# Nothing here re-derives a ratio, re-runs a screen, or re-reads a vendor:
# each value is read off objects run_broad_scan already holds, which is
# what makes recording them incapable of changing what was selected.
# --------------------------------------------------------------------------

# A requested symbol the vendor response didn't contain at all.
OUTCOME_MISSING_FROM_FETCH = "MISSING_FROM_FETCH"
# Present, but fewer than MIN_HISTORY_BARS bars, so no Snapshot was built.
OUTCOME_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
# Present with enough history, but the newest daily bar is from a prior
# US-market session. It is never eligible for live Stage 2.
OUTCOME_STALE_SESSION_BAR = "STALE_SESSION_BAR"
# A real Snapshot whose baseline screen_snapshot refuses to score.
OUTCOME_INVALID_BASELINE = "INVALID_BASELINE"
# Scored, and nothing crossed a threshold. The ~99% case -- aggregated by
# default, written per-symbol only in verbose audit mode.
OUTCOME_QUIET = "QUIET"
# Cleared the screen but ranked below the promotion cap. The bucket that
# does not exist anywhere today, and the one a "why was this missed?"
# investigation most needs.
OUTCOME_CANDIDATE_NOT_PROMOTED = "CANDIDATE_NOT_PROMOTED"
# Cleared the screen and made the cut -- handed to Stage 2.
OUTCOME_PROMOTED = "PROMOTED"
# A symbol that was never requested but appeared anyway. Expected to be 0
# under the vendor's API contract; recorded rather than assumed.
OUTCOME_UNEXPECTED_FROM_FETCH = "UNEXPECTED_FROM_FETCH"


@dataclass(frozen=True)
class ScreeningOutcome:
    """One symbol's Stage 1 result for one tick.

    screen_score is named to prevent the single most likely misreading of
    this table: it is in Stage 1 units (see this module's docstring),
    NEVER comparable to detections.score, and never shown to a user."""

    symbol: str
    outcome: str
    screen_score: float | None = None
    rank: int | None = None  # 1-based position among candidates, None if unscored
    reasons: tuple[str, ...] = ()
    detail: dict | None = None


@dataclass(frozen=True)
class ScreeningTick:
    """The tick-level summary: the funnel counts, the thresholds applied,
    and whether the conservation invariant held.

    counts carries every bucket including quiet, so the aggregate case
    loses no information at the tick level -- with per-symbol QUIET rows
    switched off, "was X screened and quiet?" is answered by subtraction,
    and that subtraction is only valid because invariant_ok says so."""

    universe_count: int
    counts: dict
    thresholds: dict
    invariant_ok: bool
    promotion_limit: int


def _snapshot_detail(snapshot) -> dict:
    """The Snapshot's own fields, not derived ratios.

    Deliberately raw inputs: recording rvol/move_pct/etc. would mean
    recomputing what screen_snapshot computed internally and threw away,
    and a second implementation of a formula is a second thing that can
    disagree. From these five numbers a reader can derive any ratio
    exactly, and they cannot drift from what was actually screened."""
    return {
        "open": snapshot.open, "high": snapshot.high, "low": snapshot.low,
        "close": snapshot.close, "prior_close": snapshot.prior_close,
        "volume": snapshot.volume, "avg_volume": snapshot.avg_volume,
    }


def classify_screen_outcomes(
    symbols,
    bars_by_symbol: dict,
    snapshots,
    promoted,
    selected,
    promotion_limit: int,
    verbose_audit: bool = False,
    session_date: date | None = None,
) -> tuple[ScreeningTick, list[ScreeningOutcome]]:
    """What happened to every symbol in one Stage 1 pass.

    Mirrors runner._log_broad_scan_shadow_counts' bucket definitions
    exactly -- same set differences, same guard, same conservation
    equations -- but returns them per-symbol and as data instead of a log
    line. The two derive the same counts independently; a test asserts
    they agree, so a future edit to either that made them disagree fails
    rather than drifting silently.

    verbose_audit=False (the default, and what production runs) omits the
    per-symbol QUIET rows -- roughly 99% of the universe, ~185k rows a
    session -- while still counting them in the tick. True is for a
    bounded investigation window, not steady state.
    """
    requested = set(symbols)
    returned = set(bars_by_symbol)
    requested_fetched = requested & returned
    missing = requested - returned
    unexpected = returned - requested

    snapshot_by_symbol = {s.symbol: s for s in snapshots}
    requested_snapshots = {sym: s for sym, s in snapshot_by_symbol.items() if sym in requested}
    stale_session = {
        sym for sym in requested_fetched - set(requested_snapshots)
        if session_date is not None
        and len(bars_by_symbol[sym]) >= MIN_HISTORY_BARS
        and max(bars_by_symbol[sym], key=lambda b: b.ts).ts.astimezone(ET).date() != session_date
    }
    insufficient = requested_fetched - set(requested_snapshots) - stale_session

    invalid = {
        sym for sym, snap in requested_snapshots.items()
        # The one deliberate duplication of screen_snapshot's own guard
        # (broad_scan.py's baseline check), against Snapshot's public
        # fields -- not a re-derivation of any ratio.
        if snap.avg_volume <= 0 or snap.prior_close <= 0
    }

    selected_symbols = {c.symbol for c in selected}
    events: list[ScreeningOutcome] = []

    for rank, candidate in enumerate(promoted, start=1):
        snap = snapshot_by_symbol.get(candidate.symbol)
        events.append(ScreeningOutcome(
            symbol=candidate.symbol,
            outcome=OUTCOME_PROMOTED if candidate.symbol in selected_symbols else OUTCOME_CANDIDATE_NOT_PROMOTED,
            screen_score=candidate.score,
            rank=rank,
            reasons=tuple(candidate.reasons),
            detail=_snapshot_detail(snap) if snap is not None else None,
        ))

    for symbol in sorted(missing):
        events.append(ScreeningOutcome(symbol=symbol, outcome=OUTCOME_MISSING_FROM_FETCH))

    for symbol in sorted(insufficient):
        events.append(ScreeningOutcome(
            symbol=symbol, outcome=OUTCOME_INSUFFICIENT_HISTORY,
            detail={"bar_count": len(bars_by_symbol[symbol])},
        ))

    for symbol in sorted(stale_session):
        latest = max(bars_by_symbol[symbol], key=lambda b: b.ts)
        events.append(ScreeningOutcome(
            symbol=symbol, outcome=OUTCOME_STALE_SESSION_BAR,
            detail={
                "bar_count": len(bars_by_symbol[symbol]),
                "latest_session_date": latest.ts.astimezone(ET).date().isoformat(),
                "required_session_date": session_date.isoformat(),
            },
        ))

    for symbol in sorted(invalid):
        events.append(ScreeningOutcome(
            symbol=symbol, outcome=OUTCOME_INVALID_BASELINE,
            detail=_snapshot_detail(requested_snapshots[symbol]),
        ))

    for symbol in sorted(unexpected):
        events.append(ScreeningOutcome(symbol=symbol, outcome=OUTCOME_UNEXPECTED_FROM_FETCH))

    candidate_symbols = {c.symbol for c in promoted}
    quiet = set(requested_snapshots) - invalid - candidate_symbols
    if verbose_audit:
        for symbol in sorted(quiet):
            events.append(ScreeningOutcome(
                symbol=symbol, outcome=OUTCOME_QUIET,
                detail=_snapshot_detail(requested_snapshots[symbol]),
            ))

    requested_candidate_count = len(candidate_symbols & requested)
    counts = {
        "requested": len(requested),
        "fetched": len(requested_fetched),
        "missing_from_fetch": len(missing),
        "insufficient_history": len(insufficient),
        "stale_session_bar": len(stale_session),
        "requested_snapshot": len(requested_snapshots),
        "invalid_baseline": len(invalid),
        "quiet": len(quiet),
        "requested_candidate": requested_candidate_count,
        "candidate": len(promoted),
        "selected_top_n": len(selected),
        "unexpected_from_fetch": len(unexpected),
    }

    invariant_ok = (
        counts["missing_from_fetch"] + counts["insufficient_history"]
        + counts["stale_session_bar"] + counts["requested_snapshot"]
        == counts["requested"]
    ) and (
        counts["invalid_baseline"] + counts["quiet"] + requested_candidate_count
        == counts["requested_snapshot"]
    )

    tick = ScreeningTick(
        universe_count=len(requested),
        counts=counts,
        thresholds=screen_thresholds(),
        invariant_ok=invariant_ok,
        promotion_limit=promotion_limit,
    )
    return tick, events
