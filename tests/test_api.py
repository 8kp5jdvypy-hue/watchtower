"""Exercises tradebot.api.app end to end through Flask's test client —
the same interface a real HTTP request goes through, not internal
function calls, so a wiring mistake (a route that never got registered,
a session that never actually persists) would be caught the way it
would in production, not just at the unit level of accounts.py /
entitlements.py (already covered in their own test files).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradebot import accounts
from tradebot.api.app import create_app
from tradebot.detectors import Detection
from tradebot.email_sender import DevEmailSender
from tradebot.journal import write_cluster
from tradebot.runner import ET
from tradebot.telegram_bot import db as users_db


@pytest.fixture
def app(tmp_path):
    application = create_app(
        users_db_path=tmp_path / "users.db",
        journal_db_path=tmp_path / "journal.db",
    )
    application.config["TESTING"] = True
    application.email_sender = DevEmailSender()
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _request_and_extract_token(app, client, email: str) -> str:
    """The magic-link flow issues a token server-side and emails a link
    containing it — DevEmailSender just logs it, so tests pull the token
    straight from magic_link_tokens instead of parsing a log line, which
    is both simpler and a closer check of what the DB actually holds."""
    response = client.post("/auth/magic-link/request", json={"email": email})
    assert response.status_code == 202
    row = app.users_conn.execute(
        "SELECT token FROM magic_link_tokens WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    assert row is not None
    return row[0]


def test_healthz():
    app = create_app(users_db_path=":memory:", journal_db_path=":memory:")
    client = app.test_client()
    assert client.get("/healthz").get_json() == {"ok": True}


def test_protected_endpoint_without_a_session_is_401(client):
    response = client.get("/me")
    assert response.status_code == 401


def test_magic_link_request_then_verify_logs_the_user_in(app, client):
    token = _request_and_extract_token(app, client, "alice@example.com")

    verify_response = client.get(f"/auth/magic-link/verify?token={token}", follow_redirects=False)
    assert verify_response.status_code == 302
    assert verify_response.headers["Location"] == app.frontend_url

    me_response = client.get("/me")
    assert me_response.status_code == 200
    body = me_response.get_json()
    assert body["email"] == "alice@example.com"
    assert body["plan"] == "beta"
    assert body["linked_identities"] == []


def test_magic_link_token_cannot_be_reused(app, client):
    token = _request_and_extract_token(app, client, "bob@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    second_client = app.test_client()
    reuse_response = second_client.get(f"/auth/magic-link/verify?token={token}")
    assert reuse_response.status_code == 400


def test_magic_link_verify_rejects_an_unknown_token(client):
    response = client.get("/auth/magic-link/verify?token=not-a-real-token")
    assert response.status_code == 400


def test_magic_link_request_rejects_a_missing_or_bad_email(client):
    assert client.post("/auth/magic-link/request", json={}).status_code == 400
    assert client.post("/auth/magic-link/request", json={"email": "not-an-email"}).status_code == 400


def test_logout_clears_the_session(app, client):
    token = _request_and_extract_token(app, client, "carol@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")
    assert client.get("/me").status_code == 200

    logout_response = client.post("/auth/logout")
    assert logout_response.get_json() == {"ok": True}
    assert client.get("/me").status_code == 401


def test_watchlist_defaults_to_the_global_watchlist_with_no_linked_telegram(app, client):
    token = _request_and_extract_token(app, client, "dana@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get("/watchlist")
    body = response.get_json()
    assert body["is_custom"] is False
    assert len(body["symbols"]) > 0


def test_watchlist_reflects_a_linked_telegram_users_custom_list(app, client):
    token = _request_and_extract_token(app, client, "erin@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")
    me_body = client.get("/me").get_json()

    telegram_user = users_db.get_or_create_user(app.users_conn, 4242, 4242, "erin")
    accounts.link_identity(app.users_conn, me_body["id"], "telegram", str(telegram_user.telegram_user_id))
    users_db.set_watchlist(app.users_conn, telegram_user.telegram_user_id, ["AAPL", "TSLA"])

    response = client.get("/watchlist")
    body = response.get_json()
    assert body["is_custom"] is True
    assert body["symbols"] == ["AAPL", "TSLA"]


def test_activity_is_empty_with_no_linked_telegram_identity(app, client):
    token = _request_and_extract_token(app, client, "frank@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get("/activity")
    body = response.get_json()
    assert body == {"trades": [], "stats": None}


def test_signals_today_and_feed_return_real_journaled_detections(app, client):
    now = datetime.now(timezone.utc)
    today = datetime.now(ET).date().isoformat()  # same session_date_fn definition the endpoint uses
    detection = Detection("SPY", "gap", now, 5.0, "SPY gapped up", {})
    write_cluster(
        app.journal_conn,
        session=today,
        symbol="SPY",
        ts_utc=now.isoformat(),
        kinds="gap",
        headlines="SPY gapped up",
        score=5.0,
        close=450.0,
        atr14=2.0,
        trend="up",
        detections=[detection],
        code_version_str="test",
    )
    app.journal_conn.commit()

    token = _request_and_extract_token(app, client, "grace@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    feed_body = client.get("/signals/feed").get_json()
    assert len(feed_body["signals"]) == 1
    assert feed_body["signals"][0]["symbol"] == "SPY"
    assert feed_body["signals"][0]["kinds"] == ["gap"]


def test_performance_endpoint_returns_json_with_no_data_yet(app, client):
    token = _request_and_extract_token(app, client, "heidi@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get("/performance")
    assert response.status_code == 200
    body = response.get_json()
    assert body["by_tier"] == {}
    assert body["track_record"] is None


def test_cors_header_only_reflects_an_allowed_origin(app, client):
    allowed = client.get("/healthz", headers={"Origin": app.frontend_url})
    assert allowed.headers.get("Access-Control-Allow-Origin") == app.frontend_url

    disallowed = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in disallowed.headers
