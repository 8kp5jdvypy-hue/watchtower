"""Tests for tradebot.config's symbol liquidity classification."""
from __future__ import annotations

from tradebot.config import WATCHLIST, liquidity_class


def test_index_and_mega_cap_symbols_are_deep():
    for symbol in ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA"):
        assert liquidity_class(symbol) == "deep"


def test_high_beta_and_thin_symbols_default_to_thin():
    for symbol in ("BE", "IONQ", "SMCI", "COIN", "PLTR", "AMD", "USO"):
        assert liquidity_class(symbol) == "thin"


def test_every_watchlist_symbol_gets_a_classification():
    for symbol in WATCHLIST:
        assert liquidity_class(symbol) in ("deep", "thin")
