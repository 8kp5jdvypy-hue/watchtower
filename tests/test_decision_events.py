"""Tests for the append-only decision_events ledger in tradebot.journal.

These exercise the helper and the table's append-only guarantee directly.
The runner's own call sites — which branches record an event, in what
order, and under which run attribution — are covered separately in
tests/test_runner_decision_events.py.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from tradebot import journal
from tradebot.detectors import Detection
from tradebot.journal import (
    MAX_DECISION_DETAIL_JSON_LEN,
    RUN_MODE_LIVE,
    RUN_MODE_REPLAY,
    RUN_MODE_UNKNOWN,
    UNATTRIBUTED_RUN_ID,
    connect,
    decision_events_for_detection,
    new_run_id,
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
    """The helper commits itself by default (same as record_iv_sample /
    record_contract_selection), so a standalone caller — one not already
    inside a transaction of its own — can't lose the record of a decision
    to a crash right after taking it. See the commit=False tests below
    for the caller that owns its own boundary."""
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


# ---------------------------------------------------------------------------
# commit=False — for a caller that already owns a transaction boundary
# ---------------------------------------------------------------------------


def test_commit_false_leaves_the_row_invisible_until_the_caller_commits(tmp_path):
    """An uncommitted SQLite transaction is invisible to every other
    connection, and is exactly what a SIGKILL discards — so a second
    connection seeing nothing here is precisely 'the helper did not
    commit on its own'."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)
    conn.commit()

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS, commit=False)

    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0

    conn.commit()
    assert other.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 1


def test_commit_false_does_not_flush_the_callers_own_pending_writes(tmp_path):
    """The property runner.process_new_bar depends on: recording a
    decision must not commit the detection the caller is deliberately
    still holding open (see runner._commit_then_send)."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    detection_id = _write_detection(conn)  # write_cluster does NOT commit

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS, commit=False)

    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0
    assert other.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0


def test_commit_false_rows_roll_back_with_the_caller(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS, commit=False)
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0


def test_commit_true_remains_the_default(tmp_path):
    """Nothing about the standalone contract changed."""
    import inspect

    assert inspect.signature(record_decision_event).parameters["commit"].default is True


# ---------------------------------------------------------------------------
# run_mode / run_id
# ---------------------------------------------------------------------------


def test_run_mode_and_run_id_round_trip(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)
    run_id = new_run_id()

    record_decision_event(
        conn, detection_id, stage="dedup", decision="pass", ts_utc=TS,
        run_mode=RUN_MODE_REPLAY, run_id=run_id,
    )

    event = decision_events_for_detection(conn, detection_id)[0]
    assert event.run_mode == RUN_MODE_REPLAY
    assert event.run_id == run_id


def test_an_omitted_run_is_recorded_as_unattributed_never_as_live(tmp_path):
    """'this row did not say' is a fact the ledger states out loud. A NULL
    would leave a reader free to assume live, which is the one reading
    that must never be available."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(conn, detection_id, stage="dedup", decision="pass", ts_utc=TS)

    event = decision_events_for_detection(conn, detection_id)[0]
    assert event.run_mode == RUN_MODE_UNKNOWN
    assert event.run_id == UNATTRIBUTED_RUN_ID
    assert event.run_mode != RUN_MODE_LIVE

    stored = conn.execute("SELECT run_mode, run_id FROM decision_events").fetchone()
    assert stored == (RUN_MODE_UNKNOWN, UNATTRIBUTED_RUN_ID)  # not NULL in the column either


@pytest.mark.parametrize("empty", [None, ""])
def test_a_falsy_run_attribution_collapses_to_the_loud_default(tmp_path, empty):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)

    record_decision_event(
        conn, detection_id, stage="dedup", decision="pass", ts_utc=TS,
        run_mode=empty, run_id=empty,
    )

    event = decision_events_for_detection(conn, detection_id)[0]
    assert event.run_mode == RUN_MODE_UNKNOWN
    assert event.run_id == UNATTRIBUTED_RUN_ID


def test_replayed_events_never_read_as_a_later_revision_of_the_live_ones(tmp_path):
    """detection_id is a hash of symbol/session/ts/kinds, so a replay of a
    live session appends to the SAME detection's history, after it. Only
    run_mode/run_id separate them."""
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_detection(conn)
    live_id, replay_id = new_run_id(), new_run_id()

    record_decision_event(
        conn, detection_id, stage="alert_routing", decision="send", ts_utc=TS,
        run_mode=RUN_MODE_LIVE, run_id=live_id,
    )
    record_decision_event(
        conn, detection_id, stage="alert_routing", decision="daily_cap_reached", ts_utc=TS,
        run_mode=RUN_MODE_REPLAY, run_id=replay_id,
    )

    events = decision_events_for_detection(conn, detection_id)
    assert [e.run_mode for e in events] == [RUN_MODE_LIVE, RUN_MODE_REPLAY]
    assert [e.run_id for e in events] == [live_id, replay_id]
    # The replay's row is the later one in the ledger — which is exactly
    # why a reader must filter on run_mode rather than take the last row.
    assert events[-1].run_mode == RUN_MODE_REPLAY


def test_new_run_id_is_unique_per_call():
    assert len({new_run_id() for _ in range(100)}) == 100


def test_run_columns_are_added_to_a_ledger_created_before_they_existed(tmp_path):
    """decision_events shipped one release before anything wrote to it, so
    a journal.db from that window has the table but neither run column."""
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    conn.execute("DROP TRIGGER decision_events_no_update")
    conn.execute("DROP TRIGGER decision_events_no_delete")
    conn.execute("DROP TABLE decision_events")
    conn.execute(
        """
        CREATE TABLE decision_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_id TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            stage TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            detail_json TEXT,
            code_version TEXT
        )
        """
    )
    conn.execute("INSERT INTO decision_events (detection_id, ts_utc, stage, decision) VALUES ('old','x','s','d')")
    conn.commit()
    conn.close()

    migrated = connect(db_path)

    columns = {row[1] for row in migrated.execute("PRAGMA table_info(decision_events)")}
    assert {"run_mode", "run_id"} <= columns
    assert migrated.execute("SELECT run_mode, run_id FROM decision_events").fetchone() == (
        RUN_MODE_UNKNOWN, UNATTRIBUTED_RUN_ID,
    )
