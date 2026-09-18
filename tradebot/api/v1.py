"""/v1 -- the iOS app's API (docs/telegram-sunset-plan-2026-09.md, M1).

The contract is the app's own validator, perch-mobile-mvp/src/api/
schemas.ts: every schema there is `strictObject`, so an extra key is a
contract failure on the device, ids are UUIDs, datetimes carry an
offset, and enums are closed. Nothing here invents data the journal
does not hold -- where the contract has a nullable slot and we have no
honest value (historicalContext, quote for an instrument the price
feed does not carry), it is null.

Auth is bearer tokens (see v1_store), not the web app's cookie session:
the app keeps tokens in the iOS Keychain and rotates the refresh token
single-flight. The two schemes coexist on one Flask app; nothing under
/v1 reads the cookie and nothing outside /v1 reads a bearer token.

Not implemented in M1 (each returns a well-formed 501 error the client
classifies as `http`, never a crash): Sign in with Apple, StoreKit
sync, device registration (M2 -- APNs), account export/deletion.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request

from tradebot import accounts, rate_limit
from tradebot.marketdata import ET, PricePoint, _session_bounds_utc

bp = Blueprint("v1", __name__, url_prefix="/v1")

# uuid5 namespace for turning journal detection ids (16 hex chars) into
# the UUIDs the contract requires. Stable across processes and deploys.
SIGNAL_ID_NAMESPACE = uuid.UUID("8f0a4b7e-2c3d-4e5f-9a1b-7c6d5e4f3a2b")
SYMBOL_RE = re.compile(r"^[A-Z0-9.-]{1,15}$")
SIGNALS_DEFAULT_LIMIT = 20
SIGNALS_MAX_LIMIT = 100
# Newest window a signal detail lookup will scan when resolving a UUID
# back to a detection id (uuid5 is one-way). Two years of HIGH/MEDIUM
# rows is well under this in practice.
SIGNAL_LOOKUP_SCAN = 20000
BAR_MINUTES = 5
DATA_MAX_AGE_SECONDS = BAR_MINUTES * 60
MAGIC_LINK_PER_EMAIL = (5, 3600)
MAGIC_LINK_PER_IP = (20, 3600)
METHODOLOGY_VERSION = "2026-09-m1"
LIMITATIONS_VERSION = "2026-09-m1"
CALCULATION_VERSION_FALLBACK = "unknown"

_LIMITATIONS = [
    "Observations are computed from 5-minute IEX bars; a bar is not known until it has closed.",
    "IEX is a single exchange's tape and can lag or thin out relative to the consolidated market on low-volume names.",
    "Historical context, when shown, is an observed base rate over past detections of the same kind and direction, not a forecast.",
    "Nothing here is a recommendation to trade; Perch never places orders and has no brokerage access.",
]


# --------------------------------------------------------------------------
# Helpers: errors, time, ids
# --------------------------------------------------------------------------

def _error(code: str, message: str, status: int, **headers: str) -> Response:
    resp = jsonify({"code": code, "message": message[:500], "requestId": str(uuid.uuid4())})
    resp.status_code = status
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _dt(dt).isoformat()


def signal_uuid(detection_id: str) -> str:
    return str(uuid.uuid5(SIGNAL_ID_NAMESPACE, detection_id))


def account_uuid(account_id: str) -> str:
    try:
        return str(uuid.UUID(account_id))
    except ValueError:
        return str(uuid.uuid5(SIGNAL_ID_NAMESPACE, f"account:{account_id}"))


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _bearer_token() -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    return token or None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from tradebot.api import v1_store

        token = _bearer_token()
        if token is None:
            return _error("session_required", "Sign in to continue.", 401)
        rec = v1_store.lookup(current_app.users_conn, token)
        now = _now()
        if rec is None or rec.kind != "access" or rec.revoked:
            return _error("session_required", "Sign in to continue.", 401)
        if now >= rec.expires_at:
            return _error("session_expired", "Your session expired; refreshing.", 401)
        account = accounts.get_account(current_app.users_conn, rec.account_id)
        if account is None:
            return _error("session_required", "Sign in to continue.", 401)
        g.v1_account = account
        g.v1_token_family = rec.family_id
        return fn(*args, **kwargs)

    return wrapper


def _entitlement(account: accounts.Account) -> dict:
    plan_map = {"beta": "beta", "free": "free", "core": "core", "pro": "core"}
    plan = "founding_member" if account.founding_member else plan_map.get(account.plan, "beta")
    return {
        "plan": plan,
        "state": "active",
        "source": "grant" if plan in ("beta", "founding_member") else "web",
        "productId": None,
        "expiresAt": None,
        "watchlistLimit": None,
        "realtimePriorityObservations": True,
        "analyticsAccess": True,
        "verifiedAt": _iso(_now()),
    }


def _account_json(account: accounts.Account) -> dict:
    return {
        "id": account_uuid(account.id),
        "email": account.email,
        "createdAt": _iso(account.created_at),
        "entitlement": _entitlement(account),
        "deletionState": "active",
    }


def _tokens_json(pair) -> dict:
    return {
        "accessToken": pair.access_token,
        "accessTokenExpiresAt": _iso(pair.access_expires_at),
        "refreshToken": pair.refresh_token,
    }


@bp.post("/auth/magic-link/request")
def magic_link_request():
    email = str(_body().get("email", "")).strip().lower()
    if "@" not in email or len(email) > 254:
        return _error("invalid_email", "Enter a valid email address.", 400)
    conn = current_app.users_conn
    if not rate_limit.allow_all(
        conn,
        [
            (f"v1_magic:email:{email}", *MAGIC_LINK_PER_EMAIL),
            (f"v1_magic:ip:{request.remote_addr}", *MAGIC_LINK_PER_IP),
        ],
    ):
        return _error("rate_limited", "Too many sign-in links requested; try again later.", 429, **{"Retry-After": "600"})
    now = _now()
    token = accounts.create_magic_link_token(conn, email, now)
    # The app reads the token from the URL FRAGMENT of a universal link
    # (authLinks.ts: https://app.perchmarkets.com/auth/magic-link#token=...);
    # a `?token=` query is explicitly rejected there. The fragment never
    # reaches a server, so a link opened without the app installed
    # cannot leak the token into access logs either.
    link_url = f"{current_app.frontend_url.rstrip('/')}/auth/magic-link#token={token}"
    try:
        current_app.email_sender.send_magic_link(email, link_url)
    except Exception:
        current_app.logger.exception("v1 magic-link email send failed")
        return _error("email_unavailable", "We could not send the sign-in link right now.", 503)
    return jsonify({"accepted": True})


@bp.post("/auth/magic-link/verify")
def magic_link_verify():
    from tradebot.api import v1_store

    body = _body()
    token = str(body.get("token", "")).strip()
    device_name = str(body.get("deviceName", "")).strip()[:100] or None
    if not token:
        return _error("invalid_token", "This sign-in link is not valid.", 400)
    conn = current_app.users_conn
    now = _now()
    email = accounts.verify_magic_link_token(conn, token, now)
    if email is None:
        return _error("invalid_token", "This sign-in link is invalid or has expired.", 401)
    account = accounts.get_or_create_account_for_email(conn, email)
    pair = v1_store.issue_pair(conn, account.id, now=now, device_name=device_name)
    return jsonify({"account": _account_json(account), "tokens": _tokens_json(pair)})


@bp.post("/auth/refresh")
def auth_refresh():
    from tradebot.api import v1_store

    refresh_token = str(_body().get("refreshToken", "")).strip()
    if not refresh_token:
        return _error("session_required", "Sign in to continue.", 401)
    pair = v1_store.rotate(current_app.users_conn, refresh_token, now=_now())
    if pair is None:
        return _error("session_expired", "Sign in again to continue.", 401)
    return jsonify(_tokens_json(pair))


@bp.post("/auth/logout")
@login_required
def auth_logout():
    from tradebot.api import v1_store

    v1_store.revoke_family(current_app.users_conn, g.v1_token_family, now=_now())
    return Response(status=204)


@bp.post("/auth/apple")
def auth_apple():
    return _error("not_implemented", "Sign in with Apple is not available yet; use an email sign-in link.", 501)


@bp.get("/me")
@login_required
def me():
    return jsonify(_account_json(g.v1_account))


@bp.get("/entitlements")
@login_required
def entitlements():
    return jsonify(_entitlement(g.v1_account))


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketState:
    state: str  # premarket | open | after_hours | closed | holiday
    next_transition_at: datetime | None


def _next_session_premarket(after: date) -> datetime | None:
    for offset in range(1, 15):
        d = after + timedelta(days=offset)
        bounds = _session_bounds_utc(d)
        if bounds is not None:
            return datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET).astimezone(timezone.utc)
    return None


def market_state(now: datetime) -> MarketState:
    now = _dt(now)
    local = now.astimezone(ET)
    today = local.date()
    bounds = _session_bounds_utc(today)
    if bounds is None:
        state = "holiday" if local.weekday() < 5 else "closed"
        return MarketState(state, _next_session_premarket(today))
    open_at, close_at = bounds
    pre_start = local.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    ah_end = local.replace(hour=20, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if now < pre_start:
        return MarketState("closed", pre_start)
    if now < open_at:
        return MarketState("premarket", open_at)
    if now < close_at:
        return MarketState("open", close_at)
    if now < ah_end:
        return MarketState("after_hours", ah_end)
    return MarketState("closed", _next_session_premarket(today))


def _read_heartbeat(path: Path) -> datetime | None:
    try:
        payload = json.loads(path.read_text())
        return _dt(payload["ts_utc"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def service_status(now: datetime, *, heartbeat_at: datetime | None, halted: bool) -> dict:
    ms = market_state(now)
    trading = ms.state in ("premarket", "open", "after_hours")
    if heartbeat_at is None:
        feed_status, feed_msg = ("unknown", "No scanner heartbeat recorded yet.") if trading else ("operational", None)
    else:
        age = (now - heartbeat_at).total_seconds()
        if trading and age > BAR_MINUTES * 60 * 2:
            feed_status, feed_msg = "stale", f"Last scanner loop {int(age)}s ago."
        else:
            feed_status, feed_msg = "operational", None
    if halted:
        overall, message = "maintenance", "Alerts are paused by the operator."
    elif feed_status == "stale":
        overall, message = "degraded", feed_msg
    elif feed_status == "unknown":
        overall, message = "degraded", feed_msg
    else:
        overall, message = "operational", None
    return {
        "status": overall,
        "generatedAt": _iso(now),
        "market": {
            "state": ms.state,
            "referenceTimeZone": "America/New_York",
            "nextTransitionAt": _iso(ms.next_transition_at) if ms.next_transition_at else None,
        },
        "sources": [
            {
                "vendor": "alpaca",
                "feed": "iex",
                "status": feed_status,
                "lastObservationAt": _iso(heartbeat_at) if heartbeat_at else None,
                "message": feed_msg,
            }
        ],
        "message": message,
    }


def _status_now() -> dict:
    from tradebot.runner import HALT_FILE, HEARTBEAT_FILE

    return service_status(_now(), heartbeat_at=_read_heartbeat(HEARTBEAT_FILE), halted=HALT_FILE.exists())


@bp.get("/status")
def status():
    return jsonify(_status_now())


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

_SIGNAL_COLUMNS = (
    "id, ts_utc, session, symbol, kinds, headlines, score, tier, trend, close, context_json, "
    "code_version, no_trade, news_driven, primary_kind, event_kind, event_severity, data_feed, origin, "
    "pct_from_prior_close, pct_from_prior_close_status"
)

_EVIDENCE_KIND = {
    "level_break": "price_move",
    "range_expansion": "range",
    "rvol": "volume",
    "volume_surge": "volume",
    "vwap_reclaim": "vwap",
    "vwap_break": "vwap",
    "relative_strength_break": "benchmark",
    "gap": "price_move",
    "momentum": "price_move",
}


def _session_label(ts: datetime) -> str:
    local = ts.astimezone(ET)
    bounds = _session_bounds_utc(local.date())
    if bounds is None:
        return "regular"
    open_at, close_at = bounds
    if ts < open_at:
        return "premarket"
    if ts < close_at:
        return "regular"
    return "after_hours"


def _source_ref(feed: str | None, observed_at: datetime, ingested_at: datetime | None = None) -> dict:
    return {
        "vendor": "alpaca",
        "feed": (feed or "iex").lower(),
        "delayed": False,
        "delaySeconds": None,
        "observedAt": _iso(observed_at),
        "ingestedAt": _iso(ingested_at or observed_at),
        "maxAgeSeconds": DATA_MAX_AGE_SECONDS,
        "attribution": "Market data by Alpaca",
    }


def _summary(row: tuple) -> dict:
    (id_, ts_utc, _session, symbol, _kinds, headlines, _score, tier, _trend, _close, _ctx, _cv, _nt, _nd,
     _pk, _ek, _es, data_feed, origin, _pct, _pct_status) = row
    ts = _dt(ts_utc)
    return {
        "id": signal_uuid(id_),
        "symbol": symbol,
        "observedAt": _iso(ts),
        "session": _session_label(ts),
        "origin": "watchlist" if (origin or "watchlist") == "watchlist" else "radar",
        "label": "worth_a_closer_look" if tier == "high" else "context",
        "headline": (headlines or f"{symbol} observation")[:300],
        "source": _source_ref(data_feed, ts),
        "correctionState": "original",
    }


def _encode_cursor(ts_utc: str, id_: str) -> str:
    raw = json.dumps({"ts": ts_utc, "id": id_}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(data["ts"]), str(data["id"])
    except (ValueError, KeyError, TypeError):
        return None


def _query_signals(
    conn, *, limit: int, cursor: tuple[str, str] | None, symbols: list[str] | None, origin: str | None,
    session_date: str | None = None,
) -> list[tuple]:
    sql = f"SELECT {_SIGNAL_COLUMNS} FROM detections WHERE tier IN ('high', 'medium')"
    params: list[Any] = []
    if session_date is not None:
        sql += " AND session = ?"
        params.append(session_date)
    if cursor is not None:
        sql += " AND (ts_utc < ? OR (ts_utc = ? AND id < ?))"
        params.extend([cursor[0], cursor[0], cursor[1]])
    if symbols:
        sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
        params.extend(symbols)
    if origin == "watchlist":
        sql += " AND (origin IS NULL OR origin = 'watchlist')"
    elif origin == "radar":
        sql += " AND origin IS NOT NULL AND origin != 'watchlist'"
    sql += " ORDER BY ts_utc DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


@bp.get("/today")
@login_required
def today():
    # Same session-date definition runner.py uses (now in ET), via _now()
    # so the whole module has one clock to test against.
    session_date = _now().astimezone(ET).date().isoformat()
    rows = _query_signals(current_app.journal_conn, limit=3, cursor=None, symbols=None, origin=None, session_date=session_date)
    return jsonify({"sessionDate": session_date, "status": _status_now(), "items": [_summary(r) for r in rows]})


@bp.get("/signals")
@login_required
def signals():
    try:
        limit = int(request.args.get("limit", SIGNALS_DEFAULT_LIMIT))
    except ValueError:
        return _error("invalid_limit", "limit must be an integer.", 400)
    limit = max(1, min(limit, SIGNALS_MAX_LIMIT))
    cursor_arg = request.args.get("cursor")
    cursor = _decode_cursor(cursor_arg) if cursor_arg else None
    if cursor_arg and cursor is None:
        return _error("invalid_cursor", "cursor is not valid.", 400)
    symbols = [s.strip().upper() for s in request.args.get("symbols", "").split(",") if s.strip()]
    if any(not SYMBOL_RE.match(s) for s in symbols):
        return _error("invalid_symbol", "symbols contains an invalid symbol.", 400)
    origin = request.args.get("origin")
    if origin not in (None, "watchlist", "radar"):
        return _error("invalid_origin", "origin must be watchlist or radar.", 400)
    if request.args.get("corrected") == "true":
        # No corrections have ever been published; an honest empty page.
        return jsonify({"items": [], "nextCursor": None, "generatedAt": _iso(_now())})
    rows = _query_signals(current_app.journal_conn, limit=limit + 1, cursor=cursor, symbols=symbols or None, origin=origin)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1][1], rows[-1][0]) if has_more and rows else None
    return jsonify({"items": [_summary(r) for r in rows], "nextCursor": next_cursor, "generatedAt": _iso(_now())})


def _resolve_detection_id(conn, signal_id: str) -> str | None:
    try:
        uuid.UUID(signal_id)
    except ValueError:
        return None
    rows = conn.execute(
        "SELECT id FROM detections WHERE tier IN ('high', 'medium') ORDER BY ts_utc DESC LIMIT ?", (SIGNAL_LOOKUP_SCAN,)
    ).fetchall()
    for (detection_id,) in rows:
        if signal_uuid(detection_id) == signal_id:
            return detection_id
    return None


def _evidence_items(kinds: list[str], contexts: list[dict], ts: datetime, close, pct, pct_status) -> list[dict]:
    items: list[dict] = []
    if close is not None:
        items.append({"kind": "price_move", "label": "Price at observation", "value": f"${float(close):,.2f}", "observedAt": _iso(ts)})
    if pct_status == "AVAILABLE" and pct is not None:
        items.append({"kind": "price_move", "label": "Move from prior close", "value": f"{float(pct):+.2f}%", "observedAt": _iso(ts)})
    for index, kind in enumerate(kinds):
        ctx = contexts[index] if index < len(contexts) and isinstance(contexts[index], dict) else {}
        evidence_kind = _EVIDENCE_KIND.get(kind, "data_quality" if kind.startswith("dq_") else "price_move")
        value = ", ".join(f"{k}={v}" for k, v in ctx.items() if isinstance(v, (int, float, str)))[:500] or "detected"
        items.append({"kind": evidence_kind, "label": kind.replace("_", " "), "value": value, "observedAt": _iso(ts)})
    return items


@bp.get("/signals/<signal_id>")
@login_required
def signal_detail(signal_id: str):
    conn = current_app.journal_conn
    detection_id = _resolve_detection_id(conn, signal_id)
    if detection_id is None:
        return _error("not_found", "Signal not found.", 404)
    row = conn.execute(f"SELECT {_SIGNAL_COLUMNS} FROM detections WHERE id = ?", (detection_id,)).fetchone()
    if row is None:
        return _error("not_found", "Signal not found.", 404)
    (id_, ts_utc, _session, symbol, kinds, headlines, score, tier, trend, close, context_json,
     code_version, no_trade, news_driven, primary_kind, event_kind, event_severity, data_feed, _origin,
     pct, pct_status) = row
    ts = _dt(ts_utc)
    kinds_list = [k for k in (kinds or "").split(",") if k]
    try:
        contexts = json.loads(context_json) if context_json else []
    except ValueError:
        contexts = []
    weakening: list[str] = []
    if no_trade:
        weakening.append("Flagged no-trade by the scanner's own guard at the time of observation.")
    if news_driven:
        weakening.append("The move coincided with news; base rates for this kind exclude news-driven history.")
    if event_kind:
        sev = f" ({event_severity})" if event_severity else ""
        weakening.append(f"Scheduled event context: {event_kind}{sev}.")
    if tier == "medium":
        weakening.append("Medium tier: shown for context, below the threshold Perch treats as worth a closer look.")
    lead_kind = primary_kind or (kinds_list[0] if kinds_list else "observation")
    why = f"{lead_kind.replace('_', ' ')} on {symbol}"
    if trend:
        why += f", {trend} trend"
    if score is not None:
        why += f", score {float(score):.1f}"
    why += "."
    detail = _summary(row)
    detail.update(
        {
            "whatHappened": (headlines or why)[:2000],
            "whyItStandsOut": why[:2000],
            "supportingEvidence": _evidence_items(kinds_list, contexts, ts, close, pct, pct_status),
            "weakeningFactors": weakening,
            "evidenceSnapshot": {
                "calculationVersion": (code_version or CALCULATION_VERSION_FALLBACK)[:100],
                "barTimeframeMinutes": BAR_MINUTES,
                "source": _source_ref(data_feed, ts),
                "detectors": kinds_list,
                "comparisonBenchmark": "SPY" if "relative_strength_break" in kinds_list else None,
                "dataQualityFlags": [],
                "limitationsVersion": LIMITATIONS_VERSION,
            },
            # journal.historical_performance returns an aggregate without
            # the sample's date bounds the contract requires; rather than
            # invent them, M1 ships null (the app omits the chart).
            "historicalContext": None,
            "limitations": _LIMITATIONS,
            "corrections": [],
        }
    )
    return jsonify(detail)


# --------------------------------------------------------------------------
# Instruments and watchlist
# --------------------------------------------------------------------------

_ETF_HINTS = ("ETF", "TRUST", "FUND", "ISHARES", "SPDR", "VANGUARD", "INVESCO", "PROSHARES")


def _instrument_json(symbol: str, name: str | None, exchange: str | None, active: bool, now: datetime) -> dict:
    upper_name = (name or "").upper()
    asset_type = "etf" if any(h in upper_name for h in _ETF_HINTS) else "equity"
    return {
        "symbol": symbol,
        "name": (name or symbol)[:300],
        "exchange": (exchange or "UNKNOWN")[:40],
        "assetType": asset_type,
        "active": bool(active),
        "source": {
            "vendor": "alpaca",
            "feed": "assets",
            "delayed": False,
            "delaySeconds": None,
            "observedAt": _iso(now),
            "ingestedAt": _iso(now),
            "maxAgeSeconds": 86400,
            "attribution": "Asset reference data by Alpaca",
        },
    }


def _lookup_instruments(symbols: list[str], now: datetime) -> dict[str, dict]:
    conn = getattr(current_app, "universe_conn", None)
    found: dict[str, dict] = {}
    if conn is not None and symbols:
        rows = conn.execute(
            f"SELECT symbol, name, exchange, is_active FROM assets WHERE symbol IN ({','.join('?' * len(symbols))})",
            symbols,
        ).fetchall()
        for symbol, name, exchange, is_active in rows:
            found[symbol] = _instrument_json(symbol, name, exchange, bool(is_active), now)
    for symbol in symbols:
        found.setdefault(symbol, _instrument_json(symbol, None, None, True, now))
    return found


_PRICE_SOURCE = {
    "coinbase": ("coinbase", "exchange"),
    "kraken": ("kraken", "public"),
    "alpaca_iex": ("alpaca", "iex"),
    "finnhub": ("finnhub", "quote"),
}


def _quote_json(point: PricePoint, now: datetime) -> dict:
    vendor, feed = _PRICE_SOURCE.get(point.source, (point.source, "unknown"))
    return {
        "symbol": point.symbol,
        "value": point.price,
        "valueKind": "last",
        "currency": "USD",
        "source": {
            "vendor": vendor,
            "feed": feed,
            "delayed": False,
            "delaySeconds": None,
            "observedAt": _iso(point.ts),
            "ingestedAt": _iso(now),
            "maxAgeSeconds": DATA_MAX_AGE_SECONDS,
            "attribution": None,
        },
        "stale": bool(point.stale),
    }


def _quotes_for(symbols: list[str], now: datetime) -> dict[str, dict]:
    feed = getattr(current_app, "price_feed", None)
    if feed is None:
        return {}
    configured = {i.symbol for i in feed.instruments}
    wanted = [s for s in symbols if s in configured]
    if not wanted:
        return {}
    try:
        points = feed.get(wanted)
    except Exception:
        current_app.logger.exception("v1 price feed failed; watchlist quotes omitted")
        return {}
    return {s: _quote_json(p, now) for s, p in points.items()}


def _watchlist_json(stored, now: datetime) -> dict:
    symbols = [item["symbol"] for item in stored.items]
    instruments = _lookup_instruments(symbols, now)
    quotes = _quotes_for(symbols, now)
    return {
        "version": stored.version,
        "limit": None,
        "items": [
            {
                "symbol": item["symbol"],
                "notificationsEnabled": bool(item.get("notificationsEnabled", True)),
                "instrument": instruments[item["symbol"]],
                "quote": quotes.get(item["symbol"]),
            }
            for item in stored.items
        ],
        "radarEnabled": stored.radar_enabled,
        "updatedAt": _iso(stored.updated_at),
    }


@bp.get("/watchlist")
@login_required
def watchlist_get():
    from tradebot.api import v1_store

    now = _now()
    stored = v1_store.get_watchlist(current_app.users_conn, g.v1_account.id, now=now)
    return jsonify(_watchlist_json(stored, now))


@bp.put("/watchlist")
@login_required
def watchlist_put():
    from tradebot.api import v1_store

    body = _body()
    raw_items = body.get("items")
    version = body.get("version")
    if not isinstance(raw_items, list) or not isinstance(version, int) or isinstance(version, bool):
        return _error("invalid_request", "items (list) and version (integer) are required.", 400)
    items: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            return _error("invalid_request", "Each item must be an object.", 400)
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not SYMBOL_RE.match(symbol):
            return _error("invalid_symbol", f"{symbol or '(empty)'} is not a valid symbol.", 400)
        if symbol in seen:
            continue
        seen.add(symbol)
        items.append({"symbol": symbol, "notificationsEnabled": bool(raw.get("notificationsEnabled", True))})
    radar = body.get("radarEnabled")
    if radar is not None and not isinstance(radar, bool):
        return _error("invalid_request", "radarEnabled must be a boolean.", 400)
    now = _now()
    stored = v1_store.replace_watchlist(
        current_app.users_conn, g.v1_account.id, expected_version=version, items=items, radar_enabled=radar, now=now
    )
    if stored is None:
        return _error("watchlist_version_conflict", "Your watchlist changed on another device; reload and try again.", 409)
    return jsonify(_watchlist_json(stored, now))


@bp.get("/instruments")
@login_required
def instruments():
    query = request.args.get("query", "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 50))
    except ValueError:
        return _error("invalid_limit", "limit must be an integer.", 400)
    if len(query) < 1 or len(query) > 40:
        return jsonify({"items": []})
    conn = getattr(current_app, "universe_conn", None)
    if conn is None:
        return jsonify({"items": []})
    upper = query.upper()
    like_symbol = upper.replace("%", "").replace("_", "") + "%"
    like_name = "%" + query.replace("%", "").replace("_", "") + "%"
    rows = conn.execute(
        "SELECT symbol, name, exchange FROM assets WHERE is_active = 1 AND tradable = 1 "
        "AND (symbol LIKE ? OR name LIKE ? COLLATE NOCASE) "
        "ORDER BY (symbol = ?) DESC, (symbol LIKE ?) DESC, length(symbol), symbol LIMIT ?",
        (like_symbol, like_name, upper, like_symbol, limit * 2),
    ).fetchall()
    now = _now()
    items: list[dict] = []
    seen: set[str] = set()
    for symbol, name, exchange in rows:
        if symbol in seen or not SYMBOL_RE.match(symbol):
            continue
        seen.add(symbol)
        items.append(_instrument_json(symbol, name, exchange, True, now))
        if len(items) >= limit:
            break
    return jsonify({"items": items})


# --------------------------------------------------------------------------
# Methodology and the M1 gaps
# --------------------------------------------------------------------------

METHODOLOGY = {
    "version": METHODOLOGY_VERSION,
    "effectiveAt": "2026-09-18T00:00:00+00:00",
    "summary": (
        "Perch watches a fixed list of US equities on 5-minute IEX bars during premarket, regular, and "
        "after-hours sessions. Pure detectors compare each closed bar against session anchors (opening range, "
        "prior close, VWAP, relative volume, ATR-scaled ranges) and record every observation in a journal "
        "before anything is shown. Observations labelled 'worth a closer look' crossed the high threshold; "
        "'context' observations crossed the medium one. Thresholds are expressed in ATR units, not "
        "percentages, and anchors are frozen once per session. Perch never places orders."
    ),
    "boundaries": [
        "Research and observation only; nothing is a recommendation to buy or sell.",
        "No brokerage connection, no order placement, no position tracking on your behalf.",
        "Observations are journaled before display; nothing is shown that was not recorded first.",
    ],
    "limitations": _LIMITATIONS,
    "sourceUrl": "https://perchmarkets.com/methodology",
}


@bp.get("/methodology")
def methodology():
    return jsonify(METHODOLOGY)


def _not_implemented(what: str):
    return _error("not_implemented", f"{what} is not available in this version of Perch yet.", 501)


@bp.post("/billing/apple/sync")
def billing_apple_sync():
    return _not_implemented("Subscription sync")


@bp.post("/devices")
def devices_register():
    return _not_implemented("Push notification registration")


@bp.delete("/devices/<device_id>")
def devices_unregister(device_id: str):
    return _not_implemented("Push notification registration")


@bp.post("/account/export")
def account_export():
    return _not_implemented("Account export")


@bp.get("/account/exports/<request_id>")
def account_export_status(request_id: str):
    return _not_implemented("Account export")


@bp.delete("/account")
def account_delete():
    return _not_implemented("Account deletion")


@bp.post("/account/reauthenticate/magic-link/request")
@bp.post("/account/reauthenticate/magic-link/verify")
@bp.post("/account/reauthenticate/apple")
def account_reauthenticate():
    return _not_implemented("Re-authentication")


def register(app) -> None:
    """Wire the blueprint and its storage onto a create_app() Flask app."""
    import sqlite3

    from tradebot import universe
    from tradebot.api import v1_store

    v1_store.ensure_schema(app.users_conn)
    if getattr(app, "universe_conn", None) is None:
        # Read-only: /v1 only ever SELECTs from the universe; the scanner's
        # refresh_universe is the sole writer. check_same_thread=False
        # matches journal_connect's discipline for the same gunicorn reason.
        db_path = Path(universe.DEFAULT_DB_PATH)
        app.universe_conn = None
        if db_path.exists():
            try:
                app.universe_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            except sqlite3.Error:
                app.logger.exception("v1: universe.db unavailable; instrument search disabled")
    app.register_blueprint(bp)
