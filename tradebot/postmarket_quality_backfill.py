"""Bounded provider orchestration for append-only postmarket outcome truth.

The discovery observer calls this after ordinary screening or while idle. It
fetches bars only for candidate symbols with finalized unresolved marks,
and writes immutable daily quality reports once all checkpoints are resolved.
It cannot send alerts, mutate candidate ledgers, or place orders.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from tradebot.detectors import Bar
from tradebot.marketdata import partition_intraday_bars
from tradebot.postmarket_quality import (
    BAR_TIMEFRAME,
    CHECKPOINTS,
    FINALIZATION_GRACE,
    MARK_HORIZONS_MIN,
    NEXT_SESSION_CLOSE,
    NEXT_SESSION_OPEN,
    POSTMARKET_CLOSE,
    STREAM_TABLES,
    CandidateQualityReport,
    CandidateReference,
    candidate_quality_report,
    compute_outcome_marks,
    ensure_quality_schema,
    mark_targets,
    record_outcome_marks,
)


QUALITY_REPORT_VERSION = 1
MARKET_DATA_PROVIDER = "alpaca"


@dataclass(frozen=True)
class CandidateBackfillPlan:
    candidate: CandidateReference
    due_checkpoints: tuple[str, ...]
    next_session_required: bool


@dataclass(frozen=True)
class QualityBackfillResult:
    candidates_planned: int
    candidate_sessions_fetched: int
    symbols_fetched: int
    marks_computed: int
    marks_written: int
    unresolved_checkpoints: int
    fetch_errors: int
    fetch_error_details: tuple[str, ...]
    latency_ms: int


@dataclass(frozen=True)
class DailyQualityReport:
    report_version: int
    report_code_version: str | None
    candidate_stream: str
    session: str
    generated_at_utc: str
    source_candidate_count: int
    source_max_mark_seq: int
    operational_complete: bool
    evidence_eligible: bool
    checkpoint_reports: tuple[CandidateQualityReport, ...]
    issue_codes: tuple[str, ...]


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _candidate_rows(
    conn: sqlite3.Connection, *, reconcile_no_bar: bool,
) -> tuple[CandidateReference, ...]:
    candidates: list[CandidateReference] = []
    for stream, table in STREAM_TABLES.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        resolved_count = (
            "COUNT(DISTINCT CASE WHEN e.status='AVAILABLE' THEN e.checkpoint END)"
            if reconcile_no_bar
            else "COUNT(DISTINCT e.checkpoint)"
        )
        rows = conn.execute(
            f"""
            SELECT c.candidate_id,c.session,c.symbol,c.direction,
                   c.bar_open_ts_utc,c.close
            FROM {table} c
            LEFT JOIN postmarket_candidate_mark_events e
              ON e.candidate_stream=?
             AND e.candidate_id=c.candidate_id
             AND e.session=c.session
            GROUP BY c.candidate_id
            HAVING {resolved_count} < ?
            ORDER BY c.session,c.candidate_id
            """,
            (stream, len(CHECKPOINTS)),
        ).fetchall()
        for row in rows:
            detected = datetime.fromisoformat(row[4])
            candidates.append(
                CandidateReference(
                    candidate_stream=stream,
                    candidate_id=int(row[0]),
                    session=date.fromisoformat(row[1]),
                    symbol=row[2],
                    direction=row[3],
                    detection_bar_open_ts_utc=detected,
                    baseline_price=float(row[5]),
                )
            )
    return tuple(candidates)


def _resolved_checkpoints(
    conn: sqlite3.Connection,
    candidate: CandidateReference,
    *,
    reconcile_no_bar: bool,
) -> set[str]:
    status_filter = "AND status='AVAILABLE'" if reconcile_no_bar else ""
    return {
        row[0]
        for row in conn.execute(
            f"""
            SELECT DISTINCT checkpoint
            FROM postmarket_candidate_mark_events
            WHERE candidate_stream=? AND candidate_id=? AND session=?
            {status_filter}
            """,
            (
                candidate.candidate_stream,
                candidate.candidate_id,
                candidate.session.isoformat(),
            ),
        ).fetchall()
    }


def plan_due_backfill(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reconcile_no_bar: bool = False,
) -> tuple[CandidateBackfillPlan, ...]:
    """Return only candidates with unresolved checkpoints whose data is final."""
    current = _aware_utc(now, "now")
    ensure_quality_schema(conn)
    plans: list[CandidateBackfillPlan] = []
    postmarket_checkpoints = {
        *(f"+{minutes}m" for minutes in MARK_HORIZONS_MIN),
        POSTMARKET_CLOSE,
    }
    for candidate in _candidate_rows(
        conn, reconcile_no_bar=reconcile_no_bar
    ):
        targets = {target.checkpoint: target.target_ts_utc for target in mark_targets(candidate)}
        resolved = _resolved_checkpoints(
            conn, candidate, reconcile_no_bar=reconcile_no_bar
        )
        due: list[str] = []
        if current >= targets[POSTMARKET_CLOSE] + FINALIZATION_GRACE:
            due.extend(sorted(postmarket_checkpoints - resolved))
        if (
            current >= targets[NEXT_SESSION_OPEN] + FINALIZATION_GRACE
            and NEXT_SESSION_OPEN not in resolved
        ):
            due.append(NEXT_SESSION_OPEN)
        if (
            current >= targets[NEXT_SESSION_CLOSE] + FINALIZATION_GRACE
            and NEXT_SESSION_CLOSE not in resolved
        ):
            due.append(NEXT_SESSION_CLOSE)
        if not due:
            continue
        plans.append(
            CandidateBackfillPlan(
                candidate=candidate,
                due_checkpoints=tuple(due),
                next_session_required=any(
                    checkpoint in {NEXT_SESSION_OPEN, NEXT_SESSION_CLOSE}
                    for checkpoint in due
                ),
            )
        )
    return tuple(plans)


def _next_session_date(candidate: CandidateReference) -> date:
    targets = {target.checkpoint: target.target_ts_utc for target in mark_targets(candidate)}
    return targets[NEXT_SESSION_OPEN].astimezone(timezone.utc).date()


def run_due_quality_backfill(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    data_feed: str,
    code_version: str | None,
    run_id: str,
    bars_fetch: Callable[[list[str], date], dict[str, Sequence[Bar]]],
    market_data_provider: str = MARKET_DATA_PROVIDER,
    reconcile_no_bar: bool = False,
) -> QualityBackfillResult:
    """Fetch each required symbol/session once and append new checkpoint truth."""
    current = _aware_utc(now, "now")
    started = time.perf_counter()
    plans = plan_due_backfill(
        conn, now=current, reconcile_no_bar=reconcile_no_bar
    )
    requests: dict[date, set[str]] = {}
    for plan in plans:
        requests.setdefault(plan.candidate.session, set()).add(plan.candidate.symbol)
        if plan.next_session_required:
            requests.setdefault(_next_session_date(plan.candidate), set()).add(
                plan.candidate.symbol
            )

    fetched: dict[date, dict[str, Sequence[Bar]]] = {}
    fetch_errors = 0
    fetch_error_details: list[str] = []
    for session, symbols in sorted(requests.items()):
        try:
            fetched[session] = bars_fetch(sorted(symbols), session)
        except Exception as exc:
            fetched[session] = {}
            fetch_error_details.append(
                f"{session.isoformat()}:bulk_fetch:{type(exc).__name__}"
            )

    marks_computed = 0
    marks_written = 0
    unresolved = 0
    symbols_fetched: set[tuple[date, str]] = set()
    for plan in plans:
        candidate = plan.candidate
        original_response = fetched.get(candidate.session)
        original_complete = (
            original_response is not None and candidate.symbol in original_response
        )
        original_bars = (
            original_response[candidate.symbol] if original_complete else ()
        )
        next_complete = not plan.next_session_required
        next_bars: Sequence[Bar] = ()
        if plan.next_session_required:
            next_date = _next_session_date(candidate)
            next_response = fetched.get(next_date)
            next_complete = next_response is not None and candidate.symbol in next_response
            if next_complete:
                next_bars = next_response[candidate.symbol]
                symbols_fetched.add((next_date, candidate.symbol))
        if original_complete:
            symbols_fetched.add((candidate.session, candidate.symbol))
        else:
            fetch_errors += 1
            fetch_error_details.append(
                f"{candidate.session.isoformat()}:{candidate.symbol}:missing_response"
            )
        if plan.next_session_required and not next_complete:
            fetch_errors += 1
            fetch_error_details.append(
                f"{_next_session_date(candidate).isoformat()}:{candidate.symbol}:"
                "missing_response"
            )

        postmarket = partition_intraday_bars(original_bars).postmarket if original_complete else ()
        next_rth = partition_intraday_bars(next_bars).rth if next_complete else ()
        marks = compute_outcome_marks(
            candidate,
            postmarket,
            next_rth,
            as_of=current,
            postmarket_data_complete=original_complete,
            next_session_data_complete=next_complete,
        )
        due = set(plan.due_checkpoints)
        selected = tuple(mark for mark in marks if mark.checkpoint in due)
        marks_computed += len(selected)
        marks_written += record_outcome_marks(
            conn,
            selected,
            data_feed=data_feed,
            market_data_provider=market_data_provider,
            bar_timeframe=BAR_TIMEFRAME,
            code_version=code_version,
            run_id=run_id,
            recorded_at_utc=current,
        )
        unresolved += len(due - {mark.checkpoint for mark in selected})

    return QualityBackfillResult(
        candidates_planned=len(plans),
        candidate_sessions_fetched=len(requests),
        symbols_fetched=len(symbols_fetched),
        marks_computed=marks_computed,
        marks_written=marks_written,
        unresolved_checkpoints=unresolved,
        fetch_errors=fetch_errors,
        fetch_error_details=tuple(fetch_error_details),
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


def build_daily_quality_report(
    conn: sqlite3.Connection,
    *,
    candidate_stream: str,
    session: date,
    generated_at: datetime,
    report_code_version: str | None,
    report_version: int = QUALITY_REPORT_VERSION,
) -> DailyQualityReport:
    generated = _aware_utc(generated_at, "generated_at")
    ensure_quality_schema(conn)
    table = STREAM_TABLES.get(candidate_stream)
    if table is None:
        raise ValueError("candidate_stream must be marketwide or scheduled")
    source_candidate_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session=?",
            (session.isoformat(),),
        ).fetchone()[0]
    )
    source_max_mark_seq = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(seq),0)
            FROM postmarket_candidate_mark_events
            WHERE candidate_stream=? AND session=?
            """,
            (candidate_stream, session.isoformat()),
        ).fetchone()[0]
    )
    reports = tuple(
        candidate_quality_report(
            conn,
            candidate_stream=candidate_stream,
            session=session,
            checkpoint=checkpoint,
        )
        for checkpoint in sorted(CHECKPOINTS)
    )
    operational_complete = all(
        report.available_marks + report.no_bar_marks == report.total_candidates
        for report in reports
    )
    issues: list[str] = []
    if not operational_complete:
        issues.append("INCOMPLETE_MARKS")
    if any(report.no_bar_marks for report in reports):
        issues.append("NO_BAR_MARKS")
    if reports and reports[0].total_candidates < reports[0].minimum_sample:
        issues.append("BELOW_MINIMUM_SAMPLE")
    if report_code_version in {None, "", "unknown"}:
        issues.append("CODE_VERSION_MISSING")
    evidence_eligible = (
        operational_complete
        and report_code_version not in {None, "", "unknown"}
        and all(report.evidence_eligible for report in reports)
    )
    return DailyQualityReport(
        report_version=report_version,
        report_code_version=report_code_version,
        candidate_stream=candidate_stream,
        session=session.isoformat(),
        generated_at_utc=generated.isoformat(),
        source_candidate_count=source_candidate_count,
        source_max_mark_seq=source_max_mark_seq,
        operational_complete=operational_complete,
        evidence_eligible=evidence_eligible,
        checkpoint_reports=reports,
        issue_codes=tuple(issues),
    )


def report_json(report: DailyQualityReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def write_report_atomic(path: Path | str, report: DailyQualityReport) -> bool:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return False
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(report_json(report))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, destination)
        except FileExistsError:
            tmp_path.unlink()
            return False
        tmp_path.unlink()
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_completed_quality_reports(
    conn: sqlite3.Connection,
    output_dir: Path | str,
    *,
    now: datetime,
    report_code_version: str | None,
) -> tuple[DailyQualityReport, ...]:
    """Write immutable reports only after next-session close and full resolution."""
    current = _aware_utc(now, "now")
    ensure_quality_schema(conn)
    written: list[DailyQualityReport] = []
    destination_dir = Path(output_dir)
    for stream, table in STREAM_TABLES.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        sessions = [
            date.fromisoformat(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT session FROM {table} ORDER BY session"
            ).fetchall()
        ]
        for session in sessions:
            representative = conn.execute(
                f"""
                SELECT candidate_id,symbol,direction,bar_open_ts_utc,close
                FROM {table} WHERE session=? ORDER BY candidate_id LIMIT 1
                """,
                (session.isoformat(),),
            ).fetchone()
            candidate = CandidateReference(
                stream,
                int(representative[0]),
                session,
                representative[1],
                representative[2],
                datetime.fromisoformat(representative[3]),
                float(representative[4]),
            )
            targets = {target.checkpoint: target.target_ts_utc for target in mark_targets(candidate)}
            if current < targets[NEXT_SESSION_CLOSE] + FINALIZATION_GRACE:
                continue
            prefix = f"postmarket_quality_{stream}_{session.isoformat()}_v"
            existing: list[tuple[int, dict]] = []
            for path in destination_dir.glob(f"{prefix}*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    version = int(payload["report_version"])
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"invalid existing postmarket quality report: {path}"
                    ) from exc
                existing.append((version, payload))
            if existing:
                latest_payload = max(existing, key=lambda item: item[0])[1]
                candidate_count = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE session=?",
                        (session.isoformat(),),
                    ).fetchone()[0]
                )
                max_mark_seq = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(seq),0)
                        FROM postmarket_candidate_mark_events
                        WHERE candidate_stream=? AND session=?
                        """,
                        (stream, session.isoformat()),
                    ).fetchone()[0]
                )
                if (
                    latest_payload.get("source_candidate_count") == candidate_count
                    and latest_payload.get("source_max_mark_seq") == max_mark_seq
                ):
                    continue
            next_version = max((version for version, _ in existing), default=0) + 1
            report = build_daily_quality_report(
                conn,
                candidate_stream=stream,
                session=session,
                generated_at=current,
                report_code_version=report_code_version,
                report_version=next_version,
            )
            if not report.operational_complete:
                continue
            semantic = json.loads(json.dumps(asdict(report), sort_keys=True))
            for key in ("report_version", "report_code_version", "generated_at_utc"):
                semantic.pop(key)
            if existing:
                latest_payload = max(existing, key=lambda item: item[0])[1]
                latest_semantic = dict(latest_payload)
                for key in ("report_version", "report_code_version", "generated_at_utc"):
                    latest_semantic.pop(key, None)
                if latest_semantic == semantic:
                    continue
            destination = destination_dir / f"{prefix}{next_version}.json"
            if write_report_atomic(destination, report):
                written.append(report)
    return tuple(written)


def latest_quality_report_summaries(output_dir: Path | str) -> tuple[dict, ...]:
    """Return the latest immutable verdict for each candidate stream."""
    latest: dict[str, tuple[tuple[str, int], dict]] = {}
    for path in Path(output_dir).glob("postmarket_quality_*_v*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stream = str(payload["candidate_stream"])
            key = (str(payload["session"]), int(payload["report_version"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid postmarket quality report: {path}") from exc
        if stream not in STREAM_TABLES:
            raise ValueError(f"invalid candidate stream in quality report: {path}")
        if stream not in latest or key > latest[stream][0]:
            latest[stream] = (key, payload)
    return tuple(
        {
            "candidate_stream": stream,
            "session": payload["session"],
            "report_version": payload["report_version"],
            "operational_complete": payload["operational_complete"],
            "evidence_eligible": payload["evidence_eligible"],
            "issue_codes": payload["issue_codes"],
        }
        for stream, (_, payload) in sorted(latest.items())
    )
