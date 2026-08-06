"""Single-instance guarantee for the outbox worker via a plain OS file
lock (fcntl.flock) — this deployment is one host, not a cluster, so a
real distributed lock (Redis, a DB lease with heartbeats) would be
solving a problem this project doesn't have. flock is released
automatically if the process dies for any reason (crash, SIGKILL), which
is exactly the property needed: a dead worker must never permanently
block a new one from starting.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunningError(f"another instance already holds the lock at {self.path}") from exc
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
