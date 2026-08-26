"""Market-aware Docker health probe for the postmarket shadow observer."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tradebot.postmarket_shadow import postmarket_is_active, shadow_enabled


DEFAULT_MAX_AGE_SECONDS = 600


@dataclass(frozen=True)
class PostmarketHealth:
    healthy: bool
    enabled: bool
    window_active: bool
    detail: str
    heartbeat_age_seconds: float | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evaluate_postmarket_health(
    heartbeat_path: Path,
    *,
    enabled: bool,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> PostmarketHealth:
    now = now or _utc_now()
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    if not enabled:
        return PostmarketHealth(True, False, False, "shadow observer disabled by kill switch")

    active = postmarket_is_active(now)
    if not active:
        return PostmarketHealth(True, True, False, "postmarket window inactive")

    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        raw_ts = payload["ts_utc"]
        status = payload["status"]
        if not isinstance(raw_ts, str) or not isinstance(status, str):
            raise TypeError("ts_utc and status must be strings")
        heartbeat_ts = datetime.fromisoformat(raw_ts)
        if heartbeat_ts.tzinfo is None or heartbeat_ts.utcoffset() is None:
            raise ValueError("ts_utc must be timezone-aware")
    except FileNotFoundError:
        return PostmarketHealth(False, True, True, "no heartbeat during postmarket window")
    except (json.JSONDecodeError, KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
        return PostmarketHealth(False, True, True, f"unreadable postmarket heartbeat: {exc}")

    age = (now - heartbeat_ts).total_seconds()
    if age < 0:
        return PostmarketHealth(
            False, True, True, f"postmarket heartbeat is {-age:.0f}s in the future", age,
        )
    if age >= max_age_seconds:
        return PostmarketHealth(
            False, True, True,
            f"postmarket heartbeat is stale ({age:.0f}s old; limit {max_age_seconds}s)", age,
        )
    if status == "error":
        return PostmarketHealth(False, True, True, "postmarket observer reported an error", age)
    if status not in {"running", "ok"}:
        return PostmarketHealth(
            False, True, True, f"unexpected postmarket heartbeat status: {status}", age,
        )
    return PostmarketHealth(
        True, True, True, f"postmarket heartbeat is fresh ({age:.0f}s old; {status})", age,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", type=Path, default=Path("data/postmarket_heartbeat.json"))
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    try:
        enabled = shadow_enabled()
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
