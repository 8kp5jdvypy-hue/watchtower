"""Tests for tradebot.alerts — AlertBudget decision logic and
TelegramAlerter delivery. Message rendering itself is covered by
tests/test_templates.py (golden-file tests) — this module is only
WHETHER/WHEN to send and HOW to deliver.

AlertBudget takes an injectable clock, so these tests never sleep or
depend on real time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from tradebot.alerts import AlertBudget, Cluster, Decision, TelegramAlerter


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


def test_telegram_alerter_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter()

    rate_limited = MagicMock(status_code=429)
    rate_limited.json.return_value = {"parameters": {"retry_after": 0.01}}
    ok = MagicMock(status_code=200)

    with patch("tradebot.alerts.requests.post", side_effect=[rate_limited, ok]) as mock_post, \
         patch("tradebot.alerts.time.sleep") as mock_sleep:
        alerter.send("hello")

    assert mock_post.call_count == 2
    mock_sleep.assert_called_once_with(0.01)  # honored Telegram's own retry_after hint
    ok.raise_for_status.assert_called_once()
    rate_limited.raise_for_status.assert_not_called()  # never raised on the retryable attempt


def test_telegram_alerter_sends_html_parse_mode(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter()

    ok = MagicMock(status_code=200)
    with patch("tradebot.alerts.requests.post", return_value=ok) as mock_post:
        alerter.send("<b>hi</b>")

    assert mock_post.call_args.kwargs["json"]["parse_mode"] == "HTML"


def test_telegram_alerter_raises_after_max_retries_exhausted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter()

    rate_limited = MagicMock(status_code=429)
    rate_limited.json.return_value = {"parameters": {"retry_after": 0.01}}
    rate_limited.raise_for_status.side_effect = requests.exceptions.HTTPError("429")

    with patch("tradebot.alerts.requests.post", return_value=rate_limited) as mock_post, \
         patch("tradebot.alerts.time.sleep"):
        with pytest.raises(requests.exceptions.HTTPError):
            alerter.send("hello", max_retries=3)

    assert mock_post.call_count == 3


def test_telegram_alerter_retries_on_network_timeout_then_succeeds(monkeypatch):
    """Real failure this caught: a transient ReadTimeout during a live
    send used to propagate straight up and crash the whole runner."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter()

    ok = MagicMock(status_code=200)

    with patch(
        "tradebot.alerts.requests.post",
        side_effect=[requests.exceptions.ReadTimeout("timed out"), ok],
    ) as mock_post, patch("tradebot.alerts.time.sleep") as mock_sleep:
        alerter.send("hello")

    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()  # backed off before retrying
    ok.raise_for_status.assert_called_once()


def test_telegram_alerter_raises_after_network_timeouts_exhaust_retries(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerter = TelegramAlerter()

    with patch(
        "tradebot.alerts.requests.post", side_effect=requests.exceptions.ReadTimeout("timed out")
    ) as mock_post, patch("tradebot.alerts.time.sleep"):
        with pytest.raises(requests.exceptions.ReadTimeout):
            alerter.send("hello", max_retries=3)

    assert mock_post.call_count == 3
