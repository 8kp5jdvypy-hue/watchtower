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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_METRICS_PATH = REPO_ROOT / "data" / "metrics.json"

_lock = threading.Lock()


def _label_key(name: str, labels: dict) -> str:
    if not labels:
        return name
    tags = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{tags}}}"


def increment(name: str, path: Path | None = None, **labels) -> None:
    """Increments a counter by 1 and persists it. Safe to call from
    multiple threads (guarded by a lock) or multiple processes (each
    read-modify-write is small and infrequent — alert rejections, not a
    hot path — so the rare lost update under real concurrent processes
    is an acceptable tradeoff against adding a real metrics backend).

    path defaults to None (resolved to DEFAULT_METRICS_PATH at call
    time, not import time) so tests can monkeypatch
    tradebot.metrics.DEFAULT_METRICS_PATH and have every caller that
    didn't pass an explicit path honor it."""
    path = path if path is not None else DEFAULT_METRICS_PATH
    key = _label_key(name, labels)
    with _lock:
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        data[key] = data.get(key, 0) + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))


def read_all(path: Path | None = None) -> dict:
    path = path if path is not None else DEFAULT_METRICS_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
