"""Tests for the append-only decision_events ledger in tradebot.journal.

Foundation only: nothing in the pipeline writes to this table yet, so
these tests exercise the helper and the table's append-only guarantee
directly rather than through the runner.
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


def test_record_decision_event_optional_fields_default_to_none(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)

    event = decision_events_for_detection(conn, detection_id)[0]
    assert event.reason is None
    assert event.detail is None
    assert event.code_version is None


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
