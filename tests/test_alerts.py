"""Tests for tradebot.alerts — AlertBudget decision logic and
TelegramAlerter delivery. Message rendering itself is covered by
tests/test_templates.py (golden-file tests) — this module is only
WHETHER/WHEN to send and HOW to deliver.

AlertBudget takes an injectable clock, so these tests never sleep or
depend on real time.

TelegramAlerter.send() only enqueues to the outbox now — it makes no
network call itself, so there's nothing here to mock at the requests
level. The retry/backoff/429-handling behavior that used to live in this
class moved to tradebot.telegram_bot.outbound (single-attempt HTTP,
tested in test_outbound.py) and tradebot.telegram_bot.worker (retry
scheduling, tested in test_worker.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.alerts import AlertBudget, Cluster, Decision, TelegramAlerter
from tradebot.telegram_bot import outbox


class FakeClock:
    def __init__(self, start: datetime):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


def _cluster(symbol="TSLA", kinds="gap", tier="high", score=5.0, cid="abc123") -> Cluster:
    return Cluster(
        id=cid,
        ts_utc="2026-07-23T13:35:00+00:00",
        session="2026-07-23",
        symbol=symbol,
        kinds=kinds,
        headlines="fake headline",
        primary_headline="fake headline",
        score=score,
        tier=tier,
        close=431.2,
        atr14=3.85,
        trend="up",
        code_version="f665fba",
    )


def test_high_tier_sends_up_to_the_daily_cap_then_notices_once_then_suppresses():
    clock = FakeClock(datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc))
    budget = AlertBudget(now=clock, max_high_per_day=8, cooldown_minutes=45)

    decisions = []
    for i in range(10):
        # distinct symbols so cooldown never interferes with this test
        decisions.append(budget.evaluate(_cluster(symbol=f"SYM{i}", cid=f"id{i}")))
        clock.advance(minutes=1)

    assert decisions[:8] == [Decision.SEND] * 8
    assert decisions[8] == Decision.CAP_REACHED_NOTICE
    assert decisions[9] == Decision.SUPPRESS_CAP


def test_cooldown_blocks_the_same_symbol_and_kind_within_the_window():
    clock = FakeClock(datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc))
    budget = AlertBudget(now=clock, cooldown_minutes=45)

    assert budget.evaluate(_cluster(symbol="TSLA", kinds="gap", cid="a")) == Decision.SEND

    clock.advance(minutes=10)
    assert budget.evaluate(_cluster(symbol="TSLA", kinds="gap", cid="b")) == Decision.SUPPRESS_COOLDOWN

    # different kind, same symbol — not covered by the gap cooldown
    assert budget.evaluate(_cluster(symbol="TSLA", kinds="level_break", cid="c")) == Decision.SEND

    # different symbol, same kind — not covered either
    assert budget.evaluate(_cluster(symbol="QQQ", kinds="gap", cid="d")) == Decision.SEND

    clock.advance(minutes=36)  # total 46 minutes since the first TSLA/gap send
    assert budget.evaluate(_cluster(symbol="TSLA", kinds="gap", cid="e")) == Decision.SEND


def test_medium_tier_is_queued_and_released_once_per_hour_boundary():
    clock = FakeClock(datetime(2026, 7, 23, 13, 5, tzinfo=timezone.utc))
    budget = AlertBudget(now=clock)

    assert budget.evaluate(_cluster(tier="medium", cid="m1")) == Decision.QUEUED_FOR_DIGEST
    assert budget.pop_medium_digest_if_due() is None  # still within the 13:00 hour slot

    clock.advance(minutes=20)
    assert budget.evaluate(_cluster(tier="medium", cid="m2")) == Decision.QUEUED_FOR_DIGEST
    assert budget.pop_medium_digest_if_due() is None  # still 13:xx

    clock.advance(minutes=40)  # now 14:05 — crossed the hour boundary
    digest = budget.pop_medium_digest_if_due()
    assert digest is not None
    assert [c.id for c in digest] == ["m1", "m2"]

    # queue is drained; calling again in the same hour slot returns None
    assert budget.pop_medium_digest_if_due() is None


def test_log_tier_is_queued_and_drained_on_demand():
    clock = FakeClock(datetime(2026, 7, 23, 13, 5, tzinfo=timezone.utc))
    budget = AlertBudget(now=clock)

    budget.evaluate(_cluster(tier="log", cid="l1"))
    budget.evaluate(_cluster(tier="log", cid="l2"))
    summary = budget.pop_log_summary()
    assert [c.id for c in summary] == ["l1", "l2"]
    assert budget.pop_log_summary() == []


def test_budget_resets_on_a_new_day():
    clock = FakeClock(datetime(2026, 7, 23, 23, 55, tzinfo=timezone.utc))
    budget = AlertBudget(now=clock, max_high_per_day=1)

    assert budget.evaluate(_cluster(symbol="TSLA", cid="a")) == Decision.SEND
    assert budget.evaluate(_cluster(symbol="QQQ", cid="b")) == Decision.CAP_REACHED_NOTICE

    clock.advance(hours=1)  # crosses midnight UTC
    assert budget.evaluate(_cluster(symbol="TSLA", cid="c")) == Decision.SEND


def test_telegram_alerter_enqueues_without_calling_the_network(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter(users_db_path=tmp_path / "users.db")

    alerter.send("hello", priority=outbox.PRIORITY_HIGH, alert_id="det1")

    row = alerter._outbox_conn().execute("SELECT chat_id, text, priority, status FROM outbox").fetchone()
    assert row == (12345, "hello", outbox.PRIORITY_HIGH, "pending")


def test_telegram_alerter_default_priority_is_normal(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter(users_db_path=tmp_path / "users.db")

    alerter.send("hello")

    priority = alerter._outbox_conn().execute("SELECT priority FROM outbox").fetchone()[0]
    assert priority == outbox.PRIORITY_NORMAL


def test_telegram_alerter_generates_an_alert_id_when_none_given(monkeypatch, tmp_path):
    """Digests, heartbeats, and system notices have no natural alert_id —
    each gets its own fresh id rather than colliding on an empty string."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter(users_db_path=tmp_path / "users.db")

    alerter.send("digest one")
    alerter.send("digest two")

    rows = alerter._outbox_conn().execute("SELECT alert_id, text FROM outbox ORDER BY text").fetchall()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]  # distinct alert_ids -> both actually got enqueued, not deduped


def test_telegram_alerter_reuses_the_same_connection_across_sends(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter(users_db_path=tmp_path / "users.db")

    alerter.send("one", alert_id="a1")
    conn_after_first = alerter._users_conn
    alerter.send("two", alert_id="a2")

    assert alerter._users_conn is conn_after_first  # lazy-opened once, not reopened per send
