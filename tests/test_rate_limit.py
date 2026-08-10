from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradebot import rate_limit
from tradebot.telegram_bot import db as users_db


@pytest.fixture
def conn(tmp_path):
    return users_db.connect(tmp_path / "users.db")


def test_allows_calls_under_the_limit(conn):
    now = datetime.now(timezone.utc)
    assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is True
    assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is True
    assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is True


def test_denies_the_call_that_would_exceed_the_limit(conn):
    now = datetime.now(timezone.utc)
    for _ in range(3):
        assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is True
    assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is False


def test_different_keys_have_independent_limits(conn):
    now = datetime.now(timezone.utc)
    for _ in range(3):
        assert rate_limit.allow(conn, "a", limit=3, window_seconds=60, now=now) is True
    # "a" is now exhausted, "b" starts fresh
    assert rate_limit.allow(conn, "a", limit=3, window_seconds=60, now=now) is False
    assert rate_limit.allow(conn, "b", limit=3, window_seconds=60, now=now) is True


def test_a_new_window_resets_the_count(conn):
    now = datetime.now(timezone.utc)
    for _ in range(3):
        assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is True
    assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=now) is False

    later = now + timedelta(seconds=61)
    assert rate_limit.allow(conn, "k", limit=3, window_seconds=60, now=later) is True


def test_old_windows_are_pruned(conn):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=12)
    rate_limit.allow(conn, "old", limit=1, window_seconds=60, now=long_ago)
    assert conn.execute("SELECT COUNT(*) FROM rate_limit_counters").fetchone()[0] == 1

    # Any later call runs the prune, regardless of its own key.
    rate_limit.allow(conn, "new", limit=1, window_seconds=60, now=datetime.now(timezone.utc))
    remaining = conn.execute("SELECT bucket_key FROM rate_limit_counters").fetchall()
    assert remaining == [("new",)]
