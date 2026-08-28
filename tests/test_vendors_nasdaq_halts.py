"""Nasdaq Trader halt-feed tests use synthetic RSS only."""
from __future__ import annotations

from datetime import date

import pytest
import requests

from tradebot.vendors import nasdaq_halts


DESCRIPTION = """
<table><tr><th>Symbol</th></tr><tr>
<td>ABC</td><td>ABC Corp</td><td>Q</td><td>LUDP</td><td>10.00</td>
<td>08/27/2026</td><td>16:05:01</td><td>08/27/2026</td>
<td>16:10:00</td><td>16:15:00</td></tr></table>
"""
RSS = f"""<?xml version="1.0"?><rss><channel><item>
<description><![CDATA[{DESCRIPTION}]]></description>
</item></channel></rss>""".encode()


class FakeResponse:
    def __init__(self, content=RSS, status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)


class FakeSession:
    def __init__(self, content=RSS, status_code=200):
        self.response = FakeResponse(content, status_code)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_parse_feed_returns_structured_timezone_aware_record():
    records = nasdaq_halts.parse_feed(RSS)
    assert len(records) == 1
    record = records[0]
    assert record.symbol == "ABC"
    assert record.reason_code == "LUDP"
    assert record.halted_at.isoformat() == "2026-08-27T16:05:01-04:00"
    assert record.resume_trade_at.isoformat() == "2026-08-27T16:15:00-04:00"


def test_fetch_halts_uses_one_bounded_union_date_query():
    client = FakeSession()
    records = nasdaq_halts.fetch_halts(date(2026, 8, 27), session=client)
    assert len(records) == 1
    url, kwargs = client.calls[0]
    assert url == "https://www.nasdaqtrader.com/rss.aspx"
    assert kwargs["params"] == {
        "feed": "tradehalts", "haltdate": "08272026", "resumedate": "08272026",
    }
    assert kwargs["timeout"] == (5, 15)


def test_invalid_feed_fails_visible_instead_of_becoming_no_halts():
    with pytest.raises(nasdaq_halts.NasdaqHaltError, match="invalid XML"):
        nasdaq_halts.parse_feed(b"not xml")


def test_malformed_item_fails_visible_instead_of_becoming_no_halts():
    malformed = b"<rss><channel><item><description>no table</description></item></channel></rss>"
    with pytest.raises(nasdaq_halts.NasdaqHaltError, match="ten fields"):
        nasdaq_halts.parse_feed(malformed)
