"""A tiny liveness file the live scanner loop touches once per pass over
the watchlist, and /status reads. The scanner (run_live in runner.py) and
the command dispatcher are separate long-running processes with no other
shared state — this file is the only thing letting /status say anything
honest about data-feed freshness without a real IPC channel.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def write_heartbeat(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts_utc": when.isoformat()}))


def read_heartbeat(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
