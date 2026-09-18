"""Finnhub quote adapter -- the OPTIONAL second US-equity price source
for tradebot.pricefeed, behind Alpaca IEX (decision 2026-09-17,
docs/DECISIONS.md).

Optional means: this adapter is inert until FINNHUB_API_KEY is set.
tradebot.pricefeed only registers the Finnhub source when
`configured()` is True, so a deployment without a key simply has one
equity source (Alpaca) and no Finnhub calls are ever attempted. The
owner creates the Finnhub account and key himself (free tier, 60
requests/minute) -- nothing here does, and nothing here needs to.

The key is sent as the X-Finnhub-Token header, not the `token=` query
parameter Finnhub also accepts, so it can never land in a URL that a
log line, exception text, or proxy might record -- the same discipline
as vendors.massive's "never logs credential-bearing URLs".

Finnhub's /quote is one symbol per request (no batching), so the
pricefeed's equity fallback costs N requests for N symbols; at the
5-minute cadence that is still ~1.4 requests/minute for a 7-symbol
list against the 60/minute limit.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

import requests

from tradebot.marketdata import PricePoint

BASE_URL = "https://finnhub.io/api/v1"
QUOTE_ENDPOINT = "/quote"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
SOURCE_NAME = "finnhub"


class FinnhubError(RuntimeError):
    """Never carries the key or a key-bearing URL."""


def api_key() -> str | None:
    value = os.environ.get("FINNHUB_API_KEY")
    return value.strip() if value and value.strip() else None


def configured() -> bool:
    return api_key() is not None


def _finite_positive(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("provider price must be finite and positive")
    return parsed


def fetch_quote(symbol: str, *, session: requests.Session | None = None) -> PricePoint:
    """Current price for one US equity. Finnhub returns c=0, t=0 for a
    symbol it doesn't know rather than an HTTP error; that is raised
    here as FinnhubError so the pricefeed treats it as a miss."""
    key = api_key()
    if key is None:
        raise FinnhubError("FINNHUB_API_KEY is not configured")
    client = session or requests.Session()
    try:
        response = client.get(
            f"{BASE_URL}{QUOTE_ENDPOINT}",
            params={"symbol": symbol},
            headers={"X-Finnhub-Token": key},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise FinnhubError(f"Finnhub request failed for {symbol}{suffix}") from exc
    except ValueError as exc:
        raise FinnhubError(f"Finnhub returned invalid JSON for {symbol}") from exc
    if not isinstance(payload, dict):
        raise FinnhubError(f"Finnhub quote for {symbol} was not an object")
    try:
        price = _finite_positive(payload.get("c"))
        unix_ts = int(payload.get("t") or 0)
    except (TypeError, ValueError) as exc:
        raise FinnhubError(f"Finnhub has no usable quote for {symbol}") from exc
    if unix_ts <= 0:
        raise FinnhubError(f"Finnhub has no usable quote for {symbol}")
    return PricePoint(
        symbol=symbol,
        price=price,
        ts=datetime.fromtimestamp(unix_ts, tz=timezone.utc),
        source=SOURCE_NAME,
        asset_class="equity",
    )
