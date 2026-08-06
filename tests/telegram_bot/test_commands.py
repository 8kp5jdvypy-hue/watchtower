"""Direct tests for the BotFather drift check — the guard that must fail
loudly the moment the code's command registry and BotFather's registered
commands disagree, in any of the three ways they could disagree."""
from __future__ import annotations

import pytest

from tradebot.telegram_bot.commands import COMMANDS, CommandDriftError, verify_commands_match_botfather


class _FakeClient:
    def __init__(self, live_commands):
        self._live = live_commands

    def get_my_commands(self):
        return self._live


def _as_api_shape(pairs):
    return [{"command": c, "description": d} for c, d in pairs]


def test_passes_when_identical():
    client = _FakeClient(_as_api_shape(COMMANDS))
    verify_commands_match_botfather(client)  # must not raise


def test_fails_when_botfather_is_missing_a_command():
    live = _as_api_shape(COMMANDS[:-1])  # /halt missing
    client = _FakeClient(live)
    with pytest.raises(CommandDriftError, match="halt"):
        verify_commands_match_botfather(client)


def test_fails_when_botfather_has_an_extra_command():
    live = _as_api_shape(COMMANDS) + [{"command": "secret", "description": "undocumented"}]
    client = _FakeClient(live)
    with pytest.raises(CommandDriftError, match="secret"):
        verify_commands_match_botfather(client)


def test_fails_on_a_description_mismatch():
    live = _as_api_shape(COMMANDS)
    live[0] = {**live[0], "description": "a totally different description"}
    client = _FakeClient(live)
    with pytest.raises(CommandDriftError, match="description mismatch"):
        verify_commands_match_botfather(client)
