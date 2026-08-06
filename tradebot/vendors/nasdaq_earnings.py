"""NASDAQ earnings calendar adapter — the only file allowed to talk to
this endpoint directly, same rule as vendors/alpaca.py and
vendors/sec_edgar.py.

This is an undocumented but widely-used public JSON endpoint, no API key.
It's the only free source found for forward-looking earnings dates — SEC
EDGAR only shows earnings AFTER they're filed (8-K Item 2.02), never
before (see sec_edgar.py). Being undocumented, it can change shape or
get rate-limited without notice, so every function here degrades to []
rather than raise — same discipline as sec_edgar.py's fetch_filings().

Verified against a live response before writing this (2026-08-06): when
a date has nothing scheduled, `data.rows` comes back as JSON null, not
an empty list — handled explicitly below. Don't assume the shape without
checking; that null was the kind of thing that looks fine until the
first weekend date crashes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import requests

NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

_TIMING_MAP = {"time-pre-market": "pre-market", "time-after-hours": "after-hours"}


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    report_date: date
    timing: str  # "pre-market" | "after-hours" | "unspecified"


def fetch_earnings_calendar(report_date: date) -> list[EarningsEvent]:
    """Every symbol NASDAQ lists as reporting on report_date. Returns []
    on any failure (network, non-2xx, unexpected shape) — a missed
    earnings date means no blackout window gets created for it, the same
    fail-safe direction as every other vendor adapter in this project."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        resp = requests.get(
            NASDAQ_EARNINGS_URL, params={"date": report_date.isoformat()}, headers=headers, timeout=15
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return []

    rows = (payload.get("data") or {}).get("rows") or []
    events = []
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        timing = _TIMING_MAP.get(row.get("time"), "unspecified")
        events.append(EarningsEvent(symbol=symbol, report_date=report_date, timing=timing))
    return events


def fetch_earnings_for_symbols(report_date: date, symbols: set) -> list[EarningsEvent]:
    """Convenience filter down to just the watchlist — NASDAQ's calendar
    for a busy day covers hundreds of tickers we don't track."""
    return [e for e in fetch_earnings_calendar(report_date) if e.symbol in symbols]
