"""tradebot.telegram_bot.access is the single seam feature checks route
through. Everything is free during beta, so can_access always returns
True — this test exists so a future accidental gate (someone adding a
condition to this function) gets caught immediately, and so this stays
the one place that changes when billing eventually arrives."""
from __future__ import annotations

from tradebot import accounts
from tradebot.telegram_bot import access, db


def test_can_access_is_unconditionally_true_during_beta():
    conn = db.connect(":memory:")
    user = db.get_or_create_user(conn, 1, 1, "alice")
    assert access.can_access(user, "watchlist_edit") is True
    assert access.can_access(user, "anything_at_all") is True
    assert access.can_access(None, "even_with_no_user") is True


def test_resolve_plan_prefers_the_linked_account_over_the_legacy_column():
    conn = db.connect(":memory:")
    user = db.get_or_create_user(conn, 1, 1, "alice")
    assert access.resolve_plan(conn, user) == user.plan == "beta"

    account = accounts.create_account(conn, email="alice@example.com", plan="pro")
    accounts.link_identity(conn, account.id, "telegram", "1")

    assert access.resolve_plan(conn, user) == "pro"


def test_resolve_plan_falls_back_to_the_legacy_column_with_no_linked_account():
    conn = db.connect(":memory:")
    user = db.get_or_create_user(conn, 1, 1, "alice")
    assert access.resolve_plan(conn, user) == "beta"
