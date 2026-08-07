"""Append-only incident log for the public status page (see
tradebot.status_page). Two kinds recorded so far:

- "heartbeat_stale" — auto-opened/closed by the outbox worker's deadman-
  switch paging (tradebot.telegram_bot.worker._maybe_page_on_stale_heartbeat)
  when the scanner goes quiet during RTH and recovers.
- "halt" — opened when /halt (admin) or a HALT file stops a live session;
  there is no in-process "resume" moment for a halt (stopping ends that
  session's run_live() call entirely — see runner.py), so it's closed at
  the top of the NEXT run_live() call instead, since reaching that point
  is itself proof the system came back.

Not a general-purpose event log — every incident here is something a
real user waiting on alerts would want to know happened. JSON Lines,
one incident per line, append-only in spirit: open/close rewrite the
whole (small, infrequent) file rather than editing in place, since
sqlite3 isn't warranted for a log this size and this rare.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INCIDENTS_PATH = REPO_ROOT / "data" / "incidents.jsonl"


def _read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    incidents = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            incidents.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # one corrupted line must never take down the whole log
    return incidents


def _write_all(path: Path, incidents: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(i, sort_keys=True) for i in incidents)
    path.write_text(body + "\n" if incidents else "")


def open_incident(kind: str, detail: str, when: datetime, path: Path | None = None) -> None:
    """Starts a new incident, unless one of the same kind is already
    open — e.g. a heartbeat check that re-confirms staleness every 30s
    must not spawn a fresh incident each time."""
    path = path if path is not None else DEFAULT_INCIDENTS_PATH
    incidents = _read_all(path)
    if any(i["kind"] == kind and i["ended_at"] is None for i in incidents):
        return
    incidents.append({"kind": kind, "detail": detail, "started_at": when.isoformat(), "ended_at": None})
    _write_all(path, incidents)


def close_incident(kind: str, when: datetime, path: Path | None = None) -> None:
    """Closes the most recent open incident of this kind, if any. A
    no-op (not an error) if none is open — callers call this
    unconditionally on every recovery/startup rather than tracking
    whether one was actually open."""
    path = path if path is not None else DEFAULT_INCIDENTS_PATH
    incidents = _read_all(path)
    for incident in reversed(incidents):
        if incident["kind"] == kind and incident["ended_at"] is None:
            incident["ended_at"] = when.isoformat()
            _write_all(path, incidents)
            return


def list_incidents(path: Path | None = None) -> list[dict]:
    path = path if path is not None else DEFAULT_INCIDENTS_PATH
    return _read_all(path)
