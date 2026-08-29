"""Prospectively lock a postmarket evidence range and acceptance policy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from tradebot.postmarket_evidence_gate import CALENDAR, _expected_sessions, _parse_policy


def lock_evidence_campaign(
    output_path: Path | str,
    *,
    campaign_id: str,
    locked_at: datetime,
    coverage_start: date,
    coverage_end: date,
    policy: dict,
) -> tuple[str, dict]:
    """Create one immutable campaign file before its first XNYS session."""
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError("campaign_id must be non-empty")
    identifier = campaign_id.strip()
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise ValueError("locked_at must be timezone-aware")
    locked = locked_at.astimezone(timezone.utc)
    sessions = _expected_sessions(coverage_start, coverage_end)
    if not sessions:
        raise ValueError("campaign coverage contains no XNYS sessions")
    validated_policy = _parse_policy(policy)
    if len(sessions) < validated_policy.min_clean_sessions:
        raise ValueError(
            "campaign coverage contains fewer XNYS sessions than "
            "policy.min_clean_sessions"
        )
    first_open = CALENDAR.session_open(sessions[0]).to_pydatetime().astimezone(timezone.utc)
    if locked >= first_open:
        raise ValueError("campaign must be locked before its first session opens")
    payload = {
        "schema_version": 1,
        "status": "locked",
        "campaign_id": identifier,
        "locked_at_utc": locked.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "policy": asdict(validated_policy),
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    payload = json.loads(raw)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("campaign output cannot be a symlink")
    descriptor = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        os.link(temporary_path, output, follow_symlinks=False)
        temporary_path.unlink()
        temporary_path = None
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(raw).hexdigest(), payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--coverage-start", type=date.fromisoformat, required=True)
    parser.add_argument("--coverage-end", type=date.fromisoformat, required=True)
    parser.add_argument("--min-clean-sessions", type=int, required=True)
    parser.add_argument("--min-definitive-labels", type=int, required=True)
    parser.add_argument("--min-positive-labels", type=int, required=True)
    parser.add_argument("--min-recall", type=float, required=True)
    parser.add_argument("--min-precision", type=float, required=True)
    parser.add_argument("--max-detection-latency-seconds", type=float, required=True)
    parser.add_argument("--allowed-data-feed", action="append", required=True)
    parser.add_argument("--allowed-market-data-provider", action="append", required=True)
    parser.add_argument("--allowed-audit-version", action="append", type=int, required=True)
    parser.add_argument("--allowed-observer-version", action="append", type=int, required=True)
    parser.add_argument("--allowed-audit-code-version", action="append", required=True)
    parser.add_argument("--allowed-observer-code-version", action="append", required=True)
    args = parser.parse_args(argv)
    policy = {
        "min_clean_sessions": args.min_clean_sessions,
        "min_definitive_labels": args.min_definitive_labels,
        "min_positive_labels": args.min_positive_labels,
        "min_recall": args.min_recall,
        "min_precision": args.min_precision,
        "max_detection_latency_seconds": args.max_detection_latency_seconds,
        "allowed_data_feeds": args.allowed_data_feed,
        "allowed_market_data_providers": args.allowed_market_data_provider,
        "allowed_audit_versions": args.allowed_audit_version,
        "allowed_observer_versions": args.allowed_observer_version,
        "allowed_audit_code_versions": args.allowed_audit_code_version,
        "allowed_observer_code_versions": args.allowed_observer_code_version,
        "require_zero_dirty_sessions": True,
        "require_zero_direction_mismatches": True,
        "require_complete_session_inventory": True,
    }
    digest, payload = lock_evidence_campaign(
        args.output,
        campaign_id=args.campaign_id,
        locked_at=datetime.now(timezone.utc),
        coverage_start=args.coverage_start,
        coverage_end=args.coverage_end,
        policy=policy,
    )
    print(
        json.dumps(
            {
                "campaign_id": payload["campaign_id"],
                "coverage_start": payload["coverage_start"],
                "coverage_end": payload["coverage_end"],
                "campaign_sha256": digest,
                "path": str(args.output),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
