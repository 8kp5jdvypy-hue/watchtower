"""Minimal, dependency-free metrics: counters persisted to a JSON file.

Not a real metrics system — no histograms, no export format, no
aggregation windows. Just enough to make "how often is X happening"
answerable by reading a file instead of grepping logs, without adding a
statsd/prometheus_client dependency for a bot this size. If real volume
ever justifies it, this is the seam to swap.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_METRICS_PATH = REPO_ROOT / "data" / "metrics.json"

# Where a replay's counters go. Live operational metrics answer "how often
# is this happening in production right now" -- a replay of an old session
# incrementing validator_rejection or suppression makes that question
# unanswerable, and the contamination is invisible after the fact because
# a counter is just a number with no record of who added to it.
REPLAY_METRICS_PATH = REPO_ROOT / "data" / "metrics_replay.json"

_lock = threading.Lock()

# The active redirect, or None for "use DEFAULT_METRICS_PATH". Module-level
# mutable state, which is normally worth avoiding -- justified here because
# of what it is scoped to: live and replay are separate execution MODES,
# selected once per process at the CLI (run_live vs run_replay) and never
# running in the same one. So this is not really global state that anything
# races over; it is a per-process constant that happens to be set slightly
# after import, by the one boundary that knows which mode this process is.
#
# The alternative -- threading a metrics_path down to each of the thirteen
# replay-reachable increment() call sites -- would put the decision in
# thirteen places that must all agree, and would silently miss the
# fourteenth added later. Same reasoning as the replay journal boundary in
# journal.resolve_replay_db_path: decide once, where the mode is known.
_redirect_path: Path | None = None


@contextmanager
def redirect_to(path):
    """Send every counter written inside this block to `path` instead of
    DEFAULT_METRICS_PATH. Used by runner.run_replay so a replay's counters
    never land in the live file.

    Restores the previous value in a finally, so an exception escaping the
    block -- or a replay that raises partway through -- cannot leave the
    process permanently redirected. Nests correctly (the previous value is
    saved, not assumed to be None), which matters less for the single
    current caller than for the next one.

    An explicitly-passed path argument to increment()/read_all() still
    wins over this: a caller that names a file means it."""
    global _redirect_path
    previous = _redirect_path
    _redirect_path = Path(path)
    try:
        yield _redirect_path
    finally:
        _redirect_path = previous


def active_path() -> Path:
    """The file counters are currently being written to. Read-only view of
    the resolution below, for a caller (or a test) that wants to report or
    assert where metrics are going without inferring it."""
    return _resolve(None)


def _resolve(path: Path | None) -> Path:
    """explicit argument > active redirect > module default.

    DEFAULT_METRICS_PATH is read here at call time, not captured at import
    or in a signature default, so a test that monkeypatches it is still
    honoured by every caller that didn't pass a path -- the property
    increment's docstring has always promised."""
    if path is not None:
        return path
    if _redirect_path is not None:
        return _redirect_path
    return DEFAULT_METRICS_PATH


def _label_key(name: str, labels: dict) -> str:
    if not labels:
        return name
    tags = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{tags}}}"


def increment(name: str, path: Path | None = None, amount: int = 1, **labels) -> None:
    """Increments a counter by `amount` (default 1, unchanged from every
    call site written before this parameter existed) and persists it.
    Safe to call from multiple threads (guarded by a lock) or multiple
    processes (each read-modify-write is small and infrequent — alert
    rejections, not a hot path — so the rare lost update under real
    concurrent processes is an acceptable tradeoff against adding a real
    metrics backend).

    amount lets a caller accumulate a running total (e.g.
    tradebot.broad_scan's *_latency_ms_total counters) instead of calling
    this once per unit, which would be its own hot-path cost for exactly
    the callers most likely to run over a large batch.

    path defaults to None (resolved at call time, not import time) so
    tests can monkeypatch tradebot.metrics.DEFAULT_METRICS_PATH and have
    every caller that didn't pass an explicit path honor it. An active
    redirect_to() block takes precedence over the module default but not
    over an explicit argument -- see _resolve.

    NOTE: `path` is the second POSITIONAL parameter, so
    increment("name", something) sets a destination, not a label. Labels
    are keyword-only by construction (**labels)."""
    path = _resolve(path)
    key = _label_key(name, labels)
    with _lock:
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        data[key] = data.get(key, 0) + amount
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))


def read_all(path: Path | None = None) -> dict:
    """Same resolution as increment(): explicit argument, then an active
    redirect_to(), then the module default. So a reader inside a replay
    sees the replay's own counters rather than the live file's."""
    path = _resolve(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
