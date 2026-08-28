"""Nasdaq Trader regulatory-halt RSS adapter.

The feed covers Nasdaq-listed and other exchange-listed securities and is
published once per minute.  This adapter performs one bounded historical-date
request and returns structured halt records without scraping presentation pages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Sequence
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")
FEED_URL = "https://www.nasdaqtrader.com/rss.aspx"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15


class NasdaqHaltError(RuntimeError):
    pass


@dataclass(frozen=True)
class HaltRecord:
    symbol: str
    name: str
    market: str
    reason_code: str
    pause_threshold_price: str | None
    halted_at: datetime
    resume_quote_at: datetime | None
    resume_trade_at: datetime | None


class _TableCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag.lower() in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _event_time(date_text: str, time_text: str) -> datetime | None:
    raw_date = date_text.strip()
    raw_time = time_text.strip()
    if not raw_date or not raw_time:
        return None
    for time_format in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(
                f"{raw_date} {raw_time}", f"%m/%d/%Y {time_format}",
            ).replace(tzinfo=ET)
        except ValueError:
            continue
    raise NasdaqHaltError("Nasdaq halt feed contained an invalid date/time")


def _parse_item(description: str) -> HaltRecord:
    parser = _TableCells()
    parser.feed(description)
    rows = [row for row in parser.rows if len(row) >= 10]
    if not rows:
        raise NasdaqHaltError("Nasdaq halt feed item did not contain ten fields")
    values = rows[-1]
    halted_at = _event_time(values[5], values[6])
    if halted_at is None:
        raise NasdaqHaltError("Nasdaq halt feed item omitted halt time")
    symbol = values[0].strip().upper()
    if not symbol:
        raise NasdaqHaltError("Nasdaq halt feed item omitted symbol")
    return HaltRecord(
        symbol=symbol,
        name=values[1],
        market=values[2],
        reason_code=values[3],
        pause_threshold_price=values[4] or None,
        halted_at=halted_at,
        resume_quote_at=_event_time(values[7], values[8]),
        resume_trade_at=_event_time(values[7], values[9]),
    )


def parse_feed(content: bytes) -> Sequence[HaltRecord]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise NasdaqHaltError("Nasdaq halt feed was invalid XML") from exc
    records = []
    for item in root.findall(".//item"):
        description = item.findtext("description")
        if description is None:
            raise NasdaqHaltError("Nasdaq halt feed item omitted description")
        records.append(_parse_item(description))
    records.sort(key=lambda row: (row.halted_at, row.symbol, row.reason_code))
    return tuple(records)


def fetch_halts(
    session_date: date,
    *,
    session: requests.Session | None = None,
) -> Sequence[HaltRecord]:
    """Fetch halts begun or resumed on a date using one provider request."""
    client = session or requests.Session()
    value = session_date.strftime("%m%d%Y")
    try:
        response = client.get(
            FEED_URL,
            params={"feed": "tradehalts", "haltdate": value, "resumedate": value},
            headers={"User-Agent": "Perch market-integrity audit/1.0"},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise NasdaqHaltError(f"Nasdaq halt feed request failed{suffix}") from exc
    return parse_feed(response.content)
