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
    onboarding_step TEXT
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


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
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
    "pending_limits_json, halted_session, onboarding_step"
)


def _row_to_user(row) -> User:
    (
        uid, chat_id, username, created_at, onboarded_at, risk_ack_at, tz, qh_start, qh_end,
        tier, is_admin, paused_until, pause_reason, locked_until, lock_reason,
        max_trades, max_loss, max_size, pending_json, halted_session, onboarding_step,
    ) = row
    return User(
        telegram_user_id=uid, chat_id=chat_id, username=username, created_at=created_at,
        onboarded_at=onboarded_at, risk_ack_at=risk_ack_at, timezone=tz,
        quiet_hours_start=qh_start, quiet_hours_end=qh_end, tier=tier, is_admin=bool(is_admin),
        paused_until=paused_until, pause_reason=pause_reason, locked_until=locked_until,
        lock_reason=lock_reason, max_trades_per_day=max_trades, max_daily_loss=max_loss,
        max_position_size=max_size, pending_limits=json.loads(pending_json), halted_session=halted_session,
        onboarding_step=onboarding_step,
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


@dataclass(frozen=True)
class Trade:
    id: str
    telegram_user_id: int
    detection_id: str | None
    symbol: str
    kind: str | None
    tier: str | None
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


_TRADE_COLUMNS = (
    "id, telegram_user_id, detection_id, symbol, kind, tier, alert_ts_utc, taken_at, "
    "reaction_seconds, after_no_trade, contracts, entry_price, exit_price, closed_at, "
    "pnl_pct, status, emotional_tag"
)


def _row_to_trade(row) -> Trade:
    (
        tid, uid, detection_id, symbol, kind, tier, alert_ts_utc, taken_at, reaction_seconds,
        after_no_trade, contracts, entry_price, exit_price, closed_at, pnl_pct, status, emotional_tag,
    ) = row
    return Trade(
        id=tid, telegram_user_id=uid, detection_id=detection_id, symbol=symbol, kind=kind, tier=tier,
        alert_ts_utc=alert_ts_utc, taken_at=taken_at, reaction_seconds=reaction_seconds,
        after_no_trade=bool(after_no_trade), contracts=contracts, entry_price=entry_price,
        exit_price=exit_price, closed_at=closed_at, pnl_pct=pnl_pct, status=status, emotional_tag=emotional_tag,
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
            (id, telegram_user_id, detection_id, symbol, kind, tier, alert_ts_utc, taken_at,
             reaction_seconds, after_no_trade, contracts, entry_price, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            trade_id, telegram_user_id, detection_id, symbol, kind, tier, alert_ts_utc,
            taken_at.isoformat(), reaction_seconds, int(after_no_trade), contracts, entry_price,
        ),
    )
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
    conn: sqlite3.Connection, trade_id: str, *, exit_price: float, closed_at: datetime, emotional_tag: str | None = None
) -> Trade:
    trade = get_trade(conn, trade_id)
    if trade is None:
        raise ValueError(f"no such trade: {trade_id}")
    pnl_pct = None
    if trade.entry_price:
        pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
    conn.execute(
        "UPDATE user_trades SET exit_price = ?, closed_at = ?, pnl_pct = ?, status = 'closed', "
        "emotional_tag = COALESCE(?, emotional_tag) WHERE id = ?",
        (exit_price, closed_at.isoformat(), pnl_pct, emotional_tag, trade_id),
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


def _win_rate(trades: list[Trade]) -> tuple[float, int] | None:
    closed = [t for t in trades if t.status == "closed" and t.pnl_pct is not None]
    if len(closed) < MIN_STAT_SAMPLE:
        return None
    wins = sum(1 for t in closed if t.pnl_pct > 0)
    return wins / len(closed), len(closed)


def personal_stats(conn: sqlite3.Connection, telegram_user_id: int) -> dict:
    trades = list_trades(conn, telegram_user_id)
    closed = [t for t in trades if t.status == "closed" and t.pnl_pct is not None]

    by_detector: dict[str, tuple[float, int] | None] = {}
    for kind in sorted({t.kind for t in trades if t.kind}):
        by_detector[kind] = _win_rate([t for t in trades if t.kind == kind])

    by_symbol: dict[str, tuple[float, int] | None] = {}
    for symbol in sorted({t.symbol for t in trades}):
        by_symbol[symbol] = _win_rate([t for t in trades if t.symbol == symbol])

    fast = [t for t in closed if t.reaction_seconds is not None and t.reaction_seconds <= 120]
    slow = [t for t in closed if t.reaction_seconds is not None and t.reaction_seconds > 120]
    fast_vs_slow = {"within_2min": _win_rate(fast), "later": _win_rate(slow)}

    after_no_trade = [t for t in closed if t.after_no_trade]
    normal = [t for t in closed if not t.after_no_trade]
    no_trade_comparison = {"after_no_trade": _win_rate(after_no_trade), "normal": _win_rate(normal)}

    pnl_by_tag: dict[str, tuple[float, int] | None] = {}
    for tag in sorted({t.emotional_tag for t in closed if t.emotional_tag}):
        tagged = [t for t in closed if t.emotional_tag == tag]
        if len(tagged) < MIN_STAT_SAMPLE:
            pnl_by_tag[tag] = None
        else:
            pnl_by_tag[tag] = (sum(t.pnl_pct for t in tagged) / len(tagged), len(tagged))

    # Adherence: of every alert this person responded to at all (took or
    # skipped — a real decision, logged), what fraction did they actually
    # log an outcome for (took -> closed, not left open forever, or
    # skipped)? A trade opened via /took and never closed isn't adherence,
    # it's an abandoned log entry.
    responded = conn.execute(
        "SELECT COUNT(*) FROM alert_responses WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()[0]
    dangling = sum(1 for t in trades if t.status == "open")
    adherence_score = None
    if responded >= MIN_STAT_SAMPLE:
        adherence_score = max(0.0, (responded - dangling) / responded)

    return {
        "overall": _win_rate(trades),
        "by_detector": by_detector,
        "by_symbol": by_symbol,
        "fast_vs_slow": fast_vs_slow,
        "no_trade_comparison": no_trade_comparison,
        "pnl_by_tag": pnl_by_tag,
        "adherence_score": adherence_score,
        "total_alerts_responded": responded,
        "open_trades": dangling,
    }

