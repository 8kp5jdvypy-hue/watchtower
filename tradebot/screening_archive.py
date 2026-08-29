"""Immutable, deterministic archives of completed Stage-1 screening sessions.

This module deliberately does not prune ``universe.db``. It establishes and
verifies the durable archive prerequisite first; deletion requires a separate
review after real archives have been restored and compared with their source.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_GRACE = timedelta(minutes=15)
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "universe.db"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "screening_archives"
ARCHIVE_NAME = re.compile(
    r"^screening_(\d{4}-\d{2}-\d{2})_([0-9a-f]{64})\.jsonl\.gz$"
)

TICK_COLUMNS = (
    "tick_id",
    "session",
    "tick_utc",
    "run_id",
    "run_mode",
    "screen_version",
    "code_version",
    "audit_mode",
    "universe_count",
    "thresholds_json",
    "counts_json",
    "invariant_ok",
    "promotion_limit",
    "latency_ms",
)
EVENT_COLUMNS = (
    "seq",
    "tick_id",
    "symbol",
    "outcome",
    "screen_score",
    "rank",
    "reasons_json",
    "detail_json",
)


@dataclass(frozen=True)
class ScreeningArchiveReport:
    path: str
    sha256: str
    session: str
    tick_count: int
    event_count: int
    failed_invariant_ticks: int
    first_tick_utc: str
    last_tick_utc: str
    idempotent: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _calendar():
    # Verification and restore deliberately need no market-calendar package so
    # the host-level backup service can validate archives. Only archive
    # selection/finality imports the application dependency inside a container.
    import exchange_calendars as ecals

    return ecals.get_calendar("XNYS")


def _session(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("session must be an ISO date") from exc
    if not _calendar().is_session(parsed):
        raise ValueError(f"{value} is not an XNYS session")
    return parsed


def _closed(session: date, now: datetime) -> bool:
    close = _calendar().session_close(session).to_pydatetime().astimezone(timezone.utc)
    return _utc(now) >= close + ARCHIVE_GRACE


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"screening database is missing or unsafe: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    return connection


def latest_closed_session(
    database: Path | str,
    *,
    now: datetime,
) -> str | None:
    """Return the newest stored XNYS session whose RTH close is final."""
    connection = _readonly_connection(Path(database))
    try:
        rows = connection.execute(
            "SELECT DISTINCT session FROM screening_ticks ORDER BY session DESC"
        )
        for row in rows:
            candidate = _session(str(row[0]))
            if _closed(candidate, now):
                return candidate.isoformat()
        return None
    finally:
        connection.close()


def pending_closed_sessions(
    database: Path | str,
    output_directory: Path | str,
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Return every final stored session without a verified archive.

    Existing artifacts are verified rather than trusted by name. Any corrupt
    artifact fails the job loudly; it is never skipped in favor of a new file
    that could conceal lost custody.
    """
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("screening archive directory must not be a symlink")
    connection = _readonly_connection(Path(database))
    try:
        _validate_source_schema(connection)
        stored = [str(row[0]) for row in connection.execute(
            "SELECT DISTINCT session FROM screening_ticks ORDER BY session"
        )]
        summaries = {
            value: _session_summary(connection, value)
            for value in stored
        }
    finally:
        connection.close()
    pending = []
    for value in stored:
        parsed = _session(value)
        if not _closed(parsed, now):
            continue
        existing = sorted(output.glob(f"screening_{value}_*.jsonl.gz"))
        verified = [verify_screening_archive(artifact) for artifact in existing]
        current = summaries[value]
        if not any(
            report.tick_count == current["tick_count"]
            and report.event_count == current["event_count"]
            and report.failed_invariant_ticks == current["failed_invariant_ticks"]
            and report.first_tick_utc == current["first_tick_utc"]
            and report.last_tick_utc == current["last_tick_utc"]
            for report in verified
        ):
            pending.append(value)
    return tuple(pending)


def _rows(
    connection: sqlite3.Connection,
    columns: Sequence[str],
    query: str,
    parameters: Sequence[Any],
) -> Iterator[dict[str, Any]]:
    for row in connection.execute(query, parameters):
        yield {column: row[column] for column in columns}


def _validate_source_schema(connection: sqlite3.Connection) -> None:
    for table, expected in (
        ("screening_ticks", TICK_COLUMNS),
        ("screening_events", EVENT_COLUMNS),
    ):
        observed = tuple(
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if observed != expected:
            raise ValueError(
                f"{table} schema changed; bump the screening archive contract "
                f"before archiving: expected={expected!r} observed={observed!r}"
            )


def _session_summary(
    connection: sqlite3.Connection,
    session: str,
) -> dict[str, int | str]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS tick_count,
               COALESCE(SUM(CASE WHEN invariant_ok=0 THEN 1 ELSE 0 END), 0)
                 AS failed_invariant_ticks,
               MIN(tick_utc) AS first_tick_utc,
               MAX(tick_utc) AS last_tick_utc
        FROM screening_ticks WHERE session=?
        """,
        (session,),
    ).fetchone()
    event_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM screening_events e
            JOIN screening_ticks t ON t.tick_id=e.tick_id
            WHERE t.session=?
            """,
            (session,),
        ).fetchone()[0]
    )
    return {
        "tick_count": int(row["tick_count"]),
        "event_count": event_count,
        "failed_invariant_ticks": int(row["failed_invariant_ticks"]),
        "first_tick_utc": str(row["first_tick_utc"]),
        "last_tick_utc": str(row["last_tick_utc"]),
    }


def _line(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json(line: str, *, context: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    try:
        return json.loads(line, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, _DuplicateJsonKey) as exc:
        raise ValueError(f"invalid screening archive JSON in {context}") from exc


def _archive_lines(path: Path) -> Iterator[str]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            yield from handle
    except (EOFError, OSError, UnicodeDecodeError) as exc:
        raise ValueError("screening archive gzip stream is unreadable") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_screening_session(
    database: Path | str,
    output_directory: Path | str,
    *,
    session: str,
    now: datetime,
) -> ScreeningArchiveReport:
    """Write and independently verify one completed session archive.

    The source is held in one SQLite read transaction. The gzip stream has a
    fixed timestamp and no original filename, making identical source rows
    byte-identical across retries. Publication is same-directory and atomic.
    """
    parsed_session = _session(session)
    if not _closed(parsed_session, now):
        raise ValueError(f"screening session {session} is not final yet")

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("screening archive directory must not be a symlink")

    connection = _readonly_connection(Path(database))
    temporary: Path | None = None
    try:
        _validate_source_schema(connection)
        summary = _session_summary(connection, session)
        tick_count = int(summary["tick_count"])
        if tick_count == 0:
            raise ValueError(f"screening session {session} has no ticks")
        event_count = int(summary["event_count"])
        manifest = {
            "record_type": "screening_archive_manifest",
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "session": session,
            "tick_columns": list(TICK_COLUMNS),
            "event_columns": list(EVENT_COLUMNS),
            "tick_count": tick_count,
            "event_count": event_count,
            "failed_invariant_ticks": int(summary["failed_invariant_ticks"]),
            "first_tick_utc": summary["first_tick_utc"],
            "last_tick_utc": summary["last_tick_utc"],
        }

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".screening_{session}.", suffix=".tmp", dir=output
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                compressed.write(_line(manifest))
                for row in _rows(
                    connection,
                    TICK_COLUMNS,
                    f"SELECT {','.join(TICK_COLUMNS)} FROM screening_ticks "
                    "WHERE session=? ORDER BY tick_id",
                    (session,),
                ):
                    compressed.write(_line({"record_type": "screening_tick", "row": row}))
                for row in _rows(
                    connection,
                    EVENT_COLUMNS,
                    f"SELECT {','.join('e.' + column for column in EVENT_COLUMNS)} "
                    "FROM screening_events e JOIN screening_ticks t ON t.tick_id=e.tick_id "
                    "WHERE t.session=? ORDER BY e.seq",
                    (session,),
                ):
                    compressed.write(_line({"record_type": "screening_event", "row": row}))
            raw.flush()
            os.fsync(raw.fileno())

        digest = _sha256(temporary)
        final_path = output / f"screening_{session}_{digest}.jsonl.gz"
        idempotent = final_path.exists()
        if idempotent:
            if final_path.is_symlink() or _sha256(final_path) != digest:
                raise ValueError(f"existing screening archive is unsafe: {final_path}")
            temporary.unlink()
            temporary = None
        else:
            try:
                os.link(temporary, final_path)
            except FileExistsError:
                if final_path.is_symlink() or _sha256(final_path) != digest:
                    raise ValueError(
                        f"concurrent screening archive publication conflicted: {final_path}"
                    )
                idempotent = True
            else:
                final_path.chmod(0o444)
                _fsync_directory(output)
            temporary.unlink()
            temporary = None
    finally:
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    verified = verify_screening_archive(final_path)
    return ScreeningArchiveReport(**{**asdict(verified), "idempotent": idempotent})


def verify_screening_archive(path: Path | str) -> ScreeningArchiveReport:
    """Verify filename digest, schema, counts, identities, and ordering."""
    archive = Path(path)
    if not archive.is_file() or archive.is_symlink():
        raise FileNotFoundError(f"screening archive is missing or unsafe: {archive}")
    match = ARCHIVE_NAME.fullmatch(archive.name)
    if not match:
        raise ValueError("invalid screening archive filename")
    filename_session, expected_digest = match.groups()
    observed_digest = _sha256(archive)
    if observed_digest != expected_digest:
        raise ValueError("screening archive digest does not match its filename")

    lines = iter(_archive_lines(archive))
    try:
        manifest = _strict_json(next(lines), context="manifest")
    except StopIteration as exc:
        raise ValueError("screening archive has no valid manifest") from exc
    if not isinstance(manifest, dict):
        raise ValueError("screening archive manifest must be an object")
    required_manifest = {
        "record_type",
        "schema_version",
        "session",
        "tick_columns",
        "event_columns",
        "tick_count",
        "event_count",
        "failed_invariant_ticks",
        "first_tick_utc",
        "last_tick_utc",
    }
    if set(manifest) != required_manifest:
        raise ValueError("screening archive manifest fields are invalid")
    if manifest["record_type"] != "screening_archive_manifest":
        raise ValueError("screening archive manifest record type is invalid")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != ARCHIVE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported screening archive schema version")
    if (
        not isinstance(manifest["session"], str)
        or manifest["session"] != filename_session
    ):
        raise ValueError("screening archive session does not match its filename")
    if manifest["tick_columns"] != list(TICK_COLUMNS):
        raise ValueError("screening archive tick columns are invalid")
    if manifest["event_columns"] != list(EVENT_COLUMNS):
        raise ValueError("screening archive event columns are invalid")
    for field in ("tick_count", "event_count", "failed_invariant_ticks"):
        if type(manifest[field]) is not int or manifest[field] < 0:
            raise ValueError(f"screening archive manifest {field} is invalid")
    for field in ("first_tick_utc", "last_tick_utc"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise ValueError(f"screening archive manifest {field} is invalid")

    tick_count = event_count = failed_invariants = 0
    tick_ids: set[int] = set()
    first_tick = last_tick = None
    prior_tick_id = prior_event_seq = None
    in_events = False
    for line_number, line in enumerate(lines, 2):
        payload = _strict_json(line, context=f"line {line_number}")
        if not isinstance(payload, dict):
            raise ValueError(f"invalid screening archive record at line {line_number}")
        if set(payload) != {"record_type", "row"} or not isinstance(
            payload["row"], dict
        ):
            raise ValueError(f"invalid screening archive record at line {line_number}")
        record_type = payload["record_type"]
        row = payload["row"]
        if record_type == "screening_tick":
            if in_events or set(row) != set(TICK_COLUMNS):
                raise ValueError("screening archive tick order or columns are invalid")
            tick_id = row["tick_id"]
            if type(tick_id) is not int:
                raise ValueError("screening archive tick ID is invalid")
            if prior_tick_id is not None and tick_id <= prior_tick_id:
                raise ValueError("screening archive tick IDs are not strictly increasing")
            if row["session"] != filename_session:
                raise ValueError("screening archive contains a foreign session tick")
            if not isinstance(row["tick_utc"], str) or not row["tick_utc"]:
                raise ValueError("screening archive tick timestamp is invalid")
            if (
                type(row["invariant_ok"]) is not int
                or row["invariant_ok"] not in {0, 1}
            ):
                raise ValueError("screening archive invariant flag is invalid")
            tick_ids.add(tick_id)
            prior_tick_id = tick_id
            tick_count += 1
            failed_invariants += int(row["invariant_ok"] == 0)
            first_tick = (
                row["tick_utc"]
                if first_tick is None
                else min(first_tick, row["tick_utc"])
            )
            last_tick = (
                row["tick_utc"]
                if last_tick is None
                else max(last_tick, row["tick_utc"])
            )
        elif record_type == "screening_event":
            in_events = True
            if set(row) != set(EVENT_COLUMNS):
                raise ValueError("screening archive event columns are invalid")
            sequence = row["seq"]
            tick_id = row["tick_id"]
            if type(sequence) is not int or type(tick_id) is not int:
                raise ValueError("screening archive event identity is invalid")
            if prior_event_seq is not None and sequence <= prior_event_seq:
                raise ValueError("screening archive event sequences are not strictly increasing")
            if tick_id not in tick_ids:
                raise ValueError("screening archive event references an absent tick")
            prior_event_seq = sequence
            event_count += 1
        else:
            raise ValueError(f"unexpected screening archive record type {record_type!r}")

    observed = {
        "tick_count": tick_count,
        "event_count": event_count,
        "failed_invariant_ticks": failed_invariants,
        "first_tick_utc": first_tick,
        "last_tick_utc": last_tick,
    }
    for field, value in observed.items():
        if manifest[field] != value:
            raise ValueError(f"screening archive {field} does not reconcile")
    return ScreeningArchiveReport(
        path=str(archive),
        sha256=observed_digest,
        session=filename_session,
        tick_count=tick_count,
        event_count=event_count,
        failed_invariant_ticks=failed_invariants,
        first_tick_utc=str(first_tick),
        last_tick_utc=str(last_tick),
        idempotent=False,
    )


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--session")
    parser.add_argument("--now", help="timezone-aware ISO timestamp (tests/operations)")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    if args.verify is not None:
        print(json.dumps(asdict(verify_screening_archive(args.verify)), sort_keys=True))
        return 0
    now = _parse_now(args.now)
    if args.session is not None:
        report = archive_screening_session(
            args.database,
            args.output_directory,
            session=args.session,
            now=now,
        )
        print(json.dumps(asdict(report), sort_keys=True))
        return 0
    sessions = pending_closed_sessions(
        args.database, args.output_directory, now=now
    )
    reports = [
        archive_screening_session(
            args.database,
            args.output_directory,
            session=session,
            now=now,
        )
        for session in sessions
    ]
    status = "archived" if reports else "no_pending_closed_screening_session"
    print(
        json.dumps(
            {"status": status, "archives": [asdict(report) for report in reports]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
