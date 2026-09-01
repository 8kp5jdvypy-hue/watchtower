"""Read-only, fail-closed reconstruction of one symbol's signal path.

The operator question this module answers is deliberately narrower than
"did Perch catch it?".  That phrase can mean provider admission, an evaluated
bar, a qualified shadow candidate, an owner-only outbox row, or customer
delivery.  The reconstruction preserves those stages separately and refuses
to turn missing evidence into a fabricated caught/missed verdict.

Every SQLite source is opened with ``mode=ro`` and ``query_only`` inside a
read transaction.  Live databases are identified by path, size, mtime and
SQLite transaction metadata; mutable files are not hashed as though a hash
were an atomic cross-database snapshot.  Verified immutable Stage-1 archives
retain their filename SHA-256 identity.

This module has no provider, Telegram, broker, order, or delivery dependency.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import exchange_calendars as ecals

from tradebot.screening_archive import verify_screening_archive


RECONSTRUCTION_VERSION = 1
MAX_STAGE_ROWS = 10_000
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_ARCHIVE_DIR = DEFAULT_DATA_DIR / "screening_archives"
CALENDAR = ecals.get_calendar("XNYS")

STATUS_PRESENT = "PRESENT"
STATUS_ABSENT = "ABSENT"
STATUS_DATABASE_MISSING = "DATABASE_MISSING"
STATUS_DATABASE_UNSAFE = "DATABASE_UNSAFE"
STATUS_DATABASE_ERROR = "DATABASE_ERROR"
STATUS_TABLE_MISSING = "TABLE_MISSING"
STATUS_SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"
STATUS_QUERY_ERROR = "QUERY_ERROR"
STATUS_PRESENT_WITH_ISSUES = "PRESENT_WITH_DATA_QUALITY_ISSUES"
STATUS_ARCHIVE_MISSING = "ARCHIVE_MISSING"
STATUS_ARCHIVE_CORRUPT = "ARCHIVE_CORRUPT"
STATUS_ARCHIVE_AMBIGUOUS = "ARCHIVE_AMBIGUOUS"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,31}$")


@dataclass(frozen=True)
class ReconstructionPaths:
    universe: Path
    journal: Path
    evaluations: Path
    postmarket: Path
    users: Path
    screening_archives: Path

    @classmethod
    def from_data_directory(
        cls,
        data_directory: Path | str,
        *,
        screening_archives: Path | str | None = None,
    ) -> "ReconstructionPaths":
        root = Path(data_directory)
        return cls(
            universe=root / "universe.db",
            journal=root / "journal.db",
            evaluations=root / "evaluations.db",
            postmarket=root / "postmarket_shadow.db",
            users=root / "users.db",
            screening_archives=(
                Path(screening_archives)
                if screening_archives is not None
                else root / "screening_archives"
            ),
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("symbol must be a canonical US-market symbol")
    return symbol


def _normalize_session(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("session must be an ISO date") from exc


def _strict_json(value: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=unique_object)


def _aware_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is missing or not text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is timezone-naive")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp is not stored in UTC")
    return parsed.astimezone(timezone.utc)


def _row_identity(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in fields)


def _assess_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    timestamp_fields: Sequence[str] = (),
    optional_timestamp_fields: Sequence[str] = (),
    json_fields: Sequence[str] = (),
    optional_json_fields: Sequence[str] = (),
    identity_fields: Sequence[str] = (),
    ordered_timestamp_field: str | None = None,
) -> dict[str, Any]:
    """Return reproducible missing/duplicate/order/time/JSON diagnostics."""
    issues: list[dict[str, Any]] = []
    parsed_order: list[tuple[int, datetime]] = []
    for index, row in enumerate(rows):
        for field in timestamp_fields:
            try:
                parsed = _aware_utc(row.get(field))
            except (TypeError, ValueError) as exc:
                issues.append(
                    {
                        "code": "INVALID_REQUIRED_UTC_TIMESTAMP",
                        "row_index": index,
                        "field": field,
                        "detail": str(exc),
                    }
                )
            else:
                if field == ordered_timestamp_field:
                    parsed_order.append((index, parsed))
        for field in optional_timestamp_fields:
            value = row.get(field)
            if value is None or value == "":
                continue
            try:
                parsed = _aware_utc(value)
            except (TypeError, ValueError) as exc:
                issues.append(
                    {
                        "code": "INVALID_OPTIONAL_UTC_TIMESTAMP",
                        "row_index": index,
                        "field": field,
                        "detail": str(exc),
                    }
                )
            else:
                if field == ordered_timestamp_field:
                    parsed_order.append((index, parsed))
        for field in json_fields:
            value = row.get(field)
            if not isinstance(value, str):
                issues.append(
                    {
                        "code": "INVALID_JSON_TYPE",
                        "row_index": index,
                        "field": field,
                    }
                )
                continue
            try:
                _strict_json(value)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                issues.append(
                    {
                        "code": "INVALID_JSON",
                        "row_index": index,
                        "field": field,
                        "detail": str(exc),
                    }
                )
        for field in optional_json_fields:
            value = row.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                issues.append(
                    {
                        "code": "INVALID_OPTIONAL_JSON_TYPE",
                        "row_index": index,
                        "field": field,
                    }
                )
                continue
            try:
                _strict_json(value)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                issues.append(
                    {
                        "code": "INVALID_OPTIONAL_JSON",
                        "row_index": index,
                        "field": field,
                        "detail": str(exc),
                    }
                )

    duplicate_identities: list[dict[str, Any]] = []
    if identity_fields:
        locations: defaultdict[tuple[Any, ...], list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            locations[_row_identity(row, identity_fields)].append(index)
        duplicate_identities = [
            {"identity": list(identity), "row_indices": indices}
            for identity, indices in locations.items()
            if len(indices) > 1
        ]
        for duplicate in duplicate_identities:
            issues.append(
                {
                    "code": "DUPLICATE_IDENTITY",
                    "identity_fields": list(identity_fields),
                    **duplicate,
                }
            )

    out_of_order: list[dict[str, Any]] = []
    for (left_index, left), (right_index, right) in zip(
        parsed_order, parsed_order[1:]
    ):
        if right < left:
            item = {
                "code": "OUT_OF_ORDER_TIMESTAMP",
                "field": ordered_timestamp_field,
                "prior_row_index": left_index,
                "row_index": right_index,
                "prior": left.isoformat(),
                "current": right.isoformat(),
            }
            out_of_order.append(item)
            issues.append(item)

    return {
        "row_count": len(rows),
        "issue_count": len(issues),
        "duplicate_identity_count": len(duplicate_identities),
        "out_of_order_count": len(out_of_order),
        "issues": issues,
    }


def _stage_from_rows(
    name: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: Mapping[str, Any],
    scope: str,
    timestamp_fields: Sequence[str] = (),
    optional_timestamp_fields: Sequence[str] = (),
    json_fields: Sequence[str] = (),
    optional_json_fields: Sequence[str] = (),
    identity_fields: Sequence[str] = (),
    ordered_timestamp_field: str | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    quality = _assess_rows(
        rows,
        timestamp_fields=timestamp_fields,
        optional_timestamp_fields=optional_timestamp_fields,
        json_fields=json_fields,
        optional_json_fields=optional_json_fields,
        identity_fields=identity_fields,
        ordered_timestamp_field=ordered_timestamp_field,
    )
    if truncated:
        quality["issues"].append(
            {
                "code": "STAGE_ROW_LIMIT_EXCEEDED",
                "limit": MAX_STAGE_ROWS,
            }
        )
        quality["issue_count"] += 1
    status = STATUS_ABSENT if not rows else STATUS_PRESENT
    if quality["issue_count"]:
        status = STATUS_PRESENT_WITH_ISSUES if rows else STATUS_ABSENT
    return {
        "name": name,
        "scope": scope,
        "status": status,
        "source": dict(source),
        "quality": quality,
        "rows": [dict(row) for row in rows[:MAX_STAGE_ROWS]],
    }


class ReadonlyDatabase(AbstractContextManager["ReadonlyDatabase"]):
    """One repeatable read transaction and its explicit source identity."""

    def __init__(self, label: str, path: Path | str):
        self.label = label
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None
        self.status = STATUS_DATABASE_MISSING
        self.error: str | None = None
        self.source: dict[str, Any] = {
            "kind": "sqlite",
            "label": label,
            "path": str(self.path),
        }

    def __enter__(self) -> "ReadonlyDatabase":
        if not self.path.exists():
            return self
        if not self.path.is_file() or self.path.is_symlink():
            self.status = STATUS_DATABASE_UNSAFE
            self.error = "database path is not a regular non-symlink file"
            return self
        try:
            stat = self.path.stat()
            resolved = self.path.resolve()
            connection = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro", uri=True
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            self.connection = connection
            self.status = STATUS_PRESENT
            self.source.update(
                {
                    "path": str(resolved),
                    "size_bytes": stat.st_size,
                    "mtime_utc": _iso_utc(
                        datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                    ),
                    "transaction_started_at_utc": _iso_utc(_utc_now()),
                    "sqlite_version": sqlite3.sqlite_version,
                    "schema_version": int(
                        connection.execute("PRAGMA schema_version").fetchone()[0]
                    ),
                    "data_version": int(
                        connection.execute("PRAGMA data_version").fetchone()[0]
                    ),
                    "journal_mode": str(
                        connection.execute("PRAGMA journal_mode").fetchone()[0]
                    ),
                    "query_only": bool(
                        connection.execute("PRAGMA query_only").fetchone()[0]
                    ),
                }
            )
        except (OSError, sqlite3.Error) as exc:
            self.status = STATUS_DATABASE_ERROR
            self.error = f"{type(exc).__name__}: {exc}"
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.connection is not None:
            self.connection.rollback()
            self.connection.close()
            self.connection = None

    def unavailable_stage(self, name: str, *, scope: str) -> dict[str, Any]:
        return {
            "name": name,
            "scope": scope,
            "status": self.status,
            "source": dict(self.source),
            "error": self.error,
            "quality": {"row_count": 0, "issue_count": 0, "issues": []},
            "rows": [],
        }

    def query_stage(
        self,
        name: str,
        *,
        scope: str,
        required: Mapping[str, Sequence[str]],
        sql: str,
        parameters: Sequence[Any] = (),
        timestamp_fields: Sequence[str] = (),
        optional_timestamp_fields: Sequence[str] = (),
        json_fields: Sequence[str] = (),
        optional_json_fields: Sequence[str] = (),
        identity_fields: Sequence[str] = (),
        ordered_timestamp_field: str | None = None,
    ) -> dict[str, Any]:
        if self.connection is None:
            return self.unavailable_stage(name, scope=scope)
        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}
        try:
            for table, columns in required.items():
                exists = self.connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if exists is None:
                    missing_tables.append(table)
                    continue
                observed = {
                    str(row[1])
                    for row in self.connection.execute(
                        f"PRAGMA table_info({table})"
                    )
                }
                missing = sorted(set(columns) - observed)
                if missing:
                    missing_columns[table] = missing
        except sqlite3.Error as exc:
            return {
                **self.unavailable_stage(name, scope=scope),
                "status": STATUS_QUERY_ERROR,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if missing_tables:
            return {
                **self.unavailable_stage(name, scope=scope),
                "status": STATUS_TABLE_MISSING,
                "missing_tables": missing_tables,
            }
        if missing_columns:
            return {
                **self.unavailable_stage(name, scope=scope),
                "status": STATUS_SCHEMA_INCOMPATIBLE,
                "missing_columns": missing_columns,
            }
        try:
            rows = [dict(row) for row in self.connection.execute(sql, parameters)]
        except sqlite3.Error as exc:
            return {
                **self.unavailable_stage(name, scope=scope),
                "status": STATUS_QUERY_ERROR,
                "error": f"{type(exc).__name__}: {exc}",
            }
        truncated = len(rows) > MAX_STAGE_ROWS
        return _stage_from_rows(
            name,
            rows[: MAX_STAGE_ROWS + 1],
            source=self.source,
            scope=scope,
            timestamp_fields=timestamp_fields,
            optional_timestamp_fields=optional_timestamp_fields,
            json_fields=json_fields,
            optional_json_fields=optional_json_fields,
            identity_fields=identity_fields,
            ordered_timestamp_field=ordered_timestamp_field,
            truncated=truncated,
        )


def _screening_interpretations(
    ticks: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: defaultdict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[int(event["tick_id"])].append(event)
    interpretations = []
    for tick in ticks:
        tick_id = int(tick["tick_id"])
        matches = grouped.get(tick_id, [])
        if matches:
            outcome = "DIRECT_SYMBOL_EVENT"
            symbol_outcomes = [str(row["outcome"]) for row in matches]
        elif int(tick["invariant_ok"]) != 1:
            outcome = "UNKNOWN_FAILED_TICK_INVARIANT"
            symbol_outcomes = []
        elif int(tick["audit_mode"]) == 1:
            outcome = "MISSING_VERBOSE_SYMBOL_EVENT"
            symbol_outcomes = []
        else:
            outcome = "QUIET_BY_INVARIANT"
            symbol_outcomes = []
        interpretations.append(
            {
                "tick_id": tick_id,
                "tick_utc": tick["tick_utc"],
                "code_version": tick.get("code_version"),
                "screen_version": tick["screen_version"],
                "invariant_ok": tick["invariant_ok"],
                "audit_mode": tick["audit_mode"],
                "interpretation": outcome,
                "symbol_outcomes": symbol_outcomes,
            }
        )
    return interpretations


def _load_screening_archive(
    directory: Path,
    *,
    session: str,
    symbol: str,
) -> dict[str, Any]:
    source = {
        "kind": "screening_archive",
        "directory": str(directory),
    }
    if not directory.exists():
        return {
            "name": "stage1_screening",
            "scope": "symbol_and_session",
            "status": STATUS_ARCHIVE_MISSING,
            "source": source,
            "quality": {"row_count": 0, "issue_count": 0, "issues": []},
            "rows": [],
        }
    if not directory.is_dir() or directory.is_symlink():
        return {
            "name": "stage1_screening",
            "scope": "symbol_and_session",
            "status": STATUS_DATABASE_UNSAFE,
            "source": source,
            "error": "archive directory is not a regular non-symlink directory",
            "quality": {"row_count": 0, "issue_count": 0, "issues": []},
            "rows": [],
        }
    matches = sorted(directory.glob(f"screening_{session}_*.jsonl.gz"))
    if not matches:
        return {
            "name": "stage1_screening",
            "scope": "symbol_and_session",
            "status": STATUS_ARCHIVE_MISSING,
            "source": source,
            "quality": {"row_count": 0, "issue_count": 0, "issues": []},
            "rows": [],
        }
    reports = []
    try:
        for path in matches:
            reports.append(verify_screening_archive(path))
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {
            "name": "stage1_screening",
            "scope": "symbol_and_session",
            "status": STATUS_ARCHIVE_CORRUPT,
            "source": source,
            "error": f"{type(exc).__name__}: {exc}",
            "quality": {
                "row_count": 0,
                "issue_count": 1,
                "issues": [{"code": "SCREENING_ARCHIVE_VERIFICATION_FAILED"}],
            },
            "rows": [],
        }
    if len(reports) != 1:
        return {
            "name": "stage1_screening",
            "scope": "symbol_and_session",
            "status": STATUS_ARCHIVE_AMBIGUOUS,
            "source": {
                **source,
                "archives": [report.path for report in reports],
                "sha256": [report.sha256 for report in reports],
            },
            "error": "multiple independently valid archives exist for one session",
            "quality": {
                "row_count": 0,
                "issue_count": 1,
                "issues": [{"code": "MULTIPLE_VERIFIED_SCREENING_ARCHIVES"}],
            },
            "rows": [],
        }
    report = reports[0]
    ticks: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    with gzip.open(report.path, "rt", encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            payload = _strict_json(line)
            if payload["record_type"] == "screening_tick":
                ticks.append(payload["row"])
            elif (
                payload["record_type"] == "screening_event"
                and payload["row"]["symbol"] == symbol
            ):
                events.append(payload["row"])
    source = {
        "kind": "verified_screening_archive",
        "path": report.path,
        "sha256": report.sha256,
        "session": report.session,
        "tick_count": report.tick_count,
        "event_count": report.event_count,
        "failed_invariant_ticks": report.failed_invariant_ticks,
    }
    tick_stage = _stage_from_rows(
        "stage1_screening_ticks",
        ticks,
        source=source,
        scope="session",
        timestamp_fields=("tick_utc",),
        json_fields=("thresholds_json", "counts_json"),
        identity_fields=("tick_id",),
        ordered_timestamp_field="tick_utc",
    )
    event_stage = _stage_from_rows(
        "stage1_screening_events",
        events,
        source=source,
        scope="symbol",
        optional_json_fields=("reasons_json", "detail_json"),
        identity_fields=("seq",),
    )
    return {
        "name": "stage1_screening",
        "scope": "symbol_and_session",
        "status": (
            STATUS_PRESENT_WITH_ISSUES
            if tick_stage["quality"]["issue_count"]
            or event_stage["quality"]["issue_count"]
            else STATUS_PRESENT
        ),
        "source": source,
        "ticks": tick_stage,
        "symbol_events": event_stage,
        "interpretations": _screening_interpretations(ticks, events),
    }


def _collect_stage1(
    reader: ReadonlyDatabase,
    *,
    session: str,
    symbol: str,
    archive_directory: Path,
) -> dict[str, Any]:
    ticks = reader.query_stage(
        "stage1_screening_ticks",
        scope="session",
        required={
            "screening_ticks": (
                "tick_id",
                "session",
                "tick_utc",
                "screen_version",
                "code_version",
                "audit_mode",
                "thresholds_json",
                "counts_json",
                "invariant_ok",
            )
        },
        sql="SELECT * FROM screening_ticks WHERE session=? ORDER BY tick_id",
        parameters=(session,),
        timestamp_fields=("tick_utc",),
        json_fields=("thresholds_json", "counts_json"),
        identity_fields=("tick_id",),
        ordered_timestamp_field="tick_utc",
    )
    events = reader.query_stage(
        "stage1_screening_events",
        scope="symbol",
        required={
            "screening_ticks": ("tick_id", "session"),
            "screening_events": (
                "seq",
                "tick_id",
                "symbol",
                "outcome",
                "reasons_json",
                "detail_json",
            ),
        },
        sql=(
            "SELECT e.* FROM screening_events e JOIN screening_ticks t "
            "ON t.tick_id=e.tick_id WHERE t.session=? AND e.symbol=? ORDER BY e.seq"
        ),
        parameters=(session, symbol),
        optional_json_fields=("reasons_json", "detail_json"),
        identity_fields=("seq",),
    )
    if ticks["status"] in {STATUS_PRESENT, STATUS_PRESENT_WITH_ISSUES}:
        return {
            "name": "stage1_screening",
            "scope": "symbol_and_session",
            "status": (
                STATUS_PRESENT_WITH_ISSUES
                if ticks["quality"]["issue_count"]
                or events["quality"]["issue_count"]
                else STATUS_PRESENT
            ),
            "source": {**reader.source, "selection": "live_sqlite_transaction"},
            "ticks": ticks,
            "symbol_events": events,
            "interpretations": _screening_interpretations(
                ticks["rows"], events["rows"]
            ),
        }
    archive = _load_screening_archive(
        archive_directory, session=session, symbol=symbol
    )
    archive["live_source_status"] = ticks["status"]
    archive["live_source_error"] = ticks.get("error")
    return archive


def _query_definitions(
    session: str,
    symbol: str,
    *,
    session_open: str,
    session_close: str,
) -> dict[str, list[dict[str, Any]]]:
    """Declarative queries grouped by source database."""
    return {
        "journal": [
            {
                "name": "journal_detections",
                "scope": "symbol",
                "required": {"detections": ("id", "session", "symbol", "ts_utc")},
                "sql": "SELECT * FROM detections WHERE session=? AND symbol=? ORDER BY ts_utc,id",
                "parameters": (session, symbol),
                "timestamp_fields": ("ts_utc",),
                "optional_timestamp_fields": (),
                "optional_json_fields": ("context_json",),
                "identity_fields": ("id",),
                "ordered_timestamp_field": "ts_utc",
            },
            {
                "name": "journal_decision_events",
                "scope": "symbol",
                "required": {
                    "detections": ("id", "session", "symbol"),
                    "decision_events": ("seq", "detection_id", "ts_utc"),
                },
                "sql": (
                    "SELECT e.* FROM decision_events e JOIN detections d "
                    "ON d.id=e.detection_id WHERE d.session=? AND d.symbol=? ORDER BY e.seq"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("ts_utc",),
                "optional_json_fields": ("detail_json",),
                "identity_fields": ("seq",),
                "ordered_timestamp_field": "ts_utc",
            },
            {
                "name": "journal_marks",
                "scope": "symbol",
                "required": {
                    "detections": ("id", "session", "symbol"),
                    "marks": ("detection_id", "offset_min", "price"),
                },
                "sql": (
                    "SELECT m.* FROM marks m JOIN detections d ON d.id=m.detection_id "
                    "WHERE d.session=? AND d.symbol=? ORDER BY m.detection_id,m.offset_min"
                ),
                "parameters": (session, symbol),
                "identity_fields": ("detection_id", "offset_min"),
            },
            {
                "name": "journal_mark_resolution_events",
                "scope": "symbol",
                "required": {
                    "detections": ("id", "session", "symbol"),
                    "mark_resolution_events": (
                        "event_id",
                        "detection_id",
                        "created_at",
                    ),
                },
                "sql": (
                    "SELECT m.* FROM mark_resolution_events m JOIN detections d "
                    "ON d.id=m.detection_id WHERE d.session=? AND d.symbol=? "
                    "ORDER BY m.event_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("created_at",),
                "identity_fields": ("event_id",),
                "ordered_timestamp_field": "created_at",
            },
            {
                "name": "catalyst_event_windows",
                "scope": "symbol_and_market",
                "required": {
                    "event_windows": (
                        "id",
                        "symbol",
                        "start_utc",
                        "end_utc",
                        "source",
                    )
                },
                "sql": (
                    "SELECT * FROM event_windows WHERE (symbol=? OR symbol IS NULL) "
                    "AND start_utc<=? AND end_utc>=? ORDER BY start_utc,id"
                ),
                "parameters": (symbol, session_close, session_open),
                "timestamp_fields": ("start_utc", "end_utc", "created_at"),
                "identity_fields": ("id",),
                "ordered_timestamp_field": "start_utc",
            },
            {
                "name": "catalyst_ingestion_runs",
                "scope": "session",
                "required": {
                    "event_ingestion_runs": (
                        "id",
                        "report_date",
                        "attempted_at",
                        "completed_at",
                        "status",
                    )
                },
                "sql": "SELECT * FROM event_ingestion_runs WHERE report_date=? ORDER BY id",
                "parameters": (session,),
                "timestamp_fields": ("attempted_at", "completed_at"),
                "identity_fields": ("id",),
                "ordered_timestamp_field": "attempted_at",
            },
        ],
        "evaluations": [
            {
                "name": "bar_evaluations",
                "scope": "symbol",
                "required": {
                    "evaluation_sessions": (
                        "eval_session_id",
                        "session",
                        "symbol",
                        "run_id",
                        "run_mode",
                        "created_at",
                    ),
                    "bar_evaluations": (
                        "seq",
                        "eval_session_id",
                        "bar_ts_utc",
                        "outcome",
                    ),
                },
                "sql": (
                    "SELECT s.session,s.symbol,s.run_id,s.run_mode,s.evaluation_version,"
                    "s.code_version,s.origin,s.created_at,b.* FROM evaluation_sessions s "
                    "JOIN bar_evaluations b ON b.eval_session_id=s.eval_session_id "
                    "WHERE s.session=? AND s.symbol=? ORDER BY s.eval_session_id,b.seq"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("created_at", "bar_ts_utc"),
                "identity_fields": ("seq",),
            }
        ],
        "postmarket": [
            {
                "name": "rth_momentum_ticks",
                "scope": "session",
                "required": {
                    "rth_momentum_ticks": (
                        "tick_id",
                        "session",
                        "scheduled_tick_utc",
                        "tick_utc",
                        "completed_utc",
                        "invariant_ok",
                    )
                },
                "sql": "SELECT * FROM rth_momentum_ticks WHERE session=? ORDER BY tick_id",
                "parameters": (session,),
                "timestamp_fields": (
                    "scheduled_tick_utc",
                    "tick_utc",
                    "completed_utc",
                    "window_start_utc",
                    "session_close_utc",
                ),
                "json_fields": ("thresholds_json",),
                "identity_fields": ("tick_id",),
                "ordered_timestamp_field": "scheduled_tick_utc",
            },
            {
                "name": "rth_momentum_observations",
                "scope": "symbol",
                "required": {
                    "rth_momentum_observations": (
                        "seq",
                        "session",
                        "symbol",
                        "outcome",
                    )
                },
                "sql": (
                    "SELECT * FROM rth_momentum_observations WHERE session=? AND symbol=? "
                    "ORDER BY seq"
                ),
                "parameters": (session, symbol),
                "optional_timestamp_fields": ("bar_open_ts_utc",),
                "json_fields": (
                    "sources_json",
                    "ranks_json",
                    "screen_evidence_json",
                ),
                "identity_fields": ("seq",),
                "ordered_timestamp_field": "bar_open_ts_utc",
            },
            {
                "name": "rth_momentum_candidates",
                "scope": "symbol",
                "required": {
                    "rth_momentum_candidates": (
                        "candidate_id",
                        "session",
                        "symbol",
                        "first_detected_at",
                    )
                },
                "sql": (
                    "SELECT * FROM rth_momentum_candidates WHERE session=? AND symbol=? "
                    "ORDER BY candidate_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("first_detected_at", "bar_open_ts_utc"),
                "json_fields": ("sources_json",),
                "identity_fields": ("candidate_id",),
                "ordered_timestamp_field": "first_detected_at",
            },
            {
                "name": "rth_postmarket_handoffs",
                "scope": "symbol",
                "required": {
                    "rth_postmarket_handoffs": (
                        "handoff_id",
                        "session",
                        "symbol",
                        "transition_at_utc",
                    )
                },
                "sql": (
                    "SELECT * FROM rth_postmarket_handoffs WHERE session=? AND symbol=? "
                    "ORDER BY handoff_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("transition_at_utc",),
                "identity_fields": ("handoff_id",),
                "ordered_timestamp_field": "transition_at_utc",
            },
            {
                "name": "rth_missed_mover_census",
                "scope": "symbol",
                "required": {
                    "rth_missed_mover_census_runs": (
                        "census_id",
                        "session",
                        "attempt",
                        "status",
                    ),
                    "rth_missed_mover_census_events": (
                        "seq",
                        "census_id",
                        "symbol",
                        "outcome",
                    ),
                },
                "sql": (
                    "SELECT r.session,r.attempt,r.status AS census_status,"
                    "r.invariant_ok,r.code_version,r.completed_at_utc,e.* "
                    "FROM rth_missed_mover_census_runs r "
                    "JOIN rth_missed_mover_census_events e ON e.census_id=r.census_id "
                    "WHERE r.session=? AND e.symbol=? ORDER BY r.attempt,e.seq"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("completed_at_utc",),
                "json_fields": (
                    "qualifying_directions_json",
                    "excursion_directions_json",
                    "fast_lane_directions_json",
                    "fast_lane_outcomes_json",
                    "missed_directions_json",
                    "miss_reasons_json",
                ),
                "identity_fields": ("census_id", "symbol"),
                "ordered_timestamp_field": "completed_at_utc",
            },
            {
                "name": "postmarket_discovery_ticks",
                "scope": "session",
                "required": {
                    "postmarket_discovery_ticks": (
                        "tick_id",
                        "session",
                        "tick_utc",
                        "completed_utc",
                        "invariant_ok",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_discovery_ticks WHERE session=? "
                    "ORDER BY tick_id"
                ),
                "parameters": (session,),
                "timestamp_fields": ("tick_utc", "completed_utc"),
                "json_fields": (
                    "endpoints_json",
                    "source_updates_json",
                    "thresholds_json",
                ),
                "identity_fields": ("tick_id",),
                "ordered_timestamp_field": "tick_utc",
            },
            {
                "name": "postmarket_discovery_observations",
                "scope": "symbol",
                "required": {
                    "postmarket_discovery_observations": (
                        "seq",
                        "symbol",
                        "event_date",
                        "outcome",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_discovery_observations "
                    "WHERE event_date=? AND symbol=? ORDER BY seq"
                ),
                "parameters": (session, symbol),
                "optional_timestamp_fields": ("bar_open_ts_utc",),
                "json_fields": (
                    "sources_json",
                    "ranks_json",
                    "screen_evidence_json",
                ),
                "identity_fields": ("seq",),
                "ordered_timestamp_field": "bar_open_ts_utc",
            },
            {
                "name": "postmarket_discovery_candidates",
                "scope": "symbol",
                "required": {
                    "postmarket_discovery_candidates": (
                        "candidate_id",
                        "session",
                        "symbol",
                        "first_detected_at",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_discovery_candidates "
                    "WHERE session=? AND symbol=? ORDER BY candidate_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("first_detected_at", "bar_open_ts_utc"),
                "json_fields": ("sources_json",),
                "identity_fields": ("candidate_id",),
                "ordered_timestamp_field": "first_detected_at",
            },
            {
                "name": "scheduled_postmarket_observations",
                "scope": "symbol",
                "required": {
                    "postmarket_observations": (
                        "seq",
                        "symbol",
                        "event_date",
                        "outcome",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_observations WHERE event_date=? "
                    "AND symbol=? ORDER BY seq"
                ),
                "parameters": (session, symbol),
                "optional_timestamp_fields": ("bar_open_ts_utc",),
                "identity_fields": ("seq",),
                "ordered_timestamp_field": "bar_open_ts_utc",
            },
            {
                "name": "scheduled_postmarket_candidates",
                "scope": "symbol",
                "required": {
                    "postmarket_candidates": (
                        "candidate_id",
                        "session",
                        "symbol",
                        "first_detected_at",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_candidates WHERE session=? AND symbol=? "
                    "ORDER BY candidate_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("first_detected_at", "bar_open_ts_utc"),
                "identity_fields": ("candidate_id",),
                "ordered_timestamp_field": "first_detected_at",
            },
            {
                "name": "postmarket_lifecycle_transitions",
                "scope": "symbol",
                "required": {
                    "postmarket_candidate_lifecycle": (
                        "transition_id",
                        "session",
                        "symbol",
                        "transition_at_utc",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_candidate_lifecycle "
                    "WHERE session=? AND symbol=? ORDER BY transition_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("transition_at_utc", "recorded_at_utc"),
                "optional_timestamp_fields": ("evidence_bar_open_ts_utc",),
                "identity_fields": ("transition_id",),
                "ordered_timestamp_field": "transition_at_utc",
            },
            {
                "name": "postmarket_lifecycle_observations",
                "scope": "symbol",
                "required": {
                    "postmarket_candidate_lifecycle_observations": (
                        "seq",
                        "session",
                        "symbol",
                        "observed_at_utc",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_candidate_lifecycle_observations "
                    "WHERE session=? AND symbol=? ORDER BY seq"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("observed_at_utc", "evidence_bar_open_ts_utc"),
                "identity_fields": ("seq",),
                "ordered_timestamp_field": "observed_at_utc",
            },
            {
                "name": "postmarket_candidate_context",
                "scope": "symbol",
                "required": {
                    "postmarket_candidate_context": (
                        "context_id",
                        "session",
                        "symbol",
                        "observed_at_utc",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_candidate_context WHERE session=? "
                    "AND symbol=? ORDER BY context_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("candidate_detected_at", "observed_at_utc"),
                "optional_timestamp_fields": ("quote_ts_utc", "asset_observed_at_utc"),
                "json_fields": (
                    "catalyst_sources_json",
                    "catalyst_details_json",
                    "catalyst_coverage_json",
                    "issues_json",
                ),
                "identity_fields": ("context_id",),
                "ordered_timestamp_field": "observed_at_utc",
            },
            {
                "name": "postmarket_candidate_ranks",
                "scope": "symbol",
                "required": {
                    "postmarket_rank_runs": (
                        "rank_run_id",
                        "session",
                        "as_of_utc",
                    ),
                    "postmarket_candidate_ranks": (
                        "rank_id",
                        "rank_run_id",
                        "session",
                        "symbol",
                    ),
                },
                "sql": (
                    "SELECT rr.rank_version,rr.as_of_utc,rr.recorded_at_utc,"
                    "rr.code_version,rr.status AS rank_run_status,r.* "
                    "FROM postmarket_candidate_ranks r JOIN postmarket_rank_runs rr "
                    "ON rr.rank_run_id=r.rank_run_id WHERE r.session=? AND r.symbol=? "
                    "ORDER BY r.rank_run_id,r.rank_id"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": ("as_of_utc", "recorded_at_utc"),
                "json_fields": (
                    "components_json",
                    "penalties_json",
                    "exclusion_reasons_json",
                    "explanation_json",
                ),
                "identity_fields": ("rank_id",),
                "ordered_timestamp_field": "as_of_utc",
            },
            {
                "name": "postmarket_outcome_marks",
                "scope": "symbol",
                "required": {
                    "postmarket_candidate_mark_events": (
                        "seq",
                        "session",
                        "symbol",
                        "target_ts_utc",
                        "recorded_at_utc",
                    )
                },
                "sql": (
                    "SELECT * FROM postmarket_candidate_mark_events "
                    "WHERE session=? AND symbol=? ORDER BY seq"
                ),
                "parameters": (session, symbol),
                "timestamp_fields": (
                    "target_ts_utc",
                    "detection_ts_utc",
                    "recorded_at_utc",
                ),
                "optional_timestamp_fields": (
                    "observed_bar_open_ts_utc",
                    "observed_at_utc",
                ),
                "json_fields": ("detail_json",),
                "identity_fields": ("seq",),
                "ordered_timestamp_field": "recorded_at_utc",
            },
        ],
    }


def _session_clock(session: date) -> dict[str, Any]:
    if not CALENDAR.is_session(session):
        return {
            "calendar": "XNYS",
            "is_session": False,
            "session": session.isoformat(),
            "open_utc": None,
            "close_utc": None,
        }
    opened = CALENDAR.session_open(session).to_pydatetime().astimezone(timezone.utc)
    closed = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    return {
        "calendar": "XNYS",
        "is_session": True,
        "session": session.isoformat(),
        "open_utc": opened.isoformat(),
        "close_utc": closed.isoformat(),
        "duration_minutes": int((closed - opened).total_seconds() / 60),
    }


def _stage_map(stages: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {stage["name"]: stage for stage in stages}


def _rows(stages: Mapping[str, dict[str, Any]], name: str) -> list[dict[str, Any]]:
    stage = stages.get(name, {})
    return list(stage.get("rows", []))


def _strict_json_list(value: Any) -> list[Any]:
    if not isinstance(value, str):
        return []
    try:
        decoded = _strict_json(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _conclusions(stages: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    observation_names = (
        "stage1_screening_events",
        "bar_evaluations",
        "journal_detections",
        "rth_momentum_observations",
        "rth_missed_mover_census",
        "postmarket_discovery_observations",
        "scheduled_postmarket_observations",
        "postmarket_lifecycle_observations",
    )
    qualification_names = (
        "rth_momentum_candidates",
        "postmarket_discovery_candidates",
        "scheduled_postmarket_candidates",
    )
    def trusted_rows(name: str) -> list[dict[str, Any]]:
        stage = stages.get(name, {})
        return _rows(stages, name) if stage.get("status") == STATUS_PRESENT else []

    untrusted_evidence_stages = sorted(
        name
        for name in observation_names + qualification_names + ("operator_outbox",)
        if _rows(stages, name) and stages.get(name, {}).get("status") != STATUS_PRESENT
    )
    observed = [name for name in observation_names if trusted_rows(name)]
    qualified = [name for name in qualification_names if trusted_rows(name)]
    alerted_detection_ids = [
        row.get("id")
        for row in trusted_rows("journal_detections")
        if int(row.get("alerted") or 0) == 1
    ]
    delivery_rows = trusted_rows("operator_outbox")

    missed_directions: list[str] = []
    untrusted_census_missed_directions: list[str] = []
    for row in _rows(stages, "rth_missed_mover_census"):
        directions = [
            str(item)
            for item in _strict_json_list(row.get("missed_directions_json"))
        ]
        if (
            stages.get("rth_missed_mover_census", {}).get("status") == STATUS_PRESENT
            and row.get("census_status") == "success"
            and int(row.get("invariant_ok") or 0) == 1
            and row.get("data_status") == "AVAILABLE"
        ):
            missed_directions.extend(directions)
        else:
            untrusted_census_missed_directions.extend(directions)

    if qualified:
        path_classification = "QUALIFIED_SHADOW_CANDIDATE_RECORDED"
    elif observed:
        path_classification = "DURABLE_SYMBOL_EVIDENCE_WITHOUT_QUALIFICATION"
    elif untrusted_evidence_stages:
        path_classification = "EVIDENCE_PRESENT_BUT_DATA_QUALITY_UNRESOLVED"
    else:
        path_classification = "UNKNOWN_NO_DURABLE_SYMBOL_PATH_EVIDENCE"

    if delivery_rows:
        delivery_classification = "OWNER_OPERATOR_OUTBOX_EVIDENCE_PRESENT"
    elif alerted_detection_ids:
        delivery_classification = "JOURNAL_ALERT_FLAG_PRESENT"
    else:
        delivery_classification = "NO_DURABLE_DELIVERY_EVIDENCE_FOUND"

    if missed_directions:
        caught_missed = "RTH_CENSUS_RECORDED_MISSED_DIRECTION"
    else:
        caught_missed = "BROAD_CAUGHT_OR_MISSED_CLAIM_NOT_PROVEN"

    return {
        "path_classification": path_classification,
        "observed_stages": observed,
        "qualified_stages": qualified,
        "untrusted_evidence_stages": untrusted_evidence_stages,
        "delivery_classification": delivery_classification,
        "alerted_detection_ids": alerted_detection_ids,
        "operator_outbox_rows": len(delivery_rows),
        "census_missed_directions": sorted(set(missed_directions)),
        "untrusted_census_missed_directions": sorted(
            set(untrusted_census_missed_directions)
        ),
        "caught_or_missed_verdict": caught_missed,
        "claim_boundary": (
            "A row proves only the named stage. Absence proves neither provider "
            "absence nor a system-wide miss unless a complete invariant-checked "
            "census explicitly records that miss."
        ),
    }


def reconstruct_signal_incident(
    *,
    symbol: str,
    session: str | date,
    paths: ReconstructionPaths | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Reconstruct one symbol/session without mutating or fetching anything."""
    canonical_symbol = _normalize_symbol(symbol)
    parsed_session = _normalize_session(session)
    session_text = parsed_session.isoformat()
    current = generated_at or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    selected_paths = paths or ReconstructionPaths.from_data_directory(
        DEFAULT_DATA_DIR,
        screening_archives=DEFAULT_ARCHIVE_DIR,
    )
    clock = _session_clock(parsed_session)
    close_value = clock.get("close_utc")
    clock["is_final_at_generation"] = bool(
        close_value and current.astimezone(timezone.utc) >= _aware_utc(close_value)
    )
    session_open = clock["open_utc"] or f"{session_text}T00:00:00+00:00"
    session_close = clock["close_utc"] or f"{session_text}T23:59:59+00:00"
    definitions = _query_definitions(
        session_text,
        canonical_symbol,
        session_open=session_open,
        session_close=session_close,
    )
    stages: list[dict[str, Any]] = []

    with ReadonlyDatabase("universe", selected_paths.universe) as reader:
        asset = reader.query_stage(
            "universe_asset",
            scope="symbol",
            required={
                "assets": (
                    "symbol",
                    "exchange",
                    "name",
                    "tradable",
                    "is_active",
                    "first_seen_at",
                    "last_seen_at",
                )
            },
            sql="SELECT * FROM assets WHERE symbol=?",
            parameters=(canonical_symbol,),
            timestamp_fields=("first_seen_at", "last_seen_at"),
            optional_timestamp_fields=("delisted_at",),
            json_fields=("attributes_json",),
            identity_fields=("symbol",),
        )
        stages.append(asset)
        stage1 = _collect_stage1(
            reader,
            session=session_text,
            symbol=canonical_symbol,
            archive_directory=selected_paths.screening_archives,
        )

    # Flatten Stage-1 component rows into the stage map while preserving the
    # combined interpretation as its own explicit stage.
    stages.append(stage1)
    if "ticks" in stage1:
        stages.extend([stage1["ticks"], stage1["symbol_events"]])

    for label, path in (
        ("journal", selected_paths.journal),
        ("evaluations", selected_paths.evaluations),
        ("postmarket", selected_paths.postmarket),
    ):
        with ReadonlyDatabase(label, path) as reader:
            for definition in definitions[label]:
                stages.append(reader.query_stage(**definition))

    stage_by_name = _stage_map(stages)
    marketwide_candidate_ids = [
        int(row["candidate_id"])
        for row in _rows(stage_by_name, "postmarket_discovery_candidates")
    ]
    if marketwide_candidate_ids:
        patterns = [
            f"%:candidate:{candidate_id}" for candidate_id in marketwide_candidate_ids
        ]
        with ReadonlyDatabase("users", selected_paths.users) as reader:
            stages.append(
                reader.query_stage(
                    "operator_outbox",
                    scope="symbol_delivery",
                    required={
                        "outbox": (
                            "id",
                            "alert_id",
                            "chat_id",
                            "status",
                            "created_at",
                            "delivered_at",
                        )
                    },
                    sql=(
                        "SELECT id,alert_id,CASE WHEN chat_id IS NULL THEN 0 ELSE 1 END "
                        "AS recipient_present,priority,status,attempts,next_attempt_at,"
                        "created_at,delivered_at,last_error "
                        "FROM outbox WHERE "
                        + " OR ".join("alert_id LIKE ?" for _ in patterns)
                        + " ORDER BY created_at,id"
                    ),
                    parameters=patterns,
                    timestamp_fields=("next_attempt_at", "created_at"),
                    optional_timestamp_fields=("delivered_at",),
                    identity_fields=("id",),
                    ordered_timestamp_field="created_at",
                )
            )
    else:
        stages.append(
            {
                "name": "operator_outbox",
                "scope": "symbol_delivery",
                "status": STATUS_NOT_APPLICABLE,
                "source": {
                    "kind": "sqlite",
                    "label": "users",
                    "path": str(selected_paths.users),
                },
                "reason": "no marketwide candidate ID exists to key an operator alert",
                "quality": {"row_count": 0, "issue_count": 0, "issues": []},
                "rows": [],
            }
        )

    stage_by_name = _stage_map(stages)
    degraded = sorted(
        name
        for name, stage in stage_by_name.items()
        if stage["status"]
        not in {STATUS_PRESENT, STATUS_ABSENT, STATUS_NOT_APPLICABLE}
    )
    if not clock["is_session"]:
        degraded.append("market_session_not_xnys")
    elif not clock["is_final_at_generation"]:
        degraded.append("market_session_not_final")
    degraded = sorted(set(degraded))
    source_versions: dict[str, list[str]] = {}
    for name, stage in stage_by_name.items():
        versions = sorted(
            {
                str(row["code_version"])
                for row in stage.get("rows", [])
                if row.get("code_version")
            }
        )
        if versions:
            source_versions[name] = versions
    return {
        "reconstruction_version": RECONSTRUCTION_VERSION,
        "generated_at_utc": _iso_utc(current),
        "symbol": canonical_symbol,
        "session": session_text,
        "market_session": clock,
        "report_status": "degraded" if degraded else "complete",
        "degraded_stages": degraded,
        "source_code_versions": source_versions,
        "snapshot_semantics": (
            "Each SQLite file is read in its own repeatable read transaction. "
            "The report is not an atomic snapshot across database files."
        ),
        "limitations": [
            "Normalized durable rows are reconstructed; raw vendor response payloads are not retained here.",
            "Asset currency is not stored in the current universe schema.",
            "Corporate-action adjustment and provider disagreement are only visible when another retained stage explicitly recorded them.",
            "A missing historical row cannot prove that no transient in-memory evaluation occurred.",
        ],
        "stages": stage_by_name,
        "conclusions": _conclusions(stage_by_name),
    }


def _canonical_json(payload: Mapping[str, Any], *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_reconstruction_artifact(
    report: Mapping[str, Any],
    output_directory: Path | str,
) -> tuple[Path, str]:
    """Publish one immutable, no-replace JSON evidence artifact."""
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir() or output.is_symlink():
        raise ValueError("output directory must be a non-symlink directory")
    body = _canonical_json(report, pretty=True)
    digest = hashlib.sha256(body).hexdigest()
    generated = str(report["generated_at_utc"]).replace(":", "").replace("-", "")
    generated = generated.replace("+0000", "Z").replace(".", "")
    filename = (
        f"signal_incident_{report['session']}_{report['symbol']}_v"
        f"{RECONSTRUCTION_VERSION}_{generated}_{digest[:16]}.json"
    )
    path = output / filename
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, digest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct one symbol/session from durable Perch evidence without "
            "fetching data, delivering alerts, or inferring a caught/missed verdict."
        )
    )
    parser.add_argument("symbol")
    parser.add_argument("session", help="ISO XNYS session date")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--screening-archive-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="exit 1 when any requested source is missing, malformed, or incompatible",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    paths = ReconstructionPaths.from_data_directory(
        arguments.data_dir,
        screening_archives=arguments.screening_archive_dir,
    )
    try:
        report = reconstruct_signal_incident(
            symbol=arguments.symbol,
            session=arguments.session,
            paths=paths,
        )
        artifact = None
        digest = None
        if arguments.output_dir is not None:
            path, digest = write_reconstruction_artifact(report, arguments.output_dir)
            artifact = str(path)
        envelope = {"report": report, "artifact": artifact, "sha256": digest}
        print(_canonical_json(envelope, pretty=arguments.pretty).decode(), end="")
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(
            _canonical_json(
                {"error": f"{type(exc).__name__}: {exc}"}, pretty=True
            ).decode(),
            end="",
        )
        return 2
    if arguments.fail_on_degraded and report["report_status"] != "complete":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
