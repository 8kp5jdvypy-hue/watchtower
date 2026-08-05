"""Tests for tradebot.alerts — format_alert() and the AlertBudget logic.

AlertBudget takes an injectable clock, so these tests never sleep or
depend on real time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot.alerts import AlertBudget, Cluster, Decision, format_alert
from tradebot.costs import Breakeven
from tradebot.detectors import DailyAnchors
from tradebot.marketdata import OptionContract, Quote


def _anchors() -> DailyAnchors:
    return DailyAnchors(
        symbol="TSLA",
        session_date=datetime(2026, 7, 23).date(),
        prior_close=425.5,
        prior_high=427.0,
        prior_low=424.0,
        opening_range_high=430.0,
        opening_range_low=428.0,
        opening_range_volume=100_000,
        avg_cum_volume_by_bar={},
    )


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
        score=score,
        tier=tier,
        close=431.2,
        atr14=3.85,
        trend="up",
        code_version="f665fba",
    )


def _breakeven() -> Breakeven:
    contract = OptionContract(
        symbol="TSLA260731C00430000", expiry=datetime(2026, 7, 31).date(), strike=430.0,
        right="call", bid=5.0, ask=5.2, last=5.1, delta=0.5, theta=-0.3, open_interest=1200,
    )
    return Breakeven(pct=0.0085, atr_units=1.23, contract=contract)


def test_format_alert_matches_the_scanner_plan_layout():
    cluster = _cluster(kinds="gap,rvol_spike")
    quote = Quote(symbol="TSLA", ts=datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), bid=431.1, ask=431.3, last=431.22)
    text = format_alert(cluster, anchors=_anchors(), quote=quote, breakeven=_breakeven())
    lines = text.split("\n")
    assert lines[0] == "[HIGH] TSLA — gap, rvol_spike"
    assert lines[1] == "fake headline"
    assert lines[2] == "score 5.00 ATR | close 431.20 | ATR14 3.85"
    assert lines[3] == "breakeven 0.85% (1.23 ATR) for 60m hold"
    assert lines[4] == "range 428.00-430.00 | prior close 425.50"
    assert lines[5] == "quote 431.10/431.30 (last 431.22)"
    assert lines[6] == "2026-07-23 09:35 ET"
    assert lines[7] == "id abc123 | vf665fba"


def test_format_alert_handles_missing_atr():
    cluster = _cluster()
    cluster = Cluster(**{**cluster.__dict__, "atr14": None})
    quote = Quote(symbol="TSLA", ts=datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), bid=1, ask=1, last=1)
    text = format_alert(cluster, anchors=_anchors(), quote=quote, breakeven=_breakeven())
    assert "ATR14 n/a" in text


def test_format_alert_shows_no_tradable_contract_when_breakeven_is_none():
    cluster = _cluster()
    quote = Quote(symbol="TSLA", ts=datetime(2026, 7, 23, 13, 35, tzinfo=timezone.utc), bid=1, ask=1, last=1)
    text = format_alert(cluster, anchors=_anchors(), quote=quote, breakeven=None)
    assert "breakeven no tradable contract for 60m hold" in text


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
