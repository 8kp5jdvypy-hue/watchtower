"""Default-off, alert-incapable market-wide postmarket discovery service."""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tradebot.events import scheduled_after_hours_earnings_symbols
from tradebot.journal import code_version, connect as connect_journal, new_run_id
from tradebot.marketdata import MarketWideScreen, partition_intraday_bars
from tradebot.postmarket import (
    ReactionEvaluation,
    evaluate_postmarket_reaction,
    fetch_error_evaluation,
)
from tradebot.postmarket_discovery import (
    DiscoveryTiming,
    DiscoverySelection,
    connect as connect_discovery,
    plan_tick_schedule,
    record_discovery_tick,
    select_discovery_symbols,
)
from tradebot.postmarket_discovery_audit import write_completed_discovery_audits
from tradebot.postmarket_quality_backfill import (
    latest_quality_report_summaries,
    run_due_quality_backfill,
    write_completed_quality_reports,
)
from tradebot.postmarket_context import latest_context_summary, run_context_backfill
from tradebot.postmarket_recall_census import (
    latest_census_report_summary,
    next_due_census_session,
    run_recall_census,
    write_census_report,
)
from tradebot.postmarket_shadow import idle_sleep_seconds, postmarket_is_active, postmarket_window
from tradebot.universe import active_symbols, connect as connect_universe


REPO_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = REPO_ROOT / "data" / "journal.db"
UNIVERSE_PATH = REPO_ROOT / "data" / "universe.db"
SHADOW_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "postmarket_discovery_heartbeat.json"
AUDIT_DIR = REPO_ROOT / "data" / "postmarket_audits"
POLL_SECONDS = 60
IDLE_SECONDS = 300
RUN_MODE = "postmarket-marketwide-shadow"
SCREEN_TOP_N = 50
MAX_SCREEN_AGE_SECONDS = 180
EXPECTED_ENDPOINTS = {
    "market_movers",
    "most_actives_volume",
    "most_actives_trades",
}
SOURCE_ENDPOINT = {
    "market_gainer": "market_movers",
    "market_loser": "market_movers",
    "most_active_volume": "most_actives_volume",
    "most_active_trades": "most_actives_trades",
}

logger = logging.getLogger("watchtower.postmarket_discovery_shadow")


@dataclass(frozen=True)
class DiscoveryTickResult:
    tick_id: int
    universe_symbols: int
    screen_rows: int
    discovered_symbols: int
    fetched_symbols: int
    evaluated_symbols: int
    candidate_observations: int
    new_candidates: int
    error_count: int
    scheduled_lag_ms: int
    missed_cycles: int
    screen_latency_ms: int
    selection_latency_ms: int
    bar_fetch_latency_ms: int
    evaluation_latency_ms: int
    persistence_span_max_seconds: float | None
    latency_ms: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def active_poll_sleep_seconds(
    now: datetime, *, session_close: datetime, interval_seconds: int = POLL_SECONDS,
) -> float:
    """Sleep to the next exchange-close-anchored slot without accumulating drift."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if session_close.tzinfo is None or session_close.utcoffset() is None:
        raise ValueError("session_close must be timezone-aware")
    elapsed = (now - session_close).total_seconds()
    if elapsed < 0:
        raise ValueError("now must not precede session_close")
    next_slot = math.floor(elapsed / interval_seconds) + 1
    next_tick = session_close + timedelta(seconds=next_slot * interval_seconds)
    return max(0.1, (next_tick - now).total_seconds())


def discovery_enabled(raw: str | None = None) -> bool:
    value = os.environ.get("POSTMARKET_DISCOVERY_ENABLED", "0") if raw is None else raw
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        "POSTMARKET_DISCOVERY_ENABLED must be one of 1/0, true/false, yes/no, or on/off"
    )


def _screen_fetch(top: int) -> MarketWideScreen:
    from tradebot.vendors.alpaca import fetch_marketwide_postmarket_screen

    return fetch_marketwide_postmarket_screen(top)


def _bars_fetch(symbols: list[str], session: date):
    from tradebot.vendors.alpaca import fetch_intraday_bars_bulk

    return fetch_intraday_bars_bulk(symbols, session)


def _census_bars_fetch(symbols: list[str], start: datetime, end: datetime):
    from tradebot.vendors.alpaca import fetch_intraday_bars_window_bulk

    return fetch_intraday_bars_window_bulk(symbols, start=start, end=end)


def _daily_bars_fetch(symbols: list[str]):
    from tradebot.vendors.alpaca import fetch_daily_bars_bulk

    return fetch_daily_bars_bulk(symbols, lookback_days=45)


def _quotes_fetch(symbols: list[str]):
    from tradebot.vendors.alpaca import fetch_latest_quotes

    return fetch_latest_quotes(symbols)


def _data_feed() -> str:
    from tradebot.vendors.alpaca import DETECTOR_DATA_FEED

    return str(getattr(DETECTOR_DATA_FEED, "value", DETECTOR_DATA_FEED))


def _validate_screen(
    screen: MarketWideScreen, *, now: datetime, data_feed: str, top_n: int,
) -> None:
    if screen.provider != "alpaca":
        raise ValueError(f"unexpected market-wide screen provider: {screen.provider!r}")
    if screen.feed != "sip":
        raise ValueError(f"market-wide screener must use SIP, got {screen.feed!r}")
    if data_feed != screen.feed:
        raise ValueError(
            f"market-wide screen/bar feed mismatch: {screen.feed!r} vs {data_feed!r}"
        )
    if screen.requested_top_n != top_n or not 1 <= top_n <= 50:
        raise ValueError("market-wide screen requested bound does not match the tick")
    if len(screen.endpoints) != len(EXPECTED_ENDPOINTS) or set(screen.endpoints) != EXPECTED_ENDPOINTS:
        raise ValueError("market-wide screen endpoint set is incomplete, duplicated, or unexpected")
    updates = dict(screen.source_updates)
    if len(screen.source_updates) != len(EXPECTED_ENDPOINTS) or set(updates) != EXPECTED_ENDPOINTS:
        raise ValueError("market-wide screen source timestamps are incomplete or duplicated")
    for source, updated in updates.items():
        if updated.tzinfo is None or updated.utcoffset() is None:
            raise ValueError(f"market-wide screen timestamp is naive for {source}")
        age = (now - updated).total_seconds()
        if age < 0:
            raise ValueError(f"market-wide screen timestamp is in the future for {source}")
        if age > MAX_SCREEN_AGE_SECONDS:
            raise ValueError(f"market-wide screen timestamp is stale for {source}: {age:.0f}s")
    by_source: dict[str, list] = {source: [] for source in SOURCE_ENDPOINT}
    seen_symbol_source: set[tuple[str, str]] = set()
    for entry in screen.entries:
        if entry.source not in SOURCE_ENDPOINT:
            raise ValueError(f"unknown market-wide screen row source: {entry.source!r}")
        if entry.symbol != entry.symbol.strip().upper() or not entry.symbol:
            raise ValueError("market-wide screen symbol is not canonical")
        if not 1 <= entry.rank <= top_n:
            raise ValueError("market-wide screen rank is outside the requested bound")
        key = (entry.symbol, entry.source)
        if key in seen_symbol_source:
            raise ValueError("market-wide screen duplicated a symbol within one source")
        seen_symbol_source.add(key)
        endpoint = SOURCE_ENDPOINT[entry.source]
        if entry.source_updated_at != updates[endpoint]:
            raise ValueError("market-wide screen row/source timestamps disagree")
        numeric = (
            entry.move_pct, entry.price, entry.volume, entry.trade_count,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("market-wide screen row contains a non-finite metric")
        if entry.source in {"market_gainer", "market_loser"}:
            if entry.move_pct is None or entry.price is None or entry.price <= 0:
                raise ValueError("market mover row is missing price/change provenance")
            if entry.source == "market_gainer" and entry.move_pct <= 0:
                raise ValueError("market gainer row has a non-positive move")
            if entry.source == "market_loser" and entry.move_pct >= 0:
                raise ValueError("market loser row has a non-negative move")
        elif entry.volume is None or entry.trade_count is None:
            raise ValueError("most-active row is missing activity provenance")
        elif entry.volume < 0 or entry.trade_count < 0:
            raise ValueError("most-active row contains a negative metric")
        by_source[entry.source].append(entry.rank)
    for ranks in by_source.values():
        if ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("market-wide screen ranks are duplicated or non-contiguous")


def run_discovery_tick(
    shadow_conn,
    *,
    active_universe: set[str],
    scheduled_earnings: set[str],
    now: datetime,
    run_id: str,
    version: str | None,
    data_feed: str,
    screen_fetch: Callable[[int], MarketWideScreen] = _screen_fetch,
    bars_fetch: Callable[[list[str], date], dict] = _bars_fetch,
    top_n: int = SCREEN_TOP_N,
    clock: Callable[[], float] | None = None,
) -> tuple[DiscoveryTickResult, DiscoverySelection, list[ReactionEvaluation]]:
    """Screen the market, bulk-fetch the bounded union, and conserve every row."""
    window = postmarket_window(now)
    if window is None or not (window[1] <= now <= window[2]):
        raise ValueError("run_discovery_tick requires an active postmarket window")
    session, session_close, _ = window
    timer = clock or time.perf_counter
    started = timer()
    screen = screen_fetch(top_n)
    screen_done = timer()
    _validate_screen(screen, now=now, data_feed=data_feed, top_n=top_n)
    if active_universe and not screen.entries:
        raise ValueError("market-wide screen returned no rows for a non-empty universe")
    selection = select_discovery_symbols(screen, active_universe, scheduled_earnings)
    selection_done = timer()
    symbols = [row.symbol for row in selection.symbols]
    bars_by_symbol = bars_fetch(symbols, session)
    fetch_done = timer()
    evaluations: list[ReactionEvaluation] = []
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            evaluations.append(
                fetch_error_evaluation(symbol, session, RuntimeError("missing from bulk bar response"))
            )
            continue
        try:
            snapshot = partition_intraday_bars(bars)
            evaluations.append(
                evaluate_postmarket_reaction(
                    symbol,
                    session,
                    snapshot.rth,
                    snapshot.postmarket,
                    session_close=session_close,
                    now=now,
                )
            )
        except Exception as exc:
            logger.exception("postmarket discovery evaluation failed symbol=%s", symbol)
            evaluations.append(fetch_error_evaluation(symbol, session, exc))
    evaluation_done = timer()
    screen_latency_ms = round((screen_done - started) * 1000)
    selection_latency_ms = round((selection_done - screen_done) * 1000)
    bar_fetch_latency_ms = round((fetch_done - selection_done) * 1000)
    evaluation_latency_ms = round((evaluation_done - fetch_done) * 1000)
    latency_ms = round((evaluation_done - started) * 1000)
    spans = [
        row.persistence_span_seconds
        for row in evaluations
        if row.persistence_span_seconds is not None
    ]
    schedule = plan_tick_schedule(
        shadow_conn,
        session=session,
        session_close=session_close,
        actual_start=now,
        interval_seconds=POLL_SECONDS,
    )
    timing = DiscoveryTiming(
        schedule=schedule,
        screen_latency_ms=screen_latency_ms,
        selection_latency_ms=selection_latency_ms,
        bar_fetch_latency_ms=bar_fetch_latency_ms,
        evaluation_latency_ms=evaluation_latency_ms,
        persistence_observations=len(spans),
        persistence_span_avg_seconds=(sum(spans) / len(spans) if spans else None),
        persistence_span_max_seconds=(max(spans) if spans else None),
        total_latency_ms=latency_ms,
    )
    completed_utc = now + timedelta(milliseconds=latency_ms)
    tick_id, new_candidates = record_discovery_tick(
        shadow_conn,
        selection,
        evaluations,
        screen=screen,
        fetched_symbols=sum(symbol in bars_by_symbol for symbol in symbols),
        session=session,
        tick_utc=now,
        completed_utc=completed_utc,
        run_id=run_id,
        run_mode=RUN_MODE,
        code_version=version,
        data_feed=data_feed,
        latency_ms=latency_ms,
        timing=timing,
    )
    result = DiscoveryTickResult(
        tick_id=tick_id,
        universe_symbols=selection.universe_symbols,
        screen_rows=selection.screen_rows,
        discovered_symbols=len(selection.symbols),
        fetched_symbols=sum(symbol in bars_by_symbol for symbol in symbols),
        evaluated_symbols=len(evaluations),
        candidate_observations=sum(row.outcome == "CANDIDATE" for row in evaluations),
        new_candidates=new_candidates,
        error_count=sum(row.outcome == "FETCH_ERROR" for row in evaluations),
        scheduled_lag_ms=schedule.scheduled_lag_ms,
        missed_cycles=schedule.missed_cycles,
        screen_latency_ms=screen_latency_ms,
        selection_latency_ms=selection_latency_ms,
        bar_fetch_latency_ms=bar_fetch_latency_ms,
        evaluation_latency_ms=evaluation_latency_ms,
        persistence_span_max_seconds=timing.persistence_span_max_seconds,
        latency_ms=latency_ms,
    )
    return result, selection, evaluations


def write_heartbeat_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}", suffix=".tmp", dir=path.parent)
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


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


def write_due_discovery_audits(now: datetime) -> tuple[dict, ...]:
    """Write immutable discovery reports only after full windows end."""
    reports = write_completed_discovery_audits(
        SHADOW_PATH,
        AUDIT_DIR,
        now=now,
        audit_code_version=code_version(),
    )
    summaries = tuple(
        {
            "session": report.session,
            "operational_clean": report.operational_clean,
            "session_evidence_eligible": report.session_evidence_eligible,
            "issue_codes": [issue.code for issue in report.issues],
        }
        for report in reports
    )
    for summary in summaries:
        logger.info(
            "postmarket_discovery_daily_audit session=%s operational_clean=%s "
            "evidence_eligible=%s issues=%s",
            summary["session"],
            summary["operational_clean"],
            summary["session_evidence_eligible"],
            ",".join(summary["issue_codes"]) or "none",
        )
    return summaries


def latest_discovery_audit_summary() -> dict | None:
    paths = list(AUDIT_DIR.glob("postmarket_discovery_audit_*.json"))
    if not paths:
        return None
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    payload = max(
        payloads,
        key=lambda item: (item["session"], item.get("audit_version", 0)),
    )
    return {
        "session": payload["session"],
        "operational_clean": payload["operational_clean"],
        "session_evidence_eligible": payload["session_evidence_eligible"],
        "issue_codes": [issue["code"] for issue in payload["issues"]],
    }


def discovery_audit_heartbeat_fields(now: datetime) -> dict:
    """Keep the latest discovery audit verdict visible in the heartbeat."""
    try:
        written = write_due_discovery_audits(now)
        return {
            "audit_status": "written" if written else "current",
            "audits_written": len(written),
            "latest_audit": latest_discovery_audit_summary(),
        }
    except Exception as exc:
        logger.exception("postmarket discovery daily audit failed")
        return {
            "audit_status": "error",
            "audit_error": f"{type(exc).__name__}: {exc}"[:1000],
        }


def quality_backfill_heartbeat_fields(
    now: datetime,
    shadow_conn,
    *,
    data_feed: str,
    version: str | None,
    run_id: str,
    bars_fetch: Callable[[list[str], date], dict] = _bars_fetch,
) -> dict:
    """Run bounded outcome maintenance and make every failure visible."""
    try:
        result = run_due_quality_backfill(
            shadow_conn,
            now=now,
            data_feed=data_feed,
            code_version=version,
            run_id=run_id,
            bars_fetch=bars_fetch,
        )
        reports = write_completed_quality_reports(
            shadow_conn,
            AUDIT_DIR,
            now=now,
            report_code_version=version,
        )
        if result.candidates_planned or reports:
            logger.info(
                "postmarket_quality_backfill candidates=%s fetch_sessions=%s "
                "symbols_fetched=%s marks_written=%s unresolved=%s errors=%s "
                "reports_written=%s latency_ms=%s",
                result.candidates_planned,
                result.candidate_sessions_fetched,
                result.symbols_fetched,
                result.marks_written,
                result.unresolved_checkpoints,
                result.fetch_errors,
                len(reports),
                result.latency_ms,
            )
        return {
            "quality_backfill_status": (
                "degraded"
                if result.fetch_errors or result.unresolved_checkpoints
                else "current"
            ),
            "quality_candidates_planned": result.candidates_planned,
            "quality_marks_written": result.marks_written,
            "quality_unresolved_checkpoints": result.unresolved_checkpoints,
            "quality_fetch_errors": result.fetch_errors,
            "quality_fetch_error_details": list(result.fetch_error_details[:20]),
            "quality_reports_written": len(reports),
            "latest_quality_reports": list(
                latest_quality_report_summaries(AUDIT_DIR)
            ),
            "quality_latency_ms": result.latency_ms,
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("postmarket quality backfill failed")
        return {
            "quality_backfill_status": "error",
            "quality_backfill_error": f"{type(exc).__name__}: {exc}"[:1000],
        }


def recall_census_heartbeat_fields(
    now: datetime,
    shadow_conn,
    universe_conn,
    *,
    data_feed: str,
    version: str | None,
    bars_fetch: Callable[[list[str], datetime, datetime], dict] = _census_bars_fetch,
) -> dict:
    """Run at most one finalized full-universe census attempt per idle cycle."""
    try:
        due = next_due_census_session(shadow_conn, now=now)
        if due is None:
            return {
                "recall_census_status": "current",
                "latest_recall_census": latest_census_report_summary(AUDIT_DIR),
            }
        session, session_close, postmarket_end = due
        result, _ = run_recall_census(
            shadow_conn,
            universe_symbols=active_symbols(universe_conn),
            session=session,
            session_close=session_close,
            postmarket_end=postmarket_end,
            now=now,
            run_id=new_run_id(),
            code_version=version,
            data_feed=data_feed,
            bars_fetch=bars_fetch,
        )
        report, written = write_census_report(
            shadow_conn, AUDIT_DIR, result.census_id
        )
        logger.info(
            "postmarket_recall_census session=%s attempt=%s status=%s "
            "universe=%s fetched=%s eligible_pairs=%s tp=%s fn=%s fp=%s "
            "recall=%s unavailable=%s errors=%s latency_ms=%s report_written=%s",
            result.session,
            result.attempt,
            result.status,
            result.universe_symbols,
            result.fetched_symbols,
            result.eligible_pairs,
            result.true_positive_pairs,
            result.false_negative_pairs,
            result.false_positive_pairs,
            result.recall,
            result.unavailable_symbols,
            result.error_count,
            result.latency_ms,
            written,
        )
        return {
            "recall_census_status": result.status,
            "recall_census_session": result.session,
            "recall_census_attempt": result.attempt,
            "recall_census_universe": result.universe_symbols,
            "recall_census_fetched": result.fetched_symbols,
            "recall_census_eligible_pairs": result.eligible_pairs,
            "recall_census_false_negatives": result.false_negative_pairs,
            "recall_census_recall": result.recall,
            "recall_census_unavailable": result.unavailable_symbols,
            "recall_census_errors": result.error_count,
            "recall_census_latency_ms": result.latency_ms,
            "recall_census_report_written": written,
            "latest_recall_census": {
                "session": report.session,
                "report_version": report.report_version,
                "operational_complete": report.operational_complete,
                "evidence_eligible": report.evidence_eligible,
                "recall": report.metrics["recall"],
                "false_negative_pairs": report.metrics["false_negative_pairs"],
                "unavailable_symbols": report.metrics["unavailable_symbols"],
                "issue_codes": list(report.issue_codes),
            },
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("postmarket recall census failed")
        try:
            latest = latest_census_report_summary(AUDIT_DIR)
        except Exception as summary_exc:
            latest = {
                "status": "error",
                "error": f"{type(summary_exc).__name__}: {summary_exc}"[:1000],
            }
        return {
            "recall_census_status": "error",
            "recall_census_error": f"{type(exc).__name__}: {exc}"[:1000],
            "latest_recall_census": latest,
        }


def context_backfill_heartbeat_fields(
    now: datetime,
    shadow_conn,
    journal_conn,
    universe_conn,
    *,
    version: str | None,
    intraday_fetch: Callable[[list[str], date], dict] = _bars_fetch,
    daily_fetch: Callable[[list[str]], dict] = _daily_bars_fetch,
    quote_fetch: Callable[[list[str]], dict] = _quotes_fetch,
) -> dict:
    """Enrich a bounded candidate batch without affecting qualification."""
    try:
        result = run_context_backfill(
            shadow_conn,
            journal_conn,
            universe_conn,
            now=now,
            code_version=version,
            intraday_fetch=intraday_fetch,
            daily_fetch=daily_fetch,
            quote_fetch=quote_fetch,
        )
        if result.candidates_planned:
            logger.info(
                "postmarket_context_backfill planned=%s written=%s degraded=%s "
                "fetch_errors=%s latency_ms=%s",
                result.candidates_planned,
                result.contexts_written,
                result.degraded_contexts,
                result.fetch_errors,
                result.latency_ms,
            )
        return {
            "context_backfill_status": (
                "degraded" if result.degraded_contexts else "current"
            ),
            "context_candidates_planned": result.candidates_planned,
            "contexts_written": result.contexts_written,
            "context_degraded": result.degraded_contexts,
            "context_fetch_errors": result.fetch_errors,
            "context_latency_ms": result.latency_ms,
            "latest_context": latest_context_summary(shadow_conn),
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("postmarket context backfill failed")
        return {
            "context_backfill_status": "error",
            "context_backfill_error": f"{type(exc).__name__}: {exc}"[:1000],
        }


def main() -> int:
    configure_logging()
    try:
        enabled = discovery_enabled()
    except ValueError:
        logger.exception("invalid market-wide postmarket discovery configuration")
        return 2
    if not enabled:
        logger.info("market-wide postmarket discovery disabled by kill switch")
        while True:
            now = _utc_now()
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                {
                    "ts_utc": now.isoformat(),
                    "status": "disabled",
                    "enabled": False,
                    **discovery_audit_heartbeat_fields(now),
                },
            )
            time.sleep(IDLE_SECONDS)

    journal_conn = connect_journal(JOURNAL_PATH)
    universe_conn = connect_universe(UNIVERSE_PATH)
    shadow_conn = connect_discovery(SHADOW_PATH)
    run_id = new_run_id()
    version = code_version()
    data_feed = _data_feed()
    logger.info(
        "market-wide postmarket discovery started revision=%s feed=%s run_id=%s",
        version,
        data_feed,
        run_id,
    )
    while True:
        now = _utc_now()
        if not postmarket_is_active(now):
            context_fields = context_backfill_heartbeat_fields(
                now,
                shadow_conn,
                journal_conn,
                universe_conn,
                version=version,
            )
            quality_fields = quality_backfill_heartbeat_fields(
                now,
                shadow_conn,
                data_feed=data_feed,
                version=version,
                run_id=run_id,
            )
            census_fields = recall_census_heartbeat_fields(
                now,
                shadow_conn,
                universe_conn,
                data_feed=data_feed,
                version=version,
            )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "idle",
                    now,
                    **discovery_audit_heartbeat_fields(now),
                    **context_fields,
                    **quality_fields,
                    **census_fields,
                ),
            )
            time.sleep(idle_sleep_seconds(now))
            continue
        write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("running", now))
        try:
            window = postmarket_window(now)
            assert window is not None
            session, session_close, _ = window
            result, _, _ = run_discovery_tick(
                shadow_conn,
                active_universe=set(active_symbols(universe_conn)),
                scheduled_earnings=set(
                    scheduled_after_hours_earnings_symbols(journal_conn, session)
                ),
                now=now,
                run_id=run_id,
                version=version,
                data_feed=data_feed,
            )
        except Exception as exc:
            shadow_conn.rollback()
            logger.exception("market-wide postmarket discovery tick failed")
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat("error", _utc_now(), error=f"{type(exc).__name__}: {exc}"[:1000]),
            )
        else:
            logger.info(
                "postmarket_discovery_tick tick=%s universe=%s screen_rows=%s "
                "discovered=%s fetched=%s evaluated=%s candidates=%s new=%s "
                "errors=%s lag_ms=%s missed=%s screen_ms=%s selection_ms=%s "
                "fetch_ms=%s evaluation_ms=%s persistence_max_s=%s total_ms=%s",
                result.tick_id,
                result.universe_symbols,
                result.screen_rows,
                result.discovered_symbols,
                result.fetched_symbols,
                result.evaluated_symbols,
                result.candidate_observations,
                result.new_candidates,
                result.error_count,
                result.scheduled_lag_ms,
                result.missed_cycles,
                result.screen_latency_ms,
                result.selection_latency_ms,
                result.bar_fetch_latency_ms,
                result.evaluation_latency_ms,
                result.persistence_span_max_seconds,
                result.latency_ms,
            )
            completed_now = _utc_now()
            context_fields = context_backfill_heartbeat_fields(
                completed_now,
                shadow_conn,
                journal_conn,
                universe_conn,
                version=version,
            )
            quality_fields = quality_backfill_heartbeat_fields(
                completed_now,
                shadow_conn,
                data_feed=data_feed,
                version=version,
                run_id=run_id,
            )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "ok", completed_now, **result.__dict__, **context_fields,
                    **quality_fields
                ),
            )
        sleep_now = _utc_now()
        sleep_window = postmarket_window(sleep_now)
        sleep_close = sleep_window[1] if sleep_window is not None else session_close
        time.sleep(
            active_poll_sleep_seconds(sleep_now, session_close=sleep_close)
        )


if __name__ == "__main__":
    sys.exit(main())
