"""Market-aware health probe for the live scanner process.

The runner intentionally stops writing ``data/heartbeat.json`` outside
regular trading hours.  A stale heartbeat is therefore actionable only
while the XNYS market is open; Docker already supervises the runner's
process lifetime at all other times.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as ecals


ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
DEFAULT_MAX_AGE_SECONDS = 900


@dataclass(frozen=True)
class RunnerHealth:
    healthy: bool
    market_open: bool
    detail: str
    heartbeat_age_seconds: float | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def market_is_open(now: datetime, calendar=CALENDAR) -> bool:
    """Return whether *now* falls inside an actual XNYS session."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    session_date = now.astimezone(ET).date()
    if not calendar.is_session(session_date):
        return False
    open_ts = calendar.session_open(session_date).to_pydatetime()
    close_ts = calendar.session_close(session_date).to_pydatetime()
    return open_ts <= now <= close_ts


def evaluate_runner_health(
    heartbeat_path: Path,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    calendar=CALENDAR,
) -> RunnerHealth:
    """Evaluate scanner health without treating intentional idling as a fault."""
    now = now or _utc_now()
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")

    if not market_is_open(now, calendar):
        return RunnerHealth(
            healthy=True,
            market_open=False,
            detail="market closed — heartbeat freshness not required",
        )

    try:
        payload = json.loads(heartbeat_path.read_text())
        raw_ts = payload["ts_utc"]
        if not isinstance(raw_ts, str):
            raise TypeError("ts_utc must be a string")
        heartbeat_ts = datetime.fromisoformat(raw_ts)
        if heartbeat_ts.tzinfo is None or heartbeat_ts.utcoffset() is None:
            raise ValueError("ts_utc must be timezone-aware")
    except FileNotFoundError:
        return RunnerHealth(
            healthy=False,
            market_open=True,
            detail="no heartbeat recorded during RTH",
        )
    except (json.JSONDecodeError, KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
        return RunnerHealth(
            healthy=False,
            market_open=True,
            detail=f"unreadable heartbeat during RTH: {exc}",
        )

    age = (now - heartbeat_ts).total_seconds()
    if age < 0:
        return RunnerHealth(
            healthy=False,
            market_open=True,
            heartbeat_age_seconds=age,
            detail=f"heartbeat is {-age:.0f}s in the future during RTH",
        )
    if age >= max_age_seconds:
        return RunnerHealth(
            healthy=False,
            market_open=True,
            heartbeat_age_seconds=age,
            detail=f"heartbeat is stale during RTH ({age:.0f}s old; limit {max_age_seconds}s)",
        )
    return RunnerHealth(
        healthy=True,
        market_open=True,
        heartbeat_age_seconds=age,
        detail=f"heartbeat is fresh during RTH ({age:.0f}s old)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", type=Path, default=Path("data/heartbeat.json"))
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)

    result = evaluate_runner_health(args.heartbeat, max_age_seconds=args.max_age)
    print(result.detail)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
