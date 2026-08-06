"""Tests for tradebot.metrics — the dependency-free counter file."""
from __future__ import annotations

from tradebot import metrics


def test_increment_creates_and_persists_a_counter(tmp_path):
    path = tmp_path / "metrics.json"
    metrics.increment("validator_rejection", path=path, rule="crossed_quote")
    metrics.increment("validator_rejection", path=path, rule="crossed_quote")
    metrics.increment("validator_rejection", path=path, rule="stale_quote")

    data = metrics.read_all(path)
    assert data["validator_rejection{rule=crossed_quote}"] == 2
    assert data["validator_rejection{rule=stale_quote}"] == 1


def test_read_all_on_a_missing_file_returns_empty(tmp_path):
    assert metrics.read_all(tmp_path / "does_not_exist.json") == {}


def test_increment_without_labels_uses_a_bare_name(tmp_path):
    path = tmp_path / "metrics.json"
    metrics.increment("heartbeat_loop", path=path)
    assert metrics.read_all(path) == {"heartbeat_loop": 1}
