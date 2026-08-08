"""The canonical command registry — must match what's registered in
BotFather exactly (name AND description), verified at startup by
verify_commands_match_botfather(). This list IS the source of truth for
that verification: if you change a command here, update it in BotFather
(via @BotFather -> /setcommands) in the same change, or the bot will
refuse to start.

Fetched from the live bot via getMyCommands on 2026-08-06 and confirmed
to match this list at that time. Re-synced 2026-08-08 for the Kestrel ->
Perch rebrand (/start's description text) via BotClient.set_my_commands.
"""
from __future__ import annotations

COMMANDS: list[tuple[str, str]] = [
    ("start", "Set up Perch and see how it works"),
    ("status", "Bot health, market state, alerts today"),
    ("performance", "Real track record, including losing streaks"),
    ("example", "A real win and a real day's hit rate"),
    ("me", "Your trading stats and biggest leaks"),
    ("took", "Log a trade you entered"),
    ("closed", "Log an exit"),
    ("limits", "Set daily loss and trade caps"),
    ("pause", "Mute alerts for a set time"),
    ("resume", "Turn alerts back on"),
    ("watchlist", "View or edit your symbols"),
    ("events", "Today's earnings and macro calendar"),
    ("tiers", "Plans and what each includes"),
    ("export", "Download your journal as CSV"),
    ("help", "All commands and support"),
    ("halt", "Emergency stop"),
    ("feedback", "Tell us what's broken or missing"),
]

COMMAND_NAMES = [name for name, _ in COMMANDS]

# In a group/supergroup chat, only these respond at all — everything else
# replies "DM me for that" rather than touching personal data in a shared
# chat. Regular users' /halt in a group still only affects their own
# session (see handlers.halt), so it's safe to allow there too.
GROUP_ALLOWED = frozenset({"status", "performance", "help", "halt", "feedback"})

# Channel posts (the bot posting alerts into a channel where it's admin)
# carry NO user identity at all — Telegram's `channel_post` update has no
# `from` field, so there's no one to attribute a mutation to. This is
# strictly narrower than GROUP_ALLOWED: /halt is excluded here even though
# it's group-safe, because /halt always mutates state (a personal session
# halt, or — for an admin — the global halt file) and there's no user to
# scope that mutation to in a channel post.
CHANNEL_ALLOWED = frozenset({"status", "performance", "help"})


class CommandDriftError(RuntimeError):
    """Raised when the code's command list and BotFather's registered
    list have drifted apart — this must fail loudly at startup, never
    silently serve a command BotFather doesn't advertise (or vice versa)."""


def verify_commands_match_botfather(client) -> None:
    live = client.get_my_commands()
    live_pairs = sorted((c["command"], c["description"]) for c in live)
    expected_pairs = sorted(COMMANDS)

    if live_pairs == expected_pairs:
        return

    live_map = dict(live_pairs)
    expected_map = dict(expected_pairs)
    problems = []

    missing_in_botfather = sorted(set(expected_map) - set(live_map))
    if missing_in_botfather:
        problems.append(f"registered in code but not in BotFather: {missing_in_botfather}")

    extra_in_botfather = sorted(set(live_map) - set(expected_map))
    if extra_in_botfather:
        problems.append(f"registered in BotFather but not in code: {extra_in_botfather}")

    for name in sorted(set(expected_map) & set(live_map)):
        if expected_map[name] != live_map[name]:
            problems.append(
                f"description mismatch for /{name}: code={expected_map[name]!r} BotFather={live_map[name]!r}"
            )

    raise CommandDriftError(
        "Command registry has drifted from BotFather's registered commands:\n  "
        + "\n  ".join(problems)
        + "\nUpdate tradebot/telegram_bot/commands.py or @BotFather's /setcommands so they match, then restart."
    )
