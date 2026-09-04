"""Build a strict licensed-reference manifest from a provider CSV export.

This module is deliberately offline and provider-neutral.  It normalizes an
operator-reviewed export into the exact manifest consumed by
``postmarket_reference_manifest``; it does not establish that Perch has a
license and it never infers sector membership or benchmark mappings.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from tradebot.postmarket_reference_manifest import (
    CLASSIFICATION_SYSTEMS,
    MAX_ROWS,
    REFERENCE_MANIFEST_VERSION,
    ReferenceManifest,
    parse_reference_manifest,
)


CSV_FIELDS = (
    "symbol",
    "sector_code",
    "sector_name",
    "benchmark_symbol",
    "float_shares",
    "float_as_of_date",
)


@dataclass(frozen=True)
class BuiltReferenceManifest:
    """Canonical manifest bytes plus their validated representation."""

    raw: bytes
    manifest: ReferenceManifest


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonempty(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _csv_rows(source: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError("provider export must be a regular non-symlink file") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("provider export must be a regular non-symlink file")
    with io.TextIOWrapper(
        os.fdopen(descriptor, "rb"), encoding="utf-8", newline=""
    ) as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames
        if fields is None:
            raise ValueError("provider export must contain a CSV header")
        if len(fields) != len(set(fields)):
            raise ValueError("provider export contains duplicate CSV headers")
        if tuple(fields) != CSV_FIELDS:
            raise ValueError(
                "provider export header must exactly equal " + ",".join(CSV_FIELDS)
            )
        for line_number, record in enumerate(reader, start=2):
            if None in record or set(record) != set(CSV_FIELDS):
                raise ValueError(
                    f"provider export row {line_number} has the wrong field count"
                )
            values = {
                key: value.strip() if value is not None else None
                for key, value in record.items()
            }
            symbol = _nonempty(values["symbol"], f"row {line_number} symbol").upper()
            if symbol != values["symbol"]:
                raise ValueError(f"row {line_number} symbol must be canonical uppercase")
            if symbol in seen:
                raise ValueError(f"provider export contains duplicate symbol {symbol}")
            seen.add(symbol)
            float_text = values["float_shares"]
            float_date_text = values["float_as_of_date"]
            if bool(float_text) != bool(float_date_text):
                raise ValueError(
                    f"row {line_number} float_shares and float_as_of_date "
                    "must be supplied together"
                )
            float_shares: float | None = None
            float_as_of_date: str | None = None
            if float_text:
                try:
                    float_shares = float(float_text)
                except ValueError as exc:
                    raise ValueError(
                        f"row {line_number} float_shares must be numeric"
                    ) from exc
                if not math.isfinite(float_shares) or float_shares <= 0:
                    raise ValueError(
                        f"row {line_number} float_shares must be finite and positive"
                    )
                try:
                    float_as_of_date = date.fromisoformat(str(float_date_text)).isoformat()
                except ValueError as exc:
                    raise ValueError(
                        f"row {line_number} float_as_of_date must be an ISO date"
                    ) from exc
            rows.append(
                {
                    "symbol": symbol,
                    "sector_code": _nonempty(
                        values["sector_code"], f"row {line_number} sector_code"
                    ),
                    "sector_name": _nonempty(
                        values["sector_name"], f"row {line_number} sector_name"
                    ),
                    "benchmark_symbol": _nonempty(
                        values["benchmark_symbol"],
                        f"row {line_number} benchmark_symbol",
                    ).upper(),
                    "float_shares": float_shares,
                    "float_as_of_date": float_as_of_date,
                }
            )
            if len(rows) > MAX_ROWS:
                raise ValueError(f"provider export cannot exceed {MAX_ROWS} rows")
    if not rows:
        raise ValueError("provider export must contain at least one row")
    return sorted(rows, key=lambda row: str(row["symbol"]))


def build_reference_manifest(
    source: Path | str,
    *,
    provider: str,
    dataset: str,
    license_reference: str,
    effective_date: date,
    published_at_utc: datetime,
    created_at_utc: datetime,
    classification_system: str,
) -> BuiltReferenceManifest:
    """Return canonical, parser-validated manifest bytes for ``source``."""

    created = _utc(created_at_utc, "created_at_utc")
    published = _utc(published_at_utc, "published_at_utc")
    system = classification_system.strip().upper()
    if system not in CLASSIFICATION_SYSTEMS:
        raise ValueError(
            f"classification_system must be one of {sorted(CLASSIFICATION_SYSTEMS)}"
        )
    payload = {
        "schema_version": REFERENCE_MANIFEST_VERSION,
        "status": "locked",
        "provider": provider.strip(),
        "dataset": dataset.strip(),
        "license_reference": license_reference.strip(),
        "effective_date": effective_date.isoformat(),
        "published_at_utc": published.isoformat(),
        "created_at_utc": created.isoformat(),
        "classification_system": system,
        "rows": _csv_rows(Path(source)),
    }
    raw = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    manifest = parse_reference_manifest(raw, observed_at=created)
    return BuiltReferenceManifest(raw=raw, manifest=manifest)


def write_reference_manifest_exclusive(
    path: Path | str,
    built: BuiltReferenceManifest,
) -> None:
    """Publish ``built`` read-only without replacing any existing path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"reference manifest already exists: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(built.raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        os.link(temporary, destination, follow_symlinks=False)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
