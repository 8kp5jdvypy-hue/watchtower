"""Data integrity guard — run before any alert is sent. Every rule here
exists because a specific way the pipeline can produce internally
inconsistent or stale-looking output was identified; this is not a
generic "sanity check everything" grab bag.

Returns a rejection reason (a string prefixed with a stable, short rule
name the caller can use as a metric label, e.g. "crossed_quote: bid
(431.30) > ask (431.10)") or None if the alert passes. Never raises,
never mutates, never logs, never sends anything itself — the caller
(tradebot.runner.process_new_bar) owns turning a rejection into an
ERROR log line, a metric increment, and a suppressed alert. Keeping this
module a pure predicate means it stays trivially testable and there's
exactly one place a "should this ever publish?" decision gets made.

Deliberately NOT generous: earlier drafts of this guard used a wide
slack band around the opening range, on the theory that "real breakouts
legitimately move price outside the opening range." That's true, but it
doesn't apply to the actual check here — [session_low, session_high] is
the running min/max of every bar traded so far THIS session, which by
construction already includes any real breakout. A quote sitting outside
that band isn't a big move, it's a quote that disagrees with the very
bars the alert is based on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

SPREAD_MAX_PCT_OF_MID = 0.05
QUOTE_MAX_STALENESS_SECONDS = 60
PRIOR_CLOSE_MAX_GAP_PCT = 0.25
FLOAT_TOLERANCE = 1e-6

# Proposal 3 (docs/open-awareness-proposals-2026-08.md): past
# PRIOR_CLOSE_MAX_GAP_PCT, the move is no longer auto-suppressed if it
# persists across two consecutive real-volume bars -- see
# extreme_mover_evidence(). Owner decision, Option B: a verified extreme
# mover gets a widened spread ceiling instead of the normal 5% one (thin
# names that really moved 25%+ routinely carry wide markets); the trade
# path's NO TRADE honesty is untouched by any of this.
EXTREME_MOVER_SPREAD_MAX_PCT_OF_MID = 0.15
EXTREME_MOVER_CLOSE_TOLERANCE_PCT = 0.10

_REQUIRED_CLUSTER_FIELDS = ("close", "score")
_REQUIRED_QUOTE_FIELDS = ("bid", "ask", "last")
_REQUIRED_ANCHOR_FIELDS = ("prior_close", "opening_range_low", "opening_range_high")


def _is_bad_number(value) -> bool:
    return value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))


def spread_pct_of_mid(quote) -> float | None:
    """(ask - bid) / mid, or None if mid isn't positive. The exact
    formula validate_alert_data's spread_too_wide check uses, exposed so
    a rendered extreme-mover card can show the identical number instead
    of recomputing it and risking the two ever disagreeing -- the same
    bug class _duplicate_field_reason exists to catch."""
    mid = (quote.bid + quote.ask) / 2
    if mid <= 0:
        return None
    return (quote.ask - quote.bid) / mid


@dataclass(frozen=True)
class ExtremeMoverEvidence:
    """Proof a >25%-from-prior-close move is real, not a bad print: the
    last two consecutive RTH bars both traded real volume beyond the
    line, on the same side, at closes within
    EXTREME_MOVER_CLOSE_TOLERANCE_PCT of each other."""

    gap_pct: float  # magnitude, e.g. 0.488 for +48.8% -- always positive
    verified_volume: int  # sum of the two persistence-check bars' volume


def extreme_mover_evidence(bars: Sequence, anchors, quote) -> ExtremeMoverEvidence | None:
    """None means either the move isn't past PRIOR_CLOSE_MAX_GAP_PCT at
    all, or it is but hasn't (yet) demonstrated persistence -- in both
    cases validate_alert_data's own extreme_prior_close_gap check is the
    one that decides whether to suppress. A non-None result is this
    proposal's whole point: real evidence the guard's data-integrity
    purpose survives without discarding the week's biggest movers.

    Pure and total: no exception for missing/short bars, a non-positive
    prior_close, or a bad quote.last -- all read as "no evidence",
    which is the conservative direction (validate_alert_data still
    suppresses on its own bad-numeric-field checks first in practice,
    but this function doesn't depend on that ordering)."""
    if _is_bad_number(quote.last) or anchors.prior_close <= 0:
        return None
    gap_pct = abs(quote.last - anchors.prior_close) / anchors.prior_close
    if gap_pct <= PRIOR_CLOSE_MAX_GAP_PCT:
        return None
    if not bars or len(bars) < 2:
        return None

    sign = 1 if quote.last >= anchors.prior_close else -1
    last_two = list(bars[-2:])
    for b in last_two:
        if b.volume <= 0:
            return None
        bar_gap_pct = (b.close - anchors.prior_close) / anchors.prior_close
        if sign * bar_gap_pct <= PRIOR_CLOSE_MAX_GAP_PCT:
            return None

    c1, c2 = last_two[0].close, last_two[1].close
    close_tolerance_pct = abs(c1 - c2) / ((abs(c1) + abs(c2)) / 2)
    if close_tolerance_pct > EXTREME_MOVER_CLOSE_TOLERANCE_PCT:
        return None

    return ExtremeMoverEvidence(gap_pct=gap_pct, verified_volume=sum(b.volume for b in last_two))


def _duplicate_field_reason(primary_detection, cluster, last_bar) -> str | None:
    """The exact bug this guard was built to catch: a value that gets
    embedded in the alert's headline text (a detector's own
    context["atr14"] or context["bar_range"]) silently disagreeing with
    the same concept shown elsewhere in the same message (cluster.atr14
    in the stats block). See runner.evaluate_bar's docstring comment for
    the root cause — this check is defense-in-depth in case a future
    change reintroduces it, not the primary fix."""
    if primary_detection is None:
        return None
    ctx = primary_detection.context

    detector_atr14 = ctx.get("atr14")
    if detector_atr14 is not None and cluster.atr14 is not None:
        if abs(detector_atr14 - cluster.atr14) > FLOAT_TOLERANCE:
            return (
                f"inconsistent_duplicate_field: atr14 appears as both {detector_atr14} "
                f"(in {primary_detection.kind}'s headline) and {cluster.atr14} (cluster.atr14)"
            )

    detector_bar_range = ctx.get("bar_range")
    if detector_bar_range is not None and last_bar is not None:
        actual_bar_range = last_bar.high - last_bar.low
        if abs(detector_bar_range - actual_bar_range) > FLOAT_TOLERANCE:
            return (
                f"inconsistent_duplicate_field: bar_range appears as both {detector_bar_range} "
                f"(in {primary_detection.kind}'s headline) and {actual_bar_range} (bars[-1].high - low)"
            )

    return None


def validate_alert_data(cluster, anchors, quote, *, bars=None, now=None, primary_detection=None) -> str | None:
    """Returns a rejection reason, or None if the data passes.

    bars: this session's accumulated bars so far, used to derive the
        real [session_low, session_high] band. Required for that check;
        pass None only from tests that don't exercise it.
    now: wall-clock time at send, tz-aware, for the quote-staleness
        check. Required for that check; pass None to skip it.
    primary_detection: the Detection whose headline became the alert's
        rationale — see _duplicate_field_reason.
    """
    for name in _REQUIRED_CLUSTER_FIELDS:
        if _is_bad_number(getattr(cluster, name)):
            return f"bad_numeric_field: cluster.{name} is null/NaN/inf"
    for name in _REQUIRED_QUOTE_FIELDS:
        if _is_bad_number(getattr(quote, name)):
            return f"bad_numeric_field: quote.{name} is null/NaN/inf"
    for name in _REQUIRED_ANCHOR_FIELDS:
        if _is_bad_number(getattr(anchors, name)):
            return f"bad_numeric_field: anchors.{name} is null/NaN/inf"
    if anchors.prior_close <= 0:
        return f"bad_numeric_field: anchors.prior_close ({anchors.prior_close}) is not positive"

    if quote.bid > quote.ask:
        return f"crossed_quote: bid ({quote.bid}) > ask ({quote.ask})"

    # Computed once, ahead of the spread check that consults it (Option
    # B's widened ceiling) and the extreme_prior_close_gap check below
    # that it exists for -- extreme_mover_evidence is self-contained, it
    # doesn't need bid/ask, so ordering it after the crossed_quote check
    # is a courtesy, not a requirement.
    extreme_mover = extreme_mover_evidence(bars, anchors, quote)

    spread_pct = spread_pct_of_mid(quote)
    if spread_pct is None:
        return f"crossed_quote: non-positive mid price ({(quote.bid + quote.ask) / 2})"
    spread_max = EXTREME_MOVER_SPREAD_MAX_PCT_OF_MID if extreme_mover is not None else SPREAD_MAX_PCT_OF_MID
    if spread_pct > spread_max:
        return f"spread_too_wide: spread is {spread_pct * 100:.1f}% of mid (max {spread_max * 100:.0f}%)"

    if bars:
        last_bar = bars[-1]
        if last_bar.high < last_bar.low:
            return f"invalid_bar: high ({last_bar.high}) < low ({last_bar.low})"

        session_low = min(b.low for b in bars)
        session_high = max(b.high for b in bars)
        if not (session_low <= quote.last <= session_high):
            return f"last_outside_session_range: quote.last ({quote.last}) outside [{session_low:.2f}, {session_high:.2f}]"
        if not (session_low <= cluster.close <= session_high):
            return f"last_outside_session_range: cluster.close ({cluster.close}) outside [{session_low:.2f}, {session_high:.2f}]"
    else:
        last_bar = None

    if now is not None:
        age_seconds = (now - quote.ts).total_seconds()
        if age_seconds > QUOTE_MAX_STALENESS_SECONDS:
            return f"stale_quote: quote is {age_seconds:.0f}s old (max {QUOTE_MAX_STALENESS_SECONDS}s)"

    gap_pct = abs(quote.last - anchors.prior_close) / anchors.prior_close
    if gap_pct > PRIOR_CLOSE_MAX_GAP_PCT and extreme_mover is None:
        return (
            f"extreme_prior_close_gap: last ({quote.last}) is {gap_pct * 100:.1f}% from "
            f"prior close ({anchors.prior_close}) (max {PRIOR_CLOSE_MAX_GAP_PCT * 100:.0f}%)"
        )

    duplicate_reason = _duplicate_field_reason(primary_detection, cluster, last_bar)
    if duplicate_reason is not None:
        return duplicate_reason

    return None
