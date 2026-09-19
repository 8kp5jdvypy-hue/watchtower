"""Direct APNs sender: HTTP/2 to Apple with a provider-token (ES256 JWT)
signed by the owner's .p8 key. Zero-cost, no push vendor in between.

Config (all from the environment; absent = `configured()` False and the
worker idles with a 'disabled' heartbeat, exactly like FINNHUB_API_KEY):

  APNS_KEY_P8_B64  the .p8 file contents, base64 on one line (fits the
                   console hidden-input method used for other secrets)
  APNS_KEY_ID      10-char key id from the Apple developer portal
  APNS_TEAM_ID     the team id (perch-mobile-mvp/app.json: 684SV6G7DV)
  APNS_TOPIC       bundle id; defaults to com.perchmarkets.ios

Never logs the key, the JWT, or a full device token (last 6 chars only).
"""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt

logger = logging.getLogger("watchtower.push.apns")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
DEFAULT_TOPIC = "com.perchmarkets.ios"
TOKEN_TTL_SECONDS = 50 * 60  # Apple: refresh within 60 min, no more than once per 20 min
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0

# APNs reasons that mean "this token will never work again".
UNREGISTER_REASONS = {"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"}
# Reasons worth retrying later (everything else is a permanent per-request failure).
RETRY_REASONS = {"TooManyRequests", "InternalServerError", "ServiceUnavailable", "Shutdown", "ExpiredProviderToken", "TooManyProviderTokenUpdates"}


class ApnsConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SendResult:
    status: int
    apns_id: str | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def unregister(self) -> bool:
        return self.status in (400, 410) and (self.reason in UNREGISTER_REASONS)

    @property
    def retry(self) -> bool:
        return self.status in (429, 500, 503) or self.reason in RETRY_REASONS


def key_material() -> str | None:
    raw = os.environ.get("APNS_KEY_P8_B64", "").strip()
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 -- any decode failure is a config error
        raise ApnsConfigError("APNS_KEY_P8_B64 is not valid base64") from exc


def configured() -> bool:
    return bool(os.environ.get("APNS_KEY_P8_B64", "").strip() and os.environ.get("APNS_KEY_ID", "").strip() and os.environ.get("APNS_TEAM_ID", "").strip())


def _mask(token: str) -> str:
    return f"…{token[-6:]}" if token else "(empty)"


class ProviderToken:
    """Caches the signed JWT and re-signs on the TTL. `now_fn` is
    injectable for tests."""

    def __init__(self, *, key_pem: str, key_id: str, team_id: str, now_fn=time.time) -> None:
        self._key_pem, self._key_id, self._team_id, self._now = key_pem, key_id, team_id, now_fn
        self._token: str | None = None
        self._issued_at = 0.0

    def get(self) -> str:
        now = self._now()
        if self._token is None or now - self._issued_at > TOKEN_TTL_SECONDS:
            self._issued_at = now
            self._token = jwt.encode(
                {"iss": self._team_id, "iat": int(now)}, self._key_pem, algorithm="ES256", headers={"kid": self._key_id}
            )
        return self._token


class ApnsClient:
    def __init__(self, *, provider_token: ProviderToken, topic: str = DEFAULT_TOPIC, transport: httpx.BaseTransport | None = None) -> None:
        self._provider = provider_token
        self._topic = topic
        self._clients: dict[str, httpx.Client] = {}
        self._transport = transport

    @classmethod
    def from_env(cls, transport: httpx.BaseTransport | None = None) -> "ApnsClient":
        pem = key_material()
        key_id = os.environ.get("APNS_KEY_ID", "").strip()
        team_id = os.environ.get("APNS_TEAM_ID", "").strip()
        if not (pem and key_id and team_id):
            raise ApnsConfigError("APNS_KEY_P8_B64 / APNS_KEY_ID / APNS_TEAM_ID are not all set")
        topic = os.environ.get("APNS_TOPIC", DEFAULT_TOPIC).strip() or DEFAULT_TOPIC
        return cls(provider_token=ProviderToken(key_pem=pem, key_id=key_id, team_id=team_id), topic=topic, transport=transport)

    def _client(self, environment: str) -> httpx.Client:
        if environment not in self._clients:
            base = HOSTS[environment]
            if self._transport is not None:
                self._clients[environment] = httpx.Client(base_url=base, transport=self._transport)
            else:
                self._clients[environment] = httpx.Client(base_url=base, http2=True, timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))
        return self._clients[environment]

    def send(self, *, device_token: str, environment: str, payload: dict, collapse_id: str | None = None, apns_id: str | None = None) -> SendResult:
        if environment not in HOSTS:
            raise ValueError(f"unknown APNs environment {environment!r}")
        apns_id = apns_id or str(uuid.uuid4())
        headers = {
            "authorization": f"bearer {self._provider.get()}",
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-id": apns_id,
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        try:
            resp = self._client(environment).post(f"/3/device/{device_token}", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("apns_transport_error env=%s token=%s error=%s", environment, _mask(device_token), type(exc).__name__)
            return SendResult(status=503, apns_id=apns_id, reason=f"transport:{type(exc).__name__}")
        reason = None
        if resp.status_code != 200:
            try:
                reason = resp.json().get("reason")
            except ValueError:
                reason = None
        result = SendResult(status=resp.status_code, apns_id=resp.headers.get("apns-id", apns_id), reason=reason)
        logger.info("apns_send env=%s token=%s status=%s reason=%s", environment, _mask(device_token), result.status, result.reason)
        return result

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()
