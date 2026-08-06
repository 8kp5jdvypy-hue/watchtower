"""Tests for tradebot.telegram_bot.db — the per-user account store."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

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


def test_set_sizing_field_applies_immediately_regardless_of_market_hours():
    """Unlike LIMIT_FIELDS, account_size/risk_per_trade_pct are sizing
    inputs, not protective caps — no queueing, no market-hours check."""
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    db.set_sizing_field(conn, 1, "account_size", 10_000)
    db.set_sizing_field(conn, 1, "risk_per_trade_pct", 1.5)
    user = db.get_user(conn, 1)
    assert user.account_size == 10_000
    assert user.risk_per_trade_pct == 1.5
    assert user.pending_limits == []  # never touches the queueing mechanism


def test_set_sizing_field_rejects_an_unknown_field():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    with pytest.raises(ValueError):
        db.set_sizing_field(conn, 1, "max_trades_per_day", 5)  # a LIMIT_FIELDS field, not a sizing field


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


def test_list_subscribers_for_symbol_excludes_telegram_unreachable_users():
    """Set by the outbox worker on a Forbidden/ChatNotFound response —
    see db.mark_telegram_unreachable. No point enqueueing a future alert
    for a chat that's already confirmed unreachable."""
    conn = _conn()
    now = datetime.now(timezone.utc)
    db.get_or_create_user(conn, 1, 1, "blocked_the_bot")
    db.mark_onboarded(conn, 1, now)
    db.set_risk_ack(conn, 1, now)
    db.mark_telegram_unreachable(conn, 1, now)

    subs = db.list_subscribers_for_symbol(conn, "TSLA", now.date(), now, default_watchlist=["TSLA"])
    assert subs == []
    assert db.get_user(conn, 1).is_telegram_unreachable is True


def test_get_or_create_user_clears_telegram_unreachable_on_a_new_update():
    """A real update arriving FROM the chat (e.g. /start after unblocking
    the bot) is concrete proof the chat is reachable again — the one
    thing that can undo mark_telegram_unreachable's terminal marking."""
    conn = _conn()
    now = datetime.now(timezone.utc)
    db.get_or_create_user(conn, 1, 1, "alice")
    db.mark_telegram_unreachable(conn, 1, now)
    assert db.get_user(conn, 1).is_telegram_unreachable is True

    db.get_or_create_user(conn, 1, 1, "alice")  # dispatcher calls this on every incoming update
    assert db.get_user(conn, 1).is_telegram_unreachable is False


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
    overall = stats["overall"]
    assert overall.n == db.MIN_STAT_SAMPLE and overall.win_rate == 1.0
    assert overall.avg_pnl_pct == pytest.approx(10.0)


def test_log_took_auto_fills_direction_from_the_alert():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    trade = db.log_took(conn, 1, detection_id="d1", symbol="TSLA", kind="gap", tier="high", direction="up",
                         alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
    assert trade.direction == "up"


def test_set_trade_mood_rejects_an_unknown_choice():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    trade = db.log_took(conn, 1, detection_id="d1", symbol="TSLA", kind="gap", tier="high",
                         alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
    with pytest.raises(ValueError):
        db.set_trade_mood(conn, trade.id, "ecstatic")


def test_set_trade_mood_sets_emotional_tag_and_last_tap_wins():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    trade = db.log_took(conn, 1, detection_id="d1", symbol="TSLA", kind="gap", tier="high",
                         alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
    db.set_trade_mood(conn, trade.id, "calm")
    db.set_trade_mood(conn, trade.id, "fomo")  # a second tap overrides the first
    assert db.get_trade(conn, trade.id).emotional_tag == "fomo"


def test_log_closed_note_is_independent_of_mood():
    """The free-text note (set at /closed) and the fixed-vocabulary mood
    (set via set_trade_mood at entry) are separate columns — closing with
    a note must never touch emotional_tag, and vice versa."""
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    trade = db.log_took(conn, 1, detection_id="d1", symbol="TSLA", kind="gap", tier="high",
                         alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
    db.set_trade_mood(conn, trade.id, "calm")
    closed = db.log_closed(conn, trade.id, exit_price=5.5, closed_at=now, note="took half size")
    assert closed.emotional_tag == "calm"
    assert closed.note == "took half size"


def test_personal_stats_by_direction_and_hold_time():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    base = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    # 5 bullish, all winners, closed 10 minutes later (< 15m bucket)
    for i in range(db.MIN_STAT_SAMPLE):
        taken_at = base + timedelta(minutes=i)
        trade = db.log_took(conn, 1, detection_id=f"up{i}", symbol="TSLA", kind="gap", tier="high", direction="up",
                             alert_ts_utc=taken_at.isoformat(), taken_at=taken_at, entry_price=5.0)
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=taken_at + timedelta(minutes=10))
    stats = db.personal_stats(conn, 1)
    assert stats["by_direction"]["bullish"].n == db.MIN_STAT_SAMPLE
    assert stats["by_direction"]["bullish"].win_rate == 1.0
    assert stats["by_direction"]["bearish"] is None  # no bearish trades logged at all
    assert stats["by_hold_time"]["under 15m"].n == db.MIN_STAT_SAMPLE
    assert stats["by_hold_time"]["over 4h"] is None


def test_personal_stats_adherence_score_matches_high_alerts_inside_the_rules():
    """Adherence = fraction of trades that came from a real HIGH alert and
    weren't a NO TRADE override — NOT the old 'did you log an outcome'
    metric (that's logging_completeness now)."""
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    # 3 "inside the rules": real HIGH alert, no override
    for i in range(3):
        db.log_took(conn, 1, detection_id=f"hi{i}", symbol="TSLA", kind="gap", tier="high",
                    alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
    # 1 improvised: no detection_id at all (freeform trade)
    db.log_took(conn, 1, detection_id=None, symbol="TSLA", kind=None, tier=None,
                taken_at=now, entry_price=5.0)
    # 1 improvised: took a MEDIUM alert as if it were actionable
    db.log_took(conn, 1, detection_id="med1", symbol="TSLA", kind="gap", tier="medium",
                alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
    # 1 improvised: HIGH alert but overrode an explicit NO TRADE
    db.log_took(conn, 1, detection_id="nt1", symbol="TSLA", kind="gap", tier="high",
                alert_ts_utc=now.isoformat(), taken_at=now, after_no_trade=True, entry_price=5.0)

    stats = db.personal_stats(conn, 1)
    assert stats["total_trades"] == 6
    assert stats["adherence_score"] == pytest.approx(3 / 6)


def test_personal_stats_logging_completeness_is_distinct_from_adherence():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime.now(timezone.utc)
    for i in range(db.MIN_STAT_SAMPLE):
        db.record_alert_response(conn, 1, f"d{i}", "took", now)
        trade = db.log_took(conn, 1, detection_id=f"d{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
        if i < 3:
            db.log_closed(conn, trade.id, exit_price=5.5, closed_at=now)
    stats = db.personal_stats(conn, 1)
    assert stats["logging_completeness"] == pytest.approx((db.MIN_STAT_SAMPLE - 2) / db.MIN_STAT_SAMPLE)
    assert stats["open_trades"] == 2


def test_monthly_recap_returns_none_below_min_sample():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    for i in range(db.MIN_STAT_SAMPLE - 1):
        trade = db.log_took(conn, 1, detection_id=f"d{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=now)
    assert db.monthly_recap(conn, 1, 2026, 7) is None


def test_monthly_recap_surfaces_the_worst_bucket_as_a_leak():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    # 5 calm winners, +10% each
    for i in range(db.MIN_STAT_SAMPLE):
        trade = db.log_took(conn, 1, detection_id=f"calm{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
        db.set_trade_mood(conn, trade.id, "calm")
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=now)
    # 5 revenge losers, -10% each — the leak
    for i in range(db.MIN_STAT_SAMPLE):
        trade = db.log_took(conn, 1, detection_id=f"rev{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=now.isoformat(), taken_at=now, entry_price=5.0)
        db.set_trade_mood(conn, trade.id, "revenge")
        db.log_closed(conn, trade.id, exit_price=4.5, closed_at=now)

    recap = db.monthly_recap(conn, 1, 2026, 7)
    assert recap is not None
    assert recap["trade_count"] == 10
    assert recap["overall_avg_pnl_pct"] == pytest.approx(0.0)
    assert any("revenge" in leak["label"] for leak in recap["leaks"])
    assert not any("calm" in leak["label"] for leak in recap["leaks"])  # calm beat the average, not a leak


def test_monthly_recap_scopes_to_the_requested_calendar_month():
    conn = _conn()
    db.get_or_create_user(conn, 1, 1, "a")
    july = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    august = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)
    for i in range(db.MIN_STAT_SAMPLE):
        trade = db.log_took(conn, 1, detection_id=f"jul{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=july.isoformat(), taken_at=july, entry_price=5.0)
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=july)
    for i in range(db.MIN_STAT_SAMPLE - 1):  # not enough for a real recap
        trade = db.log_took(conn, 1, detection_id=f"aug{i}", symbol="TSLA", kind="gap", tier="high",
                             alert_ts_utc=august.isoformat(), taken_at=august, entry_price=5.0)
        db.log_closed(conn, trade.id, exit_price=5.5, closed_at=august)

    assert db.monthly_recap(conn, 1, 2026, 7)["trade_count"] == db.MIN_STAT_SAMPLE
    assert db.monthly_recap(conn, 1, 2026, 8) is None


