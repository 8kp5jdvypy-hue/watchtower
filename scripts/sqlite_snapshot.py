#!/usr/bin/env python3
"""Create and verify one SQLite online-backup snapshot."""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if source.is_symlink():
        raise ValueError(f"refusing symlinked source database: {source}")
    if os.path.lexists(destination):
        raise FileExistsError(f"refusing to replace snapshot: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        result = [row[0] for row in destination_conn.execute("PRAGMA quick_check")]
        if result != ["ok"]:
            raise sqlite3.DatabaseError(f"snapshot quick_check failed: {result!r}")
        destination_conn.close()
        destination_conn = None
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        if destination_conn is not None:
            destination_conn.close()
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        source_conn.close()
        if destination_conn is not None:
            destination_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    snapshot(args.source, args.destination)
    print(f"verified SQLite snapshot: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
