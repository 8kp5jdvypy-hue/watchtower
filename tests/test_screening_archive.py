"""Immutable Stage-1 screening archive and verification contract."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot import screening_archive as archive_mod
from tradebot.screening_archive import (
    archive_screening_session,
    latest_closed_session,
    pending_closed_sessions,
    verify_screening_archive,
)
from tradebot.universe import connect


SESSION = "2026-08-28"
OTHER_SESSION = "2026-08-27"
NOW = datetime(2026, 8, 29, 0, 30, tzinfo=timezone.utc)


def _tick(
    connection: sqlite3.Connection,
    *,
    session: str = SESSION,
    tick_utc: str = "2026-08-28T20:00:00+00:00",
    invariant_ok: int = 1,
    suffix: str = "1",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO screening_ticks
          (session,tick_utc,run_id,run_mode,screen_version,code_version,
           audit_mode,universe_count,thresholds_json,counts_json,invariant_ok,
           promotion_limit,latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            session,
            tick_utc,
            f"run-{suffix}",
            "live",
            2,
            "abcdef1",
            0,
            13_095,
            '{"rvol":1.5}',
            '{"candidate":1,"quiet":13094}',
            invariant_ok,
            25,
            1234,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _event(
    connection: sqlite3.Connection,
    tick_id: int,
    *,
    symbol: str,
    outcome: str = "PROMOTED",
) -> None:
    connection.execute(
        """
        INSERT INTO screening_events
          (tick_id,symbol,outcome,screen_score,rank,reasons_json,detail_json)
        VALUES (?,?,?,?,?,?,?)
        """,
        (tick_id, symbol, outcome, 2.5, 1, '["fixture"]', '{"close":10.0}'),
    )
    connection.commit()


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "universe.db"
    connection = connect(path)
    first = _tick(
        connection,
        tick_utc="2026-08-28T19:30:00+00:00",
        suffix="first",
    )
    second = _tick(
        connection,
        tick_utc="2026-08-28T20:00:00+00:00",
        invariant_ok=0,
        suffix="second",
    )
    _event(connection, first, symbol="AAA")
    _event(connection, first, symbol="BBB", outcome="CANDIDATE_NOT_PROMOTED")
    _event(connection, second, symbol="CCC")
    connection.close()
    return path


def _archive(tmp_path: Path):
    database = _database(tmp_path)
    output = tmp_path / "archives"
    report = archive_screening_session(
        database, output, session=SESSION, now=NOW
    )
    return database, output, report


def _write_payload(path: Path, records: list[dict]) -> Path:
    temporary = path / "payload.jsonl.gz"
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
            for record in records:
                handle.write(
                    (
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    ).encode()
                )
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    final = path / f"screening_{SESSION}_{digest}.jsonl.gz"
    temporary.rename(final)
    return final


def test_archive_round_trip_reconciles_counts_identity_and_invariants(tmp_path):
    _, _, report = _archive(tmp_path)

    assert report.session == SESSION
    assert report.tick_count == 2
    assert report.event_count == 3
    assert report.failed_invariant_ticks == 1
    assert report.first_tick_utc == "2026-08-28T19:30:00+00:00"
    assert report.last_tick_utc == "2026-08-28T20:00:00+00:00"
    assert report.idempotent is False
    archive = Path(report.path)
    assert archive.name == f"screening_{SESSION}_{report.sha256}.jsonl.gz"
    assert archive.stat().st_mode & 0o222 == 0
    assert verify_screening_archive(archive).sha256 == report.sha256


def test_identical_retry_is_byte_deterministic_and_idempotent(tmp_path):
    database, output, first = _archive(tmp_path)

    second = archive_screening_session(
        database, output, session=SESSION, now=NOW
    )

    assert second.path == first.path
    assert second.sha256 == first.sha256
    assert second.idempotent is True
    assert len(list(output.glob("screening_*.jsonl.gz"))) == 1
    assert not list(output.glob(".*.tmp"))


def test_late_append_creates_new_archive_without_replacing_prior_evidence(tmp_path):
    database, output, first = _archive(tmp_path)
    connection = sqlite3.connect(database)
    late = _tick(
        connection,
        tick_utc="2026-08-28T20:01:00+00:00",
        suffix="late",
    )
    _event(connection, late, symbol="LATE")
    connection.close()

    second = archive_screening_session(
        database, output, session=SESSION, now=NOW
    )

    assert second.sha256 != first.sha256
    assert Path(first.path).is_file()
    assert Path(second.path).is_file()
    assert second.tick_count == 3
    assert second.event_count == 4


def test_nightly_pending_scan_detects_late_append_after_existing_archive(tmp_path):
    database, output, _ = _archive(tmp_path)
    connection = sqlite3.connect(database)
    late = _tick(
        connection,
        tick_utc="2026-08-28T20:01:00+00:00",
        suffix="late-pending",
    )
    _event(connection, late, symbol="LATE")
    connection.close()

    assert pending_closed_sessions(database, output, now=NOW) == (SESSION,)


def test_stage1_source_rows_are_append_only(tmp_path):
    database = _database(tmp_path)
    connection = connect(database)

    with pytest.raises(sqlite3.IntegrityError, match="screening_ticks is append-only"):
        connection.execute("UPDATE screening_ticks SET latency_ms=0")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="screening_ticks is append-only"):
        connection.execute("DELETE FROM screening_ticks")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="screening_events is append-only"):
        connection.execute("UPDATE screening_events SET outcome='QUIET'")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="screening_events is append-only"):
        connection.execute("DELETE FROM screening_events")
    connection.rollback()

    assert connection.execute("SELECT COUNT(*) FROM screening_ticks").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM screening_events").fetchone()[0] == 3
    connection.close()


def test_two_destinations_produce_identical_archive_bytes(tmp_path):
    database = _database(tmp_path)

    first = archive_screening_session(
        database, tmp_path / "one", session=SESSION, now=NOW
    )
    second = archive_screening_session(
        database, tmp_path / "two", session=SESSION, now=NOW
    )

    assert first.sha256 == second.sha256
    assert Path(first.path).read_bytes() == Path(second.path).read_bytes()


def test_session_must_be_xnys_and_final_before_any_artifact(tmp_path):
    database = _database(tmp_path)
    output = tmp_path / "archives"

    with pytest.raises(ValueError, match="not an XNYS session"):
        archive_screening_session(
            database, output, session="2026-08-29", now=NOW
        )
    with pytest.raises(ValueError, match="not final yet"):
        archive_screening_session(
            database,
            output,
            session=SESSION,
            now=datetime(2026, 8, 28, 20, 10, tzinfo=timezone.utc),
        )

    assert not list(output.glob("screening_*.jsonl.gz"))


def test_empty_completed_session_fails_without_publishing(tmp_path):
    database = tmp_path / "universe.db"
    connect(database).close()
    output = tmp_path / "archives"

    with pytest.raises(ValueError, match="has no ticks"):
        archive_screening_session(
            database, output, session=SESSION, now=NOW
        )

    assert not list(output.glob("screening_*.jsonl.gz"))


def test_source_schema_change_fails_closed_before_silently_omitting_evidence(tmp_path):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("ALTER TABLE screening_events ADD COLUMN future_evidence TEXT")
    connection.commit()
    connection.close()
    output = tmp_path / "archives"

    with pytest.raises(ValueError, match="schema changed; bump the screening archive contract"):
        archive_screening_session(
            database, output, session=SESSION, now=NOW
        )

    assert not list(output.glob("screening_*.jsonl.gz"))


def test_latest_closed_session_skips_only_unclosed_sessions(tmp_path):
    database = tmp_path / "universe.db"
    connection = connect(database)
    _tick(connection, session=OTHER_SESSION, tick_utc="2026-08-27T20:00:00+00:00")
    _tick(connection, session=SESSION, tick_utc="2026-08-28T20:00:00+00:00")
    connection.close()

    assert latest_closed_session(database, now=NOW) == SESSION
    assert (
        latest_closed_session(
            database,
            now=datetime(2026, 8, 28, 20, 10, tzinfo=timezone.utc),
        )
        == OTHER_SESSION
    )


def test_malformed_stored_session_fails_loudly_instead_of_skipping_evidence(tmp_path):
    database = tmp_path / "universe.db"
    connection = connect(database)
    _tick(connection, session="not-a-date", tick_utc="2026-08-28T20:00:00+00:00")
    connection.close()

    with pytest.raises(ValueError, match="session must be an ISO date"):
        latest_closed_session(database, now=NOW)
    with pytest.raises(ValueError, match="session must be an ISO date"):
        pending_closed_sessions(database, tmp_path / "archives", now=NOW)


def test_pending_sessions_catch_up_all_missing_history_and_then_noop(tmp_path, capsys):
    database = tmp_path / "universe.db"
    connection = connect(database)
    _tick(connection, session=OTHER_SESSION, tick_utc="2026-08-27T20:00:00+00:00")
    _tick(connection, session=SESSION, tick_utc="2026-08-28T20:00:00+00:00")
    connection.close()
    output = tmp_path / "archives"

    assert pending_closed_sessions(database, output, now=NOW) == (
        OTHER_SESSION,
        SESSION,
    )
    assert archive_mod.main(
        [
            "--database",
            str(database),
            "--output-directory",
            str(output),
            "--now",
            NOW.isoformat(),
        ]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "archived"
    assert [row["session"] for row in first["archives"]] == [
        OTHER_SESSION,
        SESSION,
    ]
    assert pending_closed_sessions(database, output, now=NOW) == ()

    assert archive_mod.main(
        [
            "--database",
            str(database),
            "--output-directory",
            str(output),
            "--now",
            NOW.isoformat(),
        ]
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == {
        "status": "no_pending_closed_screening_session",
        "archives": [],
    }


def test_corrupt_existing_archive_blocks_pending_scan(tmp_path):
    database, output, report = _archive(tmp_path)
    archive = Path(report.path)
    archive.chmod(0o644)
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="digest does not match"):
        pending_closed_sessions(database, output, now=NOW)


def test_tampering_is_rejected_by_filename_digest(tmp_path):
    _, _, report = _archive(tmp_path)
    archive = Path(report.path)
    archive.chmod(0o644)
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="digest does not match"):
        verify_screening_archive(archive)


def test_digest_valid_malformed_manifest_is_rejected(tmp_path):
    archive = _write_payload(tmp_path, [{"record_type": "wrong"}])

    with pytest.raises(ValueError, match="manifest fields are invalid"):
        verify_screening_archive(archive)


def test_digest_valid_duplicate_json_keys_are_rejected(tmp_path):
    payload = b'{"record_type":"wrong","record_type":"screening_archive_manifest"}\n'
    temporary = tmp_path / "payload.jsonl.gz"
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
            handle.write(payload)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    final = tmp_path / f"screening_{SESSION}_{digest}.jsonl.gz"
    temporary.rename(final)

    with pytest.raises(ValueError, match="invalid screening archive JSON in manifest"):
        verify_screening_archive(final)


def test_digest_valid_invalid_gzip_is_normalized_to_verification_failure(tmp_path):
    payload = b"not-a-gzip-stream"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / f"screening_{SESSION}_{digest}.jsonl.gz"
    archive.write_bytes(payload)

    with pytest.raises(ValueError, match="gzip stream is unreadable"):
        verify_screening_archive(archive)


def test_digest_valid_event_referencing_absent_tick_is_rejected(tmp_path):
    manifest = {
        "record_type": "screening_archive_manifest",
        "schema_version": 1,
        "session": SESSION,
        "tick_columns": list(archive_mod.TICK_COLUMNS),
        "event_columns": list(archive_mod.EVENT_COLUMNS),
        "tick_count": 0,
        "event_count": 1,
        "failed_invariant_ticks": 0,
        "first_tick_utc": "2026-08-28T20:00:00+00:00",
        "last_tick_utc": "2026-08-28T20:00:00+00:00",
    }
    event = {column: None for column in archive_mod.EVENT_COLUMNS}
    event.update({"seq": 1, "tick_id": 999, "symbol": "BAD", "outcome": "PROMOTED"})
    archive = _write_payload(
        tmp_path,
        [manifest, {"record_type": "screening_event", "row": event}],
    )

    with pytest.raises(ValueError, match="references an absent tick"):
        verify_screening_archive(archive)


def test_publication_failure_removes_temporary_file_and_source_is_untouched(
    tmp_path, monkeypatch
):
    database = _database(tmp_path)
    source_count = sqlite3.connect(database).execute(
        "SELECT COUNT(*) FROM screening_ticks"
    ).fetchone()[0]
    output = tmp_path / "archives"

    def fail_link(_source, _destination):
        raise OSError("injected publication failure")

    monkeypatch.setattr(archive_mod.os, "link", fail_link)
    with pytest.raises(OSError, match="injected publication failure"):
        archive_screening_session(
            database, output, session=SESSION, now=NOW
        )

    assert not list(output.iterdir())
    assert sqlite3.connect(database).execute(
        "SELECT COUNT(*) FROM screening_ticks"
    ).fetchone()[0] == source_count


def test_concurrent_identical_publication_is_an_idempotent_success(tmp_path, monkeypatch):
    database = _database(tmp_path)
    output = tmp_path / "archives"

    def competing_writer(source, destination):
        shutil.copyfile(source, destination)
        raise FileExistsError("simulated concurrent publisher")

    monkeypatch.setattr(archive_mod.os, "link", competing_writer)
    report = archive_screening_session(
        database, output, session=SESSION, now=NOW
    )

    assert report.idempotent is True
    assert verify_screening_archive(report.path).sha256 == report.sha256
    assert not list(output.glob(".*.tmp"))


def test_symlinked_output_directory_is_rejected(tmp_path):
    database = _database(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        archive_screening_session(
            database, link, session=SESSION, now=NOW
        )


def test_cli_no_closed_session_is_clean_noop(tmp_path, capsys):
    database = tmp_path / "universe.db"
    connect(database).close()

    assert archive_mod.main(
        [
            "--database",
            str(database),
            "--output-directory",
            str(tmp_path / "archives"),
            "--now",
            NOW.isoformat(),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "no_pending_closed_screening_session",
        "archives": [],
    }
