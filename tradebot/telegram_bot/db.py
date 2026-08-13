"""Per-user data store for the Telegram command layer.

Deliberately a SEPARATE database from data/journal.db. The journal is the
bot's own signal record — shared, the same for everyone, and what
/performance reads. This file is each person's own account state and
trade log — what /me and /export read. Keeping them apart means a
handler can never accidentally blend "what the bot detected" with "what
a specific human did about it."

Nothing here fabricates a statistic: every aggregate query returns an
explicit `None`/empty result when there isn't enough data, the same
discipline as tradebot.journal (see MIN_HISTORY_SAMPLE there).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "users.db"

MIN_STAT_SAMPLE = 5  # same floor as tradebot.journal.MIN_HISTORY_SAMPLE — never report a rate on fewer
# Same convention as tradebot.journal.ET / tradebot.runner.ET — every
# calendar-day bucketing decision in this codebase converts to ET first.
# monthly_recap()/personal_stats() below predate this and bucket by raw
# UTC year/month instead (see docs/BACKLOG.md) -- the Journal functions
# further down do not repeat that.
ET = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    username TEXT,
    created_at TEXT NOT NULL,
    onboarded_at TEXT,
    risk_ack_at TEXT,
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    quiet_hours_start TEXT,
    quiet_hours_end TEXT,
    tier TEXT NOT NULL DEFAULT 'free',  -- superseded by `plan` below; no longer read anywhere
    is_admin INTEGER NOT NULL DEFAULT 0,
    paused_until TEXT,
    pause_reason TEXT,
    locked_until TEXT,
    lock_reason TEXT,
    max_trades_per_day INTEGER,
    max_daily_loss REAL,
    max_position_size REAL,
    pending_limits_json TEXT NOT NULL DEFAULT '[]',
    halted_session TEXT,
    onboarding_step TEXT,
    account_size REAL,
    risk_per_trade_pct REAL
);

CREATE TABLE IF NOT EXISTS watchlists (
    telegram_user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    PRIMARY KEY (telegram_user_id, symbol)
);

CREATE TABLE IF NOT EXISTS user_trades (
    id TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    detection_id TEXT,
    symbol TEXT NOT NULL,
    kind TEXT,
    tier TEXT,
    alert_ts_utc TEXT,
    taken_at TEXT NOT NULL,
    reaction_seconds REAL,
    after_no_trade INTEGER NOT NULL DEFAULT 0,
    contracts REAL,
    entry_price REAL,
    exit_price REAL,
    closed_at TEXT,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'open',
    emotional_tag TEXT
);

CREATE TABLE IF NOT EXISTS alert_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    detection_id TEXT NOT NULL,
    response TEXT NOT NULL,
    ts_utc TEXT NOT NULL
);

-- Outbox pattern (see tradebot.telegram_bot.outbox / .worker): every
-- outbound Telegram send is persisted here BEFORE delivery is attempted,
-- and the worker is the only thing that ever calls the Telegram API.
-- UNIQUE(alert_id, chat_id) is the idempotency key — re-enqueueing the
-- same (alert_id, chat_id) pair (e.g. a producer retry after a crash) is
-- a safe no-op, never a duplicate row.
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    text TEXT NOT NULL,
    reply_markup_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    leased_by TEXT,
    leased_at TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT,
    UNIQUE(alert_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, priority, next_attempt_at);

-- One tap from anywhere (see handlers.handle_feedback) — during a free
-- beta this is the only thing being collected instead of revenue.
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    ts_utc TEXT NOT NULL
);

-- Perch-native identity (see tradebot.accounts). A Telegram user, a web
-- login, and (later) a mobile login all resolve to one of these rows via
-- linked_identities — this table, not `users`, is the root of "who is
-- this" once more than one surface exists. `email` is nullable: an
-- account created by linking Telegram first (the common case today, via
-- the one-time migration) has no email until its owner logs into the
-- web dashboard and claims one. SQLite's UNIQUE index treats each NULL
-- as distinct, so any number of email-less accounts can coexist.
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    plan TEXT NOT NULL DEFAULT 'beta',
    founding_member INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Many identities -> one account. provider is 'telegram' today;
-- 'apple'/'google' land whenever mobile does, same shape, no schema
-- change needed.
CREATE TABLE IF NOT EXISTS linked_identities (
    account_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS idx_linked_identities_account ON linked_identities(account_id);

-- Passwordless web login. A token is single-use (consumed_at) and
-- short-lived (expires_at) — see tradebot.accounts.verify_magic_link_token.
CREATE TABLE IF NOT EXISTS magic_link_tokens (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

-- Minimal, anonymous product-funnel log -- see tradebot.funnel_events
-- for the write path and the reviewed ALLOWED_EVENTS set. anon_id is a
-- random value the frontend generates and stores in localStorage, not
-- derived from anything identifying; account_id is filled in by the API
-- from the session once a visitor has signed in, so a signup funnel can
-- be traced end to end without anything before that point being tied
-- to a real person.
CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    event TEXT NOT NULL,
    anon_id TEXT NOT NULL,
    account_id TEXT,
    props_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_funnel_events_event ON funnel_events(event, ts_utc);

-- Fixed-window rate limiting -- see tradebot.rate_limit. One row per
-- (bucket_key, window_start); bucket_key encodes both what's being
-- limited and its scope, e.g. "magic_link:email:alice@example.com" or
-- "events:ip:203.0.113.4", so the same table serves every endpoint
-- that needs this without a schema change per endpoint.
CREATE TABLE IF NOT EXISTS rate_limit_counters (
    bucket_key TEXT NOT NULL,
    window_start TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_key, window_start)
);

-- Client-side error reports -- see tradebot.client_errors. Deliberately
-- separate from funnel_events: different read pattern ("show me the
-- last 50 errors"), different retention concerns, and mixing product
-- analytics with crash reports in one table would make both harder to
-- query honestly.
CREATE TABLE IF NOT EXISTS client_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    message TEXT NOT NULL,
    stack TEXT,
    url TEXT,
    user_agent TEXT,
    account_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_errors_ts ON client_errors(ts_utc);
"""


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
    """CREATE TABLE IF NOT EXISTS won't retroactively add a column to a
    pre-existing table — same migration pattern as tradebot.journal."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
    _add_column_if_missing(conn, "user_trades", "direction", "TEXT")
    _add_column_if_missing(conn, "user_trades", "note", "TEXT")
    _add_column_if_missing(conn, "users", "account_size", "REAL")
    _add_column_if_missing(conn, "users", "risk_per_trade_pct", "REAL")
    _add_column_if_missing(conn, "users", "telegram_unreachable_at", "TEXT")
    # plan/founding_member deliberately use ALTER TABLE's default-fill
    # behavior: every row that already exists the moment this migration
    # runs — including everyone onboarded before this column existed —
    # is, definitionally, a beta user, so backfilling plan='beta' and
    # founding_member=1 for them is correct, not just convenient. See
    # tradebot.telegram_bot.access for how (not yet) this gets read.
    _add_column_if_missing(conn, "users", "plan", "TEXT NOT NULL DEFAULT 'beta'")
    _add_column_if_missing(conn, "users", "founding_member", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "users", "waitlisted_at", "TEXT")
    # How much signal a user wants (see onboarding's "how much signal do
    # you want?" step) — 'quiet' raises the per-user HIGH-tier score
    # floor above the global TIER_HIGH cutoff; 'balanced' (the default,
    # including for every pre-existing row) is today's unchanged
    # behavior; 'aggressive' also personally forwards the hourly MEDIUM
    # digest, not just HIGH alerts. See tradebot.telegram_bot.delivery.
    _add_column_if_missing(conn, "users", "alert_sensitivity", "TEXT NOT NULL DEFAULT 'balanced'")
    # Trade Journal (web-native entries) — extends the same user_trades
    # table Telegram's /took and /closed already write to. All nullable,
    # all additive; see create_journal_trade's docstring for why
    # telegram_user_id (NOT NULL, legacy, unchanged here) isn't the key
    # these new columns are scoped by.
    _add_column_if_missing(conn, "user_trades", "account_id", "TEXT")
    _add_column_if_missing(conn, "user_trades", "pnl_cents", "INTEGER")
    _add_column_if_missing(conn, "user_trades", "source", "TEXT")
    _add_column_if_missing(conn, "user_trades", "is_skip", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "user_trades", "skip_reason", "TEXT")
    _add_column_if_missing(conn, "user_trades", "detection_snapshot_json", "TEXT")
    _add_column_if_missing(conn, "user_trades", "quantity", "REAL")
    _add_column_if_missing(conn, "user_trades", "fees_cents", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_trades_account ON user_trades(account_id)")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_hhmm(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm))


# --------------------------------------------------------------------------
# User
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    telegram_user_id: int
    chat_id: int
    username: str | None
    created_at: str
    onboarded_at: str | None
    risk_ack_at: str | None
    timezone: str
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    tier: str
    is_admin: bool
    paused_until: str | None
    pause_reason: str | None
    locked_until: str | None
    lock_reason: str | None
    max_trades_per_day: int | None
    max_daily_loss: float | None
    max_position_size: float | None
    pending_limits: list
    halted_session: str | None
    onboarding_step: str | None
    account_size: float | None
    risk_per_trade_pct: float | None
    telegram_unreachable_at: str | None
    plan: str
    founding_member: bool
    waitlisted_at: str | None
    alert_sensitivity: str

    @property
    def is_onboarded(self) -> bool:
        return self.onboarded_at is not None

    @property
    def has_risk_ack(self) -> bool:
        return self.risk_ack_at is not None

    @property
    def is_telegram_unreachable(self) -> bool:
        """Set by the outbox worker on a Forbidden (blocked the bot) or
        ChatNotFound response — terminal, never retried automatically.
        See db.mark_telegram_unreachable / outbox.py."""
        return self.telegram_unreachable_at is not None

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < datetime.fromisoformat(self.paused_until)

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and now < datetime.fromisoformat(self.locked_until)

    def is_halted_for_session(self, session_date: date) -> bool:
        return self.halted_session == session_date.isoformat()

    def is_in_quiet_hours(self, now_utc: datetime) -> bool:
        """Converts now_utc into the user's own local time and checks it
        against their configured quiet_hours window, handling a window
        that spans midnight (e.g. 22:00-06:00). Fails open (never quiet)
        on a bad timezone/time value rather than raising — this method
        sits on the hot path for every HIGH alert fan-out (see
        list_subscribers_for_symbol), and one corrupted row must never be
        able to take that down for everyone."""
        if self.quiet_hours_start is None or self.quiet_hours_end is None:
            return False
        try:
            local_now = now_utc.astimezone(ZoneInfo(self.timezone)).time()
            start = _parse_hhmm(self.quiet_hours_start)
            end = _parse_hhmm(self.quiet_hours_end)
        except (ZoneInfoNotFoundError, ValueError):
            return False
        if start <= end:
            return start <= local_now < end
        return local_now >= start or local_now < end


_USER_COLUMNS = (
    "telegram_user_id, chat_id, username, created_at, onboarded_at, risk_ack_at, timezone, "
    "quiet_hours_start, quiet_hours_end, tier, is_admin, paused_until, pause_reason, "
    "locked_until, lock_reason, max_trades_per_day, max_daily_loss, max_position_size, "
    "pending_limits_json, halted_session, onboarding_step, account_size, risk_per_trade_pct, "
    "telegram_unreachable_at, plan, founding_member, waitlisted_at, alert_sensitivity"
)


def _row_to_user(row) -> User:
    (
        uid, chat_id, username, created_at, onboarded_at, risk_ack_at, tz, qh_start, qh_end,
        tier, is_admin, paused_until, pause_reason, locked_until, lock_reason,
        max_trades, max_loss, max_size, pending_json, halted_session, onboarding_step,
        account_size, risk_per_trade_pct, telegram_unreachable_at, plan, founding_member, waitlisted_at,
        alert_sensitivity,
    ) = row
    return User(
        telegram_user_id=uid, chat_id=chat_id, username=username, created_at=created_at,
        onboarded_at=onboarded_at, risk_ack_at=risk_ack_at, timezone=tz,
        quiet_hours_start=qh_start, quiet_hours_end=qh_end, tier=tier, is_admin=bool(is_admin),
        paused_until=paused_until, pause_reason=pause_reason, locked_until=locked_until,
        lock_reason=lock_reason, max_trades_per_day=max_trades, max_daily_loss=max_loss,
        max_position_size=max_size, pending_limits=json.loads(pending_json), halted_session=halted_session,
        onboarding_step=onboarding_step, account_size=account_size, risk_per_trade_pct=risk_per_trade_pct,
        telegram_unreachable_at=telegram_unreachable_at, plan=plan, founding_member=bool(founding_member),
        waitlisted_at=waitlisted_at, alert_sensitivity=alert_sensitivity,
    )


def get_user(conn: sqlite3.Connection, telegram_user_id: int) -> User | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_chat_id(conn: sqlite3.Connection, chat_id: int) -> User | None:
    """For a DM, chat_id and telegram_user_id are the same Telegram id in
    practice, but the outbox worker looks this up rather than assume it —
    an outbox row only carries chat_id (it's what Telegram sends to), and
    an ops-channel broadcast's chat_id has no user row at all."""
    row = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_or_create_user(conn: sqlite3.Connection, telegram_user_id: int, chat_id: int, username: str | None) -> User:
    """Idempotent — see /start requirement: re-running never wipes
    existing settings. Only touches chat_id/username on an existing row
    (they can legitimately change), never resets onboarding state.

    chat_id is only ever overwritten when this update came from the
    user's own private chat (Telegram's convention: chat_id ==
    telegram_user_id there, never true for a group/supergroup/channel).
    A subscriber's stored delivery target -- where their HIGH alerts and
    personal position-sizing follow-ups actually get sent
    (telegram_bot.delivery) -- must never silently become a group chat
    just because they ran a GROUP_ALLOWED command (commands.py) or sent
    any message there; only re-DMing the bot can change it back. The
    very first row for a brand-new user still takes whatever chat_id
    they were first seen in (nothing to protect yet there).

    telegram_unreachable_at is cleared by the same is_private gate, for
    the same reason: a message arriving from a GROUP the bot is also in
    is concrete proof that group is reachable, not that the user's own
    private chat is -- only an update from their private chat is real
    evidence mark_telegram_unreachable's terminal marking should lift."""
    is_private = chat_id == telegram_user_id
    conn.execute(
        "INSERT INTO users (telegram_user_id, chat_id, username, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(telegram_user_id) DO UPDATE SET "
        "chat_id = CASE WHEN ? THEN excluded.chat_id ELSE users.chat_id END, "
        "username=excluded.username, "
        "telegram_unreachable_at = CASE WHEN ? THEN NULL ELSE users.telegram_unreachable_at END",
        (telegram_user_id, chat_id, username, _now_iso(), is_private, is_private),
    )
    conn.commit()
    return get_user(conn, telegram_user_id)


def set_risk_ack(conn: sqlite3.Connection, telegram_user_id: int, when: datetime) -> None:
    conn.execute(
        "UPDATE users SET risk_ack_at = ? WHERE telegram_user_id = ?", (when.isoformat(), telegram_user_id)
    )
    conn.commit()


def mark_onboarded(conn: sqlite3.Connection, telegram_user_id: int, when: datetime) -> None:
    conn.execute(
        "UPDATE users SET onboarded_at = ? WHERE telegram_user_id = ?", (when.isoformat(), telegram_user_id)
    )
    conn.commit()


def set_timezone(conn: sqlite3.Connection, telegram_user_id: int, tz_name: str) -> None:
    conn.execute("UPDATE users SET timezone = ? WHERE telegram_user_id = ?", (tz_name, telegram_user_id))
    conn.commit()


def set_quiet_hours(conn: sqlite3.Connection, telegram_user_id: int, start: str, end: str) -> None:
    conn.execute(
        "UPDATE users SET quiet_hours_start = ?, quiet_hours_end = ? WHERE telegram_user_id = ?",
        (start, end, telegram_user_id),
    )
    conn.commit()


ALERT_SENSITIVITIES = ("quiet", "balanced", "aggressive")


def set_alert_sensitivity(conn: sqlite3.Connection, telegram_user_id: int, sensitivity: str) -> None:
    if sensitivity not in ALERT_SENSITIVITIES:
        raise ValueError(f"unknown alert_sensitivity: {sensitivity!r} (expected one of {ALERT_SENSITIVITIES})")
    conn.execute(
        "UPDATE users SET alert_sensitivity = ? WHERE telegram_user_id = ?", (sensitivity, telegram_user_id)
    )
    conn.commit()


def set_admin(conn: sqlite3.Connection, telegram_user_id: int, is_admin: bool) -> None:
    conn.execute(
        "UPDATE users SET is_admin = ? WHERE telegram_user_id = ?", (int(is_admin), telegram_user_id)
    )
    conn.commit()


def set_onboarding_step(conn: sqlite3.Connection, telegram_user_id: int, step: str | None) -> None:
    conn.execute(
        "UPDATE users SET onboarding_step = ? WHERE telegram_user_id = ?", (step, telegram_user_id)
    )
    conn.commit()


# --------------------------------------------------------------------------
# Pause / lock / halt
# --------------------------------------------------------------------------


def set_pause(conn: sqlite3.Connection, telegram_user_id: int, until: datetime, reason: str) -> None:
    conn.execute(
        "UPDATE users SET paused_until = ?, pause_reason = ? WHERE telegram_user_id = ?",
        (until.isoformat(), reason, telegram_user_id),
    )
    conn.commit()


def clear_pause(conn: sqlite3.Connection, telegram_user_id: int) -> None:
    conn.execute(
        "UPDATE users SET paused_until = NULL, pause_reason = NULL WHERE telegram_user_id = ?",
        (telegram_user_id,),
    )
    conn.commit()


def set_lock(conn: sqlite3.Connection, telegram_user_id: int, until: datetime, reason: str) -> None:
    conn.execute(
        "UPDATE users SET locked_until = ?, lock_reason = ? WHERE telegram_user_id = ?",
        (until.isoformat(), reason, telegram_user_id),
    )
    conn.commit()


def set_session_halt(conn: sqlite3.Connection, telegram_user_id: int, session_date: date) -> None:
    conn.execute(
        "UPDATE users SET halted_session = ? WHERE telegram_user_id = ?",
        (session_date.isoformat(), telegram_user_id),
    )
    conn.commit()


def clear_session_halt(conn: sqlite3.Connection, telegram_user_id: int) -> None:
    conn.execute(
        "UPDATE users SET halted_session = NULL WHERE telegram_user_id = ?", (telegram_user_id,)
    )
    conn.commit()


# --------------------------------------------------------------------------
# Limits — decreases apply immediately, increases queue during market hours.
# --------------------------------------------------------------------------

LIMIT_FIELDS = {
    "max_trades_per_day": "max_trades_per_day",
    "max_daily_loss": "max_daily_loss",
    "max_position_size": "max_position_size",
}


def apply_limit_change(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    field: str,
    new_value: float,
    *,
    now: datetime,
    market_is_open: bool,
) -> str:
    """Returns 'applied' or 'queued'. A decrease (or a field with no
    current value) always applies immediately — tightening a limit is
    never something to protect someone from doing right now. An increase
    requested while the market is open is queued instead: the whole point
    of a limit is to bind future-you, including a future-you who wants to
    raise it mid-session after two losses."""
    if field not in LIMIT_FIELDS:
        raise ValueError(f"unknown limit field: {field}")
    column = LIMIT_FIELDS[field]
    user = get_user(conn, telegram_user_id)
    current = getattr(user, column)

    is_increase = current is not None and new_value > current
    if is_increase and market_is_open:
        pending = user.pending_limits + [{"field": field, "value": new_value, "queued_at": now.isoformat()}]
        conn.execute(
            "UPDATE users SET pending_limits_json = ? WHERE telegram_user_id = ?",
            (json.dumps(pending), telegram_user_id),
        )
        conn.commit()
        return "queued"

    conn.execute(f"UPDATE users SET {column} = ? WHERE telegram_user_id = ?", (new_value, telegram_user_id))
    conn.commit()
    return "applied"


def apply_pending_limits_if_due(conn: sqlite3.Connection, telegram_user_id: int, session_date: date) -> list[dict]:
    """Applies any limit increases queued from a PRIOR session (never the
    current one — queued-today changes wait for the next session
    boundary, not the next call). Returns the changes actually applied."""
    user = get_user(conn, telegram_user_id)
    if not user.pending_limits:
        return []
    session_start = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
    to_apply = [p for p in user.pending_limits if datetime.fromisoformat(p["queued_at"]) < session_start]
    if not to_apply:
        return []
    still_pending = [p for p in user.pending_limits if p not in to_apply]
    for change in to_apply:
        column = LIMIT_FIELDS[change["field"]]
        conn.execute(
            f"UPDATE users SET {column} = ? WHERE telegram_user_id = ?", (change["value"], telegram_user_id)
        )
    conn.execute(
        "UPDATE users SET pending_limits_json = ? WHERE telegram_user_id = ?",
        (json.dumps(still_pending), telegram_user_id),
    )
    conn.commit()
    return to_apply


# --------------------------------------------------------------------------
# Sizing inputs (account_size, risk_per_trade_pct) — feed the per-alert
# position-size calculator (tradebot.costs.position_size). Unlike
# LIMIT_FIELDS above, these are informational sizing inputs, not
# protective caps: there's no scenario where delaying an account-size
# update mid-session protects anyone from themselves, so they always
# apply immediately regardless of market hours.
# --------------------------------------------------------------------------

SIZING_FIELDS = {"account_size", "risk_per_trade_pct"}


def set_sizing_field(conn: sqlite3.Connection, telegram_user_id: int, field: str, value: float) -> None:
    if field not in SIZING_FIELDS:
        raise ValueError(f"unknown sizing field: {field}")
    conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_user_id = ?", (value, telegram_user_id))
    conn.commit()


# --------------------------------------------------------------------------
# Watchlist — None means "no override, use the bot default"
# --------------------------------------------------------------------------


def get_watchlist(conn: sqlite3.Connection, telegram_user_id: int) -> list[str] | None:
    rows = conn.execute(
        "SELECT symbol FROM watchlists WHERE telegram_user_id = ? ORDER BY symbol", (telegram_user_id,)
    ).fetchall()
    return [r[0] for r in rows] or None


def toggle_watchlist_symbol(conn: sqlite3.Connection, telegram_user_id: int, symbol: str) -> bool:
    """Returns True if the symbol is now selected, False if it was just removed."""
    exists = conn.execute(
        "SELECT 1 FROM watchlists WHERE telegram_user_id = ? AND symbol = ?", (telegram_user_id, symbol)
    ).fetchone()
    if exists:
        conn.execute(
            "DELETE FROM watchlists WHERE telegram_user_id = ? AND symbol = ?", (telegram_user_id, symbol)
        )
        conn.commit()
        return False
    conn.execute("INSERT INTO watchlists (telegram_user_id, symbol) VALUES (?, ?)", (telegram_user_id, symbol))
    conn.commit()
    return True


def set_watchlist(conn: sqlite3.Connection, telegram_user_id: int, symbols: list[str]) -> None:
    conn.execute("DELETE FROM watchlists WHERE telegram_user_id = ?", (telegram_user_id,))
    conn.executemany(
        "INSERT INTO watchlists (telegram_user_id, symbol) VALUES (?, ?)",
        [(telegram_user_id, s) for s in symbols],
    )
    conn.commit()


# --------------------------------------------------------------------------
# Subscribers eligible for a HIGH alert right now
# --------------------------------------------------------------------------


def list_subscribers_for_symbol(
    conn: sqlite3.Connection, symbol: str, session_date: date, now: datetime, default_watchlist: list[str]
) -> list[User]:
    rows = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE onboarded_at IS NOT NULL AND risk_ack_at IS NOT NULL"
    ).fetchall()
    subscribers = []
    for row in rows:
        user = _row_to_user(row)
        if user.is_paused(now) or user.is_locked(now) or user.is_halted_for_session(session_date):
            continue
        if user.is_telegram_unreachable:
            continue
        if user.is_in_quiet_hours(now):
            continue
        watchlist = get_watchlist(conn, user.telegram_user_id) or default_watchlist
        if symbol in watchlist:
            subscribers.append(user)
    return subscribers


def mark_telegram_unreachable(conn: sqlite3.Connection, telegram_user_id: int, when: datetime) -> None:
    """Set by the outbox worker (tradebot.telegram_bot.worker) on a
    Forbidden or ChatNotFound response — the bot was blocked, or the
    chat/account is gone. Terminal: never cleared automatically, since
    there's no event that tells us the user unblocked the bot. A real
    person can only get back on the list by re-running /start, which
    calls get_or_create_user and this doesn't touch that path."""
    conn.execute(
        "UPDATE users SET telegram_unreachable_at = ? WHERE telegram_user_id = ?",
        (when.isoformat(), telegram_user_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Capacity + waitlist. AppConfig.max_active_users is the config cap (None =
# unlimited) — see tradebot.telegram_bot.handlers.handle_start, the only
# place this is checked. Note this is about the bot's own operational
# scale (SQLite contention, support burden), not Telegram's send-rate
# limits — those are already independently enforced by the outbox worker's
# token buckets (tradebot.telegram_bot.worker) regardless of subscriber
# count, so a large but sane user base was never going to get anyone
# rate-limited on the delivery side.
# --------------------------------------------------------------------------


def count_active_users(conn: sqlite3.Connection) -> int:
    """Users who completed onboarding and aren't confirmed gone — the
    population that actually consumes a capacity slot. A paused or locked
    user still counts: they can resume any time without re-consuming one."""
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE onboarded_at IS NOT NULL AND telegram_unreachable_at IS NULL"
    ).fetchone()[0]


def set_waitlisted(conn: sqlite3.Connection, telegram_user_id: int, when: datetime) -> None:
    conn.execute(
        "UPDATE users SET waitlisted_at = ? WHERE telegram_user_id = ?", (when.isoformat(), telegram_user_id)
    )
    conn.commit()


def clear_waitlist(conn: sqlite3.Connection, telegram_user_id: int) -> None:
    conn.execute("UPDATE users SET waitlisted_at = NULL WHERE telegram_user_id = ?", (telegram_user_id,))
    conn.commit()


def waitlist_position(conn: sqlite3.Connection, telegram_user_id: int) -> int | None:
    """1-indexed, first-come-first-served by when each person landed on
    the waitlist. None if this user isn't currently waitlisted."""
    row = conn.execute(
        "SELECT waitlisted_at FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    ahead = conn.execute(
        "SELECT COUNT(*) FROM users WHERE waitlisted_at IS NOT NULL AND waitlisted_at < ?", (row[0],)
    ).fetchone()[0]
    return ahead + 1


# --------------------------------------------------------------------------
# Feedback — /feedback, one tap from anywhere. During a free beta this is
# the only thing being collected instead of revenue.
# --------------------------------------------------------------------------


def add_feedback(conn: sqlite3.Connection, telegram_user_id: int, message: str, when: datetime) -> None:
    conn.execute(
        "INSERT INTO feedback (telegram_user_id, message, ts_utc) VALUES (?, ?, ?)",
        (telegram_user_id, message, when.isoformat()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Trades — what a user logged via /took and /closed (or the alert buttons)
# --------------------------------------------------------------------------


# One-tap emotional state, logged at entry (see keyboards.mood_keyboard) —
# a fixed vocabulary, not free text, so it can be bucketed reliably. The
# free-text note is a separate field for whatever doesn't fit these five.
MOOD_CHOICES = ("calm", "rushed", "fomo", "revenge", "bored")


@dataclass(frozen=True)
class Trade:
    id: str
    telegram_user_id: int
    detection_id: str | None
    symbol: str
    kind: str | None
    tier: str | None
    direction: str | None  # "up"/"down", from the alert's own trend — bullish/bearish at display time
    alert_ts_utc: str | None
    taken_at: str
    reaction_seconds: float | None
    after_no_trade: bool
    contracts: float | None
    entry_price: float | None
    exit_price: float | None
    closed_at: str | None
    pnl_pct: float | None
    status: str
    emotional_tag: str | None
    note: str | None


_TRADE_COLUMNS = (
    "id, telegram_user_id, detection_id, symbol, kind, tier, direction, alert_ts_utc, taken_at, "
    "reaction_seconds, after_no_trade, contracts, entry_price, exit_price, closed_at, "
    "pnl_pct, status, emotional_tag, note"
)


def _row_to_trade(row) -> Trade:
    (
        tid, uid, detection_id, symbol, kind, tier, direction, alert_ts_utc, taken_at, reaction_seconds,
        after_no_trade, contracts, entry_price, exit_price, closed_at, pnl_pct, status, emotional_tag, note,
    ) = row
    return Trade(
        id=tid, telegram_user_id=uid, detection_id=detection_id, symbol=symbol, kind=kind, tier=tier,
        direction=direction, alert_ts_utc=alert_ts_utc, taken_at=taken_at, reaction_seconds=reaction_seconds,
        after_no_trade=bool(after_no_trade), contracts=contracts, entry_price=entry_price,
        exit_price=exit_price, closed_at=closed_at, pnl_pct=pnl_pct, status=status, emotional_tag=emotional_tag,
        note=note,
    )


def has_responded(conn: sqlite3.Connection, telegram_user_id: int, detection_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alert_responses WHERE telegram_user_id = ? AND detection_id = ?",
        (telegram_user_id, detection_id),
    ).fetchone()
    return row is not None


def record_alert_response(
    conn: sqlite3.Connection, telegram_user_id: int, detection_id: str, response: str, when: datetime
) -> None:
    conn.execute(
        "INSERT INTO alert_responses (telegram_user_id, detection_id, response, ts_utc) VALUES (?, ?, ?, ?)",
        (telegram_user_id, detection_id, response, when.isoformat()),
    )
    conn.commit()


def log_took(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    *,
    detection_id: str | None,
    symbol: str,
    kind: str | None = None,
    tier: str | None = None,
    direction: str | None = None,
    alert_ts_utc: str | None = None,
    taken_at: datetime,
    after_no_trade: bool = False,
    contracts: float | None = None,
    entry_price: float | None = None,
) -> Trade:
    trade_id = uuid.uuid4().hex
    reaction_seconds = None
    if alert_ts_utc is not None:
        reaction_seconds = (taken_at - datetime.fromisoformat(alert_ts_utc)).total_seconds()
    conn.execute(
        """
        INSERT INTO user_trades
            (id, telegram_user_id, detection_id, symbol, kind, tier, direction, alert_ts_utc, taken_at,
             reaction_seconds, after_no_trade, contracts, entry_price, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            trade_id, telegram_user_id, detection_id, symbol, kind, tier, direction, alert_ts_utc,
            taken_at.isoformat(), reaction_seconds, int(after_no_trade), contracts, entry_price,
        ),
    )
    conn.commit()
    return get_trade(conn, trade_id)


def set_trade_mood(conn: sqlite3.Connection, trade_id: str, mood: str) -> Trade:
    """The one-tap emotional state at entry (see keyboards.mood_keyboard) —
    separate from the free-text note, and restricted to MOOD_CHOICES so it
    can be bucketed. Last tap wins if pressed more than once."""
    if mood not in MOOD_CHOICES:
        raise ValueError(f"unknown mood: {mood!r} (expected one of {MOOD_CHOICES})")
    conn.execute("UPDATE user_trades SET emotional_tag = ? WHERE id = ?", (mood, trade_id))
    conn.commit()
    return get_trade(conn, trade_id)


def get_trade(conn: sqlite3.Connection, trade_id: str) -> Trade | None:
    row = conn.execute(f"SELECT {_TRADE_COLUMNS} FROM user_trades WHERE id = ?", (trade_id,)).fetchone()
    return _row_to_trade(row) if row else None


def get_open_trade_for_alert(conn: sqlite3.Connection, telegram_user_id: int, detection_id: str) -> Trade | None:
    row = conn.execute(
        f"SELECT {_TRADE_COLUMNS} FROM user_trades "
        "WHERE telegram_user_id = ? AND detection_id = ? AND status = 'open' ORDER BY taken_at DESC LIMIT 1",
        (telegram_user_id, detection_id),
    ).fetchone()
    return _row_to_trade(row) if row else None


def most_recent_open_trade(conn: sqlite3.Connection, telegram_user_id: int) -> Trade | None:
    row = conn.execute(
        f"SELECT {_TRADE_COLUMNS} FROM user_trades "
        "WHERE telegram_user_id = ? AND status = 'open' ORDER BY taken_at DESC LIMIT 1",
        (telegram_user_id,),
    ).fetchone()
    return _row_to_trade(row) if row else None


def log_closed(
    conn: sqlite3.Connection, trade_id: str, *, exit_price: float, closed_at: datetime, note: str | None = None
) -> Trade:
    """note is free text, separate from the fixed-vocabulary emotional_tag
    (set via set_trade_mood at entry, not here) — COALESCE so closing
    without a note doesn't erase one typed earlier."""
    trade = get_trade(conn, trade_id)
    if trade is None:
        raise ValueError(f"no such trade: {trade_id}")
    pnl_pct = None
    if trade.entry_price:
        pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
    conn.execute(
        "UPDATE user_trades SET exit_price = ?, closed_at = ?, pnl_pct = ?, status = 'closed', "
        "note = COALESCE(?, note) WHERE id = ?",
        (exit_price, closed_at.isoformat(), pnl_pct, note, trade_id),
    )
    conn.commit()
    return get_trade(conn, trade_id)


def list_trades(conn: sqlite3.Connection, telegram_user_id: int) -> list[Trade]:
    rows = conn.execute(
        f"SELECT {_TRADE_COLUMNS} FROM user_trades WHERE telegram_user_id = ? ORDER BY taken_at", (telegram_user_id,)
    ).fetchall()
    return [_row_to_trade(r) for r in rows]


# --------------------------------------------------------------------------
# /me — personal stats. Every stat here is gated on MIN_STAT_SAMPLE; a
# bucket with fewer closed trades than that reports None, never a rate.
# --------------------------------------------------------------------------

_DIRECTION_LABELS = {"up": "bullish", "down": "bearish"}

# (label, lo_minutes_inclusive, hi_minutes_exclusive_or_None)
HOLD_TIME_BUCKETS = (
    ("under 15m", 0, 15),
    ("15m-1h", 15, 60),
    ("1h-4h", 60, 240),
    ("over 4h", 240, None),
)


@dataclass(frozen=True)
class BucketStats:
    win_rate: float
    avg_pnl_pct: float
    n: int


def _bucket_stats(trades: list[Trade]) -> BucketStats | None:
    closed = [t for t in trades if t.status == "closed" and t.pnl_pct is not None]
    if len(closed) < MIN_STAT_SAMPLE:
        return None
    wins = sum(1 for t in closed if t.pnl_pct > 0)
    return BucketStats(
        win_rate=wins / len(closed),
        avg_pnl_pct=sum(t.pnl_pct for t in closed) / len(closed),
        n=len(closed),
    )


def _hold_minutes(trade: Trade) -> float | None:
    if trade.closed_at is None:
        return None
    return (datetime.fromisoformat(trade.closed_at) - datetime.fromisoformat(trade.taken_at)).total_seconds() / 60


def _hold_time_bucket_stats(trades: list[Trade]) -> dict[str, BucketStats | None]:
    result: dict[str, BucketStats | None] = {}
    for label, lo, hi in HOLD_TIME_BUCKETS:
        bucket = []
        for t in trades:
            minutes = _hold_minutes(t)
            if minutes is None:
                continue
            if minutes >= lo and (hi is None or minutes < hi):
                bucket.append(t)
        result[label] = _bucket_stats(bucket)
    return result


def personal_stats(conn: sqlite3.Connection, telegram_user_id: int, trades: list[Trade] | None = None) -> dict:
    """trades: pass an already-fetched list_trades() result to skip a
    second identical query — every existing caller that doesn't have one
    handy still gets it fetched here, unchanged."""
    if trades is None:
        trades = list_trades(conn, telegram_user_id)

    by_detector = {kind: _bucket_stats([t for t in trades if t.kind == kind]) for kind in sorted({t.kind for t in trades if t.kind})}
    by_symbol = {symbol: _bucket_stats([t for t in trades if t.symbol == symbol]) for symbol in sorted({t.symbol for t in trades})}
    by_direction = {
        label: _bucket_stats([t for t in trades if t.direction == raw]) for raw, label in _DIRECTION_LABELS.items()
    }
    by_hold_time = _hold_time_bucket_stats(trades)

    fast = [t for t in trades if t.reaction_seconds is not None and t.reaction_seconds <= 120]
    slow = [t for t in trades if t.reaction_seconds is not None and t.reaction_seconds > 120]
    fast_vs_slow = {"within_2min": _bucket_stats(fast), "later": _bucket_stats(slow)}

    # The headline number: what taking a trade AFTER the system explicitly
    # said NO TRADE actually costs, versus a normal entry.
    after_no_trade = [t for t in trades if t.after_no_trade]
    normal = [t for t in trades if not t.after_no_trade]
    no_trade_comparison = {"after_no_trade": _bucket_stats(after_no_trade), "normal": _bucket_stats(normal)}

    pnl_by_tag = {
        tag: _bucket_stats([t for t in trades if t.emotional_tag == tag])
        for tag in sorted({t.emotional_tag for t in trades if t.emotional_tag})
    }

    # Logging completeness: of every alert this person responded to at all
    # (took or skipped — a real decision, logged), what fraction did they
    # actually log an outcome for (took -> closed, not left open forever,
    # or skipped)? Distinct from adherence_score below: this measures
    # follow-through on LOGGING, not discipline in WHAT was taken.
    responded = conn.execute(
        "SELECT COUNT(*) FROM alert_responses WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()[0]
    dangling = sum(1 for t in trades if t.status == "open")
    logging_completeness = None
    if responded >= MIN_STAT_SAMPLE:
        logging_completeness = max(0.0, (responded - dangling) / responded)

    # Adherence: of every trade actually taken, what fraction came from a
    # real HIGH alert (detection_id set, tier == "high") that wasn't an
    # override of an explicit NO TRADE? Everything else — a freeform
    # trade with no alert behind it, a MEDIUM/LOG signal acted on as if it
    # were a real setup, or a NO TRADE override — counts as improvised.
    adherence_score = None
    if len(trades) >= MIN_STAT_SAMPLE:
        inside_rules = sum(
            1 for t in trades if t.detection_id is not None and t.tier == "high" and not t.after_no_trade
        )
        adherence_score = inside_rules / len(trades)

    return {
        "overall": _bucket_stats(trades),
        "by_detector": by_detector,
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "by_hold_time": by_hold_time,
        "fast_vs_slow": fast_vs_slow,
        "no_trade_comparison": no_trade_comparison,
        "pnl_by_tag": pnl_by_tag,
        "adherence_score": adherence_score,
        "logging_completeness": logging_completeness,
        "total_alerts_responded": responded,
        "open_trades": dangling,
        "total_trades": len(trades),
    }


# --------------------------------------------------------------------------
# Monthly recap — the same bucket lenses as personal_stats, scoped to one
# calendar month and ranked to surface the worst-magnitude leaks. Reuses
# BucketStats' MIN_STAT_SAMPLE floor per candidate: a month with too few
# trades in a bucket just doesn't produce a leak from it, never a stat
# built on 1-2 trades.
# --------------------------------------------------------------------------


def _avg_pnl(trades: list[Trade]) -> float:
    return sum(t.pnl_pct for t in trades) / len(trades)


def _closed_in_month(trades: list[Trade], year: int, month: int) -> list[Trade]:
    out = []
    for t in trades:
        if t.status != "closed" or t.pnl_pct is None or t.closed_at is None:
            continue
        closed_at = datetime.fromisoformat(t.closed_at)
        if closed_at.year == year and closed_at.month == month:
            out.append(t)
    return out


def _leak_candidates(trades: list[Trade]) -> list[tuple[str, float, int]]:
    """(label, avg_pnl_pct, n) for every candidate bucket with a real
    sample — every lens personal_stats() also reports, flattened into one
    list so the worst-magnitude ones can be ranked against each other."""
    candidates: list[tuple[str, float, int]] = []

    for tag in sorted({t.emotional_tag for t in trades if t.emotional_tag}):
        bucket = [t for t in trades if t.emotional_tag == tag]
        if len(bucket) >= MIN_STAT_SAMPLE:
            candidates.append((f"'{tag}' trades", _avg_pnl(bucket), len(bucket)))

    after_nt = [t for t in trades if t.after_no_trade]
    if len(after_nt) >= MIN_STAT_SAMPLE:
        candidates.append(("trades taken after a NO TRADE", _avg_pnl(after_nt), len(after_nt)))

    slow = [t for t in trades if t.reaction_seconds is not None and t.reaction_seconds > 120]
    if len(slow) >= MIN_STAT_SAMPLE:
        candidates.append(("entries taken more than 2min after the alert", _avg_pnl(slow), len(slow)))

    for label, lo, hi in HOLD_TIME_BUCKETS:
        bucket = [t for t in trades if (m := _hold_minutes(t)) is not None and m >= lo and (hi is None or m < hi)]
        if len(bucket) >= MIN_STAT_SAMPLE:
            candidates.append((f"{label} holds", _avg_pnl(bucket), len(bucket)))

    for kind in sorted({t.kind for t in trades if t.kind}):
        bucket = [t for t in trades if t.kind == kind]
        if len(bucket) >= MIN_STAT_SAMPLE:
            candidates.append((f"{kind} setups", _avg_pnl(bucket), len(bucket)))

    for symbol in sorted({t.symbol for t in trades}):
        bucket = [t for t in trades if t.symbol == symbol]
        if len(bucket) >= MIN_STAT_SAMPLE:
            candidates.append((f"{symbol} trades", _avg_pnl(bucket), len(bucket)))

    improvised = [t for t in trades if t.detection_id is None or t.tier != "high" or t.after_no_trade]
    if len(improvised) >= MIN_STAT_SAMPLE:
        candidates.append(("improvised trades (outside a HIGH alert, or overriding NO TRADE)", _avg_pnl(improvised), len(improvised)))

    return candidates


def monthly_recap(conn: sqlite3.Connection, telegram_user_id: int, year: int, month: int) -> dict | None:
    """The month's real numbers plus its 3 worst leaks — each leak is a
    bucket whose average P/L came in below the month's own overall
    average, ranked by how far below. None if there aren't enough closed
    trades this month to say anything real (never forces 3 leaks out of
    2 trades)."""
    trades = _closed_in_month(list_trades(conn, telegram_user_id), year, month)
    if len(trades) < MIN_STAT_SAMPLE:
        return None

    overall_avg = _avg_pnl(trades)
    candidates = _leak_candidates(trades)
    worse_than_average = [(label, avg, n) for label, avg, n in candidates if avg < overall_avg]
    leaks = sorted(worse_than_average, key=lambda c: c[1])[:3]

    return {
        "year": year,
        "month": month,
        "trade_count": len(trades),
        "win_rate": sum(1 for t in trades if t.pnl_pct > 0) / len(trades),
        "overall_avg_pnl_pct": overall_avg,
        "leaks": [
            {"label": label, "avg_pnl_pct": avg, "n": n, "gap_pct": avg - overall_avg} for label, avg, n in leaks
        ],
    }


# --------------------------------------------------------------------------
# Trade Journal (web-native entries)
#
# Same user_trades table Telegram's /took and /closed already write to —
# extended, not duplicated, per the columns added in connect() above.
# Scoped by `account_id` (the Perch-native identity, see the `accounts`
# table), never by telegram_user_id: a web-only account with no linked
# Telegram identity must be able to use the Journal, and telegram_user_id
# is a legacy NOT NULL column on this table with no sensible value for
# such an account. See _telegram_id_for_account for how that constraint
# is satisfied without meaning anything for Journal purposes — account_id
# is the only column any function below trusts for scoping or
# authorization, and it must always come from the caller's own resolved
# session (g.account.id in the API layer), never from request data.
#
# Every read/update/delete below repeats the account-scope check inside
# its own SQL WHERE clause (not just as a preceding lookup) — a defense
# a caller can't accidentally bypass by skipping the "does this belong to
# them" check before mutating.
# --------------------------------------------------------------------------

JOURNAL_SOURCES = ("perch_signal", "own_analysis", "both", "other")

# The `direction` column already exists (added for Telegram-origin rows)
# and stores the SIGNAL's direction ('up'/'down' -> bullish/bearish, see
# _DIRECTION_LABELS above) -- not literally "long/short" a user's own
# position. Journal entries reuse the same column but speak "long/short"
# at the API boundary; translated here so the one column keeps one
# consistent on-disk vocabulary instead of mixing two.
_JOURNAL_DIRECTION_TO_DB = {"long": "up", "short": "down"}
_JOURNAL_DIRECTION_FROM_DB = {"up": "long", "down": "short"}

# Every Journal read scopes to rows that are either directly tagged with
# this account_id (any trade logged through the web Journal) or reachable
# through a linked Telegram identity (trades logged via /took and
# /closed, before this account ever touched the web Journal) -- both
# live in this same users.db file, so this is a real SQL join, not the
# cross-database stitching journal.db-linked data would need. An account
# with no Telegram link simply gets an empty result from the subquery,
# which is harmless.
_ACCOUNT_SCOPE_SQL = """(
    account_id = :account_id
    OR telegram_user_id IN (
        SELECT CAST(provider_user_id AS INTEGER) FROM linked_identities
        WHERE provider = 'telegram' AND account_id = :account_id
    )
)"""


def _sentinel_telegram_id(account_id: str) -> int:
    """user_trades.telegram_user_id is NOT NULL with no default -- a
    column this table has always required, from before accounts existed.
    Rather than rebuild the table to relax that constraint, every
    Journal-native row gets a stable NEGATIVE placeholder derived from
    account_id -- negative on purpose, for ALL Journal rows, linked
    Telegram identity or not: real Telegram ids are always positive, and
    every legacy query in this module and handlers.py binds a real
    (positive) id, so no legacy read (list_trades -> personal_stats /
    monthly_recap / the /activity endpoint, get_open_trade_for_alert,
    most_recent_open_trade, handlers' /closed lookup) can ever match a
    Journal-native row. Writing a linked account's REAL id here instead
    would silently leak web entries into personal_stats' total_trades
    and adherence_score denominators. A Journal read that wants a linked
    account's bot-logged trades gets them via _ACCOUNT_SCOPE_SQL's
    linked_identities branch, which never contains sentinels. Nothing
    reads telegram_user_id back for Journal authorization or display --
    account_id is authoritative for that everywhere below."""
    digest = hashlib.sha256(f"journal:{account_id}".encode()).hexdigest()[:12]
    return -(int(digest, 16) % 10**15 + 1)


@dataclass(frozen=True)
class JournalTrade:
    id: str
    account_id: str | None
    detection_id: str | None
    detection_snapshot: dict | None
    symbol: str
    direction: str | None  # 'long' | 'short' | None
    source: str | None  # one of JOURNAL_SOURCES | None
    taken_at: str  # ISO UTC
    closed_at: str | None  # ISO UTC; None only for an is_skip row
    pnl_cents: int | None
    quantity: float | None
    entry_price: float | None
    exit_price: float | None
    fees_cents: int | None
    is_skip: bool
    skip_reason: str | None
    note: str | None
    status: str


_JOURNAL_COLUMNS = (
    "id, account_id, detection_id, detection_snapshot_json, symbol, direction, source, taken_at, "
    "closed_at, pnl_cents, quantity, entry_price, exit_price, fees_cents, is_skip, skip_reason, note, status"
)


def _row_to_journal_trade(row) -> JournalTrade:
    (
        tid, account_id, detection_id, snapshot_json, symbol, direction, source, taken_at, closed_at,
        pnl_cents, quantity, entry_price, exit_price, fees_cents, is_skip, skip_reason, note, status,
    ) = row
    return JournalTrade(
        id=tid,
        account_id=account_id,
        detection_id=detection_id,
        detection_snapshot=json.loads(snapshot_json) if snapshot_json else None,
        symbol=symbol,
        direction=_JOURNAL_DIRECTION_FROM_DB.get(direction, direction),
        source=source,
        taken_at=taken_at,
        closed_at=closed_at,
        pnl_cents=pnl_cents,
        quantity=quantity,
        entry_price=entry_price,
        exit_price=exit_price,
        fees_cents=fees_cents,
        is_skip=bool(is_skip),
        skip_reason=skip_reason,
        note=note,
        status=status,
    )


def et_date(iso_utc: str) -> date:
    """The one place Journal code converts a stored UTC timestamp to a
    calendar day -- always ET, matching tradebot.journal.ET /
    tradebot.runner.ET's session_date_fn convention, deliberately not
    repeating monthly_recap/personal_stats' raw-UTC bucketing above (see
    docs/BACKLOG.md)."""
    return datetime.fromisoformat(iso_utc).astimezone(ET).date()


def create_journal_trade(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    symbol: str,
    taken_at: datetime,
    direction: str | None = None,
    source: str | None = None,
    pnl_cents: int | None = None,
    note: str | None = None,
    detection_id: str | None = None,
    detection_snapshot: dict | None = None,
    is_skip: bool = False,
    skip_reason: str | None = None,
) -> JournalTrade:
    """Payload validation (required fields, source/direction vocabulary,
    pnl_cents parsing) happens in the API layer, not here -- this
    function trusts its keyword arguments the same way log_took/
    log_closed above trust theirs, and is the single place a row
    actually gets written, so every caller goes through the same
    account_id-derived telegram_user_id placeholder logic."""
    trade_id = uuid.uuid4().hex
    tg_id = _sentinel_telegram_id(account_id)
    db_direction = _JOURNAL_DIRECTION_TO_DB.get(direction, direction)
    taken_at_iso = taken_at.astimezone(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO user_trades
            (id, telegram_user_id, account_id, detection_id, detection_snapshot_json, symbol,
             direction, source, taken_at, closed_at, pnl_cents, is_skip, skip_reason, note, status)
        VALUES (:id, :tg_id, :account_id, :detection_id, :snapshot, :symbol, :direction, :source,
                :taken_at, :closed_at, :pnl_cents, :is_skip, :skip_reason, :note, :status)
        """,
        {
            "id": trade_id,
            "tg_id": tg_id,
            "account_id": account_id,
            "detection_id": detection_id,
            "snapshot": json.dumps(detection_snapshot) if detection_snapshot else None,
            "symbol": symbol,
            "direction": db_direction,
            "source": source,
            "taken_at": taken_at_iso,
            "closed_at": None if is_skip else taken_at_iso,
            "pnl_cents": None if is_skip else pnl_cents,
            "is_skip": int(is_skip),
            "skip_reason": skip_reason if is_skip else None,
            "note": note,
            "status": "skipped" if is_skip else "closed",
        },
    )
    conn.commit()
    return get_journal_trade(conn, account_id, trade_id)


def get_journal_trade(conn: sqlite3.Connection, account_id: str, trade_id: str) -> JournalTrade | None:
    row = conn.execute(
        f"SELECT {_JOURNAL_COLUMNS} FROM user_trades WHERE id = :trade_id AND {_ACCOUNT_SCOPE_SQL}",
        {"trade_id": trade_id, "account_id": account_id},
    ).fetchone()
    return _row_to_journal_trade(row) if row else None


_JOURNAL_UPDATABLE_FIELDS = {"symbol", "direction", "source", "taken_at", "pnl_cents", "note", "skip_reason"}


def update_journal_trade(conn: sqlite3.Connection, account_id: str, trade_id: str, **fields) -> JournalTrade | None:
    """Returns None if trade_id doesn't exist OR doesn't belong to
    account_id -- indistinguishable on purpose, same as get_journal_trade,
    so a caller can't probe for the existence of another account's trade
    id. The UPDATE's own WHERE clause repeats the account-scope check
    (not just this preceding lookup) -- belt and suspenders against a
    future caller skipping the pre-check."""
    existing = get_journal_trade(conn, account_id, trade_id)
    if existing is None:
        return None
    sets, params = [], {"trade_id": trade_id, "account_id": account_id}
    for key, value in fields.items():
        if key not in _JOURNAL_UPDATABLE_FIELDS:
            raise ValueError(f"cannot update field: {key}")
        if key == "direction" and value is not None:
            value = _JOURNAL_DIRECTION_TO_DB.get(value, value)
        if key == "taken_at" and isinstance(value, datetime):
            value = value.astimezone(timezone.utc).isoformat()
        sets.append(f"{key} = :{key}")
        params[key] = value
    if not sets:
        return existing
    conn.execute(f"UPDATE user_trades SET {', '.join(sets)} WHERE id = :trade_id AND {_ACCOUNT_SCOPE_SQL}", params)
    conn.commit()
    return get_journal_trade(conn, account_id, trade_id)


def delete_journal_trade(conn: sqlite3.Connection, account_id: str, trade_id: str) -> bool:
    existing = get_journal_trade(conn, account_id, trade_id)
    if existing is None:
        return False
    conn.execute(
        f"DELETE FROM user_trades WHERE id = :trade_id AND {_ACCOUNT_SCOPE_SQL}",
        {"trade_id": trade_id, "account_id": account_id},
    )
    conn.commit()
    return True


def list_journal_trades(conn: sqlite3.Connection, account_id: str, *, on_date: date | None = None) -> list[JournalTrade]:
    """Fetches the account's full Journal history and buckets by ET date
    in Python, not SQL -- SQLite has no DST-aware timezone support, and a
    fixed UTC offset would silently misbucket half the year (the exact
    class of bug flagged in docs/BACKLOG.md for monthly_recap). Fine at
    Journal v1's per-user scale; revisit if this ever needs to scan
    thousands of rows per request."""
    rows = conn.execute(
        f"SELECT {_JOURNAL_COLUMNS} FROM user_trades WHERE {_ACCOUNT_SCOPE_SQL} ORDER BY taken_at",
        {"account_id": account_id},
    ).fetchall()
    trades = [_row_to_journal_trade(r) for r in rows]
    if on_date is not None:
        trades = [t for t in trades if et_date(t.taken_at) == on_date]
    return trades


def _pnl_bucket(trades: list[JournalTrade]) -> dict:
    scored = [t for t in trades if not t.is_skip and t.pnl_cents is not None]
    return {
        "pnl_cents": sum(t.pnl_cents for t in scored),
        "trade_count": len(scored),
        "wins": sum(1 for t in scored if t.pnl_cents > 0),
        "losses": sum(1 for t in scored if t.pnl_cents < 0),
    }


def journal_summary(conn: sqlite3.Connection, account_id: str, *, now: datetime) -> dict:
    """Today/week(Mon-start)/month/all-time P&L and counts, all ET-bucketed
    off the same et_date() helper every other Journal function uses."""
    trades = list_journal_trades(conn, account_id)
    today = now.astimezone(ET).date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    by_day = [(t, et_date(t.taken_at)) for t in trades]
    return {
        "today": _pnl_bucket([t for t, d in by_day if d == today]),
        "week": _pnl_bucket([t for t, d in by_day if week_start <= d <= today]),
        "month": _pnl_bucket([t for t, d in by_day if month_start <= d <= today]),
        "all_time": _pnl_bucket(trades),
    }


def journal_calendar(conn: sqlite3.Connection, account_id: str, year: int, month: int) -> dict[str, dict]:
    """One entry per day in `year`/`month` that has at least one trade --
    days with none are simply absent, the caller/frontend renders those as
    blank rather than this returning a zero-filled row for every day of
    the month."""
    trades = list_journal_trades(conn, account_id)
    by_day: dict[date, list[JournalTrade]] = {}
    for t in trades:
        d = et_date(t.taken_at)
        if d.year == year and d.month == month:
            by_day.setdefault(d, []).append(t)
    return {d.isoformat(): _pnl_bucket(day_trades) for d, day_trades in by_day.items()}


def journal_stats(conn: sqlite3.Connection, account_id: str) -> dict:
    """Basic v1 analytics. `meaningful` is False below MIN_STAT_SAMPLE --
    the same floor personal_stats/monthly_recap use above -- so the
    frontend can render an honest "not enough trades yet" state instead
    of a win rate computed from 2 data points."""
    trades = [t for t in list_journal_trades(conn, account_id) if not t.is_skip and t.pnl_cents is not None]
    n = len(trades)
    wins = [t.pnl_cents for t in trades if t.pnl_cents > 0]
    losses = [t.pnl_cents for t in trades if t.pnl_cents < 0]
    return {
        "sample_size": n,
        "meaningful": n >= MIN_STAT_SAMPLE,
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "total_pnl_cents": sum(t.pnl_cents for t in trades),
        "win_rate": (len(wins) / n) if n >= MIN_STAT_SAMPLE else None,
        "avg_win_cents": round(sum(wins) / len(wins)) if wins else None,
        "avg_loss_cents": round(sum(losses) / len(losses)) if losses else None,
    }

