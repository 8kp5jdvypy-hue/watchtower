"""Coinbase Exchange public ticker adapter -- the primary crypto price
source for tradebot.pricefeed (decision 2026-09-17, docs/DECISIONS.md).

Public endpoint, no key, no account: GET /products/{id}/ticker returns
the last trade for one product. Coinbase's documented public rate limit
is 10 requests/second per IP; a 5-minute poll of a handful of products
is three orders of magnitude under that. This module never imports a
Coinbase SDK (there isn't one in requirements.txt and none is wanted --
one plain GET is the whole integration).
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

import requests

from tradebot.marketdata import PricePoint

BASE_URL = "https://api.exchange.coinbase.com"
TICKER_ENDPOINT = "/products/{product_id}/ticker"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
SOURCE_NAME = "coinbase"


class CoinbaseError(RuntimeError):
    pass


def _finite_positive(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("provider price must be finite and positive")
    return parsed


_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$")


def _parse_time(value: Any) -> datetime:
    # Live-observed 2026-09-18: Coinbase stamps NANOSECOND precision
    # ("2026-09-18T16:16:38.309013277Z"), which datetime.fromisoformat
    # rejects on every Python version (max 6 fractional digits), and
    # the trailing Z is rejected on 3.9 (the dev Mac's system python).
    # Parse the shape explicitly and truncate to microseconds.
    match = _TIME_RE.match(str(value))
    if match is None:
        raise ValueError(f"unrecognised Coinbase time {value!r}")
    fraction = (match.group(2) or "0")[:6].ljust(6, "0")
    ts = datetime.fromisoformat(f"{match.group(1)}.{fraction}+00:00")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def fetch_ticker(
    product_id: str,
    *,
    symbol: str | None = None,
    session: requests.Session | None = None,
) -> PricePoint:
    """Last trade for one product ("BTC-USD"). `symbol` is the
    Perch-side name the PricePoint is labelled with (default: the
    product's base, "BTC")."""
    client = session or requests.Session()
    try:
        response = client.get(
            f"{BASE_URL}{TICKER_ENDPOINT.format(product_id=product_id)}",
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise CoinbaseError(f"Coinbase request failed for {product_id}{suffix}") from exc
    except ValueError as exc:
        raise CoinbaseError(f"Coinbase returned invalid JSON for {product_id}") from exc
    if not isinstance(payload, dict) or "price" not in payload or "time" not in payload:
        raise CoinbaseError(f"Coinbase ticker for {product_id} lacked price/time")
    try:
        price = _finite_positive(payload["price"])
        ts = _parse_time(payload["time"])
    except (TypeError, ValueError) as exc:
        raise CoinbaseError(f"Coinbase ticker for {product_id} was unparseable") from exc
    return PricePoint(
        symbol=symbol or product_id.split("-")[0],
        price=price,
        ts=ts,
        source=SOURCE_NAME,
        asset_class="crypto",
    )
