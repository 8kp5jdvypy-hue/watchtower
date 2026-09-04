"""Strict OpenFIGI ticker-identity adapter.

OpenFIGI is an identity source, not a market-data source.  A ticker lookup can
legitimately return multiple venue or share-class matches, so this adapter
preserves every response row and never chooses a security on the caller's
behalf.  The caller may claim a resolved identity only when the returned FIGI
set is unambiguous under an explicit rule.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

import requests


BASE_URL = "https://api.openfigi.com/v3/mapping"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20
API_KEY_ENV = "OPENFIGI_API_KEY"
US_COMPOSITE_EXCHANGE_CODE = "US"


class OpenFigiError(RuntimeError):
    """Provider or schema failure without response bodies or credentials."""


@dataclass(frozen=True)
class OpenFigiMatch:
    figi: str
    name: str | None
    ticker: str | None
    exchange_code: str | None
    composite_figi: str | None
    share_class_figi: str | None
    market_sector: str | None
    security_type: str | None
    security_type2: str | None
    security_description: str | None


@dataclass(frozen=True)
class OpenFigiLookup:
    symbol: str
    matches: tuple[OpenFigiMatch, ...]
    provider_warning: str | None = None


def _canonical_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol or symbol != value:
        raise ValueError("symbol must be canonical uppercase")
    if len(symbol) > 32 or any(character.isspace() for character in symbol):
        raise ValueError("symbol is invalid")
    return symbol


def _optional_string(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenFigiError(f"OpenFIGI match field {key} was not a string")
    stripped = value.strip()
    return stripped or None


def parse_mapping_response(symbol: str, payload: object) -> OpenFigiLookup:
    """Validate the one-job mapping response and preserve all valid matches."""
    canonical = _canonical_symbol(symbol)
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise OpenFigiError("OpenFIGI response was not an array")
    if len(payload) != 1 or not isinstance(payload[0], Mapping):
        raise OpenFigiError("OpenFIGI response did not contain exactly one job result")
    result = payload[0]
    warning = _optional_string(result, "warning")
    provider_error = _optional_string(result, "error")
    data = result.get("data")
    if provider_error is not None:
        # "No identifier found" is a valid, attributable zero-match result.
        if provider_error.lower().startswith("no identifier found"):
            return OpenFigiLookup(canonical, (), provider_error)
        raise OpenFigiError("OpenFIGI rejected the mapping job")
    if (
        data is None
        and warning is not None
        and warning.lower().startswith("no identifier found")
    ):
        return OpenFigiLookup(canonical, (), warning)
    if data is None:
        raise OpenFigiError("OpenFIGI response omitted data and error")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        raise OpenFigiError("OpenFIGI data was not an array")

    matches: list[OpenFigiMatch] = []
    for raw in data:
        if not isinstance(raw, Mapping):
            raise OpenFigiError("OpenFIGI match was not an object")
        figi = _optional_string(raw, "figi")
        if figi is None:
            raise OpenFigiError("OpenFIGI match omitted figi")
        matches.append(OpenFigiMatch(
            figi=figi,
            name=_optional_string(raw, "name"),
            ticker=_optional_string(raw, "ticker"),
            exchange_code=_optional_string(raw, "exchCode"),
            composite_figi=_optional_string(raw, "compositeFIGI"),
            share_class_figi=_optional_string(raw, "shareClassFIGI"),
            market_sector=_optional_string(raw, "marketSector"),
            security_type=_optional_string(raw, "securityType"),
            security_type2=_optional_string(raw, "securityType2"),
            security_description=_optional_string(raw, "securityDescription"),
        ))
    ordered = tuple(sorted(matches, key=lambda row: (
        row.share_class_figi or "", row.composite_figi or "", row.figi,
    )))
    if len({row.figi for row in ordered}) != len(ordered):
        raise OpenFigiError("OpenFIGI response contained duplicate FIGIs")
    return OpenFigiLookup(canonical, ordered, warning)


def fetch_ticker_identity(
    symbol: str,
    *,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> OpenFigiLookup:
    """Fetch current equity mappings for one canonical ticker.

    An API key is optional under OpenFIGI's public API, but deployments must
    explicitly enable this caller elsewhere.  Provider response bodies are not
    included in raised errors because they are neither stable nor safe logs.
    """
    canonical = _canonical_symbol(symbol)
    key = os.environ.get(API_KEY_ENV, "").strip() if api_key is None else api_key.strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    client = session or requests.Session()
    try:
        response = client.post(
            BASE_URL,
            headers=headers,
            json=[{
                "idType": "TICKER",
                "idValue": canonical,
                "marketSecDes": "Equity",
                "exchCode": US_COMPOSITE_EXCHANGE_CODE,
            }],
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise OpenFigiError(f"OpenFIGI request failed{suffix}") from exc
    except ValueError as exc:
        raise OpenFigiError("OpenFIGI returned invalid JSON") from exc
    return parse_mapping_response(canonical, payload)
