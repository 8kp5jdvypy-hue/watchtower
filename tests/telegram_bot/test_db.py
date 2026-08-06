"""Tests for tradebot.telegram_bot.db — the per-user account store."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradebot.telegram_bot import db


def _conn():
    return db.connect(":memory:")


def test_get_or_create_user_is_idempotent_and_never_wipes_settings():
    conn = _conn()
    db.get_or_create_user(conn, 1, 100, "alice")
    db.set_timezone(conn, 1, "America/Chicago")
    db.mark_onboarded(conn, 1, datetime.now(timezone.utc))

    # re-running with a different chat_id/username (e.g. they changed their
    # Telegram @handle) must update those but never touch onboarding state
    user = db.get_or_create_user(conn, 1, 200, "alice2")
    assert user.chat_id == 200
    assert user.username == "alice2"
    assert user.timezone == "America/Chicago"
    assert user.is_onboarded


def test_pause_and_lock_state():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)

    user = db.get_user(conn, 1)
    assert not user.is_paused(now) and not user.is_locked(now)

    db.set_pause(conn, 1, now + timedelta(hours=1), "user-requested")
    user = db.get_user(conn, 1)
    assert user.is_paused(now)
    assert not user.is_paused(now + timedelta(hours=2))

    db.clear_pause(conn, 1)
    assert not db.get_user(conn, 1).is_paused(now)

    db.set_lock(conn, 1, now + timedelta(days=1), "daily loss limit hit")
    user = db.get_user(conn, 1)
    assert user.is_locked(now)
    assert user.lock_reason == "daily loss limit hit"


def test_session_halt_is_scoped_to_one_session_date():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    today = date(2026, 7, 23)
    db.set_session_halt(conn, 1, today)
    user = db.get_user(conn, 1)
    assert user.is_halted_for_session(today)
    assert not user.is_halted_for_session(date(2026, 7, 24))

    db.clear_session_halt(conn, 1)
    assert not db.get_user(conn, 1).is_halted_for_session(today)


def test_limit_decrease_applies_immediately_even_during_market_hours():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    db.apply_limit_change(conn, 1, "max_trades_per_day", 5, now=now, market_is_open=True)
    result = db.apply_limit_change(conn, 1, "max_trades_per_day", 2, now=now, market_is_open=True)
    assert result == "applied"
    assert db.get_user(conn, 1).max_trades_per_day == 2


def test_limit_increase_during_market_hours_is_queued_not_applied():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    db.apply_limit_change(conn, 1, "max_trades_per_day", 5, now=now, market_is_open=False)

    result = db.apply_limit_change(conn, 1, "max_trades_per_day", 10, now=now, market_is_open=True)
    assert result == "queued"

    user = db.get_user(conn, 1)
    assert user.max_trades_per_day == 5  # unchanged
    assert user.pending_limits == [{"field": "max_trades_per_day", "value": 10, "queued_at": now.isoformat()}]


def test_limit_increase_outside_market_hours_applies_immediately():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    db.apply_limit_change(conn, 1, "max_trades_per_day", 5, now=now, market_is_open=False)
    result = db.apply_limit_change(conn, 1, "max_trades_per_day", 10, now=now, market_is_open=False)
    assert result == "applied"
    assert db.get_user(conn, 1).max_trades_per_day == 10


def test_pending_limit_increase_applies_at_the_next_session_not_the_same_one():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    queued_at = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    db.apply_limit_change(conn, 1, "max_trades_per_day", 5, now=queued_at, market_is_open=False)
    db.apply_limit_change(conn, 1, "max_trades_per_day", 10, now=queued_at, market_is_open=True)

    # same session (later that day) — must NOT apply yet
    same_session = db.apply_pending_limits_if_due(conn, 1, date(2026, 7, 23))
    assert same_session == []
    assert db.get_user(conn, 1).max_trades_per_day == 5

    # next session — now it applies
    next_session = db.apply_pending_limits_if_due(conn, 1, date(2026, 7, 24))
    assert len(next_session) == 1 and next_session[0]["value"] == 10
    assert db.get_user(conn, 1).max_trades_per_day == 10
    assert db.get_user(conn, 1).pending_limits == []


def test_watchlist_default_is_none_until_customized():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    assert db.get_watchlist(conn, 1) is None
    db.set_watchlist(conn, 1, ["TSLA", "NVDA"])
    assert db.get_watchlist(conn, 1) == ["NVDA", "TSLA"]


def test_toggle_watchlist_symbol():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    added = db.toggle_watchlist_symbol(conn, 1, "TSLA")
    assert added is True
    assert db.get_watchlist(conn, 1) == ["TSLA"]
    removed = db.toggle_watchlist_symbol(conn, 1, "TSLA")
    assert removed is False
    assert db.get_watchlist(conn, 1) is None


def test_list_subscribers_for_symbol_excludes_unonboarded_paused_locked_and_halted():
    conn = _conn()
    now = datetime.now(timezone.utc)
    today = now.date()

    db.get_or_create_user(conn, 1, 1, "eligible")
    db.mark_onboarded(conn, 1, now)
    db.set_risk_ack(conn, 1, now)

    db.get_or_create_user(conn, 2, 2, "not_onboarded")
    db.set_risk_ack(conn, 2, now)

    db.get_or_create_user(conn, 3, 3, "paused")
    db.mark_onboarded(conn, 3, now)
    db.set_risk_ack(conn, 3, now)
    db.set_pause(conn, 3, now + timedelta(hours=1), "x")

    db.get_or_create_user(conn, 4, 4, "locked")
    db.mark_onboarded(conn, 4, now)
    db.set_risk_ack(conn, 4, now)
    db.set_lock(conn, 4, now + timedelta(hours=1), "loss limit")

    db.get_or_create_user(conn, 5, 5, "halted")
    db.mark_onboarded(conn, 5, now)
    db.set_risk_ack(conn, 5, now)
    db.set_session_halt(conn, 5, today)

    db.get_or_create_user(conn, 6, 6, "wrong_watchlist")
    db.mark_onboarded(conn, 6, now)
    db.set_risk_ack(conn, 6, now)
    db.set_watchlist(conn, 6, ["QQQ"])

    subs = db.list_subscribers_for_symbol(conn, "TSLA", today, now, default_watchlist=["TSLA", "SPY"])
    assert [u.telegram_user_id for u in subs] == [1]


def test_log_took_computes_reaction_seconds_and_log_closed_computes_pnl():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    alert_ts = datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc)
    taken_at = alert_ts + timedelta(seconds=45)
    trade = db.log_took(
        conn, 1, detection_id="d1", symbol="TSLA", kind="gap", tier="high",
        alert_ts_utc=alert_ts.isoformat(), taken_at=taken_at, contracts=2, entry_price=5.00,
    )
    assert trade.reaction_seconds == 45.0
    assert trade.status == "open"

    closed = db.log_closed(conn, trade.id, exit_price=5.50, closed_at=taken_at + timedelta(minutes=20))
    assert closed.status == "closed"
    assert closed.pnl_pct == 10.0


def test_personal_stats_never_reports_a_rate_below_min_sample():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    for i in range(db.MIN_STAT_SAMPLE - 1):
        trade = db.log_took(conn, 1, detection_id=f"d{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=now)
    stats = db.personal_stats(conn, 1)
    assert stats["overall"] is None  # one short of MIN_STAT_SAMPLE


def test_personal_stats_reports_once_min_sample_is_met():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    for i in range(db.MIN_STAT_SAMPLE):
        trade = db.log_took(conn, 1, detection_id=f"d{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=now)
    stats = db.personal_stats(conn, 1)
    win_rate, n = stats["overall"]
    assert n == db.MIN_STAT_SAMPLE and win_rate == 1.0


