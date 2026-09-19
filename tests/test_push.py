"""tradebot.push -- store, APNs client (fake transport, real ES256 key),
worker core, and the fan-out hook. No network."""
from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tradebot.api import v1_store
from tradebot.push import apns, store
from tradebot.push.delivery import build_payload, compose_hooks, eligible_devices, make_push_subscriber_hook
from tradebot.push.worker import WorkerCore

T0 = datetime(2026, 9, 18, 14, 35, tzinfo=timezone.utc)  # regular session


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    v1_store.ensure_schema(c)
    store.ensure_schema(c)
    return c


def _register(conn, account="acct-1", installation="9d2c1b9a-1b2c-4d3e-8f4a-5b6c7d8e9f00", token="ab" * 32, **prefs):
    return store.register_device(
        conn, account_id=account, installation_id=installation, platform="ios", apns_environment="sandbox", token=token,
        app_version="1.0", locale="en-US", time_zone="America/New_York", preferences=prefs, idempotency_key="k", now=T0,
    )


def _watch(conn, account, symbols):
    v1_store.replace_watchlist(conn, account, expected_version=0, items=[{"symbol": s, "notificationsEnabled": True} for s in symbols], radar_enabled=False, now=T0)


# ---- store ---------------------------------------------------------------

def test_register_is_idempotent_on_installation_and_rotates_token(conn):
    a = _register(conn, token="aa" * 32)
    b = _register(conn, token="bb" * 32)
    assert a.id == b.id and b.token == "bb" * 32 and b.active
    store.revoke_device_token(conn, a.id, "apns:Unregistered", now=T0)
    assert store.get_device(conn, a.id).active is False
    c = _register(conn, token="cc" * 32)  # a fresh registration revives a revoked row
    assert c.id == a.id and c.active


def test_unregister_is_scoped_to_the_owning_account(conn):
    d = _register(conn)
    assert store.unregister_device(conn, d.id, "someone-else", now=T0) is False
    assert store.unregister_device(conn, d.id, "acct-1", now=T0) is True
    assert store.get_device(conn, d.id).active is False


def test_enqueue_is_idempotent_per_alert_and_device(conn):
    d = _register(conn)
    assert store.enqueue(conn, "alert-1", [d], {"aps": {}}, now=T0) == 1
    assert store.enqueue(conn, "alert-1", [d], {"aps": {}}, now=T0) == 0
    assert store.delivery_summary(conn, "alert-1") == {"pending": 1}


def test_retry_backoff_then_failed(conn):
    d = _register(conn)
    store.enqueue(conn, "a", [d], {}, now=T0)
    (row,) = store.claim_ready_batch(conn, "w", 10, T0)
    store.mark_retry(conn, row.id, row.attempts, "apns:503", T0)
    assert store.claim_ready_batch(conn, "w", 10, T0) == []            # backoff: not ready yet
    assert len(store.claim_ready_batch(conn, "w", 10, T0 + timedelta(minutes=1))) == 1
    for _ in range(store.MAX_ATTEMPTS):
        store.mark_retry(conn, row.id, store.MAX_ATTEMPTS - 1, "apns:503", T0)
    assert store.delivery_summary(conn, "a") == {"failed": 1}


def test_stale_lease_is_reclaimed(conn):
    d = _register(conn)
    store.enqueue(conn, "a", [d], {}, now=T0)
    store.claim_ready_batch(conn, "w1", 10, T0)
    assert store.reclaim_stale_in_flight(conn, T0 + timedelta(minutes=1)) == 0
    assert store.reclaim_stale_in_flight(conn, T0 + timedelta(minutes=3)) == 1
    assert len(store.claim_ready_batch(conn, "w2", 10, T0 + timedelta(minutes=3))) == 1


# ---- eligibility + payload ----------------------------------------------

def test_eligible_devices_respect_watchlist_pause_session_and_quiet_hours(conn):
    _watch(conn, "acct-1", ["SPY"]); _watch(conn, "acct-2", ["SPY"]); _watch(conn, "acct-3", ["QQQ"])
    d1 = _register(conn, "acct-1", "11111111-1111-4111-8111-111111111111", "11" * 32)
    _register(conn, "acct-2", "22222222-2222-4222-8222-222222222222", "22" * 32, paused=True)
    _register(conn, "acct-3", "33333333-3333-4333-8333-333333333333", "33" * 32)
    assert [d.id for d in eligible_devices(conn, "SPY", T0)] == [d1.id]
    # premarket alert, device only wants regular
    assert eligible_devices(conn, "SPY", datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)) == []
    # quiet hours 10:00-11:00 local (ET); T0 is 10:35 ET
    _register(conn, "acct-1", "11111111-1111-4111-8111-111111111111", "11" * 32, quietStartLocal="10:00", quietEndLocal="11:00")
    assert eligible_devices(conn, "SPY", T0) == []


def test_hook_enqueues_payload_with_v1_signal_id(conn):
    from tradebot.api.v1 import signal_uuid

    _watch(conn, "acct-1", ["SPY"]); d = _register(conn)
    cluster = SimpleNamespace(id="bbc9b1637e1d977a", symbol="SPY", ts_utc=T0.isoformat(), tier="high", headlines="SPY bar range 3.11 is 5.3x ATR(14)=0.59", score=4.2)
    make_push_subscriber_hook(conn)(cluster, "rendered text", None)
    (row,) = store.claim_ready_batch(conn, "w", 10, T0)
    assert row.alert_id == "bbc9b1637e1d977a" and row.device_id == d.id and row.collapse_id == "bbc9b1637e1d977a"
    assert row.payload["aps"]["alert"] == {"title": "SPY — worth a closer look", "body": "SPY bar range 3.11 is 5.3x ATR(14)=0.59"}
    assert row.payload["signalId"] == signal_uuid("bbc9b1637e1d977a") and row.payload["symbol"] == "SPY"


def test_compose_hooks_isolates_failures():
    calls = []
    def ok(c, t, m): calls.append("ok")
    def boom(c, t, m): raise RuntimeError("push down")
    composed = compose_hooks(boom, ok)
    composed(SimpleNamespace(id="x"), "t", None)
    assert calls == ["ok"]
    assert compose_hooks(None, None) is None


# ---- APNs client ---------------------------------------------------------

def _p8_b64():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return base64.b64encode(pem).decode(), key.public_key()


def test_provider_token_is_es256_with_kid_and_refreshes_after_ttl(monkeypatch):
    b64, pub = _p8_b64()
    monkeypatch.setenv("APNS_KEY_P8_B64", b64); monkeypatch.setenv("APNS_KEY_ID", "ABC1234567"); monkeypatch.setenv("APNS_TEAM_ID", "684SV6G7DV")
    assert apns.configured()
    clock = {"t": 1_000_000.0}
    pt = apns.ProviderToken(key_pem=apns.key_material(), key_id="ABC1234567", team_id="684SV6G7DV", now_fn=lambda: clock["t"])
    tok = pt.get()
    assert jwt.get_unverified_header(tok) == {"alg": "ES256", "kid": "ABC1234567", "typ": "JWT"}
    assert jwt.decode(tok, pub, algorithms=["ES256"]) == {"iss": "684SV6G7DV", "iat": 1_000_000}
    clock["t"] += 10 * 60
    assert pt.get() == tok                       # cached
    clock["t"] += 45 * 60
    assert pt.get() != tok                       # re-signed after TTL


def test_client_maps_apns_responses(monkeypatch):
    b64, _ = _p8_b64()
    monkeypatch.setenv("APNS_KEY_P8_B64", b64); monkeypatch.setenv("APNS_KEY_ID", "K"); monkeypatch.setenv("APNS_TEAM_ID", "T")
    seen = []
    def handler(request: httpx.Request):
        seen.append(request)
        token = request.url.path.rsplit("/", 1)[1]
        if token.endswith("dead"):
            return httpx.Response(410, json={"reason": "Unregistered"})
        if token.endswith("busy"):
            return httpx.Response(503, json={"reason": "ServiceUnavailable"})
        if token.endswith("bad0"):
            return httpx.Response(400, json={"reason": "BadTopic"})
        return httpx.Response(200, headers={"apns-id": "apns-123"})
    client = apns.ApnsClient.from_env(transport=httpx.MockTransport(handler))
    ok = client.send(device_token="ab" * 30 + "okok", environment="sandbox", payload={"aps": {"alert": "x"}}, collapse_id="c1")
    assert ok.ok and ok.apns_id == "apns-123"
    req = seen[0]
    assert req.url.host == "api.sandbox.push.apple.com" and req.headers["apns-topic"] == "com.perchmarkets.ios"
    assert req.headers["authorization"].startswith("bearer ") and req.headers["apns-collapse-id"] == "c1" and req.headers["apns-push-type"] == "alert"
    dead = client.send(device_token="ab" * 30 + "dead", environment="production", payload={})
    assert dead.unregister and not dead.retry and seen[-1].url.host == "api.push.apple.com"
    assert client.send(device_token="ab" * 30 + "busy", environment="sandbox", payload={}).retry
    bad = client.send(device_token="ab" * 30 + "bad0", environment="sandbox", payload={})
    assert not bad.ok and not bad.retry and not bad.unregister


def test_unconfigured_is_explicit(monkeypatch):
    for k in ("APNS_KEY_P8_B64", "APNS_KEY_ID", "APNS_TEAM_ID"):
        monkeypatch.delenv(k, raising=False)
    assert apns.configured() is False
    with pytest.raises(apns.ApnsConfigError):
        apns.ApnsClient.from_env()


# ---- worker core -----------------------------------------------------------

class _FakeSender:
    def __init__(self, results): self.results, self.sent = results, []
    def send(self, *, device_token, environment, payload, collapse_id=None, apns_id=None):
        self.sent.append(device_token)
        return self.results[device_token]


def test_worker_tick_records_each_outcome_and_revokes_dead_tokens(conn):
    _watch(conn, "a1", ["SPY"]); _watch(conn, "a2", ["SPY"]); _watch(conn, "a3", ["SPY"])
    ok = _register(conn, "a1", "11111111-1111-4111-8111-111111111111", "aa" * 32)
    dead = _register(conn, "a2", "22222222-2222-4222-8222-222222222222", "bb" * 32)
    busy = _register(conn, "a3", "33333333-3333-4333-8333-333333333333", "cc" * 32)
    store.enqueue(conn, "alert-1", [ok, dead, busy], {"aps": {}}, now=T0)
    sender = _FakeSender({
        "aa" * 32: apns.SendResult(200, "id-ok", None),
        "bb" * 32: apns.SendResult(410, "id-dead", "Unregistered"),
        "cc" * 32: apns.SendResult(503, "id-busy", "ServiceUnavailable"),
    })
    counts = WorkerCore(conn, sender, worker_id="t", now_fn=lambda: T0).tick()
    assert counts == {"reclaimed": 0, "delivered": 1, "retried": 1, "failed": 0, "unsubscribed": 1}
    assert store.delivery_summary(conn, "alert-1") == {"delivered": 1, "pending": 1, "unsubscribed": 1}
    assert store.get_device(conn, dead.id).active is False
    assert WorkerCore(conn, sender, worker_id="t", now_fn=lambda: T0).tick()["delivered"] == 0  # nothing ready yet
