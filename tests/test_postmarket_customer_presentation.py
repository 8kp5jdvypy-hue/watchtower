import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_customer_presentation import (
    DERIVED_ONLY_SEMANTIC,
    PAYLOAD_FIELDS,
    build_customer_preview,
    render_customer_preview,
)
from tradebot.postmarket_delivery_readiness import (
    DECISION_ELIGIBLE,
    DECISION_SUPPRESSED,
    PRESENTATION_ACTIONABLE,
    PRESENTATION_DEGRADED,
    DeliveryCandidate,
    DeliveryReadinessDecision,
)
from tradebot.postmarket_lifecycle import STATE_CONFIRMED


NOW = datetime(2026, 9, 2, 21, 15, tzinfo=timezone.utc)


@pytest.fixture
def candidate():
    return DeliveryCandidate(
        transition_id=44,
        candidate_id=12,
        session="2026-09-02",
        symbol="OKTA",
        direction="up",
        lifecycle_state=STATE_CONFIRMED,
        actionability="QUALIFIED",
        transition_at=NOW - timedelta(minutes=2),
        evidence_bar_open_at=NOW - timedelta(minutes=7),
        rank_run_id=17,
        rank_id=18,
        rank_version=1,
        rank_status="complete",
        rankable=True,
        ordinal_rank=3,
        evidence_score=77,
        calibration_projection_id=55,
        calibration_run_id=9,
        calibration_version=1,
        calibration_model_sha256="c" * 64,
        calibrated_quality=0.80,
        calibration_projected_at=NOW - timedelta(minutes=1),
        calibration_code_version="abc1234",
        evidence_coverage_pct=100,
        exclusion_reasons=(),
        data_feed="sip",
        market_data_provider="alpaca",
        code_version="abc1234",
    )


@pytest.fixture
def eligible():
    return DeliveryReadinessDecision(
        decision=DECISION_ELIGIBLE,
        presentation=PRESENTATION_ACTIONABLE,
        reason_codes=(),
        idempotency_key="a" * 64,
        policy_sha256="b" * 64,
        release_id="release-1",
    )


def test_preview_is_exact_derived_only_allowlist(candidate, eligible):
    preview = build_customer_preview(
        candidate, eligible, route_id=7, generated_at=NOW
    )
    payload = json.loads(preview.payload_json)
    assert set(payload) == PAYLOAD_FIELDS
    assert payload == {
        "customer_state": "ACTIONABLE",
        "disclaimer": (
            "Derived market-intelligence signal; not a quote, chart, "
            "recommendation, or trade instruction."
        ),
        "generated_at_utc": NOW.isoformat(),
        "license_semantic": DERIVED_ONLY_SEMANTIC,
        "lifecycle": STATE_CONFIRMED,
        "ordinal_rank": 3,
        "presentation_version": 1,
        "quality_status": "MEETS_LOCKED_POLICY",
        "schema_version": 1,
        "signal": "POSTMARKET_STRENGTH",
        "symbol": "OKTA",
    }
    rendered = render_customer_preview(preview)
    assert "OKTA — POSTMARKET_STRENGTH" in rendered
    assert "Rank: 3" in rendered
    assert "$" not in rendered
    assert "%" not in rendered


def test_preview_never_serializes_raw_or_reconstructable_fields(candidate, eligible):
    preview = build_customer_preview(
        candidate, eligible, route_id=7, generated_at=NOW
    )
    payload = json.loads(preview.payload_json)
    for forbidden in (
        "price", "quote", "ohlc", "open", "high", "low", "close",
        "bid", "ask", "volume", "notional", "move_pct", "return",
        "evidence_score", "calibrated_quality", "market_data_provider",
        "data_feed", "evidence_bar_open_at",
    ):
        assert forbidden not in payload
    assert "alpaca" not in preview.payload_json.lower()
    assert '"sip"' not in preview.payload_json.lower()
    assert '"evidence_score":77' not in preview.payload_json.lower()
    assert '"calibrated_quality":0.8' not in preview.payload_json.lower()


def test_suppressed_or_malformed_candidates_fail_closed(candidate, eligible):
    suppressed = replace(
        eligible,
        decision=DECISION_SUPPRESSED,
        presentation=PRESENTATION_DEGRADED,
        reason_codes=("OPERATIONAL_STATUS_DEGRADED",),
    )
    with pytest.raises(ValueError, match="suppressed"):
        build_customer_preview(candidate, suppressed, route_id=7, generated_at=NOW)
    with pytest.raises(ValueError, match="direction"):
        build_customer_preview(
            replace(candidate, direction="sideways"),
            eligible,
            route_id=7,
            generated_at=NOW,
        )
    with pytest.raises(ValueError, match="ordinal rank"):
        build_customer_preview(
            replace(candidate, ordinal_rank=None),
            eligible,
            route_id=7,
            generated_at=NOW,
        )


def test_presentation_module_has_no_live_or_trading_dependency():
    source = Path("tradebot/postmarket_customer_presentation.py").read_text().lower()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    for forbidden in ("telegram", "outbox", "requests", "broker", "order"):
        assert not any(forbidden in line for line in imports)
