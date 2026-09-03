"""Provider selection is explicit, lazy, and fail-closed."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from tradebot.vendors.historical_reference import (
    HistoricalReferenceConfigurationError,
    provider_capabilities,
    selected_provider,
    source,
)
from tradebot.vendors.historical_reference_qualification import (
    REFERENCE_PROVIDER_QUALIFICATION_ENV,
    REQUIRED_QUALIFICATION_PROOFS,
)


def _qualification(path, *, provider="massive", dataset="us_stocks_sip/minute_aggs_v1"):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "qualified",
                "provider": provider,
                "dataset": dataset,
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
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_massive_default_is_implemented_but_not_operator_qualified(monkeypatch):
    monkeypatch.delenv("POSTMARKET_REFERENCE_PROVIDER", raising=False)
    monkeypatch.delenv(REFERENCE_PROVIDER_QUALIFICATION_ENV, raising=False)

    assert selected_provider() == "massive"
    adapter = source()
    assert adapter.provider == "massive"
    assert adapter.feed == "sip"
    assert adapter.dataset == "us_stocks_sip/minute_aggs_v1"
    assert adapter.qualification_manifest_sha256 is None
    assert adapter.capabilities.recall_proof_eligible is False
    assert adapter.capabilities.missing_recall_proof_capabilities == (
        "production_qualified",
    )


def test_massive_requires_exact_operator_qualification(monkeypatch, tmp_path):
    manifest = tmp_path / "qualification.json"
    _qualification(manifest)
    monkeypatch.setenv(REFERENCE_PROVIDER_QUALIFICATION_ENV, str(manifest))

    adapter = source()

    assert adapter.capabilities.recall_proof_eligible is True
    assert adapter.qualification_manifest_sha256 == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()


def test_foreign_qualification_cannot_authorize_selected_adapter(tmp_path):
    manifest = tmp_path / "qualification.json"
    _qualification(manifest, provider="databento", dataset="us-equities-minute")

    with pytest.raises(
        HistoricalReferenceConfigurationError,
        match="provider does not match selection",
    ):
        provider_capabilities(
            "massive",
            qualification_manifest=manifest,
            observed_at=datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc),
        )


def test_qualification_is_bound_to_the_loaded_adapter_dataset(
    monkeypatch, tmp_path,
):
    from tradebot.vendors import massive_flatfiles

    manifest = tmp_path / "qualification.json"
    _qualification(manifest)
    monkeypatch.setenv(REFERENCE_PROVIDER_QUALIFICATION_ENV, str(manifest))
    monkeypatch.setattr(massive_flatfiles, "DATASET", "replacement-dataset")

    with pytest.raises(
        HistoricalReferenceConfigurationError,
        match="dataset does not match loaded adapter",
    ):
        source()


@pytest.mark.parametrize("provider", ("tiingo", "databento"))
def test_known_provider_without_adapter_fails_closed(provider):
    assert selected_provider(provider.upper()) == provider
    with pytest.raises(HistoricalReferenceConfigurationError, match="not implemented"):
        source(provider)


def test_unknown_provider_is_rejected_without_fallback():
    with pytest.raises(HistoricalReferenceConfigurationError, match="must be one of"):
        selected_provider("alpaca")


def test_tiingo_is_not_misrepresented_as_intraday_full_universe_proof():
    capabilities = provider_capabilities("tiingo")

    assert capabilities.recall_proof_eligible is False
    assert capabilities.missing_recall_proof_capabilities == (
        "completed_intraday_bars",
        "full_universe_snapshot",
        "postmarket_coverage",
        "immutable_object_provenance",
        "production_qualified",
    )
