"""Provider selection is explicit, lazy, and fail-closed."""
from __future__ import annotations

import pytest

from tradebot.vendors.historical_reference import (
    HistoricalReferenceConfigurationError,
    selected_provider,
    source,
)


def test_massive_is_backward_compatible_default(monkeypatch):
    monkeypatch.delenv("POSTMARKET_REFERENCE_PROVIDER", raising=False)

    assert selected_provider() == "massive"
    adapter = source()
    assert adapter.provider == "massive"
    assert adapter.feed == "sip"
    assert adapter.dataset == "us_stocks_sip/minute_aggs_v1"


@pytest.mark.parametrize("provider", ("tiingo", "databento"))
def test_known_provider_without_adapter_fails_closed(provider):
    assert selected_provider(provider.upper()) == provider
    with pytest.raises(HistoricalReferenceConfigurationError, match="not implemented"):
        source(provider)


def test_unknown_provider_is_rejected_without_fallback():
    with pytest.raises(HistoricalReferenceConfigurationError, match="must be one of"):
        selected_provider("alpaca")
