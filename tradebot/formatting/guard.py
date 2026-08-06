"""Data integrity guard — run before any alert is sent.

Catches internally-inconsistent data (a stale/wrong quote, a decimal
error, a crossed market) that would make a published alert actively
misleading. Deliberately generous bounds: real breakouts legitimately
move price outside the opening range, so this is a sanity check against
gross corruption, not a tight statistical band. Returns a reason string
to reject, or None to pass — never raises, never mutates, never sends
anything itself.
"""
from __future__ import annotations

import math

# How far beyond the widest known anchor band a price is allowed to sit
# before it's treated as implausible rather than a real large move.
SESSION_RANGE_SLACK = 0.5


def _is_bad_number(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def validate_alert_data(cluster, anchors, quote) -> str | None:
    """Returns a rejection reason, or None if the data passes."""
    if _is_bad_number(cluster.close):
        return "cluster.close is null/NaN"
    if _is_bad_number(cluster.score):
        return "cluster.score is null/NaN"
    for name in ("bid", "ask", "last"):
        if _is_bad_number(getattr(quote, name)):
            return f"quote.{name} is null/NaN"
    for name in ("prior_close", "opening_range_low", "opening_range_high"):
        if _is_bad_number(getattr(anchors, name)):
            return f"anchors.{name} is null/NaN"

    if quote.bid > quote.ask:
        return f"bid ({quote.bid}) > ask ({quote.ask}) — crossed quote"

    low = min(anchors.opening_range_low, anchors.prior_low, anchors.swing_low)
    high = max(anchors.opening_range_high, anchors.prior_high, anchors.swing_high)
    span = high - low
    slack = span * SESSION_RANGE_SLACK if span > 0 else high * 0.1
    band_low, band_high = low - slack, high + slack

    if not (band_low <= quote.last <= band_high):
        return f"quote.last ({quote.last}) outside plausible session range [{band_low:.2f}, {band_high:.2f}]"
    if not (band_low <= cluster.close <= band_high):
        return f"cluster.close ({cluster.close}) outside plausible session range [{band_low:.2f}, {band_high:.2f}]"

    return None
