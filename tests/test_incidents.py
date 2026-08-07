"""Tests for tradebot.incidents — the append-only open/close log behind
the public status page's incident history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot import incidents

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


def test_open_then_close_records_a_full_incident(tmp_path):
    path = tmp_path / "incidents.jsonl"
    incidents.open_incident("heartbeat_stale", "no evaluation in 5 min", NOW, path=path)
    incidents.close_incident("heartbeat_stale", NOW + timedelta(minutes=10), path=path)

    all_incidents = incidents.list_incidents(path=path)
    assert len(all_incidents) == 1
    assert all_incidents[0]["kind"] == "heartbeat_stale"
    assert all_incidents[0]["started_at"] == NOW.isoformat()
    assert all_incidents[0]["ended_at"] == (NOW + timedelta(minutes=10)).isoformat()


def test_opening_twice_while_already_open_does_not_duplicate(tmp_path):
    path = tmp_path / "incidents.jsonl"
    incidents.open_incident("heartbeat_stale", "first check", NOW, path=path)
    incidents.open_incident("heartbeat_stale", "second check, still stale", NOW + timedelta(seconds=30), path=path)

    all_incidents = incidents.list_incidents(path=path)
    assert len(all_incidents) == 1
    assert all_incidents[0]["detail"] == "first check"  # the original open wins, not overwritten


def test_closing_with_nothing_open_is_a_safe_no_op(tmp_path):
    path = tmp_path / "incidents.jsonl"
    incidents.close_incident("halt", NOW, path=path)  # must not raise
    assert incidents.list_incidents(path=path) == []


def test_a_new_incident_can_open_after_the_previous_one_of_the_same_kind_closed(tmp_path):
    path = tmp_path / "incidents.jsonl"
    incidents.open_incident("halt", "first", NOW, path=path)
    incidents.close_incident("halt", NOW + timedelta(hours=1), path=path)
    incidents.open_incident("halt", "second", NOW + timedelta(days=1), path=path)

    all_incidents = incidents.list_incidents(path=path)
    assert len(all_incidents) == 2
    assert all_incidents[0]["ended_at"] is not None
    assert all_incidents[1]["ended_at"] is None
    assert all_incidents[1]["detail"] == "second"


def test_different_kinds_track_independently(tmp_path):
    path = tmp_path / "incidents.jsonl"
    incidents.open_incident("halt", "manual stop", NOW, path=path)
    incidents.open_incident("heartbeat_stale", "feed went quiet", NOW, path=path)

    all_incidents = incidents.list_incidents(path=path)
    assert {i["kind"] for i in all_incidents} == {"halt", "heartbeat_stale"}
    assert all(i["ended_at"] is None for i in all_incidents)


def test_a_corrupted_line_does_not_take_down_the_whole_log(tmp_path):
    path = tmp_path / "incidents.jsonl"
    incidents.open_incident("halt", "manual stop", NOW, path=path)
    with open(path, "a") as f:
        f.write("not valid json\n")

    all_incidents = incidents.list_incidents(path=path)
    assert len(all_incidents) == 1
    assert all_incidents[0]["kind"] == "halt"


def test_list_incidents_on_a_missing_file_returns_empty(tmp_path):
    assert incidents.list_incidents(path=tmp_path / "does_not_exist.jsonl") == []
