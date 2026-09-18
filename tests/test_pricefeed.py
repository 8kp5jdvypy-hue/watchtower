"""PriceFeed fallback, caching, stale-serve and cooldown, with fake
sources and an injected clock -- no network, no sleeping."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradebot import pricefeed
from tradebot.marketdata import PricePoint
from tradebot.pricefeed import Instrument, PriceFeed, crypto, equity

T0 = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, **kwargs):
        self.now += timedelta(**kwargs)


class FakeSource:
    """`prices` maps symbol->price; `fail` raises instead. Records every
    batch it was asked for."""

    def __init__(self, name, asset_class, prices=None, fail=False):
        self.name, self.asset_class = name, asset_class
        self.prices = dict(prices or {})
        self.fail = fail
        self.batches = []

    def fetch(self, instruments):
        self.batches.append([i.symbol for i in instruments])
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return {
            i.symbol: PricePoint(i.symbol, self.prices[i.symbol], T0, self.name, self.asset_class)
            for i in instruments if i.symbol in self.prices
        }


INSTRUMENTS = (crypto("BTC"), crypto("ETH"), equity("SPY"), equity("QQQ"))


def _feed(sources, clock, **kw):
    return PriceFeed(sources=sources, instruments=INSTRUMENTS, now_fn=clock, **kw)


def test_instrument_helpers_spell_vendor_ids():
    btc, sol, spy = crypto("BTC"), crypto("SOL"), equity("SPY")
    assert (btc.coinbase_product, btc.kraken_pair) == ("BTC-USD", "XBTUSD")
    assert (sol.coinbase_product, sol.kraken_pair) == ("SOL-USD", "SOLUSD")
    assert (spy.alpaca_symbol, spy.finnhub_symbol, spy.coinbase_product) == ("SPY", "SPY", None)


def test_primary_serves_and_secondary_is_not_called():
    clock = Clock()
    cb = FakeSource("coinbase", "crypto", {"BTC": 64000.0, "ETH": 2500.0})
    kr = FakeSource("kraken", "crypto", {"BTC": 64001.0, "ETH": 2501.0})
    al = FakeSource("alpaca_iex", "equity", {"SPY": 512.0, "QQQ": 440.0})
    feed = _feed([cb, kr, al], clock)
    out = feed.get()
    assert {s: (p.price, p.source) for s, p in out.items()} == {
        "BTC": (64000.0, "coinbase"), "ETH": (2500.0, "coinbase"),
        "SPY": (512.0, "alpaca_iex"), "QQQ": (440.0, "alpaca_iex"),
    }
    assert kr.batches == []
    assert feed.stats["coinbase"].calls == 1 and feed.stats["coinbase"].served == 2


def test_failure_falls_back_and_only_missing_symbols_move_on():
    clock = Clock()
    cb = FakeSource("coinbase", "crypto", {"BTC": 64000.0}, fail=False)  # has BTC, not ETH
    kr = FakeSource("kraken", "crypto", {"BTC": 1.0, "ETH": 2500.0})
    al = FakeSource("alpaca_iex", "equity", fail=True)
    fh = FakeSource("finnhub", "equity", {"SPY": 512.0, "QQQ": 440.0})
    feed = _feed([cb, kr, al, fh], clock)
    out = feed.get()
    assert out["BTC"].source == "coinbase" and out["ETH"].source == "kraken"
    assert kr.batches == [["ETH"]]                    # partial: only the miss moved on
    assert out["SPY"].source == "finnhub" and out["QQQ"].source == "finnhub"
    assert feed.stats["alpaca_iex"].failures == 1
    assert all(not p.stale for p in out.values())


def test_cache_serves_within_ttl_then_refetches():
    clock = Clock()
    cb = FakeSource("coinbase", "crypto", {"BTC": 64000.0, "ETH": 2500.0})
    feed = PriceFeed(sources=[cb], instruments=(crypto("BTC"), crypto("ETH")), now_fn=clock,
                     ttl=timedelta(minutes=5))
    feed.get(["BTC"])
    clock.tick(minutes=4)
    feed.get(["BTC"])
    assert cb.batches == [["BTC"]]
    clock.tick(minutes=1, seconds=1)
    feed.get(["BTC"])
    assert cb.batches == [["BTC"], ["BTC"]]


def test_all_sources_down_serves_stale_flagged_then_nothing():
    clock = Clock()
    cb = FakeSource("coinbase", "crypto", {"BTC": 64000.0})
    kr = FakeSource("kraken", "crypto", {"BTC": 64000.0})
    feed = PriceFeed(sources=[cb, kr], instruments=(crypto("BTC"),), now_fn=clock,
                     ttl=timedelta(minutes=5), max_stale=timedelta(minutes=30), cooldown=timedelta(0))
    assert feed.get()["BTC"].stale is False
    cb.fail = kr.fail = True
    clock.tick(minutes=6)
    stale = feed.get()["BTC"]
    assert stale.stale is True and stale.price == 64000.0 and stale.source == "coinbase"
    clock.tick(minutes=30)
    assert feed.get() == {}                           # past max_stale: absent, never invented


def test_failed_source_is_skipped_during_cooldown_then_retried():
    clock = Clock()
    cb = FakeSource("coinbase", "crypto", {"BTC": 64000.0}, fail=True)
    kr = FakeSource("kraken", "crypto", {"BTC": 64001.0})
    feed = PriceFeed(sources=[cb, kr], instruments=(crypto("BTC"),), now_fn=clock,
                     ttl=timedelta(0), cooldown=timedelta(minutes=2))
    feed.get()
    feed.get()                                        # within cooldown: coinbase not touched
    assert len(cb.batches) == 1 and feed.stats["coinbase"].calls == 1
    clock.tick(minutes=2)
    cb.fail = False
    assert feed.get()["BTC"].source == "coinbase"
    assert len(cb.batches) == 2


def test_unknown_symbol_is_rejected_loudly():
    feed = _feed([FakeSource("coinbase", "crypto")], Clock())
    with pytest.raises(KeyError, match="DOGE"):
        feed.get(["DOGE"])


def test_socialcrawl_is_not_a_source():
    names = {s.name for s in pricefeed.default_sources()}
    assert "socialcrawl" not in names and not any("crawl" in n for n in names)
    assert names >= {"coinbase", "kraken", "alpaca_iex"}


def test_finnhub_source_registered_only_with_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert "finnhub" not in {s.name for s in pricefeed.default_sources()}
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    assert "finnhub" in {s.name for s in pricefeed.default_sources()}


def test_default_instruments_are_small_and_typed():
    by_class = {}
    for inst in pricefeed.DEFAULT_INSTRUMENTS:
        by_class.setdefault(inst.asset_class, []).append(inst.symbol)
    assert by_class == {"crypto": ["BTC", "ETH", "SOL"],
                        "equity": ["SPY", "QQQ", "GOOGL", "TSLA", "BE", "IONQ"]}
    assert len(pricefeed.DEFAULT_INSTRUMENTS) <= 10   # the cost note's budget assumption
