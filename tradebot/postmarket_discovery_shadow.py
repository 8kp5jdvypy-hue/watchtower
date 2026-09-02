"""Default-off, alert-incapable market-wide postmarket discovery service."""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tradebot.events import scheduled_after_hours_earnings_symbols
from tradebot.journal import code_version, connect as connect_journal, new_run_id
from tradebot.marketdata import MarketWideScreen, partition_intraday_bars
from tradebot.marketwide_screen import SOURCE_ENDPOINT, validate_marketwide_screen
from tradebot.postmarket import (
    OUTCOME_IDENTITY_UNVERIFIED,
    OUTCOME_NO_BARS_RETURNED,
    ReactionEvaluation,
    evaluate_postmarket_reaction,
    fetch_error_evaluation,
)
from tradebot.postmarket_discovery import (
    FULL_UNIVERSE_SWEEP_CYCLE_TICKS,
    FULL_UNIVERSE_SWEEP_SOURCE,
    DiscoveryTiming,
    DiscoverySelection,
    connect as connect_discovery,
    plan_tick_schedule,
    plan_universe_sweep,
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
from tradebot.postmarket_lifecycle import (
    latest_open_session,
    lifecycle_summary,
    lifecycle_window,
    run_lifecycle_pass,
)
from tradebot.postmarket_rank import latest_rank_summary, run_rank_snapshot
from tradebot.postmarket_recall_census import (
    latest_census_report_summary,
    next_due_census_session,
    run_recall_census,
    write_census_report,
)
from tradebot.postmarket_recall_provider import (
    latest_provider_proof_summary,
    next_due_provider_proof,
    run_provider_proof,
    write_provider_proof_report,
)
from tradebot.postmarket_shadow import idle_sleep_seconds, postmarket_is_active, postmarket_window
from tradebot.rth_momentum import (
    FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS,
    ensure_rth_schema,
    latest_rth_handoff_summary,
    reconcile_rth_postmarket_handoffs,
    rth_handoff_is_active,
    rth_handoff_window,
    rth_poll_sleep_seconds,
    run_rth_momentum_tick,
)
from tradebot.rth_momentum_audit import (
    latest_rth_audit_summary,
    rth_audit_session_due,
    write_completed_rth_audits,
)
from tradebot.rth_missed_mover_census import (
    latest_rth_missed_mover_census_summary,
    next_unreported_rth_missed_mover_census,
    next_due_rth_missed_mover_census_session,
    run_rth_missed_mover_census,
    write_rth_missed_mover_census_report,
)
from tradebot.universe import (
    active_symbols,
    connect as connect_universe,
    symbols_first_seen_on,
)


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

logger = logging.getLogger("watchtower.postmarket_discovery_shadow")


@dataclass(frozen=True)
class DiscoveryTickResult:
    tick_id: int
    universe_symbols: int
    screen_rows: int
    provider_screen_unique_symbols: int
    sweep_shard_index: int | None
    sweep_shard_count: int | None
    sweep_shard_symbols: int
    sweep_overlap_symbols: int
    discovered_symbols: int
    fetched_symbols: int
    evaluated_symbols: int
    candidate_observations: int
    new_candidates: int
    identity_quarantined: int
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


def discovery_idle_sleep_seconds(now: datetime) -> float:
    """Wake at final-RTH coverage start, not merely at the exchange close."""
    window = rth_handoff_window(now)
    if window is not None and now < window[1]:
        return max(0.1, min(float(IDLE_SECONDS), (window[1] - now).total_seconds()))
    return idle_sleep_seconds(now)


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
    from tradebot.vendors.alpaca import fetch_marketwide_screen

    return fetch_marketwide_screen(top)


def _bars_fetch(symbols: list[str], session: date):
    from tradebot.vendors.alpaca import fetch_intraday_bars_bulk

    return fetch_intraday_bars_bulk(symbols, session)


def _sweep_bars_fetch(symbols: list[str], start: datetime, end: datetime):
    from tradebot.vendors.alpaca import fetch_intraday_bars_window_bulk

    return fetch_intraday_bars_window_bulk(symbols, start=start, end=end)


def _census_bars_fetch(symbols: list[str], start: datetime, end: datetime):
    from tradebot.vendors.alpaca import fetch_intraday_bars_window_bulk

    return fetch_intraday_bars_window_bulk(symbols, start=start, end=end)


def _provider_flatfile_configured() -> bool:
    from tradebot.vendors.historical_reference import source

    return source().configured()


def _provider_flatfile_fetch(session, symbols, start, end):
    from tradebot.vendors.historical_reference import source

    return source().fetch(session, symbols, start, end)


def _daily_bars_fetch(symbols: list[str]):
    from tradebot.vendors.alpaca import fetch_daily_bars_bulk

    return fetch_daily_bars_bulk(symbols, lookback_days=45)


def _rth_daily_bars_fetch(symbols: list[str]):
    from tradebot.vendors.alpaca import fetch_daily_bars_bulk

    return fetch_daily_bars_bulk(symbols, lookback_days=10)


def _quotes_fetch(symbols: list[str]):
    from tradebot.vendors.alpaca import fetch_latest_quotes

    return fetch_latest_quotes(symbols)


def _data_feed() -> str:
    from tradebot.vendors.alpaca import DETECTOR_DATA_FEED

    return str(getattr(DETECTOR_DATA_FEED, "value", DETECTOR_DATA_FEED))


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
    sweep_bars_fetch: Callable[[list[str], datetime, datetime], dict] = _sweep_bars_fetch,
    sweep_cycle_ticks: int | None = None,
    top_n: int = SCREEN_TOP_N,
    clock: Callable[[], float] | None = None,
    validation_now_fn: Callable[[], datetime] | None = None,
    identity_quarantine_symbols: frozenset[str] | set[str] = frozenset(),
) -> tuple[DiscoveryTickResult, DiscoverySelection, list[ReactionEvaluation]]:
    """Union the bounded screen and one universe shard, then conserve every row.

    ``now`` anchors the exchange session and deterministic tick schedule. Live
    callers additionally provide ``validation_now_fn`` so provider timestamps
    are checked against the wall clock *after* the screen request completes.
    Replay and tests may omit it to keep using their injected historical clock.
    """
    window = postmarket_window(now)
    if window is None or not (window[1] <= now <= window[2]):
        raise ValueError("run_discovery_tick requires an active postmarket window")
    session, session_close, _ = window
    schedule = plan_tick_schedule(
        shadow_conn,
        session=session,
        session_close=session_close,
        actual_start=now,
        interval_seconds=POLL_SECONDS,
    )
    universe_sweep = (
        plan_universe_sweep(
            active_universe,
            scheduled_tick_utc=schedule.scheduled_tick_utc,
            session_close=session_close,
            cycle_ticks=sweep_cycle_ticks,
        )
        if sweep_cycle_ticks is not None
        else None
    )
    timer = clock or time.perf_counter
    started = timer()
    screen = screen_fetch(top_n)
    screen_done = timer()
    validation_now = validation_now_fn() if validation_now_fn is not None else now
    validate_marketwide_screen(
        screen, now=validation_now, data_feed=data_feed, top_n=top_n
    )
    if active_universe and not screen.entries:
        raise ValueError("market-wide screen returned no rows for a non-empty universe")
    selection = select_discovery_symbols(
        screen,
        active_universe,
        scheduled_earnings,
        universe_sweep,
    )
    selection_done = timer()
    symbols = [row.symbol for row in selection.symbols]
    bounded_symbols = [
        row.symbol
        for row in selection.symbols
        if any(source in SOURCE_ENDPOINT for source in row.sources)
    ]
    bounded_symbol_set = set(bounded_symbols)
    sweep_only_symbols = [
        row.symbol
        for row in selection.symbols
        if FULL_UNIVERSE_SWEEP_SOURCE in row.sources
        and row.symbol not in bounded_symbol_set
    ]
    sweep_only_symbol_set = set(sweep_only_symbols)
    bars_by_symbol = bars_fetch(bounded_symbols, session)
    sweep_failures: dict[str, Exception] = {}
    if sweep_only_symbols:
        try:
            sweep_bars = sweep_bars_fetch(
                sweep_only_symbols,
                session_close - timedelta(minutes=5),
                now,
            )
        except Exception as exc:
            logger.exception("full-universe sweep bar fetch failed")
            sweep_failures = {symbol: exc for symbol in sweep_only_symbols}
        else:
            duplicate = set(bars_by_symbol) & set(sweep_bars)
            if duplicate:
                raise ValueError(
                    f"bounded and sweep bar responses overlap unexpectedly: {sorted(duplicate)[:5]}"
                )
            bars_by_symbol.update(sweep_bars)
    fetch_done = timer()
    evaluations: list[ReactionEvaluation] = []
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            if symbol in sweep_only_symbol_set and symbol not in sweep_failures:
                evaluations.append(
                    ReactionEvaluation(
                        symbol=symbol,
                        event_date=session,
                        outcome=OUTCOME_NO_BARS_RETURNED,
                        reason="no bars returned for full-universe sweep window",
                    )
                )
                continue
            failure = sweep_failures.get(
                symbol,
                RuntimeError("missing from bulk bar response"),
            )
            evaluations.append(
                fetch_error_evaluation(symbol, session, failure)
            )
            continue
        try:
            snapshot = partition_intraday_bars(bars)
            evaluation = evaluate_postmarket_reaction(
                symbol,
                session,
                snapshot.rth,
                snapshot.postmarket,
                session_close=session_close,
                now=now,
            )
            if (
                symbol in identity_quarantine_symbols
                and evaluation.outcome == "CANDIDATE"
            ):
                evaluation = replace(
                    evaluation,
                    outcome=OUTCOME_IDENTITY_UNVERIFIED,
                    reason=(
                        "asset identity was first observed during this session; "
                        "price adjustment, currency, and security-class basis are "
                        "not yet established"
                    ),
                )
            evaluations.append(evaluation)
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
        provider_screen_unique_symbols=selection.provider_screen_unique_symbols,
        sweep_shard_index=(universe_sweep.shard_index if universe_sweep else None),
        sweep_shard_count=(universe_sweep.shard_count if universe_sweep else None),
        sweep_shard_symbols=(len(universe_sweep.symbols) if universe_sweep else 0),
        sweep_overlap_symbols=selection.sweep_overlap_symbols,
        discovered_symbols=len(selection.symbols),
        fetched_symbols=sum(symbol in bars_by_symbol for symbol in symbols),
        evaluated_symbols=len(evaluations),
        candidate_observations=sum(row.outcome == "CANDIDATE" for row in evaluations),
        new_candidates=new_candidates,
        identity_quarantined=sum(
            row.outcome == OUTCOME_IDENTITY_UNVERIFIED for row in evaluations
        ),
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


def rth_audit_heartbeat_fields(
    now: datetime,
    *,
    expected_session: date | None = None,
) -> dict:
    """Write and surface immutable final-RTH coverage and handoff audits."""
    try:
        reports = write_completed_rth_audits(
            SHADOW_PATH,
            AUDIT_DIR,
            now=now,
            audit_code_version=code_version(),
            expected_sessions=((expected_session,) if expected_session else ()),
        )
        for report in reports:
            logger.info(
                "rth_momentum_daily_audit session=%s operational_clean=%s "
                "evidence_eligible=%s issues=%s",
                report.session,
                report.operational_clean,
                report.session_evidence_eligible,
                ",".join(issue.code for issue in report.issues) or "none",
            )
        return {
            "rth_audit_status": "written" if reports else "current",
            "rth_audits_written": len(reports),
            "latest_rth_audit": latest_rth_audit_summary(AUDIT_DIR),
        }
    except Exception as exc:
        logger.exception("final-RTH momentum daily audit failed")
        return {
            "rth_audit_status": "error",
            "rth_audit_error": f"{type(exc).__name__}: {exc}"[:1000],
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


def rth_missed_mover_census_heartbeat_fields(
    now: datetime,
    shadow_conn,
    universe_conn,
    *,
    data_feed: str,
    version: str | None,
    daily_fetch: Callable[[list[str]], dict] = _daily_bars_fetch,
) -> dict:
    """Run one finalized full-universe daily-mover census per idle cycle."""
    try:
        due = next_due_rth_missed_mover_census_session(shadow_conn, now=now)
        if due is None:
            unreported = next_unreported_rth_missed_mover_census(
                shadow_conn, AUDIT_DIR
            )
            report_written = False
            if unreported is not None:
                _, report_written = write_rth_missed_mover_census_report(
                    shadow_conn, AUDIT_DIR, unreported
                )
            return {
                "rth_missed_mover_census_status": "current",
                "rth_missed_mover_census_report_written": report_written,
                "latest_rth_missed_mover_census": (
                    latest_rth_missed_mover_census_summary(AUDIT_DIR)
                ),
            }
        session, _, postmarket_end = due
        result, _ = run_rth_missed_mover_census(
            shadow_conn,
            universe_symbols=active_symbols(universe_conn),
            session=session,
            postmarket_end=postmarket_end,
            now=now,
            run_id=new_run_id(),
            code_version=version,
            data_feed=data_feed,
            daily_fetch=daily_fetch,
        )
        report, written = write_rth_missed_mover_census_report(
            shadow_conn, AUDIT_DIR, result.census_id
        )
        logger.info(
            "rth_missed_mover_census session=%s attempt=%s status=%s "
            "universe=%s fetched=%s major_pairs=%s caught=%s missed=%s "
            "recall=%s excursions=%s unavailable=%s errors=%s latency_ms=%s "
            "report_written=%s",
            result.session,
            result.attempt,
            result.status,
            result.universe_symbols,
            result.fetched_symbols,
            result.major_close_pairs,
            result.caught_pairs,
            result.missed_pairs,
            result.close_recall,
            result.excursion_only_symbols,
            result.unavailable_symbols,
            result.error_count,
            result.latency_ms,
            written,
        )
        return {
            "rth_missed_mover_census_status": result.status,
            "rth_missed_mover_census_session": result.session,
            "rth_missed_mover_census_attempt": result.attempt,
            "rth_missed_mover_census_universe": result.universe_symbols,
            "rth_missed_mover_census_fetched": result.fetched_symbols,
            "rth_missed_mover_census_major_pairs": result.major_close_pairs,
            "rth_missed_mover_census_caught_pairs": result.caught_pairs,
            "rth_missed_mover_census_missed_pairs": result.missed_pairs,
            "rth_missed_mover_census_recall": result.close_recall,
            "rth_missed_mover_census_excursions": result.excursion_only_symbols,
            "rth_missed_mover_census_unavailable": result.unavailable_symbols,
            "rth_missed_mover_census_errors": result.error_count,
            "rth_missed_mover_census_latency_ms": result.latency_ms,
            "rth_missed_mover_census_report_written": written,
            "latest_rth_missed_mover_census": {
                "session": report.session,
                "report_version": report.report_version,
                "operational_complete": report.operational_complete,
                "quality_evidence_eligible": report.quality_evidence_eligible,
                "close_recall": report.metrics["close_recall"],
                "missed_pairs": report.metrics["missed_pairs"],
                "unavailable_symbols": report.metrics["unavailable_symbols"],
                "issue_codes": list(report.issue_codes),
            },
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("RTH missed-mover census failed")
        try:
            latest = latest_rth_missed_mover_census_summary(AUDIT_DIR)
        except Exception as summary_exc:
            latest = {
                "status": "error",
                "error": f"{type(summary_exc).__name__}: {summary_exc}"[:1000],
            }
        return {
            "rth_missed_mover_census_status": "error",
            "rth_missed_mover_census_error": f"{type(exc).__name__}: {exc}"[:1000],
            "latest_rth_missed_mover_census": latest,
        }


def provider_proof_heartbeat_fields(
    now: datetime,
    shadow_conn,
    *,
    version: str | None,
    primary_fetch: Callable[[list[str], datetime, datetime], dict] = _census_bars_fetch,
    independent_fetch: Callable = _provider_flatfile_fetch,
    provider_configured: Callable[[], bool] = _provider_flatfile_configured,
) -> dict:
    """Run at most one next-day full-universe provider proof per idle cycle."""
    try:
        if not provider_configured():
            return {
                "provider_proof_status": "unconfigured",
                "latest_provider_proof": latest_provider_proof_summary(AUDIT_DIR),
            }
        due = next_due_provider_proof(shadow_conn, now=now)
        if due is None:
            return {
                "provider_proof_status": "current",
                "latest_provider_proof": latest_provider_proof_summary(AUDIT_DIR),
            }
        census_id, session = due
        result, _ = run_provider_proof(
            shadow_conn,
            census_id=census_id,
            session=session,
            now=now,
            run_id=new_run_id(),
            code_version=version,
            primary_fetch=primary_fetch,
            independent_fetch=independent_fetch,
        )
        report, written = write_provider_proof_report(
            shadow_conn, AUDIT_DIR, result.comparison_id,
        )
        logger.info(
            "postmarket_provider_proof session=%s attempt=%s status=%s "
            "universe=%s comparable=%s coverage=%s primary_pairs=%s "
            "independent_pairs=%s agreement=%s independent_recall=%s "
            "price_disagreements=%s errors=%s latency_ms=%s report_written=%s",
            result.session, result.attempt, result.status, result.universe_symbols,
            result.comparable_symbols, result.comparable_coverage,
            result.primary_eligible_pairs, result.independent_eligible_pairs,
            result.eligible_pair_agreement, result.independent_recall,
            result.price_disagreement_bars, result.error_count, result.latency_ms,
            written,
        )
        return {
            "provider_proof_status": result.status,
            "provider_proof_session": result.session,
            "provider_proof_attempt": result.attempt,
            "provider_proof_universe": result.universe_symbols,
            "provider_proof_comparable": result.comparable_symbols,
            "provider_proof_coverage": result.comparable_coverage,
            "provider_proof_pair_agreement": result.eligible_pair_agreement,
            "provider_proof_independent_recall": result.independent_recall,
            "provider_proof_price_disagreements": result.price_disagreement_bars,
            "provider_proof_errors": result.error_count,
            "provider_proof_latency_ms": result.latency_ms,
            "provider_proof_report_written": written,
            "latest_provider_proof": {
                "session": report.session,
                "report_version": report.report_version,
                "operational_complete": report.operational_complete,
                "evidence_eligible": report.evidence_eligible,
                "independent_recall": report.metrics["independent_recall"],
                "eligible_pair_agreement": report.metrics["eligible_pair_agreement"],
                "comparable_coverage": report.metrics["comparable_coverage"],
                "issue_codes": list(report.issue_codes),
            },
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("postmarket full-universe provider proof failed")
        try:
            latest = latest_provider_proof_summary(AUDIT_DIR)
        except Exception as summary_exc:
            latest = {
                "status": "error",
                "error": f"{type(summary_exc).__name__}: {summary_exc}"[:1000],
            }
        return {
            "provider_proof_status": "error",
            "provider_proof_error": f"{type(exc).__name__}: {exc}"[:1000],
            "latest_provider_proof": latest,
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
    observation_clock: Callable[[], datetime] = _utc_now,
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
            observation_clock=observation_clock,
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


def lifecycle_heartbeat_fields(
    now: datetime,
    shadow_conn,
    *,
    data_feed: str,
    version: str | None,
    run_id: str,
    existing_evaluations: tuple[ReactionEvaluation, ...] = (),
    bars_fetch: Callable[[list[str], date], dict] = _bars_fetch,
) -> dict:
    """Observe every open candidate, including symbols absent from Stage 1."""
    try:
        active_window = postmarket_window(now)
        if active_window is not None and active_window[1] <= now <= active_window[2]:
            session, session_close, window_end = active_window
        else:
            session = latest_open_session(shadow_conn)
            if session is None:
                return {
                    "lifecycle_status": "current",
                    "latest_lifecycle": lifecycle_summary(shadow_conn),
                }
            session_close, window_end = lifecycle_window(session)
        result = run_lifecycle_pass(
            shadow_conn,
            session=session,
            session_close=session_close,
            window_end=window_end,
            now=now,
            code_version=version,
            run_id=run_id,
            data_feed=data_feed,
            bars_fetch=bars_fetch,
            existing_evaluations=existing_evaluations,
        )
        if result.tracked_candidates or result.transitions_written:
            logger.info(
                "postmarket_lifecycle session=%s tracked=%s fetched=%s "
                "observations=%s transitions=%s states=%s errors=%s latency_ms=%s",
                result.session,
                result.tracked_candidates,
                result.symbols_fetched,
                result.observations_written,
                result.transitions_written,
                dict(result.states_written),
                result.error_count,
                result.latency_ms,
            )
        return {
            "lifecycle_status": "degraded" if result.error_count else "current",
            "lifecycle_session": result.session,
            "lifecycle_tracked": result.tracked_candidates,
            "lifecycle_symbols_fetched": result.symbols_fetched,
            "lifecycle_observations_written": result.observations_written,
            "lifecycle_transitions_written": result.transitions_written,
            "lifecycle_states_written": dict(result.states_written),
            "lifecycle_errors": result.error_count,
            "lifecycle_latency_ms": result.latency_ms,
            "latest_lifecycle": lifecycle_summary(shadow_conn),
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("postmarket lifecycle maintenance failed")
        return {
            "lifecycle_status": "error",
            "lifecycle_error": f"{type(exc).__name__}: {exc}"[:1000],
        }


def rank_heartbeat_fields(
    now: datetime,
    shadow_conn,
    *,
    version: str | None,
    run_id: str,
) -> dict:
    """Persist a rank only when source evidence or freshness state changes."""
    try:
        row = shadow_conn.execute(
            "SELECT MAX(session) FROM postmarket_discovery_candidates"
        ).fetchone()
        session = row[0] if row and row[0] else None
        if session is None:
            return {"rank_status": "current", "latest_rank": None}
        result = run_rank_snapshot(
            shadow_conn,
            session=session,
            as_of=now,
            code_version=version,
            run_id=run_id,
        )
        if result.created:
            logger.info(
                "postmarket_rank_snapshot run=%s session=%s inputs=%s rankable=%s "
                "status=%s top=%s",
                result.rank_run_id,
                result.session,
                result.input_candidates,
                result.rankable_candidates,
                result.status,
                list(result.top_candidates),
            )
        latest = latest_rank_summary(shadow_conn)
        return {
            "rank_status": result.status,
            "rank_snapshot_created": result.created,
            "rank_run_id": result.rank_run_id,
            "rank_input_candidates": result.input_candidates,
            "rankable_candidates": result.rankable_candidates,
            "rank_top": list(result.top_candidates),
            "rank_session_peak_rankable_candidates": (
                latest["session_peak_rankable_candidates"] if latest else 0
            ),
            "rank_session_rankable_runs": (
                latest["session_rankable_runs"] if latest else 0
            ),
            "rank_latest_exclusion_counts": (
                latest["latest_exclusion_counts"] if latest else {}
            ),
            "rank_latest_rankable_snapshot": (
                latest["latest_rankable_snapshot"] if latest else None
            ),
            "latest_rank": latest,
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("postmarket rank snapshot failed")
        return {
            "rank_status": "error",
            "rank_error": f"{type(exc).__name__}: {exc}"[:1000],
        }


def rth_handoff_reconcile_heartbeat_fields(
    now: datetime,
    shadow_conn,
    *,
    version: str | None,
    run_id: str,
    session: date | None = None,
    postmarket_end: datetime | None = None,
) -> dict:
    """Keep every final-RTH candidate linked or explicitly terminal."""
    try:
        ensure_rth_schema(shadow_conn)
        if session is None:
            row = shadow_conn.execute(
                "SELECT MAX(session) FROM rth_momentum_candidates"
            ).fetchone()
            session = date.fromisoformat(row[0]) if row and row[0] else None
        if session is None:
            return {
                "rth_handoff_status": "current",
                "latest_rth_handoff": latest_rth_handoff_summary(shadow_conn),
            }
        if postmarket_end is None:
            _, postmarket_end = lifecycle_window(session)
        result = reconcile_rth_postmarket_handoffs(
            shadow_conn,
            session=session,
            now=now,
            postmarket_end=postmarket_end,
            code_version=version,
            run_id=run_id,
        )
        return {
            "rth_handoff_status": "current",
            "rth_handoff_session": result.session,
            "rth_candidates": result.rth_candidates,
            "rth_postmarket_links_written": result.postmarket_links_written,
            "rth_terminal_not_qualified_written": (
                result.terminal_not_qualified_written
            ),
            "latest_rth_handoff": latest_rth_handoff_summary(shadow_conn),
        }
    except Exception as exc:
        shadow_conn.rollback()
        logger.exception("RTH-to-postmarket handoff reconciliation failed")
        return {
            "rth_handoff_status": "error",
            "rth_handoff_error": f"{type(exc).__name__}: {exc}"[:1000],
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
                    **rth_audit_heartbeat_fields(now),
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
        if rth_handoff_is_active(now):
            rth_window = rth_handoff_window(now)
            assert rth_window is not None
            rth_session, rth_window_start, _ = rth_window
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat("ok", now, rth_handoff_status="running"),
            )
            try:
                rth_result, _, _ = run_rth_momentum_tick(
                    shadow_conn,
                    active_universe=set(active_symbols(universe_conn)),
                    scheduled_earnings=set(
                        scheduled_after_hours_earnings_symbols(
                            journal_conn, rth_session
                        )
                    ),
                    now=now,
                    run_id=run_id,
                    code_version=version,
                    data_feed=data_feed,
                    screen_fetch=_screen_fetch,
                    intraday_fetch=_bars_fetch,
                    daily_fetch=_rth_daily_bars_fetch,
                    sweep_intraday_fetch=_sweep_bars_fetch,
                    sweep_cycle_ticks=FULL_UNIVERSE_RTH_SWEEP_CYCLE_TICKS,
                    validation_now_fn=_utc_now,
                )
            except Exception as exc:
                shadow_conn.rollback()
                logger.exception("final-RTH market-wide momentum tick failed")
                write_heartbeat_atomic(
                    HEARTBEAT_PATH,
                    _heartbeat(
                        "error",
                        _utc_now(),
                        rth_handoff_status="error",
                        rth_handoff_error=f"{type(exc).__name__}: {exc}"[:1000],
                    ),
                )
            else:
                logger.info(
                    "rth_momentum_tick tick=%s session=%s sweep_shard=%s/%s "
                    "sweep_symbols=%s sweep_overlap=%s selected=%s "
                    "intraday_fetched=%s daily_fetched=%s evaluated=%s "
                    "candidates=%s new=%s invariant=%s errors=%s lag_ms=%s "
                    "missed=%s screen_ms=%s selection_ms=%s fetch_ms=%s "
                    "evaluation_ms=%s total_ms=%s",
                    rth_result.tick_id,
                    rth_result.session,
                    rth_result.sweep_shard_index,
                    rth_result.sweep_shard_count,
                    rth_result.sweep_shard_symbols,
                    rth_result.sweep_overlap_symbols,
                    rth_result.selected_symbols,
                    rth_result.intraday_symbols_fetched,
                    rth_result.daily_symbols_fetched,
                    rth_result.evaluated_symbols,
                    rth_result.candidate_observations,
                    rth_result.new_candidates,
                    rth_result.invariant_ok,
                    rth_result.error_count,
                    rth_result.scheduled_lag_ms,
                    rth_result.missed_cycles,
                    rth_result.screen_latency_ms,
                    rth_result.selection_latency_ms,
                    rth_result.bar_fetch_latency_ms,
                    rth_result.evaluation_latency_ms,
                    rth_result.latency_ms,
                )
                completed_now = _utc_now()
                write_heartbeat_atomic(
                    HEARTBEAT_PATH,
                    _heartbeat(
                        "ok",
                        completed_now,
                        rth_handoff_status=(
                            "degraded" if rth_result.error_count else "current"
                        ),
                        rth_sweep_shard_index=rth_result.sweep_shard_index,
                        rth_sweep_shard_count=rth_result.sweep_shard_count,
                        rth_sweep_shard_symbols=rth_result.sweep_shard_symbols,
                        rth_sweep_overlap_symbols=(
                            rth_result.sweep_overlap_symbols
                        ),
                        rth_selected_symbols=rth_result.selected_symbols,
                        rth_evaluated_symbols=rth_result.evaluated_symbols,
                        rth_error_count=rth_result.error_count,
                        rth_total_latency_ms=rth_result.latency_ms,
                        latest_rth_handoff=latest_rth_handoff_summary(shadow_conn),
                    ),
                )
            sleep_now = _utc_now()
            time.sleep(
                rth_poll_sleep_seconds(
                    sleep_now, window_start=rth_window_start
                )
            )
            continue
        if not postmarket_is_active(now):
            rth_fields = rth_handoff_reconcile_heartbeat_fields(
                now,
                shadow_conn,
                version=version,
                run_id=run_id,
            )
            lifecycle_fields = lifecycle_heartbeat_fields(
                now,
                shadow_conn,
                data_feed=data_feed,
                version=version,
                run_id=run_id,
            )
            context_fields = context_backfill_heartbeat_fields(
                now,
                shadow_conn,
                journal_conn,
                universe_conn,
                version=version,
            )
            rank_fields = rank_heartbeat_fields(
                _utc_now(),
                shadow_conn,
                version=version,
                run_id=run_id,
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
            rth_census_fields = rth_missed_mover_census_heartbeat_fields(
                now,
                shadow_conn,
                universe_conn,
                data_feed=data_feed,
                version=version,
            )
            provider_fields = provider_proof_heartbeat_fields(
                now,
                shadow_conn,
                version=version,
            )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "idle",
                    now,
                    **discovery_audit_heartbeat_fields(now),
                    **rth_audit_heartbeat_fields(
                        now, expected_session=rth_audit_session_due(now)
                    ),
                    **lifecycle_fields,
                    **context_fields,
                    **rank_fields,
                    **quality_fields,
                    **census_fields,
                    **rth_census_fields,
                    **provider_fields,
                    **rth_fields,
                ),
            )
            time.sleep(discovery_idle_sleep_seconds(now))
            continue
        write_heartbeat_atomic(HEARTBEAT_PATH, _heartbeat("running", now))
        try:
            window = postmarket_window(now)
            assert window is not None
            session, session_close, _ = window
            result, _, evaluations = run_discovery_tick(
                shadow_conn,
                active_universe=set(active_symbols(universe_conn)),
                scheduled_earnings=set(
                    scheduled_after_hours_earnings_symbols(journal_conn, session)
                ),
                identity_quarantine_symbols=set(
                    symbols_first_seen_on(universe_conn, session)
                ),
                now=now,
                run_id=run_id,
                version=version,
                data_feed=data_feed,
                sweep_cycle_ticks=FULL_UNIVERSE_SWEEP_CYCLE_TICKS,
                validation_now_fn=_utc_now,
            )
        except Exception as exc:
            shadow_conn.rollback()
            logger.exception("market-wide postmarket discovery tick failed")
            failed_now = _utc_now()
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "error",
                    failed_now,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                    **rth_audit_heartbeat_fields(
                        failed_now, expected_session=session
                    ),
                ),
            )
        else:
            logger.info(
                "postmarket_discovery_tick tick=%s universe=%s screen_rows=%s "
                "provider_unique=%s sweep_shard=%s/%s sweep_symbols=%s overlap=%s "
                "discovered=%s fetched=%s evaluated=%s candidates=%s new=%s "
                "identity_quarantined=%s "
                "errors=%s lag_ms=%s missed=%s screen_ms=%s selection_ms=%s "
                "fetch_ms=%s evaluation_ms=%s persistence_max_s=%s total_ms=%s",
                result.tick_id,
                result.universe_symbols,
                result.screen_rows,
                result.provider_screen_unique_symbols,
                result.sweep_shard_index,
                result.sweep_shard_count,
                result.sweep_shard_symbols,
                result.sweep_overlap_symbols,
                result.discovered_symbols,
                result.fetched_symbols,
                result.evaluated_symbols,
                result.candidate_observations,
                result.new_candidates,
                result.identity_quarantined,
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
            lifecycle_fields = lifecycle_heartbeat_fields(
                completed_now,
                shadow_conn,
                data_feed=data_feed,
                version=version,
                run_id=run_id,
                existing_evaluations=tuple(evaluations),
            )
            context_fields = context_backfill_heartbeat_fields(
                completed_now,
                shadow_conn,
                journal_conn,
                universe_conn,
                version=version,
            )
            rank_fields = rank_heartbeat_fields(
                _utc_now(),
                shadow_conn,
                version=version,
                run_id=run_id,
            )
            quality_fields = quality_backfill_heartbeat_fields(
                completed_now,
                shadow_conn,
                data_feed=data_feed,
                version=version,
                run_id=run_id,
            )
            rth_fields = rth_handoff_reconcile_heartbeat_fields(
                completed_now,
                shadow_conn,
                version=version,
                run_id=run_id,
                session=session,
                postmarket_end=window[2],
            )
            rth_audit_fields = rth_audit_heartbeat_fields(
                completed_now, expected_session=session
            )
            write_heartbeat_atomic(
                HEARTBEAT_PATH,
                _heartbeat(
                    "ok", completed_now, **result.__dict__, **lifecycle_fields,
                    **context_fields, **rank_fields, **quality_fields,
                    **rth_fields, **rth_audit_fields
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
