"""Fail-closed registry for independent historical market-data adapters.

Provider-proof logic consumes this small contract instead of importing a
vendor directly.  A provider is selectable only after its adapter is shipped;
known-but-unimplemented providers remain explicit preflight failures.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from tradebot.detectors import Bar
from tradebot.vendors.historical_reference_qualification import (
    REFERENCE_PROVIDER_QUALIFICATION_ENV,
    load_historical_reference_qualification,
)


REFERENCE_PROVIDER_ENV = "POSTMARKET_REFERENCE_PROVIDER"
DEFAULT_REFERENCE_PROVIDER = "massive"
KNOWN_REFERENCE_PROVIDERS = frozenset({"massive", "tiingo", "databento"})
IMPLEMENTED_REFERENCE_PROVIDERS = frozenset({"massive"})
REFERENCE_PROVIDER_DATASETS = {
    "massive": "us_stocks_sip/minute_aggs_v1",
}


@dataclass(frozen=True)
class HistoricalReferenceCapabilities:
    """Evidence properties required by the intraday recall-proof contract."""

    completed_intraday_bars: bool
    full_universe_snapshot: bool
    postmarket_coverage: bool
    immutable_object_provenance: bool
    production_qualified: bool

    @property
    def recall_proof_eligible(self) -> bool:
        return not self.missing_recall_proof_capabilities

    @property
    def missing_recall_proof_capabilities(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "completed_intraday_bars",
                "full_universe_snapshot",
                "postmarket_coverage",
                "immutable_object_provenance",
                "production_qualified",
            )
            if not getattr(self, name)
        )


NO_RECALL_PROOF_CAPABILITIES = HistoricalReferenceCapabilities(
    completed_intraday_bars=False,
    full_universe_snapshot=False,
    postmarket_coverage=False,
    immutable_object_provenance=False,
    production_qualified=False,
)


REFERENCE_PROVIDER_CAPABILITIES = {
    "massive": HistoricalReferenceCapabilities(
        completed_intraday_bars=True,
        full_universe_snapshot=True,
        postmarket_coverage=True,
        immutable_object_provenance=True,
        # Adapter implementation is not legal or production qualification.
        # This becomes true only after a strict operator-reviewed manifest is
        # supplied and validated by provider_capabilities().
        production_qualified=False,
    ),
    # Tiingo is approved for a derived-only EOD evaluation. Its documented
    # per-symbol intraday endpoint is beta and is not an immutable bulk
    # full-universe object, so it cannot satisfy this proof contract.
    "tiingo": NO_RECALL_PROOF_CAPABILITIES,
    "databento": NO_RECALL_PROOF_CAPABILITIES,
}


class HistoricalReferenceConfigurationError(RuntimeError):
    """The selected independent provider cannot safely serve evidence."""


@dataclass(frozen=True)
class HistoricalReferenceSnapshot:
    session: date
    object_key: str
    object_etag: str | None
    object_last_modified_utc: str | None
    object_bytes: int | None
    selected_rows_sha256: str
    rows_read: int
    selected_rows: int
    selected_symbols: int
    bars_by_symbol: Mapping[str, tuple[Bar, ...]]


@dataclass(frozen=True)
class HistoricalReferenceSource:
    provider: str
    feed: str
    dataset: str
    capabilities: HistoricalReferenceCapabilities
    configured: Callable[[], bool]
    expected_available_at: Callable[[date], datetime]
    object_key: Callable[[date], str]
    fetch: Callable[
        [date, Sequence[str], datetime, datetime], HistoricalReferenceSnapshot
    ]


def selected_provider(raw: str | None = None) -> str:
    value = os.environ.get(REFERENCE_PROVIDER_ENV, DEFAULT_REFERENCE_PROVIDER)
    normalized = (value if raw is None else raw).strip().lower()
    if not normalized:
        normalized = DEFAULT_REFERENCE_PROVIDER
    if normalized not in KNOWN_REFERENCE_PROVIDERS:
        raise HistoricalReferenceConfigurationError(
            f"{REFERENCE_PROVIDER_ENV} must be one of "
            f"{', '.join(sorted(KNOWN_REFERENCE_PROVIDERS))}"
        )
    return normalized


def provider_capabilities(
    raw: str | None = None,
    *,
    qualification_manifest: str | Path | None = None,
    observed_at: datetime | None = None,
) -> HistoricalReferenceCapabilities:
    provider = selected_provider(raw)
    capabilities = REFERENCE_PROVIDER_CAPABILITIES[provider]
    manifest_value = (
        os.environ.get(REFERENCE_PROVIDER_QUALIFICATION_ENV, "")
        if qualification_manifest is None
        else str(qualification_manifest)
    ).strip()
    if not manifest_value:
        return capabilities
    try:
        qualification = load_historical_reference_qualification(
            manifest_value,
            observed_at=observed_at,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise HistoricalReferenceConfigurationError(
            f"historical reference qualification is invalid: {exc}"
        ) from exc
    expected_dataset = REFERENCE_PROVIDER_DATASETS.get(provider)
    if qualification.provider != provider:
        raise HistoricalReferenceConfigurationError(
            "historical reference qualification provider does not match selection"
        )
    if expected_dataset is None or qualification.dataset != expected_dataset:
        raise HistoricalReferenceConfigurationError(
            "historical reference qualification dataset does not match adapter"
        )
    return replace(capabilities, production_qualified=True)


def require_recall_proof_capabilities(
    reference: HistoricalReferenceSource,
) -> None:
    missing = reference.capabilities.missing_recall_proof_capabilities
    if missing:
        raise HistoricalReferenceConfigurationError(
            f"historical reference adapter {reference.provider!r} cannot serve "
            f"the intraday full-universe recall proof; missing capabilities: "
            f"{', '.join(missing)}"
        )


def source(raw: str | None = None) -> HistoricalReferenceSource:
    provider = selected_provider(raw)
    if provider not in IMPLEMENTED_REFERENCE_PROVIDERS:
        raise HistoricalReferenceConfigurationError(
            f"historical reference adapter {provider!r} is not implemented"
        )
    if provider == "massive":
        from tradebot.vendors import massive_flatfiles

        return HistoricalReferenceSource(
            provider="massive",
            feed="sip",
            dataset=massive_flatfiles.DATASET,
            capabilities=provider_capabilities(provider),
            configured=massive_flatfiles.configured,
            expected_available_at=massive_flatfiles.expected_available_at,
            object_key=massive_flatfiles.object_key,
            fetch=lambda session, symbols, start, end: (
                massive_flatfiles.fetch_minute_aggregates(
                    session, symbols=symbols, start=start, end=end
                )
            ),
        )
    raise AssertionError(f"unhandled historical reference provider {provider!r}")
