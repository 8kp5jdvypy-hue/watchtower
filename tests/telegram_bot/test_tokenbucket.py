"""Tests for tradebot.telegram_bot.tokenbucket — a fake clock throughout,
never real time.sleep, so these run instantly regardless of the rates
being tested (see the 5,000-chat load test, which depends on this same
injectable-clock design)."""
from __future__ import annotations

from tradebot.telegram_bot.tokenbucket import TokenBucket


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_starts_full():
    clock = FakeClock()
    bucket = TokenBucket(capacity=10, refill_per_second=1, now_fn=clock)
    assert bucket.try_consume(10) is True
    assert bucket.try_consume(1) is False  # exhausted


def test_refills_over_time_not_instantly():
    clock = FakeClock()
    bucket = TokenBucket(capacity=5, refill_per_second=1, now_fn=clock)
    for _ in range(5):
        assert bucket.try_consume() is True
    assert bucket.try_consume() is False

    clock.advance(2.5)
    assert bucket.try_consume() is True  # 2.5 tokens refilled
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False  # only 2 available, third fails


def test_never_refills_past_capacity():
    clock = FakeClock()
    bucket = TokenBucket(capacity=3, refill_per_second=10, now_fn=clock)
    bucket.try_consume(3)
    clock.advance(1000)  # a huge amount of elapsed time
    assert bucket.tokens == 0  # not refilled yet — refill happens lazily on next check
    assert bucket.try_consume(3) is True
    assert bucket.try_consume(0.001) is False  # capped at 3, not 10000


def test_seconds_until_available_is_zero_when_ready():
    clock = FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_second=1, now_fn=clock)
    assert bucket.seconds_until_available() == 0.0


def test_seconds_until_available_reflects_the_real_wait():
    clock = FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_second=2, now_fn=clock)  # 0.5s per token
    bucket.try_consume()
    assert bucket.seconds_until_available() == 0.5


def test_seconds_until_available_does_not_consume_a_token():
    clock = FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_second=1, now_fn=clock)
    bucket.seconds_until_available()
    bucket.seconds_until_available()
    assert bucket.try_consume() is True  # still full — checking availability isn't consuming


def test_refund_gives_back_a_speculatively_consumed_token():
    clock = FakeClock()
    bucket = TokenBucket(capacity=5, refill_per_second=1, now_fn=clock)
    bucket.try_consume(5)
    assert bucket.try_consume() is False
    bucket.refund()
    assert bucket.try_consume() is True


def test_refund_never_exceeds_capacity():
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_second=1, now_fn=clock)
    bucket.refund(100)
    assert bucket.tokens == 2
