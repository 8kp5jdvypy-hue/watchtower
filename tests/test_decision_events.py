"""Tests for the append-only decision_events ledger in tradebot.journal.

These exercise the storage helper and the table's append-only guarantee
directly. The pipeline's own call sites — which decisions runner.py
records, and when it commits them — are covered in
test_decision_event_wiring.py.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from tradebot import journal
from tradebot.detectors import Detection
from tradebot.journal import (
    MAX_DECISION_DETAIL_JSON_LEN,
    connect,
    decision_events_for_detection,
    record_decision_event,
    record_iv_sample,
    set_news_driven,
    set_no_trade,
    write_cluster,
)

SYMBOL = "TEST"
TS = datetime(2026, 6, 15, 14, 5, tzinfo=timezone.utc)


def _detection(kind="level_break", score=4.0) -> Detection:
    return Detection(SYMBOL, kind, TS, score, "headline", {"foo": "bar"})


def _write_detection(conn, *, kinds="level_break", score=4.0) -> str:
    return write_cluster(
        conn, session="2026-06-15", symbol=SYMBOL, ts_utc="2026-06-15T14:00:00+00:00",
        kinds=kinds, headlines="broke prior_high", score=score, close=101.0, atr14=1.5,
        trend="up", detections=[_detection()], code_version_str="abc123",
    )


# ---------------------------------------------------------------------------
# Inserts work
# ---------------------------------------------------------------------------


def test_record_decision_event_round_trips_every_field(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    seq = record_decision_event(
        conn, detection_id,
        stage="alert_budget", decision="send", reason="under_daily_cap",
        detail={"cap": 6, "sent_today": 2}, ts_utc=TS, code_version_str="abc123",
        run_id="run-abc", run_mode=journal.RUN_MODE_LIVE,
    )

    assert seq == 1
    events = decision_events_for_detection(conn, detection_id)
    assert len(events) == 1
    event = events[0]
    assert event.seq == 1
    assert event.detection_id == detection_id
    assert event.ts_utc == "2026-06-15T14:05:00+00:00"
    assert event.stage == "alert_budget"
    assert event.decision == "send"
    assert event.reason == "under_daily_cap"
    assert event.detail == {"cap": 6, "sent_today": 2}
    assert event.code_version == "abc123"
    assert event.run_id == "run-abc"
    assert event.run_mode == "live"


def test_record_decision_event_optional_fields_default_to_none(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)

    event = decision_events_for_detection(conn, detection_id)[0]
    assert event.reason is None
    assert event.detail is None
    assert event.code_version is None
    # A caller that isn't a run records that it isn't one, rather than
    # getting a run identity invented for it.
    assert event.run_id is None
    assert event.run_mode is None


# ---------------------------------------------------------------------------
# commit=False — the caller owns the transaction
# ---------------------------------------------------------------------------


def test_commit_defaults_to_true_and_makes_the_row_immediately_durable(tmp_path):
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.commit()

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)

    observer = connect(db_path)  # separate connection: sees only committed rows
    assert observer.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 1


def test_commit_false_writes_the_row_but_leaves_durability_to_the_caller(tmp_path):
    """What runner.py's process_new_bar needs: the event is in the
    transaction, but the decision of WHEN that transaction closes stays
    with the code that orders its commits against an irreversible send.
    """
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.commit()

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS, commit=False)

    # Visible on the writing connection...
    assert len(decision_events_for_detection(conn, detection_id)) == 1
    # ...and to nobody else, until the caller says so.
    observer = connect(db_path)
    assert observer.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0

    conn.commit()

    after = connect(db_path)
    assert after.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 1


def test_commit_false_events_are_lost_with_the_transaction_they_rode_in_on(tmp_path):
    """The accepted consequence, asserted rather than assumed: a decision
    whose transaction rolled back is a decision that didn't end up
    happening, and the ledger should not claim otherwise."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.commit()

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS, commit=False)
    conn.rollback()

    assert decision_events_for_detection(conn, detection_id) == []


def test_record_decision_event_commits_without_caller_commit(tmp_path):
    """The helper commits itself (same as record_iv_sample /
    record_contract_selection), so a crash after the decision is taken
    can't lose the record of it."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.commit()

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)

    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 1


def test_ts_utc_defaults_to_now_and_naive_datetimes_are_stamped_utc(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    before = datetime.now(timezone.utc)
    record_decision_event(conn, detection_id, stage="dedup", decision="pass")
    after = datetime.now(timezone.utc)
    record_decision_event(
        conn, detection_id, stage="dedup", decision="pass",
        ts_utc=datetime(2026, 6, 15, 14, 5),  # naive
    )

    defaulted, naive = decision_events_for_detection(conn, detection_id)
    assert before <= datetime.fromisoformat(defaulted.ts_utc) <= after
    # Never recorded without an offset -- a bare "2026-06-15T14:05:00" in
    # a ledger is unreadable later without guessing the zone.
    assert naive.ts_utc == "2026-06-15T14:05:00+00:00"
    assert datetime.fromisoformat(naive.ts_utc).tzinfo is not None


def test_oversized_detail_is_dropped_not_truncated(tmp_path):
    """Half a JSON document isn't a smaller fact, it's an unparseable one
    -- the event itself is still recorded."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(
        conn, detection_id, stage="alert_budget", decision="send",
        detail={"blob": "x" * (MAX_DECISION_DETAIL_JSON_LEN + 100)}, ts_utc=TS,
    )

    event = decision_events_for_detection(conn, detection_id)[0]
    assert event.detail is None
    assert event.decision == "send"


def test_events_for_unknown_detection_is_empty(tmp_path):
    conn = connect(tmp_path / "journal.db")
    assert decision_events_for_detection(conn, "no-such-detection") == []


# ---------------------------------------------------------------------------
# Multiple events per detection are preserved
# ---------------------------------------------------------------------------


def test_multiple_events_per_detection_are_all_preserved_in_append_order(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(conn, detection_id, stage="guard", decision="pass", ts_utc=TS)
    record_decision_event(conn, detection_id, stage="news_window", decision="no_window", ts_utc=TS)
    record_decision_event(conn, detection_id, stage="dedup", decision="watch", ts_utc=TS)
    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)

    events = decision_events_for_detection(conn, detection_id)
    assert [e.stage for e in events] == ["guard", "news_window", "dedup", "alert_budget"]
    assert [e.seq for e in events] == sorted(e.seq for e in events)


def test_identical_stage_and_decision_repeats_are_kept_as_separate_rows(tmp_path):
    """No upsert key, no de-duplication: reaching the same decision point
    twice is a real fact about what happened, not a row to collapse."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    first = record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)
    second = record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)

    assert first != second
    events = decision_events_for_detection(conn, detection_id)
    assert len(events) == 2
    assert events[0].stage == events[1].stage == "dedup"


def test_events_are_scoped_per_detection(tmp_path):
    conn = connect(tmp_path / "journal.db")
    first_id = _write_detection(conn, kinds="level_break")
    second_id = _write_detection(conn, kinds="squeeze_release")
    assert first_id != second_id

    record_decision_event(conn, first_id, stage="alert_budget", decision="send", ts_utc=TS)
    record_decision_event(conn, second_id, stage="alert_budget", decision="cooldown_active", ts_utc=TS)
    record_decision_event(conn, second_id, stage="dedup", decision="duplicate_event", ts_utc=TS)

    assert [e.decision for e in decision_events_for_detection(conn, first_id)] == ["send"]
    assert [e.decision for e in decision_events_for_detection(conn, second_id)] == [
        "cooldown_active", "duplicate_event",
    ]


def test_a_superseding_event_never_rewrites_the_one_it_supersedes(tmp_path):
    """The ledger's whole point: correcting the record appends, so the
    earlier decision is still readable afterwards."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)
    record_decision_event(
        conn, detection_id, stage="alert_budget", decision="cooldown_active",
        reason="superseded: cooldown re-evaluated", ts_utc=TS,
    )

    events = decision_events_for_detection(conn, detection_id)
    assert [e.decision for e in events] == ["send", "cooldown_active"]


# ---------------------------------------------------------------------------
# No update/delete behavior exists
# ---------------------------------------------------------------------------


def test_journal_module_exposes_no_update_or_delete_path_for_the_ledger(tmp_path):
    """No helper in tradebot.journal can modify or remove an appended
    event -- append and read are the entire surface."""
    ledger_helpers = [
        name for name in dir(journal)
        if "decision_event" in name and callable(getattr(journal, name))
    ]
    assert sorted(ledger_helpers) == ["decision_events_for_detection", "record_decision_event"]


def test_direct_update_of_an_appended_event_is_rejected(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)
    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE decision_events SET decision = 'cooldown_active' WHERE seq = 1")

    assert decision_events_for_detection(conn, detection_id)[0].decision == "send"


def test_direct_delete_of_an_appended_event_is_rejected(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)
    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM decision_events WHERE seq = 1")

    assert len(decision_events_for_detection(conn, detection_id)) == 1


def test_bulk_delete_of_the_whole_ledger_is_rejected(tmp_path):
    """Including the shapes that don't name a row -- an unqualified
    DELETE is exactly what an append-only table must refuse."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)
    record_decision_event(conn, detection_id, stage="guard", decision="pass", ts_utc=TS)
    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM decision_events")

    assert len(decision_events_for_detection(conn, detection_id)) == 2


def test_seq_is_never_reused(tmp_path):
    """AUTOINCREMENT, so append order stays a total order for the life of
    the file rather than a rowid that could be handed out twice."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    seqs = [
        record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)
        for _ in range(3)
    ]

    assert seqs == [1, 2, 3]
    assert conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'decision_events'"
    ).fetchone()[0] == 3


def test_append_only_survives_reconnecting_to_an_existing_db(tmp_path):
    """The triggers live in SCHEMA, which connect() re-runs with IF NOT
    EXISTS -- reopening the file must not drop or bypass them."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)
    conn.commit()
    conn.close()

    reopened = connect(db_path)
    assert len(decision_events_for_detection(reopened, detection_id)) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        reopened.execute("DELETE FROM decision_events")


# ---------------------------------------------------------------------------
# Existing journal behavior is unchanged
# ---------------------------------------------------------------------------


def test_detections_table_still_upserts_and_updates_as_before(tmp_path):
    """The ledger's append-only triggers are scoped to decision_events;
    the mutable snapshot behavior the rest of the module depends on must
    be untouched."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn, score=4.0)

    # Same cluster identity re-written -> upsert, not a second row.
    again = _write_detection(conn, score=5.5)
    assert again == detection_id
    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 1
    assert conn.execute("SELECT score FROM detections").fetchone()[0] == 5.5

    # The UPDATE-based setters still work.
    set_no_trade(conn, detection_id, True)
    set_news_driven(conn, detection_id, True, kind="earnings", severity="suppress")
    row = conn.execute(
        "SELECT no_trade, news_driven, event_kind FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()
    assert row == (1, 1, "earnings")

    # And other tables' own upsert paths are unaffected.
    record_iv_sample(conn, SYMBOL, date(2026, 6, 15), 0.31)
    record_iv_sample(conn, SYMBOL, date(2026, 6, 15), 0.42)
    assert conn.execute("SELECT iv FROM iv_history").fetchone()[0] == 0.42


def test_recording_a_decision_event_does_not_touch_the_detections_row(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)
    conn.commit()
    before = conn.execute("SELECT * FROM detections WHERE id = ?", (detection_id,)).fetchone()

    record_decision_event(conn, detection_id, stage="alert_budget", decision="send", ts_utc=TS)

    after = conn.execute("SELECT * FROM detections WHERE id = ?", (detection_id,)).fetchone()
    assert after == before


def test_connect_adds_the_ledger_to_a_pre_existing_db_without_disturbing_it(tmp_path):
    """Migration path: an existing data/journal.db predates this table.
    connect() creates it in place, and the rows already in the file are
    left exactly as they were."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.commit()

    # Simulate the pre-migration file: drop the table and its triggers.
    conn.execute("DROP TRIGGER decision_events_no_update")
    conn.execute("DROP TRIGGER decision_events_no_delete")
    conn.execute("DROP TABLE decision_events")
    conn.commit()
    detections_before = conn.execute("SELECT * FROM detections").fetchall()
    conn.close()

    migrated = connect(db_path)

    assert migrated.execute("SELECT * FROM detections").fetchall() == detections_before
    assert migrated.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    seq = record_decision_event(migrated, detection_id, stage="dedup", decision="pass", ts_utc=TS)
    assert seq == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated.execute("DELETE FROM decision_events")


# The ledger's first release had no run_id/run_mode. This is the exact
# shape a journal.db written by it has on disk.
_LEDGER_WITHOUT_RUN_COLUMNS = """
CREATE TABLE decision_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    detail_json TEXT,
    code_version TEXT
);
"""


def test_connect_adds_the_run_columns_to_a_ledger_that_predates_them(tmp_path):
    """The migration that cannot be done with CREATE TABLE IF NOT EXISTS:
    the table already exists, so only ALTER TABLE reaches it. The row
    written before the columns existed keeps saying exactly what it said
    — NULL, meaning nobody recorded a run, which is true of it."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.execute("DROP TRIGGER decision_events_no_update")
    conn.execute("DROP TRIGGER decision_events_no_delete")
    conn.execute("DROP TABLE decision_events")
    conn.executescript(_LEDGER_WITHOUT_RUN_COLUMNS)
    conn.execute(
        "INSERT INTO decision_events (detection_id, ts_utc, stage, decision) VALUES (?, ?, ?, ?)",
        (detection_id, TS.isoformat(), "dedup", "pass"),
    )
    conn.commit()
    conn.close()

    migrated = connect(db_path)

    columns = {row[1] for row in migrated.execute("PRAGMA table_info(decision_events)")}
    assert {"run_id", "run_mode"} <= columns

    record_decision_event(
        migrated, detection_id, stage="alert_decision", decision="send", ts_utc=TS,
        run_id="run-1", run_mode=journal.RUN_MODE_REPLAY,
    )
    old_event, new_event = decision_events_for_detection(migrated, detection_id)
    assert (old_event.run_id, old_event.run_mode) == (None, None)
    assert (new_event.run_id, new_event.run_mode) == ("run-1", "replay")


def test_connect_indexes_run_id_without_aborting_the_rest_of_the_schema(tmp_path):
    """The index on run_id cannot live in SCHEMA: executescript runs
    before the ALTER TABLEs, so a CREATE INDEX on a column an existing
    file doesn't have yet would raise and take every later statement in
    SCHEMA down with it. This asserts both halves — the index exists, and
    a table created by a statement further down SCHEMA does too."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    conn.execute("DROP TRIGGER decision_events_no_update")
    conn.execute("DROP TRIGGER decision_events_no_delete")
    conn.execute("DROP TABLE decision_events")
    conn.executescript(_LEDGER_WITHOUT_RUN_COLUMNS)
    conn.commit()
    conn.close()

    migrated = connect(db_path)

    indexes = {row[0] for row in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'decision_events'"
    )}
    assert "idx_decision_events_run" in indexes
    # SCHEMA ran to completion: the triggers declared after the table are
    # back, so the ledger is append-only again on this migrated file. The
    # row matters -- a BEFORE DELETE trigger fires per row deleted, so on
    # an empty table this assertion would pass with no triggers at all.
    detection_id = _write_detection(migrated)
    record_decision_event(migrated, detection_id, stage="dedup", decision="pass", ts_utc=TS)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated.execute("DELETE FROM decision_events")
