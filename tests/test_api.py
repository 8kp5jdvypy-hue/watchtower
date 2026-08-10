"""Exercises tradebot.api.app end to end through Flask's test client —
the same interface a real HTTP request goes through, not internal
function calls, so a wiring mistake (a route that never got registered,
a session that never actually persists) would be caught the way it
would in production, not just at the unit level of accounts.py /
entitlements.py (already covered in their own test files).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_login_session_cookie_has_an_expiry_not_an_unbounded_lifetime(app, client):
    """The signed session cookie has no server-side revocation — without
    an explicit expiry it would stay valid forever if leaked. session.
    permanent=True (set at verify time) plus PERMANENT_SESSION_LIFETIME
    is what puts a real Max-Age/Expires on the cookie Flask sends."""
    token = _request_and_extract_token(app, client, "session-expiry@example.com")
    verify_response = client.get(f"/auth/magic-link/verify?token={token}", follow_redirects=False)

    set_cookie = verify_response.headers.get("Set-Cookie", "")
    assert "session=" in set_cookie
    assert "Max-Age=" in set_cookie or "expires=" in set_cookie.lower()


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


def test_signal_detail_returns_the_full_record_for_a_real_detection_id(app, client):
    now = datetime.now(timezone.utc)
    today = datetime.now(ET).date().isoformat()
    context = {"vwap": 451.2, "close": 450.0, "atr14": 2.0, "direction": "up"}
    detection_id = write_cluster(
        app.journal_conn,
        session=today,
        symbol="SPY",
        ts_utc=now.isoformat(),
        kinds="vwap_break",
        headlines="SPY broke above VWAP",
        score=5.0,
        close=450.0,
        atr14=2.0,
        trend="up",
        detections=[Detection("SPY", "vwap_break", now, 5.0, "SPY broke above VWAP", context)],
        code_version_str="test",
        primary_kind="vwap_break",
    )
    app.journal_conn.commit()

    token = _request_and_extract_token(app, client, "ivan@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get(f"/signals/{detection_id}")
    assert response.status_code == 200
    body = response.get_json()
    assert body["id"] == detection_id
    assert body["symbol"] == "SPY"
    assert body["kinds"] == ["vwap_break"]
    assert body["contexts"] == [context]
    assert body["tier"] == "high"
    assert body["trend"] == "up"
    assert body["close"] == 450.0
    assert body["atr14"] == 2.0
    assert body["no_trade"] is None
    assert body["news_driven"] is None
    # Fewer than MIN_HISTORY_SAMPLE same-kind/same-trend prior rows exist
    # (there are none), so historical_performance() correctly reports
    # nothing rather than a stat built on too little data.
    assert body["history"] is None


def test_signal_detail_404s_for_an_unknown_id(app, client):
    token = _request_and_extract_token(app, client, "judy2@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get("/signals/not-a-real-id")
    assert response.status_code == 404


def test_signal_detail_404s_for_a_sub_threshold_log_tier_detection(app, client):
    now = datetime.now(timezone.utc)
    today = datetime.now(ET).date().isoformat()
    detection_id = write_cluster(
        app.journal_conn,
        session=today,
        symbol="SPY",
        ts_utc=now.isoformat(),
        kinds="gap",
        headlines="SPY gapped slightly",
        score=0.5,  # below TIER_MEDIUM -> tier == 'log'
        close=450.0,
        atr14=2.0,
        trend="up",
        detections=[Detection("SPY", "gap", now, 0.5, "SPY gapped slightly", {})],
        code_version_str="test",
    )
    app.journal_conn.commit()

    token = _request_and_extract_token(app, client, "kim@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get(f"/signals/{detection_id}")
    assert response.status_code == 404


def test_performance_endpoint_returns_json_with_no_data_yet(app, client):
    token = _request_and_extract_token(app, client, "heidi@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    response = client.get("/performance")
    assert response.status_code == 200
    body = response.get_json()
    assert body["by_tier"] == {}
    assert body["track_record"] is None


def test_performance_endpoint_caches_within_the_ttl(app, client, monkeypatch):
    """tier_performance/track_record used to run on every single request
    with no caching — this locks in that a second request within the TTL
    reuses the cached result instead of recomputing."""
    import tradebot.api.app as api_app_module

    call_count = {"n": 0}
    real_tier_performance = api_app_module.tier_performance

    def counting_tier_performance(conn):
        call_count["n"] += 1
        return real_tier_performance(conn)

    monkeypatch.setattr(api_app_module, "tier_performance", counting_tier_performance)

    token = _request_and_extract_token(app, client, "judy@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    client.get("/performance")
    client.get("/performance")
    client.get("/performance")
    assert call_count["n"] == 1  # only the first request actually computed it

    # Force staleness and confirm it recomputes rather than caching forever.
    app._performance_cache["computed_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=api_app_module.PERFORMANCE_CACHE_TTL_SECONDS + 1
    )
    client.get("/performance")
    assert call_count["n"] == 2


def test_cors_header_only_reflects_an_allowed_origin(app, client):
    allowed = client.get("/healthz", headers={"Origin": app.frontend_url})
    assert allowed.headers.get("Access-Control-Allow-Origin") == app.frontend_url

    disallowed = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in disallowed.headers


def _post_event_beacon(client, body: dict):
    # Mirrors exactly how navigator.sendBeacon sends it client-side: a
    # raw text/plain body, not client.post(json=...) (which sets
    # application/json and would trigger a CORS preflight in a real
    # browser -- see the route's own docstring in tradebot/api/app.py).
    import json as _json

    return client.post("/events", data=_json.dumps(body), content_type="text/plain")


def test_events_endpoint_records_an_allowed_event(app, client):
    response = _post_event_beacon(client, {"event": "landing_view", "anon_id": "anon-1"})
    assert response.status_code == 204
    row = app.users_conn.execute("SELECT event, anon_id, account_id FROM funnel_events").fetchone()
    assert row == ("landing_view", "anon-1", None)


def test_events_endpoint_silently_ignores_an_unknown_event(app, client):
    response = _post_event_beacon(client, {"event": "definitely_not_real", "anon_id": "anon-2"})
    assert response.status_code == 204  # same response as a valid event -- no oracle for what's allowed
    assert app.users_conn.execute("SELECT COUNT(*) FROM funnel_events").fetchone()[0] == 0


def test_events_endpoint_silently_ignores_malformed_bodies(app, client):
    response = client.post("/events", data="not json at all", content_type="text/plain")
    assert response.status_code == 204
    assert app.users_conn.execute("SELECT COUNT(*) FROM funnel_events").fetchone()[0] == 0


def test_events_endpoint_captures_account_id_once_signed_in(app, client):
    token = _request_and_extract_token(app, client, "ivy@example.com")
    client.get(f"/auth/magic-link/verify?token={token}")

    _post_event_beacon(client, {"event": "app_authenticated", "anon_id": "anon-3"})

    row = app.users_conn.execute("SELECT account_id FROM funnel_events WHERE event = 'app_authenticated'").fetchone()
    account = app.users_conn.execute("SELECT id FROM accounts WHERE email = 'ivy@example.com'").fetchone()
    assert row[0] == account[0]


def test_events_endpoint_silently_drops_writes_past_the_per_anon_limit(app, client):
    from tradebot.api import app as app_module

    limit, _window = app_module._EVENTS_PER_ANON
    for _ in range(limit):
        response = _post_event_beacon(client, {"event": "landing_view", "anon_id": "flooder"})
        assert response.status_code == 204

    over_limit = _post_event_beacon(client, {"event": "landing_view", "anon_id": "flooder"})
    assert over_limit.status_code == 204  # same response either way -- see the route's own comment
    assert app.users_conn.execute(
        "SELECT COUNT(*) FROM funnel_events WHERE anon_id = 'flooder'"
    ).fetchone()[0] == limit


def test_magic_link_request_is_rate_limited_per_email(app, client):
    from tradebot.api import app as app_module

    limit, _window = app_module._MAGIC_LINK_PER_EMAIL
    for _ in range(limit):
        response = client.post("/auth/magic-link/request", json={"email": "flooded@example.com"})
        assert response.status_code == 202

    over_limit = client.post("/auth/magic-link/request", json={"email": "flooded@example.com"})
    assert over_limit.status_code == 429
    # A *different* email is unaffected -- the limit is per-address, not global.
    other = client.post("/auth/magic-link/request", json={"email": "someone-else@example.com"})
    assert other.status_code == 202


def test_client_errors_endpoint_records_a_report(app, client):
    response = client.post(
        "/client-errors",
        data='{"message": "TypeError: boom", "stack": "at x (app.js:1:1)", "url": "https://app.perchmarkets.com/"}',
        content_type="text/plain",
    )
    assert response.status_code == 204
    row = app.users_conn.execute("SELECT message, url FROM client_errors").fetchone()
    assert row == ("TypeError: boom", "https://app.perchmarkets.com/")


def test_client_errors_endpoint_silently_ignores_a_missing_message(app, client):
    response = client.post("/client-errors", data='{"stack": "no message here"}', content_type="text/plain")
    assert response.status_code == 204
    assert app.users_conn.execute("SELECT COUNT(*) FROM client_errors").fetchone()[0] == 0


def test_magic_link_request_answers_a_clean_error_when_the_email_provider_fails(app, client):
    # Reproduces a real failure seen live: ResendEmailSender.send_magic_link
    # can raise (a Resend outage, our sending account hitting a quota, a
    # network error) -- that must never surface as a raw Flask 500 page.
    class BrokenEmailSender:
        def send_magic_link(self, to_email, link_url):
            raise RuntimeError("simulated provider outage")

    app.email_sender = BrokenEmailSender()
    response = client.post("/auth/magic-link/request", json={"email": "unlucky@example.com"})

    assert response.status_code == 502
    assert response.get_json() == {"error": "couldn't send the email, try again shortly"}
    # The token is still created before the send is attempted -- that's
    # fine, it just expires unused; the point of this test is that the
    # *response* degrades cleanly, not that the token creation is undone.
    row = app.users_conn.execute(
        "SELECT email FROM magic_link_tokens WHERE email = 'unlucky@example.com'"
    ).fetchone()
    assert row is not None
