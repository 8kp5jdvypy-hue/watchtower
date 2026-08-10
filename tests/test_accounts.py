from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot import accounts
from tradebot.telegram_bot import db


def test_create_and_look_up_account_by_email():
    conn = db.connect(":memory:")
    created = accounts.create_account(conn, email="alice@example.com")
    assert created.email == "alice@example.com"
    assert created.plan == "beta"
    assert created.founding_member is False

    found = accounts.get_account_by_email(conn, "alice@example.com")
    assert found == created


def test_get_or_create_account_for_email_is_idempotent():
    conn = db.connect(":memory:")
    first = accounts.get_or_create_account_for_email(conn, "bob@example.com")
    second = accounts.get_or_create_account_for_email(conn, "bob@example.com")
    assert first.id == second.id


def test_multiple_email_less_accounts_can_coexist():
    # SQLite's UNIQUE index treats each NULL as distinct — two
    # Telegram-only accounts (no email yet) must not collide.
    conn = db.connect(":memory:")
    a = accounts.create_account(conn, email=None)
    b = accounts.create_account(conn, email=None)
    assert a.id != b.id


def test_link_identity_and_resolve_it_back_to_the_account():
    conn = db.connect(":memory:")
    account = accounts.create_account(conn, email="carol@example.com")
    accounts.link_identity(conn, account.id, "telegram", "555")

    found = accounts.get_account_for_identity(conn, "telegram", "555")
    assert found is not None
    assert found.id == account.id
    assert accounts.get_account_for_identity(conn, "telegram", "999") is None


def test_link_identity_is_idempotent():
    conn = db.connect(":memory:")
    account = accounts.create_account(conn, email="dana@example.com")
    accounts.link_identity(conn, account.id, "telegram", "555")
    accounts.link_identity(conn, account.id, "telegram", "555")  # must not raise

    row = conn.execute(
        "SELECT COUNT(*) FROM linked_identities WHERE provider = ? AND provider_user_id = ?", ("telegram", "555")
    ).fetchone()
    assert row[0] == 1


def test_magic_link_token_round_trip():
    conn = db.connect(":memory:")
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    token = accounts.create_magic_link_token(conn, "eve@example.com", now)

    email = accounts.verify_magic_link_token(conn, token, now + timedelta(minutes=1))
    assert email == "eve@example.com"


def test_magic_link_token_is_single_use():
    conn = db.connect(":memory:")
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    token = accounts.create_magic_link_token(conn, "eve@example.com", now)

    assert accounts.verify_magic_link_token(conn, token, now + timedelta(minutes=1)) == "eve@example.com"
    assert accounts.verify_magic_link_token(conn, token, now + timedelta(minutes=2)) is None


def test_magic_link_token_expires():
    conn = db.connect(":memory:")
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    token = accounts.create_magic_link_token(conn, "eve@example.com", now)

    expired = now + timedelta(minutes=accounts.MAGIC_LINK_TTL_MINUTES + 1)
    assert accounts.verify_magic_link_token(conn, token, expired) is None


def test_unknown_token_returns_none():
    conn = db.connect(":memory:")
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    assert accounts.verify_magic_link_token(conn, "not-a-real-token", now) is None


def test_migrate_existing_telegram_users_creates_one_account_each():
    conn = db.connect(":memory:")
    db.get_or_create_user(conn, 1, 1, "alice")
    db.get_or_create_user(conn, 2, 2, "bob")

    created = accounts.migrate_existing_telegram_users(conn)
    assert created == 2

    for telegram_user_id in (1, 2):
        account = accounts.get_account_for_identity(conn, "telegram", str(telegram_user_id))
        assert account is not None
        assert account.email is None


def test_migrate_carries_over_existing_plan_and_founding_member():
    conn = db.connect(":memory:")
    db.get_or_create_user(conn, 1, 1, "alice")
    conn.execute("UPDATE users SET plan = 'pro', founding_member = 0 WHERE telegram_user_id = 1")
    conn.commit()

    accounts.migrate_existing_telegram_users(conn)

    account = accounts.get_account_for_identity(conn, "telegram", "1")
    assert account.plan == "pro"
    assert account.founding_member is False


def test_migrate_is_idempotent_and_never_creates_a_second_account():
    conn = db.connect(":memory:")
    db.get_or_create_user(conn, 1, 1, "alice")

    first_run = accounts.migrate_existing_telegram_users(conn)
    second_run = accounts.migrate_existing_telegram_users(conn)

    assert first_run == 1
    assert second_run == 0
    count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert count == 1


def test_migrate_skips_a_telegram_user_already_linked_by_some_other_means():
    conn = db.connect(":memory:")
    db.get_or_create_user(conn, 1, 1, "alice")
    pre_existing = accounts.create_account(conn, email="alice@example.com")
    accounts.link_identity(conn, pre_existing.id, "telegram", "1")

    created = accounts.migrate_existing_telegram_users(conn)

    assert created == 0
    account = accounts.get_account_for_identity(conn, "telegram", "1")
    assert account.id == pre_existing.id
