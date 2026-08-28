"""Market-aware health probe for postmarket external-context shadow."""
from __future__ import annotations

import argparse
from pathlib import Path

from tradebot.postmarket_external_context_shadow import external_context_enabled
from tradebot.postmarket_health import DEFAULT_MAX_AGE_SECONDS, evaluate_postmarket_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--heartbeat", type=Path,
        default=Path("data/postmarket_external_context_heartbeat.json"),
    )
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    try:
        enabled = external_context_enabled()
    except ValueError as exc:
        print(str(exc))
        return 1
    result = evaluate_postmarket_health(
        args.heartbeat, enabled=enabled, max_age_seconds=args.max_age,
    )
    print(result.detail)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
