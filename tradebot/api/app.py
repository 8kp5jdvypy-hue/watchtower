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
import os
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, redirect, request, session

from tradebot import accounts, config
from tradebot.email_sender import build_email_sender
from tradebot.journal import connect as journal_connect
from tradebot.journal import tier_performance
from tradebot.runner import ET
from tradebot.telegram_bot import db as users_db
from tradebot.telegram_bot.performance import track_record

DEFAULT_FRONTEND_URL = "https://app.perchmarkets.com"


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


def _linked_telegram_user_id(conn, account: accounts.Account) -> int | None:
    row = conn.execute(
        "SELECT provider_user_id FROM linked_identities WHERE account_id = ? AND provider = ?",
        (account.id, accounts.TELEGRAM_PROVIDER),
    ).fetchone()
    return int(row[0]) if row else None


def create_app(users_db_path=None, journal_db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET_KEY", "dev-only-insecure-key-set-SESSION_SECRET_KEY")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
    # Unset locally (host-only cookie); ".perchmarkets.com" in production
    # so the cookie set by api.perchmarkets.com is also sent on requests
    # from app.perchmarkets.com.
    app.config["SESSION_COOKIE_DOMAIN"] = os.environ.get("SESSION_COOKIE_DOMAIN") or None

    app.frontend_url = os.environ.get("FRONTEND_URL", DEFAULT_FRONTEND_URL)
    app.users_conn = users_db.connect(users_db_path) if users_db_path is not None else users_db.connect()
    app.journal_conn = (
        journal_connect(journal_db_path, check_same_thread=False)
        if journal_db_path is not None
        else journal_connect(check_same_thread=False)
    )
    app.email_sender = build_email_sender()

    allowed_origins = {app.frontend_url, "http://localhost:5173"}

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

    @app.route("/auth/magic-link/request", methods=["POST"])
    def request_magic_link():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return jsonify({"error": "a valid email is required"}), 400
        now = datetime.now(timezone.utc)
        token = accounts.create_magic_link_token(app.users_conn, email, now)
        link_url = f"{request.host_url.rstrip('/')}/auth/magic-link/verify?token={token}"
        app.email_sender.send_magic_link(email, link_url)
        # Same response regardless of whether this email already has an
        # account — one is created on verify if not. Nothing here reveals
        # whether an address is a known user.
        return jsonify({"ok": True}), 202

    @app.route("/auth/magic-link/verify")
    def verify_magic_link():
        token = request.args.get("token", "")
        now = datetime.now(timezone.utc)
        email = accounts.verify_magic_link_token(app.users_conn, token, now)
        if email is None:
            return jsonify({"error": "invalid or expired link"}), 400
        account = accounts.get_or_create_account_for_email(app.users_conn, email)
        session["account_id"] = account.id
        return redirect(app.frontend_url)

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

    def _recent_signals(session_filter: str | None, limit: int) -> list[dict]:
        query = (
            "SELECT id, ts_utc, session, symbol, kinds, headlines, score, tier, trend, alerted "
            "FROM detections WHERE tier IN ('high', 'medium')"
        )
        params: list = []
        if session_filter is not None:
            query += " AND session = ?"
            params.append(session_filter)
        query += " ORDER BY ts_utc DESC LIMIT ?"
        params.append(limit)
        rows = app.journal_conn.execute(query, params).fetchall()
        return [
            {
                "id": row[0], "ts_utc": row[1], "session": row[2], "symbol": row[3],
                "kinds": row[4].split(","), "headlines": row[5], "score": row[6],
                "tier": row[7], "trend": row[8], "alerted": bool(row[9]),
            }
            for row in rows
        ]

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

    @app.route("/performance")
    @login_required
    def performance():
        by_tier = tier_performance(app.journal_conn)
        record = track_record(app.journal_conn)
        return jsonify({"by_tier": _to_jsonable(by_tier), "track_record": _to_jsonable(record)})

    @app.route("/activity")
    @login_required
    def activity():
        telegram_user_id = _linked_telegram_user_id(app.users_conn, g.account)
        if telegram_user_id is None:
            return jsonify({"trades": [], "stats": None})
        trades = users_db.list_trades(app.users_conn, telegram_user_id)
        stats = users_db.personal_stats(app.users_conn, telegram_user_id)
        return jsonify({"trades": _to_jsonable(trades), "stats": _to_jsonable(stats)})

    return app


if __name__ == "__main__":
    create_app().run(port=int(os.environ.get("PORT", 8000)), debug=False)
