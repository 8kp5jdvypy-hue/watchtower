"""Provider qualification is strict, complete, and digest-bound."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from tradebot.vendors.historical_reference_qualification import (
    REQUIRED_QUALIFICATION_PROOFS,
    load_historical_reference_qualification,
    parse_historical_reference_qualification,
)


OBSERVED = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)


def _payload() -> dict:
    return {
        "schema_version": 1,
        "status": "qualified",
        "provider": "massive",
        "dataset": "us_stocks_sip/minute_aggs_v1",
        "approved_at_utc": "2026-09-02T00:00:00+00:00",
        "approved_by": "operator",
        "license_reference": "agreement-2026-001",
        "proofs": [
            {
                "kind": kind,
                "reference": f"archive/{kind}.pdf",
                "sha256": hashlib.sha256(kind.encode()).hexdigest(),
            }
            for kind in sorted(REQUIRED_QUALIFICATION_PROOFS)
        ],
    }


def _raw(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def test_complete_qualification_is_digest_bound():
    raw = _raw(_payload())

    result = parse_historical_reference_qualification(raw, observed_at=OBSERVED)

    assert result.provider == "massive"
    assert result.dataset == "us_stocks_sip/minute_aggs_v1"
    assert {proof.kind for proof in result.proofs} == REQUIRED_QUALIFICATION_PROOFS
    assert result.manifest_sha256 == hashlib.sha256(raw).hexdigest()


def test_missing_acceptance_proof_fails_closed():
    payload = _payload()
    payload["proofs"] = payload["proofs"][:-1]

    with pytest.raises(ValueError, match="proofs are incomplete"):
        parse_historical_reference_qualification(_raw(payload), observed_at=OBSERVED)


def test_future_or_duplicate_qualification_fails_closed():
    payload = _payload()
    payload["approved_at_utc"] = "2026-09-03T00:00:00+00:00"
    with pytest.raises(ValueError, match="cannot be in the future"):
        parse_historical_reference_qualification(_raw(payload), observed_at=OBSERVED)

    payload = _payload()
    payload["proofs"].append(dict(payload["proofs"][0]))
    with pytest.raises(ValueError, match="duplicate qualification proof"):
        parse_historical_reference_qualification(_raw(payload), observed_at=OBSERVED)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", True, "unsupported qualification schema_version"),
        ("provider", "Massive", "canonical lowercase identifier"),
    ),
)
def test_noncanonical_identity_fields_fail_closed(field, value, message):
    payload = _payload()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        parse_historical_reference_qualification(_raw(payload), observed_at=OBSERVED)


def test_symlink_manifest_is_rejected(tmp_path):
    target = tmp_path / "qualification.json"
    target.write_bytes(_raw(_payload()))
    link = tmp_path / "qualification-link.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular non-symlink"):
        load_historical_reference_qualification(link, observed_at=OBSERVED)
