"""Market-aware Docker health probe for market-wide postmarket discovery."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from tradebot.journal import code_version
from tradebot.postmarket_discovery_shadow import discovery_enabled
from tradebot.postmarket_health import (
    DEFAULT_MAX_AGE_SECONDS,
    PostmarketHealth,
    evaluate_supervised_health,
)


OBSERVER = "postmarket-marketwide-shadow"
SUBSYSTEM_STATUS_FIELDS = (
    "audit_status",
    "quality_backfill_status",
    "recall_census_status",
    "provider_proof_status",
    "context_backfill_status",
    "lifecycle_status",
    "rank_status",
    "rth_handoff_status",
    "rth_audit_status",
    "rth_missed_mover_census_status",
)


def evaluate_discovery_health(
    heartbeat_path: Path,
    *,
    enabled: bool,
    expected_revision: str,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> PostmarketHealth:
    """Require the enabled supervisor and every explicit subsystem to be sound.

    Discovery performs finalized outcome, recall, context, lifecycle, and rank
    maintenance outside the active window, so its heartbeat must remain fresh
    all day. A degraded evidence result is observable but not a process-health
    failure; an explicit subsystem exception is.
    """
    return evaluate_supervised_health(
        heartbeat_path,
        enabled=enabled,
        expected_revision=expected_revision,
        expected_observer=OBSERVER,
        active_statuses=frozenset({"running", "ok"}),
        inactive_statuses=frozenset({"idle", "running", "ok"}),
        error_status_fields=SUBSYSTEM_STATUS_FIELDS,
        disabled_detail="market-wide discovery disabled by kill switch",
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--heartbeat", type=Path, default=Path("data/postmarket_discovery_heartbeat.json")
    )
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    try:
        enabled = discovery_enabled()
    except ValueError as exc:
        print(str(exc))
        return 1
    result = evaluate_discovery_health(
        args.heartbeat,
        enabled=enabled,
        expected_revision=code_version() or "unknown",
        max_age_seconds=args.max_age,
    )
    print(result.detail)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
