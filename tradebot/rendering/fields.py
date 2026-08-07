"""Shared numeric formatting primitives.

Every rendered message in tradebot.rendering.templates composes its
numbers from these — no ad-hoc f-string number formatting anywhere else
in the codebase (see CLAUDE.md-style rule for this subpackage: pure,
data in, string out).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def money(value: float) -> str:
    """$366.00 — always 2 decimals, always a $ prefix, thousands separated."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def pct(value: float) -> str:
    """+0.22% / -0.01% — always signed, 2 decimals. `value` is already a
    percentage (0.22 means 0.22%, not 0.0022)."""
    return f"{value:+.2f}%"


def rate(value: float) -> str:
    """49.57% — unsigned, 2 decimals. For a proportion/magnitude like a
    hit rate or win rate, where a leading '+' would read as "gained
    49.57%" rather than "won 49.57% of the time". `value` is already a
    percentage (49.57 means 49.57%, not 0.4957)."""
    return f"{value:.2f}%"


def atr(value: float) -> str:
    """2.26 ATR — 2 decimals plus the unit."""
    return f"{value:.2f} ATR"


def qty(value: int) -> str:
    """213,745 — thousands separators, no decimals."""
    return f"{value:,.0f}"


def ratio(value: float) -> str:
    """3.2x — one decimal plus the unit."""
    return f"{value:.1f}x"


def ts(dt: datetime) -> str:
    """09:35 ET — always Eastern, matching the exchange this project
    trades. Time only, no date: every message here is read in real time
    about today, so a date is noise, not information."""
    return f"{dt.astimezone(ET).strftime('%H:%M')} ET"


def dash(value, formatter) -> str:
    """em-dash for a missing value, otherwise `formatter(value)` — the
    rule is a row is never omitted for missing data, it prints with a
    dash instead. `formatter` is one of the functions above (or any
    callable taking the raw value)."""
    return "—" if value is None else formatter(value)
