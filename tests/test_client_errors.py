from __future__ import annotations

from tradebot import client_errors
from tradebot.telegram_bot import db as users_db
import pytest


@pytest.fixture
def conn(tmp_path):
    return users_db.connect(tmp_path / "users.db")


def test_record_error_writes_a_row(conn):
    ok = client_errors.record_error(conn, message="TypeError: x is not a function", stack="at foo (app.js:1:1)")
    assert ok is True
    row = conn.execute("SELECT message, stack FROM client_errors").fetchone()
    assert row == ("TypeError: x is not a function", "at foo (app.js:1:1)")


def test_record_error_rejects_an_empty_message(conn):
    assert client_errors.record_error(conn, message="") is False
    assert client_errors.record_error(conn, message="   ") is False
    assert conn.execute("SELECT COUNT(*) FROM client_errors").fetchone()[0] == 0


def test_record_error_truncates_oversized_fields_rather_than_rejecting(conn):
    client_errors.record_error(conn, message="x" * 1000, stack="y" * 5000)
    row = conn.execute("SELECT message, stack FROM client_errors").fetchone()
    assert len(row[0]) == client_errors.MAX_MESSAGE_LEN
    assert len(row[1]) == client_errors.MAX_STACK_LEN


def test_recent_errors_returns_newest_first(conn):
    client_errors.record_error(conn, message="first")
    client_errors.record_error(conn, message="second")
    results = client_errors.recent_errors(conn)
    assert [e.message for e in results] == ["second", "first"]


def test_recent_errors_respects_limit(conn):
    for i in range(5):
        client_errors.record_error(conn, message=f"error {i}")
    assert len(client_errors.recent_errors(conn, limit=2)) == 2
