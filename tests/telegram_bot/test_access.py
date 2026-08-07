"""tradebot.telegram_bot.access is the single seam feature checks route
through. Everything is free during beta, so can_access always returns
True — this test exists so a future accidental gate (someone adding a
condition to this function) gets caught immediately, and so this stays
the one place that changes when billing eventually arrives."""
from __future__ import annotations

from tradebot.telegram_bot import access, db


def test_can_access_is_unconditionally_true_during_beta():
    conn = db.connect(":memory:")
    user = db.get_or_create_user(conn, 1, 1, "alice")
    assert access.can_access(user, "watchlist_edit") is True
    assert access.can_access(user, "anything_at_all") is True
    assert access.can_access(None, "even_with_no_user") is True
