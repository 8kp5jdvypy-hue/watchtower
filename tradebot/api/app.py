"""Internal read/auth API for the web dashboard (app.perchmarkets.com).

Deliberately thin: every endpoint calls into existing tradebot.journal /
tradebot.telegram_bot.db / tradebot.telegram_bot.performance query
functions rather than reimplementing anything. The only new logic here
is HTTP plumbing (routing, auth, CORS) — see tradebot.accounts for the
actual identity/magic-link logic and tradebot.entitlements for plans.

Auth is a signed session cookie holding an account_id (Flask's built-in
itsdangerous-signed cookie session — no server-side session table to
manage or expire). A magic-link token from tradebot.accounts is what
proves the person controls that email address, once, at verify time;
after that the cookie is what keeps them signed in.

Run in production under gunicorn (see docker-compose.yml's `api`
service): `gunicorn tradebot.api.wsgi:app -b 0.0.0.0:8000`. create_app()
here is a factory, not a module-level `app` object, on purpose — it
opens real sqlite connections to data/users.db and data/journal.db as a
side effect, and that must only happen when something actually intends
to serve requests, never just from importing this module (e.g. in tests
that only want create_app itself to call with tmp_path databases). See
tradebot/api/wsgi.py for the module that actually instantiates it for
gunicorn. Running this file directly (`python -m tradebot.api.app`)
uses Flask's dev server, for local development only.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response as WerkzeugResponse

logger = logging.getLogger(__name__)

import csv
import io

from tradebot import accounts, client_errors, config, funnel_events, rate_limit
from tradebot.telegram_bot.access import can_access
from tradebot.email_sender import build_email_sender
from tradebot.journal import connect as journal_connect
from tradebot.journal import CLOSE_MARK_OFFSET_MIN, historical_performance, kind_performance, tier_performance
from tradebot.marketdata import fetch_quotes
from tradebot.runner import ET
from tradebot.telegram_bot import db as users_db
from tradebot.telegram_bot.performance import public_alert_history, track_record

DEFAULT_FRONTEND_URL = "https://app.perchmarkets.com"
# The itsdangerous-signed session cookie has no server-side revocation —
# without an expiry, a copied/leaked cookie stays valid forever. 30 days
# bounds that blast radius while still being "stay signed in" for a
# passwordless product where re-auth means waiting on another email.
SESSION_LIFETIME_DAYS = 30
# This API accepts only compact JSON/text beacon payloads and has no
# upload endpoint. Keep this in lockstep with Caddyfile's 64KB outer
# limit so oversized bodies are rejected both before proxying and when
# gunicorn/Flask is reached directly.
MAX_REQUEST_BODY_BYTES = 64_000
MAX_EMAIL_LENGTH = 254


class _RequestBodyLimitMiddleware:
    """Bound even chunked WSGI bodies before Flask materializes them.

    Werkzeug enforces ``MAX_CONTENT_LENGTH`` for declared lengths, but
    a terminated stream without Content-Length can stop exactly at the
    limit without probing for one more byte. Reading at most limit + 1
    here distinguishes an oversized stream while keeping memory use
    bounded, then restores an ordinary fixed-length stream for Flask.
    """

    def __init__(self, application, max_bytes: int):
        self.application = application
        self.max_bytes = max_bytes

    def __call__(self, environ, start_response):
        content_length = environ.get("CONTENT_LENGTH")
        try:
            declared_length = int(content_length) if content_length else None
        except (TypeError, ValueError):
            declared_length = None

        if declared_length is not None and declared_length > self.max_bytes:
            return self._too_large(environ, start_response)

        if declared_length is None and environ.get("wsgi.input_terminated"):
            body = environ["wsgi.input"].read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                return self._too_large(environ, start_response)
            environ["wsgi.input"] = io.BytesIO(body)
            environ["CONTENT_LENGTH"] = str(len(body))
            environ.pop("wsgi.input_terminated", None)

        return self.application(environ, start_response)

    @staticmethod
    def _too_large(environ, start_response):
        response = WerkzeugResponse(
            json.dumps({"error": "request body too large"}) + "\n",
            status=413,
            content_type="application/json",
        )
        return response(environ, start_response)

# /performance ran two unbounded, uncached full-table queries on every
# single request. A short TTL cache is a pragmatic fix, not a real
# invalidation scheme — performance stats don't need per-request
# freshness. Per-process (gunicorn runs --workers 2), so each worker
# caches independently; that just means up to 2x the query rate, not
# stale-across-workers inconsistency, since every worker eventually
# converges on the same underlying data.
PERFORMANCE_CACHE_TTL_SECONDS = 60
# Quotes move fast; much shorter than /performance's TTL. Per-symbol, not
# a single blob (see the /quotes route) -- multiple browser tabs polling
# different symbol sets shouldn't multiply real Alpaca calls, but a
# quote genuinely goes stale in seconds, not minutes.
QUOTE_CACHE_TTL_SECONDS = 10

# How many magic-link requests one email address / one IP can make
# before being rate-limited, and the same for the two public write
# endpoints below. Deliberately generous for /events -- real usage
# (a page view plus a handful of CTA clicks) is nowhere near these
# numbers; they exist to cap abuse, not to constrain a real visitor.
_MAGIC_LINK_PER_EMAIL = (3, 900)     # 3 per 15 minutes -- matches the token TTL
_MAGIC_LINK_PER_IP = (10, 3600)      # 10 per hour
_EVENTS_PER_ANON = (120, 300)        # 120 per 5 minutes
_EVENTS_PER_IP = (600, 300)          # 600 per 5 minutes
_CLIENT_ERRORS_PER_IP = (60, 300)    # 60 per 5 minutes


def _to_jsonable(obj):
    """Recursively converts dataclass instances found anywhere inside
    dicts/lists/tuples into plain dicts, so query-layer return values
    (TierPerformance, TrackRecord, Trade, BucketStats, ...) can go
    straight into jsonify() without each endpoint hand-rolling its own
    field list — and without ever getting a field name wrong or out of
    sync with the dataclass it's describing."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _to_jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


# One or two fields each kind's card headline actually needs from its
# context -- never the full blob (see /signals/<id> for that). Extend this
# dict, not the endpoints' response shapes, if a future kind needs more.
# level_break's raw context field is "level" (see detectors.py); renamed
# to level_value here so it isn't ambiguous next to level_name.
_HEADLINE_CONTEXT_FIELDS = {
    # (out_key, src_key) pairs per kind, src_key naming the field in the
    # detector's own context dict (tradebot/detectors.py). Every kind is
    # listed so card headlines can carry the card's own numbers instead
    # of one templated sentence per kind (design review M5) -- the
    # frontend's signalHeadlines.js builders degrade to the raw engine
    # sentence whenever these fields are missing (pre-migration rows).
    "level_break": (("level_name", "level_name"), ("level_value", "level")),
    "range_expansion": (("bar_range", "bar_range"), ("atr", "atr14")),
    "rvol_spike": (("cum_volume", "cum_volume"), ("baseline", "baseline")),
    "vwap_break": (("vwap", "vwap"),),
    "round_number_break": (("level", "level"),),
    "gap": (("gap_size", "gap_size"), ("prior_close", "prior_close")),
    "relative_strength_break": (("market_proxy", "market_proxy"), ("divergence", "divergence"), ("atr", "atr14")),
}


def _context_summary(kinds_list: list[str], primary_kind: str | None, context_json: str | None) -> dict | None:
    """Just the field(s) a card headline needs from the PRIMARY detector's
    context, keyed by position in `kinds_list` (contexts are written in
    the same order as kinds -- see journal.write_cluster). None for any
    kind without an entry in _HEADLINE_CONTEXT_FIELDS, or when there's
    nothing recorded to read from."""
    fields = _HEADLINE_CONTEXT_FIELDS.get(primary_kind)
    if not fields or not context_json or primary_kind not in kinds_list:
        return None
    contexts = json.loads(context_json)
    idx = kinds_list.index(primary_kind)
    if idx >= len(contexts):
        return None
    ctx = contexts[idx]
    summary = {out_key: ctx[src_key] for out_key, src_key in fields if src_key in ctx}
    return summary or None


def _linked_telegram_user_id(conn, account: accounts.Account) -> int | None:
    row = conn.execute(
        "SELECT provider_user_id FROM linked_identities WHERE account_id = ? AND provider = ?",
        (account.id, accounts.TELEGRAM_PROVIDER),
    ).fetchone()
    return int(row[0]) if row else None


def create_app(users_db_path=None, journal_db_path=None) -> Flask:
    app = Flask(__name__)
    # Caddy (see ../../Caddyfile) sits in front of this app in production
    # and, like any reverse proxy, is the direct TCP peer for every
    # request -- without this, request.remote_addr is always Caddy's own
    # address, never the real client's, which would make IP-based rate
    # limiting below apply to one shared bucket for everyone. x_for=1
    # trusts exactly one hop of X-Forwarded-For (the one Caddy itself
    # appends), not any earlier entry a client could forge.
    app.wsgi_app = _RequestBodyLimitMiddleware(
        ProxyFix(app.wsgi_app, x_for=1),
        MAX_REQUEST_BODY_BYTES,
    )
    # No insecure fallback here on purpose -- this key signs every
    # session cookie, and a hardcoded default sitting in a public repo
    # (this one is public on GitHub) is a full account-takeover waiting
    # on a single missing env var. Refuse to start rather than silently
    # run with a signing key anyone can read the source of.
    session_secret_key = os.environ.get("SESSION_SECRET_KEY")
    if not session_secret_key:
        raise RuntimeError(
            "SESSION_SECRET_KEY is not set. Generate one with "
            '`python3 -c "import secrets; print(secrets.token_hex(32))"` '
            "and set it in the environment before starting this app -- "
            "there is no safe default for a session-signing key."
        )
    app.config["SECRET_KEY"] = session_secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
    # Unset locally (host-only cookie); ".perchmarkets.com" in production
    # so the cookie set by api.perchmarkets.com is also sent on requests
    # from app.perchmarkets.com.
    app.config["SESSION_COOKIE_DOMAIN"] = os.environ.get("SESSION_COOKIE_DOMAIN") or None
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_LIFETIME_DAYS)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY_BYTES

    app.frontend_url = os.environ.get("FRONTEND_URL", DEFAULT_FRONTEND_URL)
    app.users_conn = users_db.connect(users_db_path) if users_db_path is not None else users_db.connect()
    app.journal_conn = (
        journal_connect(journal_db_path, check_same_thread=False)
        if journal_db_path is not None
        else journal_connect(check_same_thread=False)
    )
    app.email_sender = build_email_sender()
    app._performance_cache: dict = {"data": None, "computed_at": None}
    app._public_record_cache: dict = {"data": None, "computed_at": None}
    app._quote_cache: dict = {}

    # Trusts app.frontend_url only, plus whatever a developer's own local
    # frontend dev server is running on -- opt-in via DEV_CORS_ORIGIN
    # (e.g. "http://localhost:5173"), never assumed. A hardcoded
    # localhost origin used to be trusted unconditionally here, in
    # production too; SESSION_COOKIE_SAMESITE=Lax already stopped that
    # from being directly exploitable, but a public API has no business
    # trusting a dev origin at all unless someone explicitly asked it to.
    allowed_origins = {app.frontend_url}
    dev_cors_origin = os.environ.get("DEV_CORS_ORIGIN")
    if dev_cors_origin:
        allowed_origins.add(dev_cors_origin)

    @app.after_request
    def add_cors_headers(response):
        # /public/* is unauthenticated, read-only, and never reads a
        # cookie for authorization — deliberately open to any origin
        # rather than folded into the credentialed allowlist below,
        # which exists to answer a different question (who gets a
        # session cookie honored). Keeping the two separate means
        # allowed_origins keeps meaning exactly one thing, and this
        # wildcard can never leak onto a route that does read the
        # session. Scoped to this one prefix, not app-wide.
        if request.path.startswith("/public/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            return response
        origin = request.headers.get("Origin")
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            # PATCH/DELETE for the Trade Journal's edit/delete endpoints;
            # everything else here is still GET/POST only.
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        return response

    @app.errorhandler(413)
    def request_body_too_large(_error):
        return jsonify({"error": "request body too large"}), 413

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            account_id = session.get("account_id")
            account = accounts.get_account(app.users_conn, account_id) if account_id else None
            if account is None:
                session.clear()
                return jsonify({"error": "not authenticated"}), 401
            g.account = account
            return view(*args, **kwargs)

        return wrapped

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.route("/events", methods=["POST"])
    def track_event():
        # Sent via navigator.sendBeacon as a text/plain body (see
        # web/src/analytics.js and web-app/src/analytics.js) on purpose:
        # a "simple" cross-origin POST skips the CORS preflight, so this
        # works from both perchmarkets.com and app.perchmarkets.com
        # without extending `allowed_origins` above — and since the
        # client never reads the response (fire-and-forget), no
        # Access-Control-Allow-Origin is needed here at all. Bodies that
        # pass the shared size ceiling always receive 204 regardless of
        # content: a public endpoint should not distinguish malformed
        # input from a valid event. Oversized bodies are the deliberate
        # exception and receive 413 before this handler can buffer them.
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            return "", 204
        if not isinstance(payload, dict):
            return "", 204
        anon_id = str(payload.get("anon_id") or "")
        if not anon_id or len(anon_id) > funnel_events.MAX_ANON_ID_LEN:
            return "", 204
        # Rate-limited the same silent way as everything else on this
        # route: over the limit looks identical to "recorded normally"
        # from the outside, just without the write.
        if not rate_limit.allow_all(
            app.users_conn,
            [
                (f"events:ip:{request.remote_addr}", *_EVENTS_PER_IP),
                (f"events:anon:{anon_id}", *_EVENTS_PER_ANON),
            ],
        ):
            return "", 204
        props = payload.get("props")
        funnel_events.record_event(
            app.users_conn,
            event=str(payload.get("event") or ""),
            anon_id=anon_id,
            account_id=session.get("account_id"),
            props=props if isinstance(props, dict) else None,
        )
        return "", 204

    @app.route("/client-errors", methods=["POST"])
    def report_client_error():
        # Same sendBeacon/text-plain/204-after-size-admission shape as
        # /events, for the same reasons (see that route's comment) --
        # and rate-limited by IP alone (no anon_id involved here) so a
        # loop that throws on every render can't flood this table.
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            return "", 204
        if not isinstance(payload, dict):
            return "", 204
        if not rate_limit.allow(app.users_conn, f"client_errors:ip:{request.remote_addr}", *_CLIENT_ERRORS_PER_IP):
            return "", 204
        client_errors.record_error(
            app.users_conn,
            message=str(payload.get("message") or ""),
            stack=str(payload.get("stack") or "") or None,
            url=str(payload.get("url") or "") or None,
            user_agent=request.headers.get("User-Agent"),
            account_id=session.get("account_id"),
        )
        return "", 204

    @app.route("/auth/magic-link/request", methods=["POST"])
    def request_magic_link():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        if not email or len(email) > MAX_EMAIL_LENGTH or "@" not in email:
            return jsonify({"error": "a valid email is required"}), 400
        now = datetime.now(timezone.utc)
        # Two separate limits: per-email caps how many times any one
        # address can be emailed (the real risk -- this endpoint sends a
        # real message to whatever address is given, with no proof yet
        # that the requester controls it), per-IP catches one requester
        # cycling through many addresses. Either one tripping answers
        # the same 429 either way -- nothing here reveals which limit
        # was hit, since that alone would leak whether this email has
        # been requested before.
        if not rate_limit.allow_all(
            app.users_conn,
            [
                (f"magic_link:ip:{request.remote_addr}", *_MAGIC_LINK_PER_IP),
                (f"magic_link:email:{email}", *_MAGIC_LINK_PER_EMAIL),
            ],
            now=now,
        ):
            return jsonify({"error": "too many requests, try again shortly"}), 429
        token = accounts.create_magic_link_token(app.users_conn, email, now)
        # Points at the frontend's own confirmation screen
        # (VerifyMagicLink.jsx), not directly at /auth/magic-link/verify
        # below -- see that route's comment for why a bare GET can never
        # be the thing that actually signs someone in.
        link_url = f"{app.frontend_url.rstrip('/')}/?token={token}"
        try:
            app.email_sender.send_magic_link(email, link_url)
        except Exception:
            # Found live: an unhandled failure here (Resend down, a
            # quota/rate limit on our sending account, a network hiccup)
            # was reaching the client as a raw Flask 500 page -- an
            # implementation detail of *our* email provider becoming a
            # broken sign-in page for a real person. The token already
            # exists in magic_link_tokens either way; if the email
            # genuinely never went out, it just expires unused per its
            # normal TTL, same as any other unused token.
            logger.exception("send_magic_link failed for a request (email withheld from logs)")
            return jsonify({"error": "couldn't send the email, try again shortly"}), 502
        # Same response regardless of whether this email already has an
        # account — one is created on verify if not. Nothing here reveals
        # whether an address is a known user.
        return jsonify({"ok": True}), 202

    @app.route("/auth/magic-link/verify", methods=["POST"])
    def verify_magic_link():
        # POST with a JSON body, not the GET-with-token-in-the-query this
        # used to be. A bare GET is triggerable by anything that merely
        # LOADS a URL -- an <img src="…?token=…">, an email-scanning
        # security appliance prefetching links, a <link rel=prefetch> --
        # with no click and no JS, and the Set-Cookie in the response
        # would land regardless of how the request was triggered. A JSON
        # POST can't be forged the same way: request()'s
        # Content-Type: application/json (see web-app/src/api.js) forces
        # a CORS preflight, and add_cors_headers above only allows it
        # from app.frontend_url -- an attacker page's fetch() never gets
        # past that, and a plain auto-submitting HTML <form> can't set a
        # JSON content-type at all. See web-app/src/components/
        # VerifyMagicLink.jsx for the confirmation screen this now
        # requires an actual button press on.
        data = request.get_json(silent=True) or {}
        token = data.get("token") or ""
        now = datetime.now(timezone.utc)
        email = accounts.verify_magic_link_token(app.users_conn, token, now)
        if email is None:
            return jsonify({"error": "invalid or expired link"}), 400
        account = accounts.get_or_create_account_for_email(app.users_conn, email)
        session.permanent = True  # opts into PERMANENT_SESSION_LIFETIME instead of an unbounded session
        session["account_id"] = account.id
        return jsonify({"ok": True})

    @app.route("/auth/logout", methods=["POST"])
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/me")
    @login_required
    def me():
        account = g.account
        identity_rows = app.users_conn.execute(
            "SELECT provider, provider_user_id FROM linked_identities WHERE account_id = ?", (account.id,)
        ).fetchall()
        return jsonify(
            {
                "id": account.id,
                "email": account.email,
                "plan": account.plan,
                "founding_member": account.founding_member,
                "linked_identities": [{"provider": p, "provider_user_id": pid} for p, pid in identity_rows],
            }
        )

    @app.route("/watchlist")
    @login_required
    def watchlist():
        telegram_user_id = _linked_telegram_user_id(app.users_conn, g.account)
        custom = users_db.get_watchlist(app.users_conn, telegram_user_id) if telegram_user_id else None
        active = custom or config.WATCHLIST
        return jsonify({"symbols": active, "is_custom": custom is not None})

    @app.route("/quotes")
    @login_required
    def quotes():
        # Same watchlist resolution as /watchlist -- this can't become a
        # general quote-lookup proxy, only the account's own symbols are
        # ever fetchable. Silently drops anything outside that set rather
        # than 400ing, same "no oracle for what's allowed" discipline as
        # /events elsewhere in this file.
        requested = {s.strip().upper() for s in request.args.get("symbols", "").split(",") if s.strip()}
        telegram_user_id = _linked_telegram_user_id(app.users_conn, g.account)
        custom = users_db.get_watchlist(app.users_conn, telegram_user_id) if telegram_user_id else None
        allowed = set(custom or config.WATCHLIST)
        symbols = sorted(requested & allowed)

        now = datetime.now(timezone.utc)
        cache = app._quote_cache  # {symbol: (Quote, fetched_at)}
        stale = [
            s for s in symbols
            if s not in cache or (now - cache[s][1]).total_seconds() > QUOTE_CACHE_TTL_SECONDS
        ]
        if stale:
            try:
                fetched = fetch_quotes(stale)
            except Exception:
                # Same "vendor hiccup shouldn't become a broken page"
                # discipline as /auth/magic-link/request above. Unlike
                # that endpoint, there's a good degraded response here:
                # serve whatever's already cached (even if stale past its
                # TTL) instead of 500ing symbols that didn't need a fetch
                # at all.
                logger.exception("fetch_quotes failed; serving cached quotes where available")
            else:
                for symbol, q in fetched.items():
                    cache[symbol] = (q, now)

        return jsonify({"quotes": {s: _to_jsonable(cache[s][0]) for s in symbols if s in cache}})

    def _recent_signals(session_filter: str | None, limit: int) -> list[dict]:
        query = (
            "SELECT id, ts_utc, session, symbol, kinds, headlines, score, tier, trend, alerted, "
            "primary_kind, context_json, close, origin "
            "FROM detections WHERE tier IN ('high', 'medium')"
        )
        params: list = []
        if session_filter is not None:
            query += " AND session = ?"
            params.append(session_filter)
        query += " ORDER BY ts_utc DESC LIMIT ?"
        params.append(limit)
        rows = app.journal_conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            kinds_list = row[4].split(",")
            primary_kind = row[10]
            result.append({
                "id": row[0], "ts_utc": row[1], "session": row[2], "symbol": row[3],
                "kinds": kinds_list, "headlines": row[5], "score": row[6],
                "tier": row[7], "trend": row[8], "alerted": bool(row[9]),
                "primary_kind": primary_kind,
                "context_summary": _context_summary(kinds_list, primary_kind, row[11]),
                "close": row[12],
                # NULL on every row written before this shipped (see
                # journal.write_cluster's docstring) -- reported as
                # "watchlist" rather than null/None, since that's the true
                # origin for every pre-broad_scan row and the frontend
                # badge only needs to know "screening" vs. everything else.
                "origin": row[13] or "watchlist",
            })
        return result

    @app.route("/signals/feed")
    @login_required
    def signals_feed():
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
        return jsonify({"signals": _recent_signals(None, limit)})

    @app.route("/signals/today")
    @login_required
    def signals_today():
        # "3 things worth knowing" — see the plan's Today view spec.
        # Same session_date_fn definition runner.py uses (now in ET,
        # date()) — not the server's own local/UTC date, which would be
        # wrong on a VPS and around the ET midnight rollover.
        session_date = datetime.now(ET).date().isoformat()
        return jsonify({"session": session_date, "signals": _recent_signals(session_date, 3)})

    @app.route("/signals/<detection_id>")
    @login_required
    def signal_detail(detection_id):
        # Same tier restriction as _recent_signals -- sub-threshold ('log'
        # tier) detections were never meant to be user-facing, so a
        # detection id for one 404s here exactly like an unknown id would.
        row = app.journal_conn.execute(
            "SELECT id, ts_utc, session, symbol, kinds, headlines, score, tier, trend, "
            "alerted, close, atr14, context_json, primary_kind, no_trade, news_driven, "
            "event_kind, event_severity, origin "
            "FROM detections WHERE id = ? AND tier IN ('high', 'medium')",
            (detection_id,),
        ).fetchone()
        if row is None:
            return jsonify({"error": "signal not found"}), 404
        (
            id_, ts_utc, session, symbol, kinds, headlines, score, tier, trend,
            alerted, close, atr14, context_json, primary_kind, no_trade, news_driven,
            event_kind, event_severity, origin,
        ) = row
        # One context dict per detector kind that fired in this cluster,
        # same order as kinds.split(",") -- see journal.write_cluster,
        # which writes json.dumps([d.context for d in detections]).
        contexts = json.loads(context_json) if context_json else []
        # Real base-rate history for this exact kind+direction, excluding
        # this detection itself and any news-driven rows -- reuses
        # journal.historical_performance() as-is (no new query logic),
        # the same function /performance already relies on for tier-level
        # stats. None when there's no primary_kind/trend to match on, or
        # when the sample is too small -- never fabricated.
        history = (
            historical_performance(app.journal_conn, primary_kind, trend, id_)
            if primary_kind and trend
            else None
        )
        # Real forward prices for THIS detection, once backfilled -- see
        # journal.backfill_marks(), which only runs once, at the end of
        # the session that produced this detection. Empty until then --
        # never a live/current price, and never fabricated for an
        # interval that hasn't been reached yet. at_close marks the
        # CLOSE_MARK_OFFSET_MIN sentinel row so the frontend never needs
        # to know that -1 means "session close." Render as "After
        # detection" + offset_min, or "At session close" when at_close is
        # true -- never as a live quote.
        mark_rows = app.journal_conn.execute(
            "SELECT offset_min, price FROM marks WHERE detection_id = ?", (id_,)
        ).fetchall()
        marks = [
            {
                "offset_min": None if offset == CLOSE_MARK_OFFSET_MIN else offset,
                "at_close": offset == CLOSE_MARK_OFFSET_MIN,
                "price": price,
            }
            for offset, price in sorted(mark_rows, key=lambda r: (r[0] == CLOSE_MARK_OFFSET_MIN, r[0]))
        ]
        return jsonify(
            {
                "id": id_,
                "ts_utc": ts_utc,
                "session": session,
                "symbol": symbol,
                "kinds": kinds.split(","),
                "contexts": contexts,
                "headlines": headlines,
                "score": score,
                "tier": tier,
                "trend": trend,
                "alerted": bool(alerted),
                "close": close,
                "atr14": atr14,
                "no_trade": bool(no_trade) if no_trade is not None else None,
                "news_driven": bool(news_driven) if news_driven is not None else None,
                "event_kind": event_kind,
                "event_severity": event_severity,
                "history": _to_jsonable(history),
                "marks": marks,
                "origin": origin or "watchlist",
            }
        )

    @app.route("/performance")
    @login_required
    def performance():
        cache = app._performance_cache
        now = datetime.now(timezone.utc)
        stale = cache["computed_at"] is None or (now - cache["computed_at"]).total_seconds() > PERFORMANCE_CACHE_TTL_SECONDS
        if stale:
            by_tier = tier_performance(app.journal_conn)
            by_kind = kind_performance(app.journal_conn)
            record = track_record(app.journal_conn)
            cache["data"] = {
                "by_tier": _to_jsonable(by_tier),
                "by_kind": _to_jsonable(by_kind),
                "track_record": _to_jsonable(record),
            }
            cache["computed_at"] = now
        return jsonify(cache["data"])

    @app.route("/public/track-record")
    def public_track_record():
        # No @login_required — this is the whole point (see
        # docs/phase4-proof-engine-proposal.md, Part A). No cookie is
        # ever read here, matching the CORS wildcard above.
        #
        # alerted_only=True everywhere below: the public record is the
        # ALERTED population, binding (owner decision, 2026-08-18) — see
        # track_record()'s own docstring for why the default (every
        # HIGH-tier detection, alerted or not) would be wrong here.
        cache = app._public_record_cache
        now = datetime.now(timezone.utc)
        stale = cache["computed_at"] is None or (now - cache["computed_at"]).total_seconds() > PERFORMANCE_CACHE_TTL_SECONDS
        if stale:
            limit = min(max(int(request.args.get("limit", 1000)), 1), 5000)
            record = track_record(app.journal_conn, alerted_only=True)
            alerts = public_alert_history(app.journal_conn, app.users_conn, limit=limit)
            cache["data"] = {
                "track_record": _to_jsonable(record),
                "alerts": _to_jsonable(alerts),
            }
            cache["computed_at"] = now
        return jsonify(cache["data"])

    @app.route("/activity")
    @login_required
    def activity():
        telegram_user_id = _linked_telegram_user_id(app.users_conn, g.account)
        if telegram_user_id is None:
            return jsonify({"trades": [], "stats": None})
        trades = users_db.list_trades(app.users_conn, telegram_user_id)
        stats = users_db.personal_stats(app.users_conn, telegram_user_id, trades=trades)
        return jsonify({"trades": _to_jsonable(trades), "stats": _to_jsonable(stats)})

    # ---- Trade Journal ---------------------------------------------------
    # Every route below derives its scope exclusively from g.account.id
    # (set by login_required from the signed session cookie) — no request
    # parameter can name a different user, and the db-layer functions
    # repeat the account-scope check inside their own SQL (see
    # users_db._ACCOUNT_SCOPE_SQL). Journal access routes through
    # can_access() like every other feature: free for everyone today, and
    # the one sanctioned seam for gating later.

    MAX_JOURNAL_TEXT = 2000  # notes / skip reasons; a journal, not a blog
    MAX_JOURNAL_SYMBOL_LEN = 12
    # A year's aggressive day-trading is ~2-5k entries; 100k of anything
    # is a runaway client, not a person.
    MAX_PNL_CENTS = 10**11  # +/- one billion dollars, in cents

    def _journal_gate():
        """None when allowed; a (response, status) pair when gated. Uses
        the linked Telegram user when one exists, None otherwise —
        can_access accepts both."""
        tg_id = _linked_telegram_user_id(app.users_conn, g.account)
        user = users_db.get_user(app.users_conn, tg_id) if tg_id else None
        if not can_access(user, "journal"):
            return jsonify({"error": "journal not available on this plan"}), 403
        return None

    def _detection_snapshot(detection_id: str) -> dict | None:
        """The immutable copy of a detection a journal entry keeps for
        itself, captured at link time from journal.db. None when the row
        isn't there — which is a real, designed-for state, not an error:
        users.db and journal.db are separate files with no cross-database
        transaction (see docs/BACKLOG.md's atomicity finding), so a
        freshly-alerted detection_id can reference a row that hasn't
        committed (or, post-crash, never will). A None snapshot degrades
        to "signal detail unavailable" in the UI rather than blocking
        the user from journaling the trade they really took. Log-tier
        rows return None too — never user-facing, same rule as
        /signals/<id>."""
        row = app.journal_conn.execute(
            "SELECT id, ts_utc, session, symbol, kinds, headlines, score, tier, trend, primary_kind "
            "FROM detections WHERE id = ? AND tier IN ('high', 'medium')",
            (detection_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "ts_utc": row[1], "session": row[2], "symbol": row[3],
            "kinds": row[4].split(","), "headlines": row[5], "score": row[6],
            "tier": row[7], "trend": row[8], "primary_kind": row[9],
        }

    # The complete request-body vocabulary per endpoint. Anything else in
    # the body is a 400, not silently dropped — a client sending a field
    # this API doesn't know (a typo like "pnl_cent", or a probe) must
    # find out immediately, never have its input vanish without a trace.
    _JOURNAL_CREATE_KEYS = frozenset(
        {"symbol", "direction", "source", "taken_at", "pnl_cents", "note", "skip_reason", "detection_id", "is_skip"}
    )
    _JOURNAL_PATCH_KEYS = frozenset(
        {"symbol", "direction", "source", "taken_at", "pnl_cents", "note", "skip_reason", "detection_id"}
    )

    def _parse_journal_payload(data: dict, *, partial: bool) -> tuple[dict, str | None]:
        """Server-side validation for create (partial=False) and edit
        (partial=True). Returns (fields, None) or ({}, error_message).
        Every field is validated here regardless of what the client sent
        — types, vocabulary, bounds — because nothing about the request
        body is trusted."""
        allowed = _JOURNAL_PATCH_KEYS if partial else _JOURNAL_CREATE_KEYS
        unknown = set(data) - allowed
        if unknown:
            return {}, f"unknown field(s): {', '.join(sorted(unknown))}"
        fields: dict = {}
        if "symbol" in data or not partial:
            symbol = str(data.get("symbol") or "").strip().upper()
            if not symbol or len(symbol) > MAX_JOURNAL_SYMBOL_LEN or not symbol.replace(".", "").replace("-", "").isalnum():
                return {}, "symbol is required (letters/digits, max 12 chars)"
            fields["symbol"] = symbol
        if "direction" in data:
            direction = data.get("direction")
            if direction is not None and direction not in ("long", "short"):
                return {}, "direction must be 'long', 'short', or null"
            fields["direction"] = direction
        if "source" in data:
            source = data.get("source")
            if source is not None and source not in users_db.JOURNAL_SOURCES:
                return {}, f"source must be one of {list(users_db.JOURNAL_SOURCES)} or null"
            fields["source"] = source
        if "taken_at" in data or not partial:
            raw = data.get("taken_at")
            if raw is None:
                taken_at = datetime.now(timezone.utc)
            else:
                try:
                    taken_at = datetime.fromisoformat(str(raw))
                except ValueError:
                    return {}, "taken_at must be an ISO-8601 datetime"
                if taken_at.tzinfo is None:
                    return {}, "taken_at must include a timezone offset"
                if taken_at > datetime.now(timezone.utc) + timedelta(minutes=5):
                    return {}, "taken_at cannot be in the future"
            fields["taken_at"] = taken_at
        if "pnl_cents" in data:
            pnl = data.get("pnl_cents")
            if pnl is not None:
                # bool is an int subclass; True would otherwise pass as 1.
                if isinstance(pnl, bool) or not isinstance(pnl, int):
                    return {}, "pnl_cents must be an integer number of cents (signed), not a float or string"
                if abs(pnl) > MAX_PNL_CENTS:
                    return {}, "pnl_cents is out of range"
            fields["pnl_cents"] = pnl
        for text_field in ("note", "skip_reason"):
            if text_field in data:
                value = data.get(text_field)
                if value is not None:
                    value = str(value).strip() or None
                if value is not None and len(value) > MAX_JOURNAL_TEXT:
                    return {}, f"{text_field} is too long (max {MAX_JOURNAL_TEXT} chars)"
                fields[text_field] = value
        return fields, None

    @app.route("/journal/summary")
    @login_required
    def journal_summary():
        gated = _journal_gate()
        if gated:
            return gated
        summary = users_db.journal_summary(app.users_conn, g.account.id, now=datetime.now(timezone.utc))
        stats = users_db.journal_stats(app.users_conn, g.account.id)
        return jsonify({"summary": summary, "stats": stats})

    @app.route("/journal/calendar")
    @login_required
    def journal_calendar():
        gated = _journal_gate()
        if gated:
            return gated
        month_param = request.args.get("month", "")
        try:
            year_s, month_s = month_param.split("-")
            year, month = int(year_s), int(month_s)
            if not (2000 <= year <= 2100 and 1 <= month <= 12):
                raise ValueError
        except ValueError:
            return jsonify({"error": "month must be YYYY-MM"}), 400
        days = users_db.journal_calendar(app.users_conn, g.account.id, year, month)
        return jsonify({"month": f"{year:04d}-{month:02d}", "days": days})

    @app.route("/journal/trades")
    @login_required
    def journal_trades():
        gated = _journal_gate()
        if gated:
            return gated
        date_param = request.args.get("date")
        on_date = None
        if date_param:
            try:
                on_date = datetime.strptime(date_param, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        trades = users_db.list_journal_trades(app.users_conn, g.account.id, on_date=on_date)
        return jsonify({"trades": _to_jsonable(trades)})

    @app.route("/journal/trades", methods=["POST"])
    @login_required
    def journal_create_trade():
        gated = _journal_gate()
        if gated:
            return gated
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "a JSON body is required"}), 400
        is_skip = bool(data.get("is_skip"))
        fields, err = _parse_journal_payload(data, partial=False)
        if err:
            return jsonify({"error": err}), 400
        detection_id = data.get("detection_id")
        if detection_id is not None:
            detection_id = str(detection_id)
        # The snapshot is derived server-side from journal.db — never
        # accepted from the client, which could otherwise fabricate
        # "this trade came from a HIGH signal" evidence. None (missing/
        # log-tier row) still stores the id: see _detection_snapshot.
        snapshot = _detection_snapshot(detection_id) if detection_id else None
        trade = users_db.create_journal_trade(
            app.users_conn,
            g.account.id,
            symbol=fields["symbol"],
            taken_at=fields["taken_at"],
            direction=fields.get("direction"),
            source=fields.get("source"),
            pnl_cents=fields.get("pnl_cents"),
            note=fields.get("note"),
            detection_id=detection_id,
            detection_snapshot=snapshot,
            is_skip=is_skip,
            skip_reason=fields.get("skip_reason"),
        )
        return jsonify({"trade": _to_jsonable(trade)}), 201

    @app.route("/journal/trades/<trade_id>", methods=["PATCH"])
    @login_required
    def journal_update_trade(trade_id):
        gated = _journal_gate()
        if gated:
            return gated
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "a JSON body is required"}), 400
        fields, err = _parse_journal_payload(data, partial=True)
        if err:
            return jsonify({"error": err}), 400
        # detection_id passes through the payload parser untouched (it's
        # in the PATCH vocabulary but not a validated field) — pull it
        # out here and re-derive the snapshot server-side, exactly as
        # POST does. null unlinks: both columns clear together so a
        # stale snapshot can never outlive its link.
        if "detection_id" in data:
            detection_id = data.get("detection_id")
            detection_id = str(detection_id) if detection_id is not None else None
            snapshot = _detection_snapshot(detection_id) if detection_id else None
            fields["detection_id"] = detection_id
            fields["detection_snapshot_json"] = json.dumps(snapshot) if snapshot else None
        if not fields:
            return jsonify({"error": "nothing to update"}), 400
        trade = users_db.update_journal_trade(app.users_conn, g.account.id, trade_id, **fields)
        if trade is None:
            # Same 404 whether the id is unknown or belongs to someone
            # else — never an oracle for other accounts' trade ids.
            return jsonify({"error": "trade not found"}), 404
        return jsonify({"trade": _to_jsonable(trade)})

    @app.route("/journal/trades/<trade_id>", methods=["DELETE"])
    @login_required
    def journal_delete_trade(trade_id):
        gated = _journal_gate()
        if gated:
            return gated
        deleted = users_db.delete_journal_trade(app.users_conn, g.account.id, trade_id)
        if not deleted:
            return jsonify({"error": "trade not found"}), 404
        return jsonify({"ok": True})

    @app.route("/journal/linkable-signals")
    @login_required
    def journal_linkable_signals():
        """Recent alerts THIS account was actually sent, for "link my
        trade to the alert I got." Grounded in the outbox delivery log
        (users.db) — one row per (alert_id, chat_id), so this is real
        per-user delivery history, not the global feed. chat_id equals
        telegram_user_id for DMs (see telegram_bot.db.get_or_create_user)
        and alerts are only ever DM'd, so the linked Telegram id is the
        chat to match. Detection details come from journal.db in a second
        query — application-code stitching, never a cross-DB JOIN; an
        alert whose detection row is missing (the known atomicity gap)
        is simply omitted here. No Telegram link means no delivery
        history: an empty list, not an error."""
        gated = _journal_gate()
        if gated:
            return gated
        tg_id = _linked_telegram_user_id(app.users_conn, g.account)
        if tg_id is None:
            return jsonify({"signals": [], "delivery_history": False})
        symbol_filter = (request.args.get("symbol") or "").strip().upper() or None
        rows = app.users_conn.execute(
            "SELECT alert_id, delivered_at FROM outbox "
            "WHERE chat_id = ? AND status = 'delivered' ORDER BY delivered_at DESC LIMIT 100",
            (tg_id,),
        ).fetchall()
        # A sizing follow-up shares its alert's id with a ':sizing'
        # suffix (see outbox.enqueue_broadcast callers) — fold those
        # onto the base detection id, keeping the newest delivery time.
        delivered: dict[str, str] = {}
        for alert_id, delivered_at in rows:
            base_id = alert_id.split(":", 1)[0]
            if base_id not in delivered:
                delivered[base_id] = delivered_at
        signals = []
        for detection_id, delivered_at in delivered.items():
            snapshot = _detection_snapshot(detection_id)
            if snapshot is None:
                continue
            if symbol_filter and snapshot["symbol"] != symbol_filter:
                continue
            signals.append({**snapshot, "detection_id": detection_id, "delivered_at": delivered_at})
            if len(signals) >= 20:
                break
        return jsonify({"signals": signals, "delivery_history": True})

    @app.route("/journal/export.csv")
    @login_required
    def journal_export():
        gated = _journal_gate()
        if gated:
            return gated
        trades = users_db.list_journal_trades(app.users_conn, g.account.id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "date_et", "taken_at_utc", "symbol", "direction", "source", "pnl_usd",
            "is_skip", "skip_reason", "note", "detection_id", "status",
        ])
        for t in trades:
            writer.writerow([
                t.id,
                users_db.et_date(t.taken_at).isoformat(),
                t.taken_at,
                t.symbol,
                t.direction or "",
                t.source or "",
                f"{t.pnl_cents / 100:.2f}" if t.pnl_cents is not None else "",
                int(t.is_skip),
                t.skip_reason or "",
                t.note or "",
                t.detection_id or "",
                t.status,
            ])
        return (
            buf.getvalue(),
            200,
            {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": "attachment; filename=perch-journal.csv",
            },
        )

    return app


if __name__ == "__main__":
    create_app().run(port=int(os.environ.get("PORT", 8000)), debug=False)
