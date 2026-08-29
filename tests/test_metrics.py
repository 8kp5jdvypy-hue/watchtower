"""Tests for tradebot.metrics — the dependency-free counter file."""
from __future__ import annotations

import json

import pytest

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


def test_increment_publishes_with_same_directory_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    real_replace = metrics.os.replace
    replacements = []

    def recording_replace(source, destination):
        replacements.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(metrics.os, "replace", recording_replace)

    metrics.increment("heartbeat_loop", path=path)

    assert len(replacements) == 1
    source, destination = map(metrics.Path, replacements[0])
    assert source.parent == path.parent
    assert destination == path
    assert not source.exists()
    assert json.loads(path.read_text()) == {"heartbeat_loop": 1}


def test_failed_atomic_replace_preserves_previous_file_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    original = '{"heartbeat_loop": 7}\n'
    path.write_text(original)

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(metrics.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        metrics.increment("heartbeat_loop", path=path)

    assert path.read_text() == original
    assert list(tmp_path.glob(".metrics.json.*.tmp")) == []


@pytest.mark.parametrize("corrupt_content", ["{broken", "[]", "null"])
def test_increment_preserves_corrupt_file_before_starting_a_new_counter_object(
    tmp_path,
    caplog,
    corrupt_content,
):
    path = tmp_path / "metrics.json"
    path.write_text(corrupt_content)

    with caplog.at_level("ERROR", logger="watchtower.metrics"):
        metrics.increment("heartbeat_loop", path=path)

    backups = list(tmp_path.glob("metrics.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == corrupt_content
    assert metrics.read_all(path) == {"heartbeat_loop": 1}
    assert "preserved original before reset" in caplog.text
    assert str(backups[0]) in caplog.text


def test_increment_refuses_to_overwrite_corruption_if_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    original = "{broken"
    path.write_text(original)

    def fail_copy(source, destination):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(metrics.shutil, "copy2", fail_copy)

    with pytest.raises(OSError, match="simulated backup failure"):
        metrics.increment("heartbeat_loop", path=path)

    assert path.read_text() == original
    assert list(tmp_path.glob("metrics.json.corrupt-*")) == []


def test_read_all_logs_corruption_without_mutating_the_file(tmp_path, caplog):
    path = tmp_path / "metrics.json"
    original = "{broken"
    path.write_text(original)

    with caplog.at_level("ERROR", logger="watchtower.metrics"):
        assert metrics.read_all(path) == {}

    assert path.read_text() == original
    assert list(tmp_path.glob("metrics.json.corrupt-*")) == []
    assert "corrupt and unreadable" in caplog.text


def test_increment_refuses_to_overwrite_an_unreadable_existing_file(
    tmp_path,
    caplog,
    monkeypatch,
):
    path = tmp_path / "metrics.json"
    original = '{"heartbeat_loop": 7}\n'
    path.write_text(original)

    def fail_read(candidate):
        raise PermissionError("simulated read failure")

    monkeypatch.setattr(metrics, "_read_metrics_object", fail_read)

    with caplog.at_level("ERROR", logger="watchtower.metrics"):
        with pytest.raises(PermissionError, match="simulated read failure"):
            metrics.increment("heartbeat_loop", path=path)

    assert path.read_text() == original
    assert "refusing to overwrite it" in caplog.text
