"""Streaming Massive full-market SIP minute-aggregate adapter.

One next-day S3 object replaces thousands of per-symbol REST requests.  The
reader retains only a bounded session window and active-universe symbols,
resamples observed one-minute aggregates into exact five-minute buckets, and
never fills an interval with no qualifying trade aggregate.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import BinaryIO, Mapping, Sequence
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from tradebot.detectors import Bar


ET = ZoneInfo("America/New_York")
ENDPOINT_URL = "https://files.massive.com"
BUCKET = "flatfiles"
DATASET = "us_stocks_sip/minute_aggs_v1"
EXPECTED_COLUMNS = {
    "ticker", "volume", "open", "close", "high", "low",
    "window_start", "transactions",
}


class MassiveFlatFileError(RuntimeError):
    """Credential-safe bulk-provider error."""


@dataclass(frozen=True)
class FlatFileSnapshot:
    session: date
    object_key: str
    object_etag: str | None
    object_last_modified_utc: str | None
    object_bytes: int | None
    selected_rows_sha256: str
    rows_read: int
    selected_rows: int
    selected_symbols: int
    bars_by_symbol: Mapping[str, tuple[Bar, ...]]


def access_key_id() -> str | None:
    value = os.environ.get("MASSIVE_S3_ACCESS_KEY_ID")
    return value.strip() if value and value.strip() else None


def secret_access_key() -> str | None:
    value = os.environ.get("MASSIVE_S3_SECRET_ACCESS_KEY")
    return value.strip() if value and value.strip() else None


def configured() -> bool:
    return access_key_id() is not None and secret_access_key() is not None


def object_key(session: date) -> str:
    return (
        f"{DATASET}/{session:%Y}/{session:%m}/{session.isoformat()}.csv.gz"
    )


def expected_available_at(session: date) -> datetime:
    """Documented approximate 11 ET publication plus a two-hour safety lag."""
    next_day = date.fromordinal(session.toordinal() + 1)
    return datetime.combine(next_day, time(13, 5), tzinfo=ET).astimezone(timezone.utc)


def _client():
    key = access_key_id()
    secret = secret_access_key()
    if key is None or secret is None:
        raise MassiveFlatFileError("Massive flat-file S3 credentials are not configured")
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("flat-file price must be finite and positive")
    return parsed


def _finite_nonnegative(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("flat-file value must be finite and nonnegative")
    return parsed


def _parse_window_start(value: str) -> datetime:
    raw = int(value)
    # The documented sample uses Unix nanoseconds. Refuse ambiguous magnitudes.
    if raw < 1_000_000_000_000_000_000 or raw >= 10_000_000_000_000_000_000:
        raise ValueError("window_start must be a 19-digit Unix-nanosecond timestamp")
    seconds, nanoseconds = divmod(raw, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        microseconds=nanoseconds // 1_000
    )


def parse_minute_aggregates(
    source: BinaryIO,
    *,
    session: date,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    object_key_value: str,
    object_etag: str | None = None,
    object_last_modified: datetime | None = None,
    object_bytes: int | None = None,
) -> FlatFileSnapshot:
    start_utc = _aware_utc(start, "start")
    end_utc = _aware_utc(end, "end")
    if end_utc <= start_utc:
        raise ValueError("end must be later than start")
    wanted = frozenset(symbols)
    if not wanted or any(not value or value != value.strip().upper() for value in wanted):
        raise ValueError("symbols must be canonical uppercase")
    rows_read = selected_rows = 0
    digest = hashlib.sha256()
    aggregates: dict[tuple[str, datetime], dict] = {}
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                reader = csv.DictReader(text)
                if reader.fieldnames is None or not EXPECTED_COLUMNS <= set(reader.fieldnames):
                    raise MassiveFlatFileError("Massive minute file schema did not match contract")
                for row in reader:
                    rows_read += 1
                    symbol = row["ticker"].strip().upper()
                    if symbol not in wanted:
                        continue
                    try:
                        ts = _parse_window_start(row["window_start"])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise MassiveFlatFileError("Massive minute timestamp was invalid") from exc
                    if ts < start_utc or ts >= end_utc:
                        continue
                    try:
                        minute_open = _finite_positive(row["open"])
                        minute_close = _finite_positive(row["close"])
                        minute_high = _finite_positive(row["high"])
                        minute_low = _finite_positive(row["low"])
                        minute_volume = int(_finite_nonnegative(row["volume"]))
                    except (TypeError, ValueError) as exc:
                        raise MassiveFlatFileError("Massive minute OHLCV row was invalid") from exc
                    if not (
                        minute_low <= min(minute_open, minute_close)
                        <= max(minute_open, minute_close) <= minute_high
                    ):
                        raise MassiveFlatFileError("Massive minute OHLC values were inconsistent")
                    bucket_epoch = int(ts.timestamp()) // 300 * 300
                    bucket = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
                    minute_index = int((ts - bucket).total_seconds() // 60)
                    minute_bit = 1 << minute_index
                    key = (symbol, bucket)
                    current = aggregates.get(key)
                    if current is None:
                        aggregates[key] = {
                            "open": minute_open, "high": minute_high, "low": minute_low,
                            "close": minute_close, "volume": minute_volume,
                            "first_ts": ts, "last_ts": ts, "minute_mask": minute_bit,
                        }
                    else:
                        if current["minute_mask"] & minute_bit:
                            raise MassiveFlatFileError(
                                "Massive minute timestamps were duplicated"
                            )
                        current["high"] = max(current["high"], minute_high)
                        current["low"] = min(current["low"], minute_low)
                        current["volume"] += minute_volume
                        current["minute_mask"] |= minute_bit
                        if ts < current["first_ts"]:
                            current["first_ts"] = ts
                            current["open"] = minute_open
                        if ts > current["last_ts"]:
                            current["last_ts"] = ts
                            current["close"] = minute_close
                    selected_rows += 1
                    digest.update(
                        (
                            f"{symbol},{row['volume']},{row['open']},{row['close']},"
                            f"{row['high']},{row['low']},{row['window_start']},"
                            f"{row['transactions']}\n"
                        ).encode()
                    )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise MassiveFlatFileError("Massive minute file could not be decoded") from exc
    bars: dict[str, list[Bar]] = {}
    for (symbol, ts), values in sorted(aggregates.items()):
        bars.setdefault(symbol, []).append(Bar(
            symbol, ts, values["open"], values["high"], values["low"],
            values["close"], values["volume"],
        ))
    updated = None
    if object_last_modified is not None:
        updated = _aware_utc(object_last_modified, "object_last_modified").isoformat()
    return FlatFileSnapshot(
        session=session,
        object_key=object_key_value,
        object_etag=object_etag.strip('"') if object_etag else None,
        object_last_modified_utc=updated,
        object_bytes=object_bytes,
        selected_rows_sha256=digest.hexdigest(),
        rows_read=rows_read,
        selected_rows=selected_rows,
        selected_symbols=len(bars),
        bars_by_symbol={symbol: tuple(values) for symbol, values in bars.items()},
    )


def fetch_minute_aggregates(
    session_date: date,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    client=None,
) -> FlatFileSnapshot:
    key = object_key(session_date)
    s3 = client or _client()
    try:
        response = s3.get_object(Bucket=BUCKET, Key=key)
        body = response["Body"]
    except (BotoCoreError, ClientError, KeyError) as exc:
        code = None
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code")
        suffix = f" code={code}" if code else ""
        raise MassiveFlatFileError(f"Massive flat-file request failed{suffix}") from exc
    try:
        return parse_minute_aggregates(
            body,
            session=session_date,
            symbols=symbols,
            start=start,
            end=end,
            object_key_value=key,
            object_etag=response.get("ETag"),
            object_last_modified=response.get("LastModified"),
            object_bytes=response.get("ContentLength"),
        )
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
