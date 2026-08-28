"""Bounded Massive REST adapter for independent, point-in-time evidence.

This adapter is intentionally separate from Alpaca.  It supplies historical
five-minute stock aggregates for provider reconciliation and dated ticker
reference facts.  It never falls back to Alpaca and never logs credentials or
credential-bearing URLs.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import requests

from tradebot.detectors import Bar


BASE_URL = "https://api.massive.com"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
AGGREGATES_ENDPOINT = "/v2/aggs/ticker/{ticker}/range/5/minute/{start_ms}/{end_ms}"
TICKER_REFERENCE_ENDPOINT = "/v3/reference/tickers/{ticker}"


class MassiveError(RuntimeError):
    """Safe provider error whose text never contains a secret-bearing URL."""


@dataclass(frozen=True)
class TickerReference:
    symbol: str
    as_of: date
    active: bool | None
    market: str | None
    primary_exchange: str | None
    security_type: str | None
    currency_name: str | None
    market_cap: float | None
    share_class_shares_outstanding: float | None
    weighted_shares_outstanding: float | None
    sic_code: str | None
    sic_description: str | None
    last_updated_utc: datetime | None


def api_key() -> str | None:
    """Return the configured key, accepting the provider's legacy name."""
    value = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY")
    return value.strip() if value and value.strip() else None


def configured() -> bool:
    return api_key() is not None


def _canonical_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not value or value != symbol:
        raise ValueError("symbol must be canonical uppercase")
    return value


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_positive(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("provider price must be finite and positive")
    return parsed


def _finite_nonnegative(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("provider value must be finite and nonnegative")
    return parsed


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _request_json(
    endpoint: str,
    *,
    params: Mapping[str, object],
    session: requests.Session | None = None,
) -> dict:
    key = api_key()
    if key is None:
        raise MassiveError("MASSIVE_API_KEY is not configured")
    client = session or requests.Session()
    try:
        response = client.get(
            f"{BASE_URL}{endpoint}",
            params={**params, "apiKey": key},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise MassiveError(f"Massive request failed{suffix}") from exc
    except ValueError as exc:
        raise MassiveError("Massive returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {"OK", "DELAYED"}:
        status = payload.get("status") if isinstance(payload, dict) else None
        raise MassiveError(f"Massive response status was not usable: {status!r}")
    return payload


def fetch_intraday_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    session: requests.Session | None = None,
) -> Sequence[Bar]:
    """Fetch unadjusted five-minute bars for one exact UTC interval."""
    canonical = _canonical_symbol(symbol)
    start_utc = _aware_utc(start, "start")
    end_utc = _aware_utc(end, "end")
    if end_utc <= start_utc:
        raise ValueError("end must be later than start")
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    endpoint = AGGREGATES_ENDPOINT.format(
        ticker=quote(canonical, safe=""), start_ms=start_ms, end_ms=end_ms,
    )
    payload = _request_json(
        endpoint,
        params={"adjusted": "false", "sort": "asc", "limit": 50_000},
        session=session,
    )
    if payload.get("ticker") not in {None, canonical}:
        raise MassiveError("Massive response ticker did not match request")
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise MassiveError("Massive aggregate results were not a list")
    bars: list[Bar] = []
    seen: set[datetime] = set()
    prior: datetime | None = None
    for row in results:
        if not isinstance(row, dict):
            raise MassiveError("Massive aggregate row was not an object")
        try:
            ts = datetime.fromtimestamp(int(row["t"]) / 1000, tz=timezone.utc)
            bar = Bar(
                canonical, ts,
                _finite_positive(row["o"]),
                _finite_positive(row["h"]),
                _finite_positive(row["l"]),
                _finite_positive(row["c"]),
                int(_finite_nonnegative(row["v"])),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise MassiveError("Massive aggregate row was invalid") from exc
        if ts < start_utc or ts > end_utc:
            raise MassiveError("Massive aggregate timestamp was outside the request")
        if ts in seen:
            raise MassiveError("Massive aggregate timestamps were duplicated")
        if prior is not None and ts < prior:
            raise MassiveError("Massive aggregate timestamps were out of order")
        if not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
            raise MassiveError("Massive aggregate OHLC values were inconsistent")
        seen.add(ts)
        prior = ts
        bars.append(bar)
    return tuple(bars)


def fetch_ticker_reference(
    symbol: str,
    as_of: date,
    *,
    session: requests.Session | None = None,
) -> TickerReference:
    """Fetch the provider's dated ticker reference record."""
    canonical = _canonical_symbol(symbol)
    endpoint = TICKER_REFERENCE_ENDPOINT.format(ticker=quote(canonical, safe=""))
    payload = _request_json(endpoint, params={"date": as_of.isoformat()}, session=session)
    result = payload.get("results")
    if not isinstance(result, dict):
        raise MassiveError("Massive ticker reference result was missing")
    if result.get("ticker") not in {None, canonical}:
        raise MassiveError("Massive response ticker did not match request")
    updated = result.get("last_updated_utc")
    updated_at = None
    if updated:
        try:
            updated_at = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
            updated_at = _aware_utc(updated_at, "last_updated_utc")
        except ValueError as exc:
            raise MassiveError("Massive ticker reference timestamp was invalid") from exc
    return TickerReference(
        canonical,
        as_of,
        result.get("active") if isinstance(result.get("active"), bool) else None,
        str(result["market"]) if result.get("market") is not None else None,
        str(result["primary_exchange"]) if result.get("primary_exchange") is not None else None,
        str(result["type"]) if result.get("type") is not None else None,
        str(result["currency_name"]) if result.get("currency_name") is not None else None,
        _optional_number(result.get("market_cap")),
        _optional_number(result.get("share_class_shares_outstanding")),
        _optional_number(result.get("weighted_shares_outstanding")),
        str(result["sic_code"]) if result.get("sic_code") is not None else None,
        str(result["sic_description"]) if result.get("sic_description") is not None else None,
        updated_at,
    )
