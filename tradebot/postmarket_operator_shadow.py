"""Default-off, owner-only postmarket opportunity notification supervisor."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from tradebot.journal import code_version
from tradebot.postmarket_operator import run_operator_cycle
from tradebot.postmarket_shadow import idle_sleep_seconds, postmarket_is_active, postmarket_window
from tradebot.telegram_bot.db import connect as connect_users


REPO_ROOT = Path(__file__).resolve().parent.parent
SHADOW_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"
USERS_PATH = REPO_ROOT / "data" / "users.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "postmarket_operator_heartbeat.json"
OBSERVER = "postmarket-owner-operator-shadow"
POLL_SECONDS = 15
IDLE_SECONDS = 300

logger = logging.getLogger("watchtower.postmarket_operator_shadow")


def operator_alerts_enabled(raw: str | None = None) -> bool:
    value = os.environ.get("POSTMARKET_OPERATOR_ALERTS_ENABLED", "0") if raw is None else raw
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        "POSTMARKET_OPERATOR_ALERTS_ENABLED must be one of 1/0, true/false, "
        "yes/no, or on/off"
    )


def operator_chat_id(raw: str | None = None) -> int:
    value = os.environ.get("POSTMARKET_OPERATOR_CHAT_ID") if raw is None else raw
    if value is None or not value.strip():
        raise ValueError("POSTMARKET_OPERATOR_CHAT_ID is required when operator alerts are enabled")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("POSTMARKET_OPERATOR_CHAT_ID must be an integer") from exc
    if parsed == 0:
        raise ValueError("POSTMARKET_OPERATOR_CHAT_ID must be nonzero")
    return parsed


def connect_shadow_readonly(path: Path = SHADOW_PATH) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def write_heartbeat_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _heartbeat(status: str, now: datetime, *, enabled: bool, **extra) -> dict:
    return {
        "ts_utc": now.isoformat(),
        "status": status,
        "enabled": enabled,
        "observer": OBSERVER,
        "code_version": code_version() or "unknown",
        **extra,
    }


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    configure_logging()
    try:
        enabled = operator_alerts_enabled()
        chat_id = operator_chat_id() if enabled else None
    except ValueError:
        logger.exception("invalid postmarket operator configuration")
        return 2

    if not enabled:
        logger.info("postmarket operator alerts disabled by kill switch")
        while True:
            now = datetime.now(timezone.utc)
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat("disabled", now, enabled=False),
            )
            time.sleep(IDLE_SECONDS)

    shadow_conn = connect_shadow_readonly()
    users_conn = connect_users(USERS_PATH)
    logger.info("postmarket operator shadow started revision=%s", code_version())
    while True:
        now = datetime.now(timezone.utc)
        if not postmarket_is_active(now):
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat("idle", now, enabled=True),
            )
            time.sleep(idle_sleep_seconds(now))
            continue
        window = postmarket_window(now)
        assert window is not None
        try:
            result = run_operator_cycle(
                shadow_conn,
                users_conn,
                session=window[0],
                chat_id=chat_id,
                now=now,
            )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "ok",
                    now,
                    enabled=True,
                    session=result.session,
                    candidates_seen=result.candidates_seen,
                    eligible_candidates=result.eligible_candidates,
                    alerts_enqueued=result.alerts_enqueued,
                    alerts_deduplicated=result.alerts_deduplicated,
                    stale_candidates=result.stale_candidates,
                ),
            )
            if result.alerts_enqueued:
                logger.info(
                    "postmarket operator alerts enqueued=%s eligible=%s seen=%s",
                    result.alerts_enqueued,
                    result.eligible_candidates,
                    result.candidates_seen,
                )
        except Exception as exc:
            logger.exception("postmarket operator cycle failed")
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "error",
                    now,
                    enabled=True,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                ),
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
