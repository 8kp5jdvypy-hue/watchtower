"""/v1 -- the iOS app's API. Two layers:

1. Behavioural tests against the Flask test client (auth lifecycle,
   pagination, optimistic watchlist versioning, error shapes).
2. A contract gate: every response body collected in (1) is validated
   with the APP'S OWN zod schemas (perch-mobile-mvp/src/api/schemas.ts)
   through scripts/v1_schema_check.mjs. That test skips, loudly, when
   the app checkout or a type-stripping Node is not present; it is the
   one that proves the device would accept what we send.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot.api.app import create_app
from tradebot.api import v1 as v1_api
from tradebot.detectors import TIER_HIGH, TIER_MEDIUM, Detection
from tradebot.email_sender import DevEmailSender
from tradebot.journal import write_cluster

MOBILE_SCHEMAS = Path(
    os.environ.get(
        "PERCH_MOBILE_SCHEMAS",
        Path.home() / "Documents/Codex/2026-09-01/c/outputs/perch-mobile-mvp/src/api/schemas.ts",
    )
)
REPO_ROOT = Path(__file__).resolve().parent.parent

# Every response body the behavioural tests see, keyed by the zod schema
# it must satisfy -- consumed by test_contract_validates_with_app_schemas.
COLLECTED: dict[str, list] = {}


def collect(schema: str, payload) -> None:
    COLLECTED.setdefault(schema, []).append(payload)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-only-secret-key-do-not-use-in-production")
    monkeypatch.setattr(v1_api, "MAGIC_LINK_PER_EMAIL", (100, 3600))
    monkeypatch.setattr(v1_api, "MAGIC_LINK_PER_IP", (1000, 3600))


@pytest.fixture
def app(tmp_path):
    application = create_app(users_db_path=tmp_path / "users.db", journal_db_path=tmp_path / "journal.db")
    application.config["TESTING"] = True
    application.email_sender = DevEmailSender()
    # A tiny universe so instrument search and watchlist enrichment have
    # something real to read; the read-only production connection is
    # replaced by this in-memory one.
    uni = sqlite3.connect(":memory:", check_same_thread=False)
    uni.executescript(
        "CREATE TABLE assets (symbol TEXT PRIMARY KEY, exchange TEXT, name TEXT, tradable INTEGER, "
        "options_enabled INTEGER, overnight_eligible INTEGER, attributes_json TEXT, is_active INTEGER, "
        "first_seen_at TEXT, last_seen_at TEXT);"
    )
    uni.executemany(
        "INSERT INTO assets VALUES (?, ?, ?, 1, 1, NULL, '[]', ?, 't', 't')",
        [
            ("SPY", "ARCA", "SPDR S&P 500 ETF Trust", 1),
            ("AAPL", "NASDAQ", "Apple Inc. Common Stock", 1),
            ("APLD", "NASDAQ", "Applied Digital Corporation", 1),
            ("DEAD", "NYSE", "Delisted Co", 0),
            ("BRK/B", "NYSE", "Berkshire Hathaway Class B", 1),  # symbol fails the app's regex; must be filtered
        ],
    )
    uni.commit()
    application.universe_conn = uni
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_signals(app, *, session_date: str, count: int = 5):
    """count HIGH/MEDIUM detections on session_date, newest last. Every
    other one is a radar (screening) hit, and the last one is MEDIUM."""
    base = datetime.fromisoformat(session_date + "T14:35:00+00:00")
    ids = []
    for i in range(count):
        ts = base + timedelta(minutes=5 * i)
        score = TIER_MEDIUM + 0.1 if i == count - 1 else TIER_HIGH + 0.5
        symbol = "SPY" if i % 2 == 0 else "APLD"
        det = Detection(symbol, "level_break", ts, score, f"{symbol} broke the opening range", {"level": 100 + i, "atr_mult": 2.5})
        ids.append(
            write_cluster(
                app.journal_conn, session=session_date, symbol=symbol, ts_utc=ts.isoformat(), kinds="level_break",
                headlines=det.headline, score=score, close=100.0 + i, atr14=1.2, trend="up", detections=[det],
                code_version_str="test-v1", primary_kind="level_break", data_feed="iex",
                origin=None if i % 2 == 0 else "screening",
                pct_from_prior_close=1.25 if i == 0 else None,
                pct_from_prior_close_status="AVAILABLE" if i == 0 else "UNAVAILABLE",
            )
        )
    return ids


def _sign_in(app, client, email="ios@example.com") -> dict:
    r = client.post("/v1/auth/magic-link/request", json={"email": email})
    assert r.status_code == 200, r.get_json()
    collect("MagicLinkAcceptedSchema", r.get_json())
    (token,) = app.users_conn.execute(
        "SELECT token FROM magic_link_tokens WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    r = client.post("/v1/auth/magic-link/verify", json={"token": token, "deviceName": "iPhone test"})
    assert r.status_code == 200, r.get_json()
    session = r.get_json()
    collect("AuthSessionSchema", session)
    return session


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['tokens']['accessToken']}"}


# ---- auth ------------------------------------------------------------------

def test_magic_link_email_uses_fragment_universal_link(app, client, caplog):
    with caplog.at_level("INFO"):
        client.post("/v1/auth/magic-link/request", json={"email": "frag@example.com"})
    link = next(m for m in caplog.messages if "magic link for frag@example.com" in m).split(": ", 1)[1]
    assert link.startswith(app.frontend_url.rstrip("/") + "/auth/magic-link#token=")
    assert "?token=" not in link  # authLinks.ts rejects a query-string token


def test_unauthenticated_request_is_a_contract_shaped_401(client):
    r = client.get("/v1/me")
    assert r.status_code == 401
    body = r.get_json()
    assert body["code"] == "session_required"
    collect("ErrorResponseSchema", body)


def test_sign_in_me_entitlements_and_logout(app, client):
    session = _sign_in(app, client)
    assert session["account"]["email"] == "ios@example.com"
    assert session["account"]["entitlement"]["plan"] == "beta"
    r = client.get("/v1/me", headers=_auth(session))
    assert r.status_code == 200 and r.get_json()["id"] == session["account"]["id"]
    collect("AccountSchema", r.get_json())
    r = client.get("/v1/entitlements", headers=_auth(session))
    assert r.status_code == 200
    collect("EntitlementSchema", r.get_json())
    r = client.post("/v1/auth/logout", headers=_auth(session))
    assert r.status_code == 204 and r.data == b""
    assert client.get("/v1/me", headers=_auth(session)).status_code == 401


def test_refresh_rotates_and_reuse_revokes_the_family(app, client):
    session = _sign_in(app, client)
    old_refresh = session["tokens"]["refreshToken"]
    r = client.post("/v1/auth/refresh", json={"refreshToken": old_refresh})
    assert r.status_code == 200
    new_tokens = r.get_json()
    collect("TokenPairSchema", new_tokens)
    assert new_tokens["refreshToken"] != old_refresh
    # old access token is dead, new one works
    assert client.get("/v1/me", headers=_auth(session)).status_code == 401
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {new_tokens['accessToken']}"}).status_code == 200
    # replaying the rotated refresh token = theft -> whole family revoked
    r = client.post("/v1/auth/refresh", json={"refreshToken": old_refresh})
    assert r.status_code == 401 and r.get_json()["code"] == "session_expired"
    collect("ErrorResponseSchema", r.get_json())
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {new_tokens['accessToken']}"}).status_code == 401
    assert client.post("/v1/auth/refresh", json={"refreshToken": new_tokens["refreshToken"]}).status_code == 401


def test_expired_access_token_is_session_expired(app, client, monkeypatch):
    session = _sign_in(app, client)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(v1_api, "_now", lambda: later)
    r = client.get("/v1/me", headers=_auth(session))
    assert r.status_code == 401 and r.get_json()["code"] == "session_expired"


def test_apple_sign_in_is_an_honest_501(client):
    r = client.post("/v1/auth/apple", json={"identityToken": "x", "authorizationCode": "y", "nonce": "z", "deviceName": "d"})
    assert r.status_code == 501 and r.get_json()["code"] == "not_implemented"
    collect("ErrorResponseSchema", r.get_json())


# ---- status ----------------------------------------------------------------

@pytest.mark.parametrize(
    "when,expected",
    [
        ("2026-09-18T12:00:00+00:00", "premarket"),    # Fri 08:00 ET
        ("2026-09-18T15:00:00+00:00", "open"),         # Fri 11:00 ET
        ("2026-09-18T21:00:00+00:00", "after_hours"),  # Fri 17:00 ET
        ("2026-09-18T23:59:00+00:00", "after_hours"),  # Fri 19:59 ET
        ("2026-09-19T01:00:00+00:00", "closed"),       # Fri 21:00 ET
        ("2026-09-19T15:00:00+00:00", "closed"),       # Sat
        ("2026-11-26T15:00:00+00:00", "holiday"),      # Thanksgiving 2026
    ],
)
def test_market_state_covers_every_phase(when, expected):
    ms = v1_api.market_state(datetime.fromisoformat(when))
    assert ms.state == expected
    assert ms.next_transition_at is None or ms.next_transition_at > datetime.fromisoformat(when)


def test_service_status_reflects_heartbeat_and_halt():
    now = datetime.fromisoformat("2026-09-18T15:00:00+00:00")  # market open
    fresh = v1_api.service_status(now, heartbeat_at=now - timedelta(seconds=30), halted=False)
    assert fresh["status"] == "operational" and fresh["sources"][0]["status"] == "operational"
    stale = v1_api.service_status(now, heartbeat_at=now - timedelta(minutes=30), halted=False)
    assert stale["status"] == "degraded" and stale["sources"][0]["status"] == "stale"
    none = v1_api.service_status(now, heartbeat_at=None, halted=False)
    assert none["sources"][0]["status"] == "unknown"
    halted = v1_api.service_status(now, heartbeat_at=now, halted=True)
    assert halted["status"] == "maintenance"
    weekend = v1_api.service_status(datetime.fromisoformat("2026-09-19T15:00:00+00:00"), heartbeat_at=None, halted=False)
    assert weekend["status"] == "operational"  # idle scanner over a weekend is not a fault
    # Production regression 2026-09-18: after-hours with the 15:56 ET
    # heartbeat is the scanner idling correctly, not a stale feed.
    after_hours = v1_api.service_status(
        datetime.fromisoformat("2026-09-18T23:20:00+00:00"),
        heartbeat_at=datetime.fromisoformat("2026-09-18T19:56:40+00:00"), halted=False,
    )
    assert after_hours["market"]["state"] == "after_hours"
    assert after_hours["status"] == "operational" and after_hours["sources"][0]["status"] == "operational"
    for payload in (fresh, stale, none, halted, weekend, after_hours):
        collect("ServiceStatusSchema", payload)


def test_status_endpoint_is_public(client):
    r = client.get("/v1/status", headers={"Origin": "https://perchmarkets.com"})
    assert r.status_code == 200
    # The marketing site reads this cross-origin; it is cookie-free, so
    # it gets the same wildcard /public/* has. /v1/me must not.
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert client.get("/v1/methodology", headers={"Origin": "https://perchmarkets.com"}).headers.get("Access-Control-Allow-Origin") == "*"
    assert client.get("/v1/me", headers={"Origin": "https://perchmarkets.com"}).headers.get("Access-Control-Allow-Origin") is None
    collect("ServiceStatusSchema", r.get_json())


# ---- signals ---------------------------------------------------------------

def test_today_returns_at_most_three_for_the_et_session_date(app, client, monkeypatch):
    session = _sign_in(app, client)
    fixed = datetime.fromisoformat("2026-09-18T18:00:00+00:00")
    _seed_signals(app, session_date="2026-09-18", count=5)
    _seed_signals(app, session_date="2026-09-17", count=2)

    monkeypatch.setattr(v1_api, "_now", lambda: fixed)
    r = client.get("/v1/today", headers=_auth(session))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    collect("TodaySchema", body)
    assert body["sessionDate"] == "2026-09-18"
    assert len(body["items"]) == 3
    assert all(i["observedAt"].startswith("2026-09-18") for i in body["items"])
    assert body["items"][0]["observedAt"] > body["items"][1]["observedAt"]  # newest first


def test_signals_paginate_with_an_opaque_cursor_and_filters(app, client):
    session = _sign_in(app, client)
    ids = _seed_signals(app, session_date="2026-09-18", count=5)
    r = client.get("/v1/signals?limit=2", headers=_auth(session))
    assert r.status_code == 200
    page1 = r.get_json()
    collect("SignalPageSchema", page1)
    assert len(page1["items"]) == 2 and page1["nextCursor"]
    assert page1["items"][0]["label"] == "context"  # newest is the MEDIUM one
    assert page1["items"][0]["origin"] == "watchlist"  # i=4 -> SPY, origin None
    assert page1["items"][1]["origin"] == "radar"
    r = client.get(f"/v1/signals?limit=2&cursor={page1['nextCursor']}", headers=_auth(session))
    page2 = r.get_json()
    collect("SignalPageSchema", page2)
    assert len(page2["items"]) == 2 and page2["nextCursor"]
    r = client.get(f"/v1/signals?limit=2&cursor={page2['nextCursor']}", headers=_auth(session))
    page3 = r.get_json()
    assert len(page3["items"]) == 1 and page3["nextCursor"] is None
    seen = [i["id"] for p in (page1, page2, page3) for i in p["items"]]
    assert len(set(seen)) == 5 and set(seen) == {v1_api.signal_uuid(i) for i in ids}

    r = client.get("/v1/signals?origin=radar", headers=_auth(session))
    assert {i["origin"] for i in r.get_json()["items"]} == {"radar"} and len(r.get_json()["items"]) == 2
    r = client.get("/v1/signals?symbols=spy", headers=_auth(session))
    assert {i["symbol"] for i in r.get_json()["items"]} == {"SPY"}
    r = client.get("/v1/signals?corrected=true", headers=_auth(session))
    assert r.get_json()["items"] == []
    assert client.get("/v1/signals?cursor=%%%", headers=_auth(session)).status_code == 400
    assert client.get("/v1/signals?symbols=bad$", headers=_auth(session)).status_code == 400


def test_signal_detail_maps_the_journal_row(app, client):
    session = _sign_in(app, client)
    ids = _seed_signals(app, session_date="2026-09-18", count=2)  # ids[0] is HIGH, SPY, with prior-close move
    r = client.get(f"/v1/signals/{v1_api.signal_uuid(ids[0])}", headers=_auth(session))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    collect("SignalDetailSchema", body)
    assert body["symbol"] == "SPY" and body["label"] == "worth_a_closer_look" and body["session"] == "regular"
    assert body["whatHappened"] == "SPY broke the opening range"
    kinds = [e["kind"] for e in body["supportingEvidence"]]
    assert kinds[:2] == ["price_move", "price_move"]  # close + prior-close move
    assert "level=100" in body["supportingEvidence"][2]["value"]
    assert body["evidenceSnapshot"]["calculationVersion"] == "test-v1"
    assert body["evidenceSnapshot"]["detectors"] == ["level_break"]
    assert body["historicalContext"] is None and body["corrections"] == []

    r = client.get(f"/v1/signals/{v1_api.signal_uuid('nope')}", headers=_auth(session))
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"
    assert client.get("/v1/signals/not-a-uuid", headers=_auth(session)).status_code == 404


# ---- watchlist and instruments ----------------------------------------------

class _FakeFeed:
    def __init__(self):
        from tradebot.pricefeed import crypto, equity

        self.instruments = (crypto("BTC"), equity("SPY"))

    def get(self, symbols):
        from tradebot.marketdata import PricePoint

        ts = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
        return {s: PricePoint(s, 759.26, ts, "alpaca_iex", "equity", stale=(s == "SPY")) for s in symbols}


def test_watchlist_round_trip_with_versioning(app, client):
    app.price_feed = _FakeFeed()
    session = _sign_in(app, client)
    r = client.get("/v1/watchlist", headers=_auth(session))
    assert r.status_code == 200
    empty = r.get_json()
    collect("WatchlistSchema", empty)
    assert empty == {**empty, "version": 0, "items": [], "radarEnabled": False}

    put = {"version": 0, "items": [
        {"symbol": "spy", "notificationsEnabled": True},
        {"symbol": "APLD", "notificationsEnabled": False},
        {"symbol": "SPY", "notificationsEnabled": True},  # duplicate collapses
        {"symbol": "ZZZZ", "notificationsEnabled": True},  # unknown to the universe -> placeholder instrument
    ], "radarEnabled": True}
    r = client.put("/v1/watchlist", json=put, headers=_auth(session))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    collect("WatchlistSchema", body)
    assert body["version"] == 1 and body["radarEnabled"] is True
    assert [i["symbol"] for i in body["items"]] == ["SPY", "APLD", "ZZZZ"]
    spy, apld, zzzz = body["items"]
    assert spy["instrument"]["assetType"] == "etf" and spy["instrument"]["exchange"] == "ARCA"
    assert spy["quote"]["value"] == 759.26 and spy["quote"]["stale"] is True and spy["quote"]["valueKind"] == "last"
    assert apld["instrument"]["assetType"] == "equity" and apld["quote"] is None  # feed doesn't carry APLD
    assert apld["notificationsEnabled"] is False
    assert zzzz["instrument"]["name"] == "ZZZZ" and zzzz["instrument"]["exchange"] == "UNKNOWN"

    # stale version -> 409, nothing changes
    r = client.put("/v1/watchlist", json={"version": 0, "items": []}, headers=_auth(session))
    assert r.status_code == 409 and r.get_json()["code"] == "watchlist_version_conflict"
    collect("ErrorResponseSchema", r.get_json())
    assert client.get("/v1/watchlist", headers=_auth(session)).get_json()["version"] == 1
    # bad symbol -> 400
    r = client.put("/v1/watchlist", json={"version": 1, "items": [{"symbol": "br/k"}]}, headers=_auth(session))
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_symbol"


def test_instrument_search_returns_only_active_regex_valid_symbols(app, client):
    session = _sign_in(app, client)
    r = client.get("/v1/instruments?query=ap&limit=20", headers=_auth(session))
    assert r.status_code == 200
    body = r.get_json()
    collect("InstrumentSearchSchema", body)
    symbols = [i["symbol"] for i in body["items"]]
    assert symbols[:2] == ["APLD", "AAPL"]  # symbol-prefix match ranks above a name match ("Apple")
    assert "DEAD" not in symbols and "BRK/B" not in symbols
    r = client.get("/v1/instruments?query=berkshire", headers=_auth(session))
    assert r.get_json()["items"] == []  # only match fails the symbol regex; never sent
    assert client.get("/v1/instruments?query=", headers=_auth(session)).get_json() == {"items": []}


# ---- methodology and the M1 gaps -------------------------------------------

def test_methodology_is_public(client):
    r = client.get("/v1/methodology")
    assert r.status_code == 200
    collect("MethodologySchema", r.get_json())


# ---- devices (M2) -----------------------------------------------------------

_DEVICE_INPUT = {
    "platform": "ios", "apnsEnvironment": "sandbox", "token": "ab" * 32,
    "installationId": "9d2c1b9a-1b2c-4d3e-8f4a-5b6c7d8e9f00", "appVersion": "1.0.0", "locale": "en-US",
    "timeZone": "America/New_York",
    "preferences": {"watchlistPriority": True, "radarDiscoveries": False, "corrections": True, "operational": True,
                    "sessions": ["regular", "premarket"], "paused": False, "quietStartLocal": "22:00", "quietEndLocal": "07:00"},
}


def test_device_registration_round_trip(app, client):
    from tradebot.push import store as push_store

    session = _sign_in(app, client)
    r = client.post("/v1/devices", json=_DEVICE_INPUT, headers={**_auth(session), "Idempotency-Key": "2f1e0d9c-8b7a-4655-9443-322110ffeedd"})
    assert r.status_code == 200, r.get_json()
    device = r.get_json()
    collect("DeviceSchema", device)
    assert device["installationId"] == _DEVICE_INPUT["installationId"] and device["active"] is True
    assert device["preferences"]["sessions"] == ["regular", "premarket"] and device["preferences"]["quietStartLocal"] == "22:00"
    # token rotation on the same installation keeps the id
    r2 = client.post("/v1/devices", json={**_DEVICE_INPUT, "token": "cd" * 32}, headers=_auth(session))
    assert r2.get_json()["id"] == device["id"]
    assert push_store.get_device(app.users_conn, device["id"]).token == "cd" * 32
    # unregister -> 204, idempotent
    assert client.delete(f"/v1/devices/{device['id']}", headers=_auth(session)).status_code == 204
    assert push_store.get_device(app.users_conn, device["id"]).active is False
    assert client.delete(f"/v1/devices/{device['id']}", headers=_auth(session)).status_code == 204


def test_device_registration_validates_input(app, client):
    session = _sign_in(app, client)
    bad = [
        {**_DEVICE_INPUT, "platform": "android"},
        {**_DEVICE_INPUT, "apnsEnvironment": "dev"},
        {**_DEVICE_INPUT, "token": "not-hex!"},
        {**_DEVICE_INPUT, "installationId": "nope"},
        {**_DEVICE_INPUT, "preferences": {"sessions": ["lunch"]}},
        {**_DEVICE_INPUT, "preferences": {"quietStartLocal": "25:00"}},
    ]
    for body in bad:
        r = client.post("/v1/devices", json=body, headers=_auth(session))
        assert r.status_code == 400, body
        assert r.get_json()["code"] == "invalid_request"
    collect("ErrorResponseSchema", r.get_json())
    assert client.post("/v1/devices", json=_DEVICE_INPUT).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/billing/apple/sync"),
        ("POST", "/v1/account/export"),
        ("GET", "/v1/account/exports/abc"),
        ("DELETE", "/v1/account"),
        ("POST", "/v1/account/reauthenticate/magic-link/request"),
        ("POST", "/v1/account/reauthenticate/apple"),
    ],
)
def test_m1_gaps_are_contract_shaped_501s(client, method, path):
    r = client.open(path, method=method, json={})
    assert r.status_code == 501
    body = r.get_json()
    assert body["code"] == "not_implemented"
    collect("ErrorResponseSchema", body)


def test_bars_return_the_latest_session_and_cache_per_symbol(app, client, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from tradebot.api import v1
    from tradebot.detectors import Bar

    session = _sign_in(app, client)
    calls: list[list[str]] = []

    def fake_bulk(symbols, session_date):
        calls.append(list(symbols))
        t0 = datetime(session_date.year, session_date.month, session_date.day, 13, 30, tzinfo=timezone.utc)
        return {s: [Bar(s, t0 + timedelta(minutes=5 * i), 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000 + i) for i in range(3)]
                for s in symbols if s != "NOBARS"}

    monkeypatch.setattr("tradebot.vendors.alpaca.fetch_intraday_bars_bulk", fake_bulk)
    # A Friday, well after the close: the session is finished -> long cache.
    monkeypatch.setattr(v1, "_now", lambda: datetime(2026, 9, 18, 23, 0, tzinfo=timezone.utc))
    v1._bars_cache.clear()

    r = client.get("/v1/bars?symbols=spy,QQQ,NOBARS,spy", headers=_auth(session))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    collect("BarSeriesSchema", body)
    assert body["sessionDate"] == "2026-09-18" and body["timeframe"] == "5m"
    assert [i["symbol"] for i in body["items"]] == ["SPY", "QQQ", "NOBARS"]
    assert len(body["items"][0]["bars"]) == 3 and body["items"][0]["bars"][0]["c"] == 100.5
    assert body["items"][2]["bars"] == []
    assert calls == [["SPY", "QQQ", "NOBARS"]]

    # Second call is served from the cache -- no vendor call.
    r = client.get("/v1/bars?symbols=SPY", headers=_auth(session))
    assert r.status_code == 200 and calls == [["SPY", "QQQ", "NOBARS"]]

    # Weekend -> still the latest session (Friday).
    monkeypatch.setattr(v1, "_now", lambda: datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc))
    session = _sign_in(app, client)  # the earlier access token aged out with the clock jump
    assert client.get("/v1/bars?symbols=SPY", headers=_auth(session)).get_json()["sessionDate"] == "2026-09-18"

    # Bounds and errors.
    r = client.get("/v1/bars?symbols=" + ",".join(f"S{i}" for i in range(13)), headers=_auth(session))
    assert r.status_code == 400 and r.get_json()["code"] == "too_many_symbols"
    collect("ErrorResponseSchema", r.get_json())
    assert client.get("/v1/bars", headers=_auth(session)).status_code == 400
    assert client.get("/v1/bars?symbols=SPY&session=2026-09-20", headers=_auth(session)).get_json()["code"] == "not_a_session"
    assert client.get("/v1/bars?symbols=SPY").status_code == 401


# ---- the contract gate -----------------------------------------------------

def test_contract_validates_with_app_schemas():
    """Runs LAST (file order): every payload collected above through the
    app's own zod schemas. Skips only when the app checkout or Node is
    missing, and says so."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH; contract gate not run")
    if not MOBILE_SCHEMAS.exists():
        pytest.skip(f"app schemas not found at {MOBILE_SCHEMAS}; set PERCH_MOBILE_SCHEMAS")
    assert COLLECTED, "no payloads collected -- test ordering broke"
    proc = subprocess.run(
        [node, str(REPO_ROOT / "scripts" / "v1_schema_check.mjs"), str(MOBILE_SCHEMAS)],
        input=json.dumps(COLLECTED), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"\n{proc.stdout}\n{proc.stderr}"
    assert "0 failure(s)" in proc.stdout
