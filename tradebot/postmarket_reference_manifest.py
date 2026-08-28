"""Strict licensed point-in-time sector/float manifest ingestion.

Perch does not scrape or infer true sector membership.  An operator may ingest
a provider-authorized manifest whose publication and observation precede a
candidate.  Rows and manifests are append-only, digest-bound, and unavailable
until that explicit contract is satisfied.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REFERENCE_MANIFEST_VERSION = 1
MAX_ROWS = 20_000
CLASSIFICATION_SYSTEMS = {"GICS", "ICB", "PROVIDER_SECTOR"}
SECTOR_BENCHMARKS = {
    "XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
    "XLI", "XLB", "XLRE", "XLK", "XLU",
}
ROOT_FIELDS = {
    "schema_version", "status", "provider", "dataset", "license_reference",
    "effective_date", "published_at_utc", "created_at_utc",
    "classification_system", "rows",
}
ROW_FIELDS = {
    "symbol", "sector_code", "sector_name", "benchmark_symbol",
    "float_shares", "float_as_of_date",
}


REFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_reference_manifests (
    reference_manifest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_version INTEGER NOT NULL,
    provider TEXT NOT NULL,
    dataset TEXT NOT NULL,
    license_reference TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    published_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    classification_system TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status='locked')
);
CREATE INDEX IF NOT EXISTS idx_postmarket_reference_manifests_effective
    ON postmarket_reference_manifests(effective_date,observed_at_utc);
CREATE TABLE IF NOT EXISTS postmarket_reference_rows (
    reference_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_manifest_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    benchmark_symbol TEXT NOT NULL,
    float_shares REAL,
    float_as_of_date TEXT,
    UNIQUE(reference_manifest_id,symbol)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_reference_rows_symbol
    ON postmarket_reference_rows(symbol,reference_manifest_id);
CREATE TRIGGER IF NOT EXISTS postmarket_reference_manifests_no_update
BEFORE UPDATE ON postmarket_reference_manifests BEGIN
    SELECT RAISE(ABORT, 'postmarket_reference_manifests is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_reference_manifests_no_delete
BEFORE DELETE ON postmarket_reference_manifests BEGIN
    SELECT RAISE(ABORT, 'postmarket_reference_manifests is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_reference_rows_no_update
BEFORE UPDATE ON postmarket_reference_rows BEGIN
    SELECT RAISE(ABORT, 'postmarket_reference_rows is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_reference_rows_no_delete
BEFORE DELETE ON postmarket_reference_rows BEGIN
    SELECT RAISE(ABORT, 'postmarket_reference_rows is append-only');
END;
"""


@dataclass(frozen=True)
class ReferenceRow:
    symbol: str
    sector_code: str
    sector_name: str
    benchmark_symbol: str
    float_shares: float | None
    float_as_of_date: date | None


@dataclass(frozen=True)
class ReferenceManifest:
    provider: str
    dataset: str
    license_reference: str
    effective_date: date
    published_at_utc: datetime
    created_at_utc: datetime
    observed_at_utc: datetime
    classification_system: str
    manifest_sha256: str
    rows: tuple[ReferenceRow, ...]


@dataclass(frozen=True)
class CandidateReference:
    reference_manifest_id: int
    provider: str
    dataset: str
    license_reference: str
    effective_date: str
    published_at_utc: str
    source_observed_at_utc: str
    classification_system: str
    manifest_sha256: str
    symbol: str
    sector_code: str
    sector_name: str
    benchmark_symbol: str
    float_shares: float | None
    float_as_of_date: str | None


def ensure_reference_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(REFERENCE_SCHEMA)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _exact_fields(payload: Mapping[str, Any], expected: set[str], name: str) -> None:
    fields = set(payload)
    if fields != expected:
        raise ValueError(
            f"{name} fields did not match contract; missing={sorted(expected-fields)} "
            f"extra={sorted(fields-expected)}"
        )


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"reference manifest contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _timestamp(value: object, name: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(_nonempty(value, name)), name)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timezone-aware timestamp") from exc


def _date(value: object, name: str) -> date:
    try:
        return date.fromisoformat(_nonempty(value, name))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def parse_reference_manifest(raw: bytes, *, observed_at: datetime) -> ReferenceManifest:
    try:
        payload = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("reference manifest was not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("reference manifest root must be an object")
    _exact_fields(payload, ROOT_FIELDS, "reference manifest")
    if (
        not isinstance(payload["schema_version"], int)
        or isinstance(payload["schema_version"], bool)
        or payload["schema_version"] != REFERENCE_MANIFEST_VERSION
    ):
        raise ValueError("unsupported reference manifest schema_version")
    if payload["status"] != "locked":
        raise ValueError("reference manifest status must be locked")
    provider = _nonempty(payload["provider"], "provider")
    dataset = _nonempty(payload["dataset"], "dataset")
    license_reference = _nonempty(payload["license_reference"], "license_reference")
    if license_reference.lower() in {"unknown", "none", "unlicensed"}:
        raise ValueError("license_reference must identify an operator-reviewed grant")
    effective = _date(payload["effective_date"], "effective_date")
    published = _timestamp(payload["published_at_utc"], "published_at_utc")
    created = _timestamp(payload["created_at_utc"], "created_at_utc")
    observed = _utc(observed_at, "observed_at")
    if not published <= created <= observed:
        raise ValueError("manifest publication, creation, and observation were not causal")
    system = _nonempty(payload["classification_system"], "classification_system")
    if system not in CLASSIFICATION_SYSTEMS:
        raise ValueError(f"classification_system must be one of {sorted(CLASSIFICATION_SYSTEMS)}")
    raw_rows = payload["rows"]
    if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > MAX_ROWS:
        raise ValueError(f"rows must contain between 1 and {MAX_ROWS} entries")
    rows = []
    seen = set()
    for index, item in enumerate(raw_rows):
        if not isinstance(item, Mapping):
            raise ValueError(f"rows[{index}] must be an object")
        _exact_fields(item, ROW_FIELDS, f"rows[{index}]")
        symbol = _nonempty(item["symbol"], f"rows[{index}].symbol").upper()
        if symbol != item["symbol"] or symbol in seen:
            raise ValueError("reference row symbols must be unique canonical uppercase")
        seen.add(symbol)
        benchmark = _nonempty(
            item["benchmark_symbol"], f"rows[{index}].benchmark_symbol"
        ).upper()
        if benchmark != item["benchmark_symbol"] or benchmark not in SECTOR_BENCHMARKS:
            raise ValueError("benchmark_symbol must be a supported Select Sector ETF")
        float_shares = item["float_shares"]
        float_date = item["float_as_of_date"]
        if (float_shares is None) != (float_date is None):
            raise ValueError("float_shares and float_as_of_date must be supplied together")
        parsed_float = None
        parsed_float_date = None
        if float_shares is not None:
            if not isinstance(float_shares, (int, float)) or isinstance(float_shares, bool):
                raise ValueError("float_shares must be a JSON number")
            parsed_float = float(float_shares)
            if not math.isfinite(parsed_float) or parsed_float <= 0:
                raise ValueError("float_shares must be finite and positive")
            parsed_float_date = _date(float_date, f"rows[{index}].float_as_of_date")
            if parsed_float_date > effective:
                raise ValueError("float_as_of_date cannot follow manifest effective_date")
        rows.append(ReferenceRow(
            symbol, _nonempty(item["sector_code"], f"rows[{index}].sector_code"),
            _nonempty(item["sector_name"], f"rows[{index}].sector_name"),
            benchmark, parsed_float, parsed_float_date,
        ))
    return ReferenceManifest(
        provider, dataset, license_reference, effective, published, created, observed,
        system, hashlib.sha256(raw).hexdigest(), tuple(sorted(rows, key=lambda row: row.symbol)),
    )


def ingest_reference_manifest(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> tuple[int, bool, ReferenceManifest]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("reference manifest must be a regular non-symlink file")
    manifest = parse_reference_manifest(source.read_bytes(), observed_at=observed_at)
    ensure_reference_schema(conn)
    existing = conn.execute(
        "SELECT reference_manifest_id FROM postmarket_reference_manifests WHERE manifest_sha256=?",
        (manifest.manifest_sha256,),
    ).fetchone()
    if existing is not None:
        return int(existing[0]), False, manifest
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_reference_manifests
                (manifest_version,provider,dataset,license_reference,effective_date,
                 published_at_utc,created_at_utc,observed_at_utc,
                 classification_system,manifest_sha256,row_count,code_version,
                 run_id,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'locked')
            """,
            (
                REFERENCE_MANIFEST_VERSION, manifest.provider, manifest.dataset,
                manifest.license_reference, manifest.effective_date.isoformat(),
                manifest.published_at_utc.isoformat(), manifest.created_at_utc.isoformat(),
                manifest.observed_at_utc.isoformat(), manifest.classification_system,
                manifest.manifest_sha256, len(manifest.rows), code_version,
                _nonempty(run_id, "run_id"),
            ),
        )
        manifest_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO postmarket_reference_rows
                (reference_manifest_id,symbol,sector_code,sector_name,
                 benchmark_symbol,float_shares,float_as_of_date)
            VALUES (?,?,?,?,?,?,?)
            """,
            [(
                manifest_id, row.symbol, row.sector_code, row.sector_name,
                row.benchmark_symbol, row.float_shares,
                row.float_as_of_date.isoformat() if row.float_as_of_date else None,
            ) for row in manifest.rows],
        )
    return manifest_id, True, manifest


def candidate_reference(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    session: date,
    detected_at: datetime,
) -> CandidateReference | None:
    ensure_reference_schema(conn)
    detected = _utc(detected_at, "detected_at")
    canonical = symbol.strip().upper()
    if not canonical or canonical != symbol:
        raise ValueError("symbol must be canonical uppercase")
    row = conn.execute(
        """
        SELECT m.reference_manifest_id,m.provider,m.dataset,m.license_reference,
               m.effective_date,m.published_at_utc,m.observed_at_utc,
               m.classification_system,m.manifest_sha256,r.symbol,r.sector_code,
               r.sector_name,r.benchmark_symbol,r.float_shares,r.float_as_of_date
        FROM postmarket_reference_rows r
        JOIN postmarket_reference_manifests m
          ON m.reference_manifest_id=r.reference_manifest_id
        WHERE r.symbol=? AND m.effective_date<=?
          AND m.published_at_utc<=? AND m.observed_at_utc<=? AND m.status='locked'
        ORDER BY m.effective_date DESC,m.observed_at_utc DESC,m.reference_manifest_id DESC
        LIMIT 1
        """,
        (canonical, session.isoformat(), detected.isoformat(), detected.isoformat()),
    ).fetchone()
    return CandidateReference(*row) if row is not None else None
