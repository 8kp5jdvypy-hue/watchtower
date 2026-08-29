"""Default-off, delivery-incapable postmarket external-context worker."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradebot.events import scheduled_after_hours_earnings_symbols
from tradebot.journal import code_version, connect as connect_journal, new_run_id
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_external_context import (
    latest_external_context_summary,
    run_external_context_backfill,
    run_pre_event_expectation_capture,
)
from tradebot.postmarket_shadow import idle_sleep_seconds, postmarket_is_active, postmarket_window


REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"
JOURNAL_PATH = REPO_ROOT / "data" / "journal.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "postmarket_external_context_heartbeat.json"
POLL_SECONDS = 60
IDLE_SECONDS = 300
PRE_CLOSE_CAPTURE_LEAD = timedelta(minutes=10)
logger = logging.getLogger("watchtower.postmarket_external_context")


def external_context_enabled(raw: str | None = None) -> bool:
    value = os.environ.get("POSTMARKET_EXTERNAL_CONTEXT_ENABLED", "0") if raw is None else raw
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        "POSTMARKET_EXTERNAL_CONTEXT_ENABLED must be one of 1/0, true/false, "
        "yes/no, or on/off"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pre_close_capture_window(now: datetime):
    """Return the actual-calendar final-ten-minute capture window, if active."""
    window = postmarket_window(now)
    if window is None:
        return None
    session, session_close, _ = window
    start = session_close - PRE_CLOSE_CAPTURE_LEAD
    return (session, start, session_close) if start <= now < session_close else None


def _option_fetch(symbol, session, spot):
    from tradebot.vendors.alpaca import fetch_nearest_option_chain

    return fetch_nearest_option_chain(symbol, session, spot)


def _news_fetch(symbol, start, end):
    from tradebot.vendors.alpaca import fetch_news

    return fetch_news(symbol, start, end)


def _quote_fetch(symbols):
    from tradebot.vendors.alpaca import fetch_latest_quotes

    return fetch_latest_quotes(symbols)


def _massive_configured() -> bool:
    from tradebot.vendors.massive import configured

    return configured()


def _independent_fetch(symbol, start, end):
    from tradebot.vendors.massive import fetch_intraday_bars

    return fetch_intraday_bars(symbol, start, end)


def _reference_fetch(symbol, as_of):
    from tradebot.vendors.massive import fetch_ticker_reference

    return fetch_ticker_reference(symbol, as_of)


def _halt_fetch(session):
    from tradebot.vendors.nasdaq_halts import fetch_halts

    return fetch_halts(session)


def _sec_context_configured() -> bool:
    return bool(os.environ.get("SEC_EDGAR_USER_AGENT", "").strip())


def _sec_context_fetch(symbol, cutoff):
    from tradebot.vendors.sec_companyfacts import fetch_point_in_time_snapshot

    return fetch_point_in_time_snapshot(symbol, cutoff)


def write_heartbeat_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _heartbeat(status: str, now: datetime, *, enabled: bool, **extra) -> dict:
    return {
        "ts_utc": now.isoformat(),
        "status": status,
        "enabled": enabled,
        "observer": "postmarket-external-context-shadow",
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
        enabled = external_context_enabled()
    except ValueError:
        logger.exception("invalid postmarket external-context configuration")
        return 2
    if not enabled:
        logger.info("postmarket external-context worker disabled by kill switch")
        while True:
            now = _utc_now()
            write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("disabled", now, enabled=False))
            time.sleep(IDLE_SECONDS)
    conn = connect(DB_PATH)
    journal_conn = connect_journal(JOURNAL_PATH)
    run_id = new_run_id()
    version = code_version()
    logger.info("postmarket external-context worker started revision=%s run_id=%s", version, run_id)
    while True:
        now = _utc_now()
        window = postmarket_window(now)
        if window is not None:
            session, session_close, _ = window
            pre_close_start = session_close - PRE_CLOSE_CAPTURE_LEAD
        else:
            session = session_close = pre_close_start = None
        capture_window = pre_close_capture_window(now)
        if capture_window is not None:
            session, _, session_close = capture_window
            try:
                result = run_pre_event_expectation_capture(
                    conn, session=session, session_close=session_close, now=now,
                    symbols=scheduled_after_hours_earnings_symbols(journal_conn, session),
                    code_version=version, run_id=run_id,
                    quote_fetch=_quote_fetch, option_fetch=_option_fetch,
                )
            except Exception as exc:
                conn.rollback()
                logger.exception("pre-event option expectation capture failed")
                write_heartbeat_atomic(
                    HEARTBEAT_PATH,
                    _heartbeat(
                        "error", _utc_now(), enabled=True,
                        error=f"{type(exc).__name__}: {exc}"[:1000],
                    ),
                )
            else:
                if result.symbols_planned:
                    logger.info(
                        "postmarket_pre_event_options scheduled=%s planned=%s written=%s "
                        "available=%s errors=%s latency_ms=%s",
                        result.scheduled_symbols, result.symbols_planned,
                        result.expectations_written, result.available_expectations,
                        result.fetch_errors, result.latency_ms,
                    )
                write_heartbeat_atomic(
                    HEARTBEAT_PATH,
                    _heartbeat("pre_event", _utc_now(), enabled=True, **asdict(result)),
                )
            time.sleep(POLL_SECONDS)
            continue
        if not postmarket_is_active(now):
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "idle", now, enabled=True,
                    latest_external_context=latest_external_context_summary(conn),
                ),
            )
            sleep_for = idle_sleep_seconds(now)
            if pre_close_start is not None and now < pre_close_start:
                sleep_for = min(sleep_for, max(0.1, (pre_close_start - now).total_seconds()))
            time.sleep(sleep_for)
            continue
        write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("running", now, enabled=True))
        try:
            second_provider_configured = _massive_configured()
            result = run_external_context_backfill(
                conn, now=now, code_version=version, run_id=run_id,
                option_fetch=_option_fetch, news_fetch=_news_fetch,
                independent_fetch=_independent_fetch if second_provider_configured else None,
                reference_fetch=_reference_fetch if second_provider_configured else None,
                filing_context_fetch=(
                    _sec_context_fetch if _sec_context_configured() else None
                ),
                halt_fetch=_halt_fetch,
            )
        except Exception as exc:
            conn.rollback()
            logger.exception("postmarket external-context backfill failed")
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "error", _utc_now(), enabled=True,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                ),
            )
        else:
            if result.candidates_planned:
                logger.info(
                    "postmarket_external_context planned=%s written=%s available=%s "
                    "errors=%s latency_ms=%s",
                    result.candidates_planned, result.facts_written,
                    result.available_facts, result.fetch_errors, result.latency_ms,
                )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "ok", _utc_now(), enabled=True, **asdict(result),
                    latest_external_context=latest_external_context_summary(conn),
                ),
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
