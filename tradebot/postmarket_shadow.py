"""Default-off, alert-incapable postmarket earnings reaction observer.

This process reads the structured catalyst ledger, evaluates completed SIP
bars, and writes only to ``data/postmarket_shadow.db``.  It deliberately has
no alert, Telegram, outbox, or order-routing dependency.  Enabling customer
delivery is a future release requiring separate approval and shadow evidence.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.events import scheduled_after_hours_earnings_symbols
from tradebot.journal import code_version, connect as connect_journal, new_run_id
from tradebot.marketdata import LiveMarketData
from tradebot.postmarket import (
    ReactionEvaluation,
    connect as connect_shadow,
    evaluate_earnings_reaction,
    fetch_error_evaluation,
    record_shadow_tick,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = REPO_ROOT / "data" / "journal.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "postmarket_heartbeat.json"
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
POLL_SECONDS = 60
IDLE_SECONDS = 300
FINAL_BAR_GRACE = timedelta(minutes=5)
RUN_MODE = "postmarket-shadow"

logger = logging.getLogger("watchtower.postmarket_shadow")


@dataclass(frozen=True)
class ShadowTickResult:
    tick_id: int
    scheduled_symbols: int
    evaluated_symbols: int
    candidate_observations: int
    new_candidates: int
    error_count: int
    latency_ms: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def shadow_enabled(raw: str | None = None) -> bool:
    value = os.environ.get("POSTMARKET_SHADOW_ENABLED", "0") if raw is None else raw
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        "POSTMARKET_SHADOW_ENABLED must be one of 1/0, true/false, yes/no, or on/off"
    )


def postmarket_window(
    now: datetime, *, calendar=CALENDAR,
) -> tuple[date, datetime, datetime] | None:
    """Return the real close-through-final-bar-processing window."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    session = now.astimezone(ET).date()
    if not calendar.is_session(session):
        return None
    close = calendar.session_close(session).to_pydatetime().astimezone(timezone.utc)
    final_close = datetime.combine(session, wall_time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return session, close, final_close + FINAL_BAR_GRACE


def postmarket_is_active(now: datetime, *, calendar=CALENDAR) -> bool:
    window = postmarket_window(now, calendar=calendar)
    return window is not None and window[1] <= now <= window[2]


def _detector_data_feed() -> str:
    from tradebot.vendors.alpaca import DETECTOR_DATA_FEED

    return str(getattr(DETECTOR_DATA_FEED, "value", DETECTOR_DATA_FEED))


def write_heartbeat_atomic(path: Path, payload: dict) -> None:
    """Expose either the previous complete heartbeat or the next one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def run_shadow_tick(
    journal_conn,
    shadow_conn,
    *,
    now: datetime,
    run_id: str,
    version: str | None,
    data_feed: str,
    market_data_factory: Callable[[str, date], LiveMarketData] = LiveMarketData,
    calendar=CALENDAR,
) -> tuple[ShadowTickResult, list[ReactionEvaluation]]:
    """Evaluate every scheduled reporter; one failure never hides its peers."""
    window = postmarket_window(now, calendar=calendar)
    if window is None or not (window[1] <= now <= window[2]):
        raise ValueError("run_shadow_tick requires an active postmarket window")
    session, session_close, _ = window
    symbols = scheduled_after_hours_earnings_symbols(journal_conn, session)
    started = time.perf_counter()
    evaluations: list[ReactionEvaluation] = []
    for symbol in symbols:
        try:
            market_data = market_data_factory(symbol, session)
            snapshot = market_data.intraday_snapshot(symbol, session)
            evaluations.append(
                evaluate_earnings_reaction(
                    symbol,
                    session,
                    snapshot.rth,
                    snapshot.postmarket,
                    session_close=session_close,
                    now=now,
                )
            )
        except Exception as exc:
            logger.exception("postmarket symbol evaluation failed symbol=%s", symbol)
            evaluations.append(fetch_error_evaluation(symbol, session, exc))

    latency_ms = round((time.perf_counter() - started) * 1000)
    completed_utc = now + timedelta(milliseconds=latency_ms)
    tick_id, new_candidates = record_shadow_tick(
        shadow_conn,
        evaluations,
        session=session,
        tick_utc=now,
        completed_utc=completed_utc,
        run_id=run_id,
        run_mode=RUN_MODE,
        code_version=version,
        data_feed=data_feed,
        scheduled_symbols=len(symbols),
        latency_ms=latency_ms,
    )
    result = ShadowTickResult(
        tick_id=tick_id,
        scheduled_symbols=len(symbols),
        evaluated_symbols=len(evaluations),
        candidate_observations=sum(e.outcome == "CANDIDATE" for e in evaluations),
        new_candidates=new_candidates,
        error_count=sum(e.outcome == "FETCH_ERROR" for e in evaluations),
        latency_ms=latency_ms,
    )
    return result, evaluations


def _heartbeat(status: str, now: datetime, **extra) -> dict:
    return {
        "ts_utc": now.isoformat(),
        "status": status,
        "enabled": True,
        "observer": RUN_MODE,
        "code_version": code_version(),
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
        enabled = shadow_enabled()
    except ValueError:
        logger.exception("invalid postmarket shadow configuration")
        return 2

    if not enabled:
        logger.info("postmarket shadow observer disabled by kill switch")
        while True:
            now = _utc_now()
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                {"ts_utc": now.isoformat(), "status": "disabled", "enabled": False},
            )
            time.sleep(IDLE_SECONDS)

    journal_conn = connect_journal(JOURNAL_PATH)
    shadow_conn = connect_shadow()
    run_id = new_run_id()
    version = code_version()
    data_feed = _detector_data_feed()
    logger.info(
        "postmarket shadow observer started revision=%s feed=%s run_id=%s",
        version, data_feed, run_id,
    )

    while True:
        now = _utc_now()
        if not postmarket_is_active(now):
            write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("idle", now))
            time.sleep(IDLE_SECONDS)
            continue

        write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("running", now))
        try:
            result, _ = run_shadow_tick(
                journal_conn,
                shadow_conn,
                now=now,
                run_id=run_id,
                version=version,
                data_feed=data_feed,
            )
        except Exception as exc:
            shadow_conn.rollback()
            logger.exception("postmarket shadow tick failed")
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat("error", _utc_now(), error=f"{type(exc).__name__}: {exc}"[:1000]),
            )
        else:
            logger.info(
                "postmarket_shadow_tick tick=%s scheduled=%s evaluated=%s "
                "candidates=%s new=%s errors=%s latency_ms=%s",
                result.tick_id, result.scheduled_symbols, result.evaluated_symbols,
                result.candidate_observations, result.new_candidates,
                result.error_count, result.latency_ms,
            )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat("ok", _utc_now(), **result.__dict__),
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
