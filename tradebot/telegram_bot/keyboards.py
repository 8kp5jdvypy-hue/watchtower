"""Inline keyboard builders. Every callback_data string is `<action>:<arg>`
so tradebot.telegram_bot.callbacks can route on the prefix without parsing
free text — the whole point of a button is that no one has to type.
"""
from __future__ import annotations

COMMON_TIMEZONES = [
    ("Eastern", "America/New_York"),
    ("Central", "America/Chicago"),
    ("Mountain", "America/Denver"),
    ("Pacific", "America/Los_Angeles"),
]


def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def alert_actions_keyboard(detection_id: str) -> dict:
    return _kb([
        [
            ("I took this", f"took:{detection_id}"),
            ("Skipped", f"skip:{detection_id}"),
            ("Why NO TRADE?", f"whynt:{detection_id}"),
        ],
        [("How'd it play out?", f"outcome:{detection_id}")],
    ])


def risk_ack_keyboard() -> dict:
    return _kb([[("I understand — continue", "ack_risk")]])


def timezone_keyboard() -> dict:
    return _kb([[(label, f"tz:{tz}") for label, tz in COMMON_TIMEZONES]])


SENSITIVITY_LABELS = (("quiet", "Quiet"), ("balanced", "Balanced"), ("aggressive", "Aggressive"))


def sensitivity_keyboard() -> dict:
    return _kb([[(label, f"sens:{value}") for value, label in SENSITIVITY_LABELS]])


SPEAK_TIMING_LABELS = (("always", "Always"), ("market_hours", "Market hours"), ("custom", "Custom"))


def speak_timing_keyboard() -> dict:
    return _kb([[(label, f"speak:{value}") for value, label in SPEAK_TIMING_LABELS]])


def pause_keyboard() -> dict:
    return _kb([[("30m", "pause:30m"), ("1h", "pause:1h"), ("Rest of day", "pause:eod")]])


def watchlist_keyboard(all_symbols: list[str], selected: set[str]) -> dict:
    rows = []
    row: list[tuple[str, str]] = []
    for symbol in all_symbols:
        mark = "✅" if symbol in selected else "⬜"
        row.append((f"{mark} {symbol}", f"wl:{symbol}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("Save", "wl:save")])
    return _kb(rows)


_MOOD_LABELS = (("calm", "Calm"), ("rushed", "Rushed"), ("fomo", "FOMO"), ("revenge", "Revenge"), ("bored", "Bored"))


def mood_keyboard(trade_id: str) -> dict:
    """One tap, optional — see tradebot.telegram_bot.db.MOOD_CHOICES. Two
    rows so it doesn't get cramped on a phone-width keyboard."""
    buttons = [(label, f"mood:{trade_id}:{value}") for value, label in _MOOD_LABELS]
    return _kb([buttons[:3], buttons[3:]])


def tiers_keyboard(portal_url: str | None) -> dict | None:
    if not portal_url:
        return None
    return {"inline_keyboard": [[{"text": "Manage billing", "url": portal_url}]]}
