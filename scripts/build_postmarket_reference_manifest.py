#!/usr/bin/env python3
"""Build an immutable licensed-reference manifest from a strict CSV export."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.postmarket_reference_manifest_builder import (
    build_reference_manifest,
    write_reference_manifest_exclusive,
)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date") from exc


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must be an ISO timezone-aware timestamp")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", type=Path, help="operator-reviewed provider CSV export"
    )
    parser.add_argument("output", type=Path, help="new immutable JSON manifest path")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--license-reference", required=True)
    parser.add_argument("--effective-date", required=True, type=_date)
    parser.add_argument("--published-at-utc", required=True, type=_timestamp)
    parser.add_argument(
        "--created-at-utc",
        type=_timestamp,
        default=None,
        help="defaults to the current UTC time; supply explicitly for reproducible builds",
    )
    parser.add_argument(
        "--classification-system",
        required=True,
        choices=("GICS", "ICB", "PROVIDER_SECTOR"),
    )
    args = parser.parse_args()
    created = args.created_at_utc or datetime.now(timezone.utc)
    built = build_reference_manifest(
        args.source,
        provider=args.provider,
        dataset=args.dataset,
        license_reference=args.license_reference,
        effective_date=args.effective_date,
        published_at_utc=args.published_at_utc,
        created_at_utc=created,
        classification_system=args.classification_system,
    )
    write_reference_manifest_exclusive(args.output, built)
    manifest = built.manifest
    print(
        json.dumps(
            {
                "classification_system": manifest.classification_system,
                "dataset": manifest.dataset,
                "effective_date": manifest.effective_date.isoformat(),
                "manifest_sha256": manifest.manifest_sha256,
                "output": str(args.output),
                "provider": manifest.provider,
                "rows": len(manifest.rows),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
