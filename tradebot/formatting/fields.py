"""Shared numeric formatting primitives.

Every rendered message in tradebot.formatting.templates composes its
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
    """2026-08-05 09:35 ET — always Eastern, matching the exchange this
    project trades; this codebase never displays any other timezone."""
    return f"{dt.astimezone(ET).strftime('%Y-%m-%d %H:%M')} ET"
