"""Docker health probe for the default-off customer-readiness dry run."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from tradebot.journal import code_version
from tradebot.postmarket_delivery_dry_run_shadow import (
    RUN_MODE,
    dry_run_shadow_enabled,
)
from tradebot.postmarket_health import (
    DEFAULT_MAX_AGE_SECONDS,
    PostmarketHealth,
    evaluate_supervised_health,
)


def evaluate_dry_run_health(
    heartbeat_path: Path,
    *,
    enabled: bool,
    expected_revision: str,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> PostmarketHealth:
    return evaluate_supervised_health(
        heartbeat_path,
        enabled=enabled,
        expected_revision=expected_revision,
        expected_observer=RUN_MODE,
        active_statuses=frozenset({"running", "ok"}),
        inactive_statuses=frozenset({"idle", "running", "ok"}),
        disabled_detail="customer-readiness dry run disabled by kill switch",
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--heartbeat",
        type=Path,
        default=Path("data/postmarket_delivery_dry_run_heartbeat.json"),
    )
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    try:
        enabled = dry_run_shadow_enabled()
    except ValueError as exc:
        print(str(exc))
        return 1
    result = evaluate_dry_run_health(
        args.heartbeat,
        enabled=enabled,
        expected_revision=code_version() or "unknown",
        max_age_seconds=args.max_age,
    )
    print(result.detail)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
