from __future__ import annotations

import json

import pytest

from tradebot import funnel_events
from tradebot.telegram_bot import db as users_db


@pytest.fixture
def conn(tmp_path):
    return users_db.connect(tmp_path / "users.db")


def test_record_event_writes_a_row(conn):
    ok = funnel_events.record_event(conn, event="landing_view", anon_id="anon-1")
    assert ok is True
    row = conn.execute("SELECT event, anon_id, account_id, props_json FROM funnel_events").fetchone()
    assert row == ("landing_view", "anon-1", None, None)


def test_record_event_captures_account_id_and_props(conn):
    funnel_events.record_event(
        conn, event="magic_link_sent", anon_id="anon-2", account_id="acct-1", props={"mode": "signup"}
    )
    row = conn.execute("SELECT account_id, props_json FROM funnel_events").fetchone()
    assert row[0] == "acct-1"
    assert json.loads(row[1]) == {"mode": "signup"}


def test_record_event_rejects_events_outside_the_allowlist(conn):
    ok = funnel_events.record_event(conn, event="literally_anything", anon_id="anon-3")
    assert ok is False
    assert conn.execute("SELECT COUNT(*) FROM funnel_events").fetchone()[0] == 0


def test_record_event_rejects_missing_anon_id(conn):
    ok = funnel_events.record_event(conn, event="landing_view", anon_id="")
    assert ok is False
    assert conn.execute("SELECT COUNT(*) FROM funnel_events").fetchone()[0] == 0


def test_record_event_drops_oversized_props_but_still_writes_the_event(conn):
    huge_props = {"blob": "x" * 2000}
    ok = funnel_events.record_event(conn, event="landing_view", anon_id="anon-4", props=huge_props)
    assert ok is True
    row = conn.execute("SELECT props_json FROM funnel_events").fetchone()
    assert row[0] is None


def test_counts_by_event_aggregates_across_multiple_anon_ids(conn):
    funnel_events.record_event(conn, event="landing_view", anon_id="a")
    funnel_events.record_event(conn, event="landing_view", anon_id="b")
    funnel_events.record_event(conn, event="signup_cta_click", anon_id="a")
    assert funnel_events.counts_by_event(conn) == {"landing_view": 2, "signup_cta_click": 1}


def test_counts_by_event_returns_empty_dict_with_no_data(conn):
    assert funnel_events.counts_by_event(conn) == {}
