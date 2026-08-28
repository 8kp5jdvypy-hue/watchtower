#!/usr/bin/env python3
"""Ingest an operator-reviewed licensed sector/float manifest append-only."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.journal import code_version, new_run_id
from tradebot.postmarket_reference_manifest import ingest_reference_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/postmarket_shadow.db"))
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        manifest_id, created, manifest = ingest_reference_manifest(
            conn, args.manifest, observed_at=datetime.now(timezone.utc),
            code_version=code_version(), run_id=new_run_id(),
        )
    finally:
        conn.close()
    print(json.dumps({
        "reference_manifest_id": manifest_id,
        "created": created,
        "provider": manifest.provider,
        "dataset": manifest.dataset,
        "effective_date": manifest.effective_date.isoformat(),
        "classification_system": manifest.classification_system,
        "manifest_sha256": manifest.manifest_sha256,
        "rows": len(manifest.rows),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
