"""Tests for tradebot.rendering.fields — the shared number primitives."""
from __future__ import annotations

from datetime import datetime, timezone

from tradebot.rendering.fields import atr, dash, money, pct, qty, ratio, ts


def test_money():
    assert money(366) == "$366.00"
    assert money(366.5) == "$366.50"
    assert money(213745.678) == "$213,745.68"
    assert money(-12.3) == "-$12.30"


def test_pct_always_signed():
    assert pct(0.22) == "+0.22%"
    assert pct(-0.01) == "-0.01%"
    assert pct(0) == "+0.00%"


def test_atr():
    assert atr(2.26) == "2.26 ATR"
    assert atr(0) == "0.00 ATR"


def test_qty_thousands_separated():
    assert qty(213745) == "213,745"
    assert qty(100) == "100"


def test_ratio():
    assert ratio(3.2) == "3.2x"
    assert ratio(5) == "5.0x"


def test_ts_always_eastern_time_only_no_date():
    dt = datetime(2026, 8, 5, 13, 35, tzinfo=timezone.utc)  # 09:35 ET (EDT, UTC-4)
    assert ts(dt) == "09:35 ET"


def test_dash_for_missing_value():
    assert dash(None, money) == "—"


def test_dash_formats_a_present_value():
    assert dash(2.26, atr) == "2.26 ATR"
