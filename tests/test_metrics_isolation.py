"""Tests for the test suite's own metrics isolation (tests/conftest.py).

Two things worth proving separately: that the real data/metrics.json is
left alone, and that metrics still genuinely work under the fixture --
because a fixture that broke counters would also, trivially, stop them
reaching the real file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tradebot import metrics as metrics_mod


def _real(name: str) -> Path:
    """The genuine repo path, computed from tradebot.metrics' own
    REPO_ROOT rather than from the module attribute -- which the autouse
    fixture has already redirected into tmp by the time a test runs."""
    return metrics_mod.REPO_ROOT / "data" / name


# ---------------------------------------------------------------------------
# The real files are not touched
# ---------------------------------------------------------------------------


def test_the_default_metrics_path_is_redirected_away_from_the_repo():
    active = metrics_mod.DEFAULT_METRICS_PATH

    assert active != _real("metrics.json")
    assert metrics_mod.REPO_ROOT not in active.parents


def test_the_replay_metrics_path_is_redirected_too():
    """run_replay writes here by default (PR #77), so a test that calls it
    without an explicit metrics_path would otherwise hit the real file."""
    active = metrics_mod.REPLAY_METRICS_PATH

    assert active != _real("metrics_replay.json")
    assert metrics_mod.REPO_ROOT not in active.parents


def test_incrementing_a_counter_does_not_write_the_real_metrics_file():
    """The leak itself, in miniature: before this fixture existed, this
    call landed in the developer's real data/metrics.json."""
    real = _real("metrics.json")
    before = (real.exists(), real.read_bytes() if real.exists() else None)

    metrics_mod.increment("isolation_probe", rule="test")

    after = (real.exists(), real.read_bytes() if real.exists() else None)
    assert after == before


def test_a_replay_shaped_counter_does_not_write_the_real_replay_file():
    real = _real("metrics_replay.json")
    before = (real.exists(), real.read_bytes() if real.exists() else None)

    with metrics_mod.redirect_to(metrics_mod.REPLAY_METRICS_PATH):
        metrics_mod.increment("isolation_probe_replay")

    after = (real.exists(), real.read_bytes() if real.exists() else None)
    assert after == before


# ---------------------------------------------------------------------------
# Metrics still work
# ---------------------------------------------------------------------------


def test_counters_still_round_trip_under_the_fixture():
    metrics_mod.increment("widgets")
    metrics_mod.increment("widgets")
    metrics_mod.increment("widgets", amount=3)

    assert metrics_mod.read_all() == {"widgets": 5}


def test_labels_still_work_under_the_fixture():
    metrics_mod.increment("suppression", category="data_integrity")
    metrics_mod.increment("suppression", category="news_blackout")
    metrics_mod.increment("suppression", category="data_integrity")

    assert metrics_mod.read_all() == {
        "suppression{category=data_integrity}": 2,
        "suppression{category=news_blackout}": 1,
    }


def test_the_redirect_context_manager_still_works_under_the_fixture(tmp_path):
    target = tmp_path / "elsewhere.json"

    with metrics_mod.redirect_to(target):
        metrics_mod.increment("inside")
    metrics_mod.increment("outside")

    assert metrics_mod.read_all(target) == {"inside": 1}
    assert metrics_mod.read_all() == {"outside": 1}


def test_an_explicit_path_argument_still_outranks_the_fixture(tmp_path):
    """Keeps broad_scan's metrics_path parameter (and its tests) working:
    a caller that names a file still gets that file."""
    named = tmp_path / "named.json"

    metrics_mod.increment("explicit", path=named)

    assert metrics_mod.read_all(named) == {"explicit": 1}
    assert metrics_mod.read_all() == {}


# ---------------------------------------------------------------------------
# The fixture doesn't get in a test's way
# ---------------------------------------------------------------------------


def test_a_test_that_monkeypatches_the_default_itself_still_wins(monkeypatch, tmp_path):
    """Three existing tests in test_runner.py do exactly this. They run
    after the autouse fixture, so their patch is the one that takes
    effect -- which is why this change needed no edits to them."""
    mine = tmp_path / "mine.json"
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", mine)

    metrics_mod.increment("counted_here")

    assert metrics_mod.read_all(mine) == {"counted_here": 1}
    assert metrics_mod.active_path() == mine


def test_each_test_gets_a_clean_counter_file_part_one():
    """Paired with part two: function-scoped isolation means counters do
    not accumulate across tests, so a read_all() assertion never depends
    on what ran before it."""
    metrics_mod.increment("shared_name")

    assert metrics_mod.read_all() == {"shared_name": 1}


def test_each_test_gets_a_clean_counter_file_part_two():
    metrics_mod.increment("shared_name")

    assert metrics_mod.read_all() == {"shared_name": 1}  # not 2


def test_the_isolated_path_is_writable_and_created_on_demand():
    """The fixture makes the directory; metrics.increment makes the file."""
    assert not metrics_mod.DEFAULT_METRICS_PATH.exists()

    metrics_mod.increment("first_write")

    assert metrics_mod.DEFAULT_METRICS_PATH.exists()
