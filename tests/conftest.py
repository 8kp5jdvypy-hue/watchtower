"""Test-suite-wide safety: keep pytest out of the real data/ tree.

tradebot.metrics writes counters to a module-level path resolved at call
time, and the overwhelming majority of call sites don't pass one — so any
test that reaches metrics.increment() (directly, or through
process_new_bar, or through run_replay) writes the developer's real
data/metrics.json. That was true before any of this existed: running
tests/test_runner.py on its own recreated the file, and a full run moved
real counters like suppression{category=budget_cooldown} by nine.

Nothing was *broken* by that — the counters are advisory — but it makes
data/metrics.json untrustworthy for the one thing it's for. "How often is
this happening in production" cannot be answered from a file that also
counts every test run anybody has done on the machine. A developer
investigating a suppression spike has no way to tell which increments
were real.

The fix belongs here rather than in each test: there are ~1000 tests and
the ones that leak are simply the ones that happen to reach a counter,
which is not a property any individual test author can be expected to
track. Production code is deliberately NOT touched — tradebot.metrics
already resolves DEFAULT_METRICS_PATH at call time precisely so a
monkeypatch is honoured, and this uses that documented seam.
"""
from __future__ import annotations

import pytest

from tradebot import metrics as metrics_mod
from tradebot import evaluations as evaluations_mod
from tradebot import universe as universe_mod


@pytest.fixture(autouse=True)
def _isolate_metrics(monkeypatch, tmp_path):
    """Point every counter this test writes at its own tmp file.

    Function-scoped, not session-scoped, on purpose: counters accumulate,
    so one shared file would leak state between tests and make a
    read_all() assertion depend on execution order.

    Redirects both well-known destinations. DEFAULT_METRICS_PATH is the
    live one; REPLAY_METRICS_PATH is where runner.run_replay sends a
    replay's counters, and a test that calls run_replay without an
    explicit metrics_path would otherwise write the real
    data/metrics_replay.json for exactly the same reason.

    monkeypatch undoes both after every test, so nothing here can leak
    into the next one. A test that monkeypatches either path itself still
    wins — it runs after this fixture — which is what keeps the three
    existing DEFAULT_METRICS_PATH tests in test_runner.py working
    unchanged. An explicit path= argument outranks both, so
    broad_scan's metrics_path tests are unaffected too.
    """
    metrics_dir = tmp_path / "_metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(metrics_mod, "DEFAULT_METRICS_PATH", metrics_dir / "metrics.json")
    monkeypatch.setattr(metrics_mod, "REPLAY_METRICS_PATH", metrics_dir / "metrics_replay.json")


@pytest.fixture(autouse=True)
def _isolate_universe_db(monkeypatch, tmp_path):
    """Same leak, different file: tradebot.universe.connect() defaults to
    the real data/universe.db, and Stage 1 observability (screening_ticks
    / screening_events) writes there. A test that reaches run_broad_scan
    without naming a path would otherwise append to the developer's real
    universe database for exactly the reason metrics did.

    Redirected here rather than per-test for the same reason as metrics:
    which tests reach it is not a property any individual test author can
    be expected to track."""
    monkeypatch.setattr(universe_mod, "DEFAULT_DB_PATH", tmp_path / "_universe" / "universe.db")


@pytest.fixture(autouse=True)
def _isolate_evaluations_db(monkeypatch, tmp_path):
    """Third file, same leak: tradebot.evaluations.connect() defaults to
    the real data/evaluations.db, and run_live opens it for Stage 2
    observability. Redirected for the same reason as metrics and
    universe -- which tests reach it is not a property any individual
    test author can be expected to track."""
    monkeypatch.setattr(evaluations_mod, "DEFAULT_DB_PATH", tmp_path / "_evaluations" / "evaluations.db")


def _snapshot(path):
    """(exists, bytes) — enough to detect any write, including one that
    happens to produce identical content length."""
    return (path.exists(), path.read_bytes() if path.exists() else None)


@pytest.fixture(scope="session", autouse=True)
def _real_metrics_files_are_untouched():
    """Session-level proof of the property, not just the mechanism.

    The per-test fixture above is the fix; this is the assertion that it
    actually held for the WHOLE run. A single test asserting "the real
    file didn't change" only speaks for itself, and the leak this exists
    to stop is a suite-wide one — any one of a thousand tests reaching a
    counter is enough.

    Snapshots the real paths (read off tradebot.metrics' own REPO_ROOT,
    NOT off the monkeypatched module attributes, which by then point into
    tmp) before the first test and compares after the last. Deliberately
    does not delete or create them: a developer's real counters are real
    data, and a test fixture has no business editing them.
    """
    real_live = metrics_mod.REPO_ROOT / "data" / "metrics.json"
    real_replay = metrics_mod.REPO_ROOT / "data" / "metrics_replay.json"
    before_live, before_replay = _snapshot(real_live), _snapshot(real_replay)

    yield

    assert _snapshot(real_live) == before_live, (
        f"the test suite wrote to the real {real_live}. Something reached "
        f"metrics.increment() with a path that escaped the _isolate_metrics "
        f"fixture — an explicit path= argument pointing at the real file, or a "
        f"module attribute captured before the monkeypatch."
    )
    assert _snapshot(real_replay) == before_replay, (
        f"the test suite wrote to the real {real_replay}. See the message above; "
        f"most likely a run_replay() call whose metrics destination escaped the fixture."
    )
