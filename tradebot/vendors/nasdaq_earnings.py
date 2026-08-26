"""NASDAQ earnings calendar adapter — the only file allowed to talk to
this endpoint directly, same rule as vendors/alpaca.py and
vendors/sec_edgar.py.

This is an undocumented but widely-used public JSON endpoint, no API key.
It's the only free source found for forward-looking earnings dates — SEC
EDGAR only shows earnings AFTER they're filed (8-K Item 2.02), never
before (see sec_edgar.py). Being undocumented, it can change shape or
get rate-limited without notice. Failures therefore raise a typed error:
an empty calendar is a legitimate fact, while an unavailable calendar is
an operational failure. Conflating the two silently erased the catalyst
coverage needed to explain real earnings moves.

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


class EarningsCalendarFetchError(RuntimeError):
    """NASDAQ's calendar could not be fetched or validated."""


def fetch_earnings_calendar(report_date: date) -> list[EarningsEvent]:
    """Every symbol NASDAQ lists as reporting on ``report_date``.

    A real, successfully fetched empty calendar returns ``[]``. Network,
    HTTP, JSON, and response-shape failures raise
    :class:`EarningsCalendarFetchError` so the ingestion ledger can record
    FAILED rather than falsely claiming a successful zero-event day.
    """
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        resp = requests.get(
            NASDAQ_EARNINGS_URL, params={"date": report_date.isoformat()}, headers=headers, timeout=15
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise EarningsCalendarFetchError(
            f"NASDAQ earnings calendar fetch failed for {report_date.isoformat()}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise EarningsCalendarFetchError("NASDAQ earnings response is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise EarningsCalendarFetchError("NASDAQ earnings response data is not an object")
    else:
        rows = data.get("rows")
        if rows is None:
            rows = []
        elif not isinstance(rows, list):
            raise EarningsCalendarFetchError("NASDAQ earnings response rows is not a list")

    events = []
    for row in rows:
        if not isinstance(row, dict):
            raise EarningsCalendarFetchError("NASDAQ earnings response contains a non-object row")
        symbol = row.get("symbol")
        if not symbol:
            continue
        timing = _TIMING_MAP.get(row.get("time"), "unspecified")
        events.append(EarningsEvent(symbol=str(symbol).strip().upper(), report_date=report_date, timing=timing))
    return events


def fetch_earnings_for_symbols(report_date: date, symbols: set) -> list[EarningsEvent]:
    """Convenience filter over a caller-supplied eligible universe."""
    return [e for e in fetch_earnings_calendar(report_date) if e.symbol in symbols]
