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

SPREAD_MAX_PCT_OF_MID = 0.05
QUOTE_MAX_STALENESS_SECONDS = 60
PRIOR_CLOSE_MAX_GAP_PCT = 0.25
FLOAT_TOLERANCE = 1e-6

_REQUIRED_CLUSTER_FIELDS = ("close", "score")
_REQUIRED_QUOTE_FIELDS = ("bid", "ask", "last")
_REQUIRED_ANCHOR_FIELDS = ("prior_close", "opening_range_low", "opening_range_high")


def _is_bad_number(value) -> bool:
    return value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))


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

    mid = (quote.bid + quote.ask) / 2
    if mid <= 0:
        return f"crossed_quote: non-positive mid price ({mid})"
    spread_pct = (quote.ask - quote.bid) / mid
    if spread_pct > SPREAD_MAX_PCT_OF_MID:
        return f"spread_too_wide: spread is {spread_pct * 100:.1f}% of mid (max {SPREAD_MAX_PCT_OF_MID * 100:.0f}%)"

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
    if gap_pct > PRIOR_CLOSE_MAX_GAP_PCT:
        return (
            f"extreme_prior_close_gap: last ({quote.last}) is {gap_pct * 100:.1f}% from "
            f"prior close ({anchors.prior_close}) (max {PRIOR_CLOSE_MAX_GAP_PCT * 100:.0f}%)"
        )

    duplicate_reason = _duplicate_field_reason(primary_detection, cluster, last_bar)
    if duplicate_reason is not None:
        return duplicate_reason

    return None
