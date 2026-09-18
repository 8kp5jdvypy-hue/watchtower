"""Kraken public ticker adapter -- the second crypto price source for
tradebot.pricefeed, used only when Coinbase fails (decision
2026-09-17, docs/DECISIONS.md).

Public endpoint, no key: GET /0/public/Ticker?pair=XBTUSD,ETHUSD
returns all requested pairs in one call. Two Kraken quirks this module
absorbs so callers never see them:

- Response keys are Kraken's internal names ("XXBTZUSD" for XBTUSD,
  "XETHZUSD" for ETHUSD, but plain "SOLUSD" for SOLUSD), not the
  names you asked with. We map results back by position-independent
  matching on the `altname`-style request pair, falling back to a
  suffix match, and raise if a requested pair can't be attributed.
- The ticker carries no trade timestamp. The PricePoint's `ts` is the
  fetch time and `ts_is_fetch_time=True` says so, per the PricePoint
  contract -- a consumer that needs the real trade time must use
  Coinbase.

Public rate limit is a per-IP counter of ~1 call/second sustained;
one batched call per poll is well inside it.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import requests

from tradebot.marketdata import PricePoint

BASE_URL = "https://api.kraken.com"
TICKER_ENDPOINT = "/0/public/Ticker"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
SOURCE_NAME = "kraken"

# Kraken's legacy asset prefixes: X for crypto, Z for fiat. Newer
# listings (SOL, and anything added after ~2019) have no prefix.
_LEGACY_INTERNAL = {"XBTUSD": "XXBTZUSD", "ETHUSD": "XETHZUSD", "LTCUSD": "XLTCZUSD",
                    "XRPUSD": "XXRPZUSD", "XDGUSD": "XXDGZUSD"}


class KrakenError(RuntimeError):
    pass


def _finite_positive(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("provider price must be finite and positive")
    return parsed


def _attribute(result: Mapping[str, Any], pair: str) -> Mapping[str, Any] | None:
    if pair in result:
        return result[pair]
    legacy = _LEGACY_INTERNAL.get(pair)
    if legacy and legacy in result:
        return result[legacy]
    # Last resort: exactly one key that ends with the pair's quote and
    # contains its base (e.g. a new prefix scheme we haven't seen).
    base, quote = pair[:-3], pair[-3:]
    candidates = [k for k in result if k.endswith(quote) and base in k]
    return result[candidates[0]] if len(candidates) == 1 else None


def fetch_tickers(
    pairs: Sequence[tuple[str, str]],
    *,
    session: requests.Session | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, PricePoint]:
    """`pairs` is [(perch_symbol, kraken_pair)], e.g. [("BTC", "XBTUSD")].
    Returns one PricePoint per pair Kraken answered for, keyed by
    perch_symbol; a pair Kraken omitted is absent, never fabricated."""
    if not pairs:
        return {}
    client = session or requests.Session()
    try:
        response = client.get(
            f"{BASE_URL}{TICKER_ENDPOINT}",
            params={"pair": ",".join(p for _, p in pairs)},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise KrakenError(f"Kraken request failed{suffix}") from exc
    except ValueError as exc:
        raise KrakenError("Kraken returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise KrakenError("Kraken response was not an object")
    errors = payload.get("error") or []
    if errors:
        raise KrakenError(f"Kraken reported errors: {', '.join(map(str, errors))}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise KrakenError("Kraken response lacked result")

    fetched_at = now_fn()
    out: dict[str, PricePoint] = {}
    for symbol, pair in pairs:
        entry = _attribute(result, pair)
        if entry is None:
            continue
        try:
            # "c": [<last trade price>, <lot volume>]
            price = _finite_positive(entry["c"][0])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise KrakenError(f"Kraken ticker for {pair} was unparseable") from exc
        out[symbol] = PricePoint(
            symbol=symbol,
            price=price,
            ts=fetched_at,
            source=SOURCE_NAME,
            asset_class="crypto",
            ts_is_fetch_time=True,
        )
    return out
