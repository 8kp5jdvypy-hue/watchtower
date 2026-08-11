"""Tests for tradebot.vendors.alpaca -- limited to the parts that don't
need a real Alpaca client (DETECTOR_DATA_FEED resolution). Everything
else in this module talks to the real Alpaca SDK by design (see its own
module docstring) and isn't unit tested here.
"""
from __future__ import annotations

import pytest
from alpaca.data.enums import DataFeed

from tradebot.vendors.alpaca import _resolve_detector_data_feed


def test_detector_data_feed_defaults_to_iex(monkeypatch):
    monkeypatch.delenv("DETECTOR_DATA_FEED", raising=False)
    assert _resolve_detector_data_feed() == DataFeed.IEX


def test_detector_data_feed_reads_sip(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "sip")
    assert _resolve_detector_data_feed() == DataFeed.SIP


def test_detector_data_feed_reads_iex_explicitly(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "iex")
    assert _resolve_detector_data_feed() == DataFeed.IEX


def test_detector_data_feed_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "  SIP  ")
    assert _resolve_detector_data_feed() == DataFeed.SIP


def test_detector_data_feed_rejects_an_unknown_value(monkeypatch):
    monkeypatch.setenv("DETECTOR_DATA_FEED", "nasdaq")
    with pytest.raises(ValueError, match="DETECTOR_DATA_FEED"):
        _resolve_detector_data_feed()


def test_module_constant_matches_the_environment_at_import_time():
    """DETECTOR_DATA_FEED itself (the module constant every detector-
    facing call site actually uses) is resolved once at import -- this
    process has no DETECTOR_DATA_FEED set, so it must be the default."""
    import tradebot.vendors.alpaca as alpaca_module

    assert alpaca_module.DETECTOR_DATA_FEED == DataFeed.IEX
