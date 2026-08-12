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

logger = logging.getLogger(__name__)

from tradebot import accounts, client_errors, config, funnel_events, rate_limit
from tradebot.email_sender import build_email_sender
from tradebot.journal import connect as journal_connect
from tradebot.journal import CLOSE_MARK_OFFSET_MIN, historical_performance, kind_performance, tier_performance
from tradebot.marketdata import fetch_quotes
from tradebot.runner import ET
from tradebot.telegram_bot import db as users_db
from tradebot.telegram_bot.performance import track_record

DEFAULT_FRONTEND_URL = "https://app.perchmarkets.com"
# The itsdangerous-signed session cookie has no server-side revocation —
# without an expiry, a copied/leaked cookie stays valid forever. 30 days
# bounds that blast radius while still being "stay signed in" for a
# passwordless product where re-auth means waiting on another email.
SESSION_LIFETIME_DAYS = 30

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
    "level_break": (("level_name", "level_name"), ("level_value", "level")),
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
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
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

    app.frontend_url = os.environ.get("FRONTEND_URL", DEFAULT_FRONTEND_URL)
    app.users_conn = users_db.connect(users_db_path) if users_db_path is not None else users_db.connect()
    app.journal_conn = (
        journal_connect(journal_db_path, check_same_thread=False)
        if journal_db_path is not None
        else journal_connect(check_same_thread=False)
    )
    app.email_sender = build_email_sender()
    app._performance_cache: dict = {"data": None, "computed_at": None}
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
        origin = request.headers.get("Origin")
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

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
        # Access-Control-Allow-Origin is needed here at all. Always
        # answer 204 regardless of what was sent: a public,
        # unauthenticated endpoint should never behave observably
        # differently for a malformed request than a valid one.
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            return "", 204
        if not isinstance(payload, dict):
            return "", 204
        anon_id = str(payload.get("anon_id") or "")
        # Rate-limited the same silent way as everything else on this
        # route: over the limit looks identical to "recorded normally"
        # from the outside, just without the write.
        if not rate_limit.allow(app.users_conn, f"events:anon:{anon_id}", *_EVENTS_PER_ANON):
            return "", 204
        if not rate_limit.allow(app.users_conn, f"events:ip:{request.remote_addr}", *_EVENTS_PER_IP):
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
        # Same sendBeacon/text-plain/always-204 shape as /events, for the
        # same reasons (see that route's comment) -- and rate-limited by
        # IP alone (no anon_id involved here) so a loop that throws on
        # every render can't flood this table.
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
        if not email or "@" not in email:
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
        if not rate_limit.allow(app.users_conn, f"magic_link:email:{email}", *_MAGIC_LINK_PER_EMAIL, now=now):
            return jsonify({"error": "too many requests, try again shortly"}), 429
        if not rate_limit.allow(app.users_conn, f"magic_link:ip:{request.remote_addr}", *_MAGIC_LINK_PER_IP, now=now):
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
            fetched = fetch_quotes(stale)
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

    @app.route("/activity")
    @login_required
    def activity():
        telegram_user_id = _linked_telegram_user_id(app.users_conn, g.account)
        if telegram_user_id is None:
            return jsonify({"trades": [], "stats": None})
        trades = users_db.list_trades(app.users_conn, telegram_user_id)
        stats = users_db.personal_stats(app.users_conn, telegram_user_id, trades=trades)
        return jsonify({"trades": _to_jsonable(trades), "stats": _to_jsonable(stats)})

    return app


if __name__ == "__main__":
    create_app().run(port=int(os.environ.get("PORT", 8000)), debug=False)
