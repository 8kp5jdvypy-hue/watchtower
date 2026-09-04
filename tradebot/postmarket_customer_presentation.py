"""Pure, derived-only customer preview for postmarket dry-run review.

The preview is intentionally unable to expose raw market data.  It accepts
only the already-derived readiness candidate and decision, emits an exact
field allowlist, and has no delivery, provider, order, or network dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from tradebot.postmarket_delivery_readiness import (
    DECISION_ELIGIBLE,
    PRESENTATION_ACTIONABLE,
    ALLOWED_STATES,
    DeliveryCandidate,
    DeliveryReadinessDecision,
)


CUSTOMER_PRESENTATION_VERSION = 1
DERIVED_ONLY_SEMANTIC = "non_reconstructable_derived_only_v1"
DISCLAIMER = (
    "Derived market-intelligence signal; not a quote, chart, recommendation, "
    "or trade instruction."
)
PAYLOAD_FIELDS = frozenset({
    "schema_version",
    "presentation_version",
    "license_semantic",
    "customer_state",
    "symbol",
    "signal",
    "lifecycle",
    "ordinal_rank",
    "quality_status",
    "generated_at_utc",
    "disclaimer",
})
SIGNAL_LABELS = {
    "up": "POSTMARKET_STRENGTH",
    "down": "POSTMARKET_WEAKNESS",
}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_json(payload: dict[str, object]) -> str:
    if set(payload) != PAYLOAD_FIELDS:
        raise ValueError("customer preview fields must match the exact allowlist")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def validate_customer_preview(
    payload_json: str,
    payload_sha256: str,
) -> dict[str, object]:
    """Reproduce and validate the exact persisted derived-only payload."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("customer preview payload must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("customer preview payload must be an object")
    canonical = _canonical_json(payload)
    if canonical != payload_json:
        raise ValueError("customer preview payload must be canonical JSON")
    if hashlib.sha256(canonical.encode()).hexdigest() != payload_sha256:
        raise ValueError("customer preview digest mismatch")
    if payload["schema_version"] != 1:
        raise ValueError("customer preview schema version is unsupported")
    if payload["presentation_version"] != CUSTOMER_PRESENTATION_VERSION:
        raise ValueError("customer preview presentation version is unsupported")
    if payload["license_semantic"] != DERIVED_ONLY_SEMANTIC:
        raise ValueError("customer preview license semantic is unsupported")
    if payload["customer_state"] != PRESENTATION_ACTIONABLE:
        raise ValueError("customer preview state must be actionable")
    if (
        not isinstance(payload["symbol"], str)
        or not SYMBOL_PATTERN.fullmatch(payload["symbol"])
    ):
        raise ValueError("customer preview symbol is invalid")
    if payload["signal"] not in set(SIGNAL_LABELS.values()):
        raise ValueError("customer preview signal is unsupported")
    if payload["lifecycle"] not in ALLOWED_STATES:
        raise ValueError("customer preview lifecycle state is unsupported")
    ordinal_rank = payload["ordinal_rank"]
    if isinstance(ordinal_rank, bool) or not isinstance(ordinal_rank, int) or ordinal_rank <= 0:
        raise ValueError("customer preview ordinal rank must be positive")
    if payload["quality_status"] != "MEETS_LOCKED_POLICY":
        raise ValueError("customer preview quality status is unsupported")
    if payload["disclaimer"] != DISCLAIMER:
        raise ValueError("customer preview disclaimer is not exact")
    if not isinstance(payload["generated_at_utc"], str):
        raise ValueError("customer preview generation time must be an ISO datetime")
    try:
        generated = datetime.fromisoformat(payload["generated_at_utc"])
    except ValueError as exc:
        raise ValueError("customer preview generation time must be an ISO datetime") from exc
    _aware_utc(generated, "customer preview generated_at_utc")
    return payload


@dataclass(frozen=True)
class CustomerPresentationPreview:
    route_id: int
    payload_json: str
    payload_sha256: str
    generated_at_utc: str


def build_customer_preview(
    candidate: DeliveryCandidate,
    decision: DeliveryReadinessDecision,
    *,
    route_id: int,
    generated_at: datetime,
) -> CustomerPresentationPreview:
    """Build one deterministic preview only for a fully eligible dry-run route."""
    if route_id <= 0:
        raise ValueError("route_id must be positive")
    if decision.decision != DECISION_ELIGIBLE:
        raise ValueError("suppressed decisions cannot produce a customer preview")
    if decision.presentation != PRESENTATION_ACTIONABLE or decision.reason_codes:
        raise ValueError("eligible customer preview must be actionable and reason-free")
    signal = SIGNAL_LABELS.get(candidate.direction.lower())
    if signal is None:
        raise ValueError("customer preview direction is unsupported")
    symbol = candidate.symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("customer preview symbol is invalid")
    if candidate.lifecycle_state not in ALLOWED_STATES:
        raise ValueError("customer preview lifecycle state is unsupported")
    if (
        isinstance(candidate.ordinal_rank, bool)
        or not isinstance(candidate.ordinal_rank, int)
        or candidate.ordinal_rank <= 0
    ):
        raise ValueError("customer preview requires a positive ordinal rank")
    generated = _aware_utc(generated_at, "generated_at")
    payload = {
        "schema_version": 1,
        "presentation_version": CUSTOMER_PRESENTATION_VERSION,
        "license_semantic": DERIVED_ONLY_SEMANTIC,
        "customer_state": PRESENTATION_ACTIONABLE,
        "symbol": symbol,
        "signal": signal,
        "lifecycle": candidate.lifecycle_state,
        "ordinal_rank": candidate.ordinal_rank,
        "quality_status": "MEETS_LOCKED_POLICY",
        "generated_at_utc": generated.isoformat(),
        "disclaimer": DISCLAIMER,
    }
    payload_json = _canonical_json(payload)
    return CustomerPresentationPreview(
        route_id=route_id,
        payload_json=payload_json,
        payload_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
        generated_at_utc=generated.isoformat(),
    )


def render_customer_preview(preview: CustomerPresentationPreview) -> str:
    """Render the whitelisted preview without adding any unstored claim."""
    payload = validate_customer_preview(
        preview.payload_json,
        preview.payload_sha256,
    )
    return (
        f"{payload['symbol']} — {payload['signal']}\n"
        f"State: {payload['lifecycle']} · Rank: {payload['ordinal_rank']}\n"
        f"Quality: {payload['quality_status']}\n"
        f"{payload['disclaimer']}"
    )
