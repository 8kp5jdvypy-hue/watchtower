"""Pure, shared feature primitives — data in, value out. No I/O, no
network, no clock reads, no globals (see CLAUDE.md).

This is the FIRST such primitive (Phase 1 perception decomposition,
experiment A1 — docs/open-blindness-investigation-2026-08.md /
docs/open-awareness-proposals-2026-08.md). A1 records prior-close
displacement as an audit feature only: not a detector, not a scoring
input, not a threshold. Deliberately not a feature-platform module — add
the next primitive as its own function when a later experiment needs
one; don't generalize ahead of a second real caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FeatureAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PctFromPriorClose:
    """value is a signed PERCENTAGE-POINT number (10.0 means +10%, not
    0.10) — see pct_from_prior_close(). value is None exactly when
    availability is UNAVAILABLE; reason is set only then, never else."""

    availability: FeatureAvailability
    value: float | None
    reason: str | None = None

    @property
    def status(self) -> str:
        """The single persisted-column encoding of availability + reason:
        'AVAILABLE' or 'UNAVAILABLE:<reason>' — see
        journal.write_cluster's pct_from_prior_close_status column."""
        if self.availability is FeatureAvailability.AVAILABLE:
            return FeatureAvailability.AVAILABLE.value
        return f"{FeatureAvailability.UNAVAILABLE.value}:{self.reason}"


def pct_from_prior_close(current_close: float, prior_close: float | None) -> PctFromPriorClose:
    """Signed displacement from the prior session's close, in PERCENTAGE
    POINTS: ((current_close - prior_close) / prior_close) * 100 — +10.0
    for a 10% gap up, -10.0 for a 10% gap down.

    Never a silent None: a missing prior_close (None — the caller has no
    anchor yet) and a non-positive prior_close (a degenerate/bad daily
    bar — the division would be meaningless, or a ZeroDivisionError at
    exactly 0) are two distinct UNAVAILABLE reasons, both explicit, never
    a bare null value standing in for either.

    Deterministic, O(1), no I/O — pure per CLAUDE.md."""
    if prior_close is None:
        return PctFromPriorClose(FeatureAvailability.UNAVAILABLE, None, reason="no_prior_close")
    if prior_close <= 0:
        return PctFromPriorClose(FeatureAvailability.UNAVAILABLE, None, reason="invalid_prior_close")
    value = ((current_close - prior_close) / prior_close) * 100.0
    return PctFromPriorClose(FeatureAvailability.AVAILABLE, value)
