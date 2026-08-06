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

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "users.db"

MIN_STAT_SAMPLE = 5  # same floor as tradebot.journal.MIN_HISTORY_SAMPLE — never report a rate on fewer

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
    tier TEXT NOT NULL DEFAULT 'free',
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
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    @property
    def is_onboarded(self) -> bool:
        return self.onboarded_at is not None

    @property
    def has_risk_ack(self) -> bool:
        return self.risk_ack_at is not None

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < datetime.fromisoformat(self.paused_until)

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and now < datetime.fromisoformat(self.locked_until)

    def is_halted_for_session(self, session_date: date) -> bool:
        return self.halted_session == session_date.isoformat()


_USER_COLUMNS = (
    "telegram_user_id, chat_id, username, created_at, onboarded_at, risk_ack_at, timezone, "
    "quiet_hours_start, quiet_hours_end, tier, is_admin, paused_until, pause_reason, "
    "locked_until, lock_reason, max_trades_per_day, max_daily_loss, max_position_size, "
    "pending_limits_json, halted_session, onboarding_step, account_size, risk_per_trade_pct"
)


def _row_to_user(row) -> User:
    (
        uid, chat_id, username, created_at, onboarded_at, risk_ack_at, tz, qh_start, qh_end,
        tier, is_admin, paused_until, pause_reason, locked_until, lock_reason,
        max_trades, max_loss, max_size, pending_json, halted_session, onboarding_step,
        account_size, risk_per_trade_pct,
    ) = row
    return User(
        telegram_user_id=uid, chat_id=chat_id, username=username, created_at=created_at,
        onboarded_at=onboarded_at, risk_ack_at=risk_ack_at, timezone=tz,
        quiet_hours_start=qh_start, quiet_hours_end=qh_end, tier=tier, is_admin=bool(is_admin),
        paused_until=paused_until, pause_reason=pause_reason, locked_until=locked_until,
        lock_reason=lock_reason, max_trades_per_day=max_trades, max_daily_loss=max_loss,
        max_position_size=max_size, pending_limits=json.loads(pending_json), halted_session=halted_session,
        onboarding_step=onboarding_step, account_size=account_size, risk_per_trade_pct=risk_per_trade_pct,
    )


def get_user(conn: sqlite3.Connection, telegram_user_id: int) -> User | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    return _row_to_user(row) if row else None


def get_or_create_user(conn: sqlite3.Connection, telegram_user_id: int, chat_id: int, username: str | None) -> User:
    """Idempotent — see /start requirement: re-running never wipes
    existing settings. Only touches chat_id/username on an existing row
    (they can legitimately change), never resets onboarding state."""
    conn.execute(
        "INSERT INTO users (telegram_user_id, chat_id, username, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(telegram_user_id) DO UPDATE SET chat_id=excluded.chat_id, username=excluded.username",
        (telegram_user_id, chat_id, username, _now_iso()),
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
        watchlist = get_watchlist(conn, user.telegram_user_id) or default_watchlist
        if symbol in watchlist:
            subscribers.append(user)
    return subscribers


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


def personal_stats(conn: sqlite3.Connection, telegram_user_id: int) -> dict:
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

