"""Massive flat-file adapter tests are fully synthetic and offline."""
from __future__ import annotations

import gzip
import io
from datetime import date, datetime, timezone

import pytest

from tradebot.vendors import massive_flatfiles as flatfiles


SESSION = date(2026, 8, 27)
START = datetime(2026, 8, 27, 19, 55, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
HEADER = "ticker,volume,open,close,high,low,window_start,transactions\n"


def _ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000


def _archive(rows):
    raw = HEADER + "".join(
        f"{symbol},{volume},{open_},{close},{high},{low},{_ns(ts)},{transactions}\n"
        for symbol, volume, open_, close, high, low, ts, transactions in rows
    )
    return io.BytesIO(gzip.compress(raw.encode()))


def test_configuration_requires_dedicated_access_and_secret(monkeypatch):
    monkeypatch.delenv("MASSIVE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("MASSIVE_S3_SECRET_ACCESS_KEY", raising=False)
    assert flatfiles.configured() is False
    monkeypatch.setenv("MASSIVE_S3_ACCESS_KEY_ID", "access")
    assert flatfiles.configured() is False
    monkeypatch.setenv("MASSIVE_S3_SECRET_ACCESS_KEY", "secret")
    assert flatfiles.configured() is True


def test_object_key_and_next_day_availability_are_deterministic():
    assert flatfiles.object_key(SESSION) == (
        "us_stocks_sip/minute_aggs_v1/2026/08/2026-08-27.csv.gz"
    )
    assert flatfiles.expected_available_at(SESSION).isoformat() == (
        "2026-08-28T17:05:00+00:00"
    )


def test_stream_parser_filters_and_resamples_without_filling_missing_minutes():
    minute_55 = datetime(2026, 8, 27, 19, 55, tzinfo=timezone.utc)
    minute_57 = datetime(2026, 8, 27, 19, 57, tzinfo=timezone.utc)
    minute_00 = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
    # Deliberately reverse two ABC rows to prove source ordering is not assumed.
    source = _archive([
        ("ABC", 20, 101, 102, 103, 100, minute_57, 2),
        ("XYZ", 99, 5, 6, 7, 4, minute_55, 3),
        ("ABC", 10, 100, 101, 102, 99, minute_55, 1),
        ("ABC", 30, 102, 104, 105, 101, minute_00, 4),
    ])
    snapshot = flatfiles.parse_minute_aggregates(
        source, session=SESSION, symbols=("ABC",), start=START, end=END,
        object_key_value=flatfiles.object_key(SESSION),
    )
    bars = snapshot.bars_by_symbol["ABC"]
    assert len(bars) == 2
    assert bars[0].ts == minute_55
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (
        100, 103, 99, 102, 30,
    )
    assert bars[1].ts == minute_00
    assert snapshot.rows_read == 4
    assert snapshot.selected_rows == 3
    assert snapshot.selected_symbols == 1
    assert len(snapshot.selected_rows_sha256) == 64


def test_duplicate_minute_fails_visible():
    minute = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
    row = ("ABC", 10, 100, 101, 102, 99, minute, 1)
    with pytest.raises(flatfiles.MassiveFlatFileError, match="duplicated"):
        flatfiles.parse_minute_aggregates(
            _archive([row, row]), session=SESSION, symbols=("ABC",),
            start=START, end=END, object_key_value="key",
        )


class FakeS3:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Body": self.body,
            "ETag": '"etag-value"',
            "LastModified": datetime(2026, 8, 28, 15, tzinfo=timezone.utc),
            "ContentLength": 123,
        }


def test_fetch_uses_one_exact_object_and_records_object_provenance():
    minute = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
    body = _archive([("ABC", 10, 100, 101, 102, 99, minute, 1)])
    client = FakeS3(body)
    snapshot = flatfiles.fetch_minute_aggregates(
        SESSION, symbols=("ABC",), start=START, end=END, client=client,
    )
    assert client.calls == [{
        "Bucket": "flatfiles", "Key": flatfiles.object_key(SESSION),
    }]
    assert snapshot.object_etag == "etag-value"
    assert snapshot.object_bytes == 123
    assert snapshot.object_last_modified_utc == "2026-08-28T15:00:00+00:00"
    assert body.closed is True
