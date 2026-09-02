"""Fail-closed registry for independent historical market-data adapters.

Provider-proof logic consumes this small contract instead of importing a
vendor directly.  A provider is selectable only after its adapter is shipped;
known-but-unimplemented providers remain explicit preflight failures.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Mapping, Sequence

from tradebot.detectors import Bar


REFERENCE_PROVIDER_ENV = "POSTMARKET_REFERENCE_PROVIDER"
DEFAULT_REFERENCE_PROVIDER = "massive"
KNOWN_REFERENCE_PROVIDERS = frozenset({"massive", "tiingo", "databento"})
IMPLEMENTED_REFERENCE_PROVIDERS = frozenset({"massive"})


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
