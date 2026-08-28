"""Append-only outcome truth for postmarket candidates.

This module is deliberately provider- and delivery-free. Callers supply bars
that have already been fetched, and the module converts them into provenance-
bound outcome events. It never sends alerts, changes candidate state, places an
order, or fabricates a price when a checkpoint has no qualifying bar.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.detectors import Bar, bar_close_ts


QUALITY_VERSION = 1
BAR_TIMEFRAME = "5Min"
MARK_HORIZONS_MIN = (5, 15, 30, 60)
POSTMARKET_CLOSE = "postmarket_close"
NEXT_SESSION_OPEN = "next_session_open"
NEXT_SESSION_CLOSE = "next_session_close"
CHECKPOINTS = {f"+{minutes}m" for minutes in MARK_HORIZONS_MIN} | {
    POSTMARKET_CLOSE,
    NEXT_SESSION_OPEN,
    NEXT_SESSION_CLOSE,
}
MARK_STATUS_AVAILABLE = "AVAILABLE"
MARK_STATUS_NO_BAR = "NO_BAR"
MARK_STATUSES = {MARK_STATUS_AVAILABLE, MARK_STATUS_NO_BAR}
MIN_QUALITY_SAMPLE = 20
FINALIZATION_GRACE = timedelta(minutes=5)
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")
STREAM_TABLES = {
    "marketwide": "postmarket_discovery_candidates",
    "scheduled": "postmarket_candidates",
}


QUALITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_candidate_mark_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    quality_version INTEGER NOT NULL,
    candidate_stream TEXT NOT NULL,
    candidate_id INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    target_ts_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    detection_ts_utc TEXT NOT NULL,
    baseline_price REAL NOT NULL,
    observed_bar_open_ts_utc TEXT,
    observed_at_utc TEXT,
    price REAL,
    directional_return_pct REAL,
    mfe_pct REAL,
    mae_pct REAL,
    time_to_mfe_minutes REAL,
    bars_examined INTEGER NOT NULL,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    CHECK (candidate_stream IN ('marketwide','scheduled')),
    CHECK (direction IN ('up','down')),
    CHECK (status IN ('AVAILABLE','NO_BAR'))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_mark_events_lookup
    ON postmarket_candidate_mark_events(
        candidate_stream,session,candidate_id,checkpoint,seq
    );
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_mark_events_session
    ON postmarket_candidate_mark_events(session,checkpoint,status);

CREATE TRIGGER IF NOT EXISTS postmarket_candidate_mark_events_no_update
BEFORE UPDATE ON postmarket_candidate_mark_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_mark_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidate_mark_events_no_delete
BEFORE DELETE ON postmarket_candidate_mark_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_mark_events is append-only');
END;
"""


@dataclass(frozen=True)
class CandidateReference:
    candidate_stream: str
    candidate_id: int
    session: date
    symbol: str
    direction: str
    detection_bar_open_ts_utc: datetime
    baseline_price: float


@dataclass(frozen=True)
class MarkTarget:
    checkpoint: str
    target_ts_utc: datetime


@dataclass(frozen=True)
class OutcomeMark:
    candidate_stream: str
    candidate_id: int
    session: str
    symbol: str
    direction: str
    checkpoint: str
    target_ts_utc: str
    status: str
    detection_ts_utc: str
    baseline_price: float
    observed_bar_open_ts_utc: str | None
    observed_at_utc: str | None
    price: float | None
    directional_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    time_to_mfe_minutes: float | None
    bars_examined: int
    detail: dict


@dataclass(frozen=True)
class CandidateQualityReport:
    candidate_stream: str
    session: str
    checkpoint: str
    total_candidates: int
    available_marks: int
    no_bar_marks: int
    missing_marks: int
    minimum_sample: int
    evidence_eligible: bool
    continuation_rate: float | None
    average_directional_return_pct: float | None
    median_directional_return_pct: float | None
    average_mfe_pct: float | None
    average_mae_pct: float | None
    median_time_to_mfe_minutes: float | None


def ensure_quality_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(QUALITY_SCHEMA)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _valid_price(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _validate_candidate(candidate: CandidateReference) -> datetime:
    if candidate.candidate_stream not in STREAM_TABLES:
        raise ValueError("candidate_stream must be marketwide or scheduled")
    if candidate.candidate_id <= 0:
        raise ValueError("candidate_id must be positive")
    if not candidate.symbol or candidate.symbol != candidate.symbol.strip().upper():
        raise ValueError("symbol must be canonical uppercase")
    if candidate.direction not in {"up", "down"}:
        raise ValueError("direction must be up or down")
    if not _valid_price(candidate.baseline_price):
        raise ValueError("baseline_price must be finite and positive")
    detection_open = _aware_utc(
        candidate.detection_bar_open_ts_utc, "detection_bar_open_ts_utc"
    )
    return detection_open + timedelta(minutes=5)


def _postmarket_end(session: date) -> datetime:
    if not CALENDAR.is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    return datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)


def _next_session_window(session: date) -> tuple[datetime, datetime]:
    next_session = CALENDAR.next_session(session)
    session_open = CALENDAR.session_open(next_session).to_pydatetime()
    session_close = CALENDAR.session_close(next_session).to_pydatetime()
    return session_open.astimezone(timezone.utc), session_close.astimezone(timezone.utc)


def mark_targets(candidate: CandidateReference) -> tuple[MarkTarget, ...]:
    detection_ts = _validate_candidate(candidate)
    postmarket_end = _postmarket_end(candidate.session)
    if detection_ts > postmarket_end:
        raise ValueError("candidate detection bar closes after the postmarket window")
    next_open, next_close = _next_session_window(candidate.session)
    targets = [
        MarkTarget(f"+{minutes}m", detection_ts + timedelta(minutes=minutes))
        for minutes in MARK_HORIZONS_MIN
    ]
    targets.extend(
        (
            MarkTarget(POSTMARKET_CLOSE, postmarket_end),
            MarkTarget(NEXT_SESSION_OPEN, next_open),
            MarkTarget(NEXT_SESSION_CLOSE, next_close),
        )
    )
    return tuple(targets)


def _validated_bars(symbol: str, bars: Sequence[Bar], name: str) -> list[Bar]:
    values = list(bars)
    timestamps = []
    for bar in values:
        ts = _aware_utc(bar.ts, f"{name} bar timestamp")
        timestamps.append(ts)
        if bar.symbol != symbol:
            raise ValueError(f"{name} contains a bar for another symbol")
        if (
            not all(_valid_price(value) for value in (bar.open, bar.high, bar.low, bar.close))
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
            or bar.volume < 0
        ):
            raise ValueError(f"{name} contains a malformed bar")
    if timestamps != sorted(timestamps):
        raise ValueError(f"{name} timestamps are out of order")
    if len(timestamps) != len(set(timestamps)):
        raise ValueError(f"{name} contains duplicate timestamps")
    return values


def _excursion(
    candidate: CandidateReference,
    detection_ts: datetime,
    bars: Sequence[Bar],
    observed_at: datetime,
) -> tuple[float, float, float, int]:
    window = [
        bar
        for bar in bars
        if detection_ts < bar_close_ts(bar).astimezone(timezone.utc) <= observed_at
        and bar.volume > 0
    ]
    if not window:
        return 0.0, 0.0, 0.0, 0
    baseline = candidate.baseline_price
    if candidate.direction == "up":
        best = max(window, key=lambda bar: bar.high)
        worst = min(window, key=lambda bar: bar.low)
        mfe = (best.high / baseline - 1) * 100
        mae = (1 - worst.low / baseline) * 100
    else:
        best = min(window, key=lambda bar: bar.low)
        worst = max(window, key=lambda bar: bar.high)
        mfe = (1 - best.low / baseline) * 100
        mae = (worst.high / baseline - 1) * 100
    time_to_mfe = (
        bar_close_ts(best).astimezone(timezone.utc) - detection_ts
    ).total_seconds() / 60
    return max(0.0, mfe), max(0.0, mae), time_to_mfe, len(window)


def _available_mark(
    candidate: CandidateReference,
    target: MarkTarget,
    detection_ts: datetime,
    bar: Bar,
    path_bars: Sequence[Bar],
    *,
    use_open: bool = False,
) -> OutcomeMark:
    price = bar.open if use_open else bar.close
    observed_at = bar.ts if use_open else bar_close_ts(bar)
    observed_at = _aware_utc(observed_at, "observed_at")
    sign = 1 if candidate.direction == "up" else -1
    directional_return = sign * (price / candidate.baseline_price - 1) * 100
    mfe, mae, time_to_mfe, bars_examined = _excursion(
        candidate, detection_ts, path_bars, observed_at
    )
    if candidate.direction == "up":
        observed_favorable = (price / candidate.baseline_price - 1) * 100
        observed_adverse = (1 - price / candidate.baseline_price) * 100
    else:
        observed_favorable = (1 - price / candidate.baseline_price) * 100
        observed_adverse = (price / candidate.baseline_price - 1) * 100
    if observed_favorable > mfe:
        mfe = observed_favorable
        time_to_mfe = (observed_at - detection_ts).total_seconds() / 60
    mae = max(mae, observed_adverse)
    return OutcomeMark(
        candidate_stream=candidate.candidate_stream,
        candidate_id=candidate.candidate_id,
        session=candidate.session.isoformat(),
        symbol=candidate.symbol,
        direction=candidate.direction,
        checkpoint=target.checkpoint,
        target_ts_utc=target.target_ts_utc.isoformat(),
        status=MARK_STATUS_AVAILABLE,
        detection_ts_utc=detection_ts.isoformat(),
        baseline_price=candidate.baseline_price,
        observed_bar_open_ts_utc=_aware_utc(bar.ts, "bar timestamp").isoformat(),
        observed_at_utc=observed_at.isoformat(),
        price=price,
        directional_return_pct=directional_return,
        mfe_pct=max(0.0, mfe),
        mae_pct=max(0.0, mae),
        time_to_mfe_minutes=time_to_mfe,
        bars_examined=bars_examined,
        detail={
            "price_field": "open" if use_open else "close",
            "target_distance_seconds": (
                observed_at - target.target_ts_utc
            ).total_seconds(),
        },
    )


def _no_bar_mark(
    candidate: CandidateReference,
    target: MarkTarget,
    detection_ts: datetime,
    reason: str,
) -> OutcomeMark:
    return OutcomeMark(
        candidate_stream=candidate.candidate_stream,
        candidate_id=candidate.candidate_id,
        session=candidate.session.isoformat(),
        symbol=candidate.symbol,
        direction=candidate.direction,
        checkpoint=target.checkpoint,
        target_ts_utc=target.target_ts_utc.isoformat(),
        status=MARK_STATUS_NO_BAR,
        detection_ts_utc=detection_ts.isoformat(),
        baseline_price=candidate.baseline_price,
        observed_bar_open_ts_utc=None,
        observed_at_utc=None,
        price=None,
        directional_return_pct=None,
        mfe_pct=None,
        mae_pct=None,
        time_to_mfe_minutes=None,
        bars_examined=0,
        detail={"reason": reason},
    )


def compute_outcome_marks(
    candidate: CandidateReference,
    postmarket_bars: Sequence[Bar],
    next_session_rth_bars: Sequence[Bar],
    *,
    as_of: datetime,
) -> tuple[OutcomeMark, ...]:
    """Compute every due checkpoint from supplied completed bars.

    A missing mark is emitted only after its containing session is final. Before
    then it remains absent rather than being mislabeled as a failure. Later
    provider corrections can append an AVAILABLE event without rewriting the
    earlier NO_BAR event.
    """
    detection_ts = _validate_candidate(candidate)
    now = _aware_utc(as_of, "as_of")
    targets = {target.checkpoint: target for target in mark_targets(candidate)}
    postmarket = _validated_bars(candidate.symbol, postmarket_bars, "postmarket")
    next_rth = _validated_bars(
        candidate.symbol, next_session_rth_bars, "next_session_rth"
    )
    all_bars = [*postmarket, *next_rth]
    postmarket_end = targets[POSTMARKET_CLOSE].target_ts_utc
    next_open = targets[NEXT_SESSION_OPEN].target_ts_utc
    next_close = targets[NEXT_SESSION_CLOSE].target_ts_utc
    postmarket_final = now >= postmarket_end + FINALIZATION_GRACE
    next_open_final = now >= next_open + FINALIZATION_GRACE
    next_close_final = now >= next_close + FINALIZATION_GRACE
    results: list[OutcomeMark] = []

    for minutes in MARK_HORIZONS_MIN:
        target = targets[f"+{minutes}m"]
        if now < target.target_ts_utc:
            continue
        bar = next(
            (
                value
                for value in postmarket
                if bar_close_ts(value).astimezone(timezone.utc) >= target.target_ts_utc
                and bar_close_ts(value).astimezone(timezone.utc) <= postmarket_end
                and value.volume > 0
            ),
            None,
        )
        if bar is not None:
            results.append(
                _available_mark(candidate, target, detection_ts, bar, postmarket)
            )
        elif postmarket_final:
            results.append(
                _no_bar_mark(candidate, target, detection_ts, "no completed postmarket bar")
            )

    close_target = targets[POSTMARKET_CLOSE]
    if postmarket_final:
        eligible = [
            bar
            for bar in postmarket
            if bar_close_ts(bar).astimezone(timezone.utc) <= postmarket_end
            and bar.volume > 0
        ]
        if eligible:
            results.append(
                _available_mark(
                    candidate, close_target, detection_ts, eligible[-1], postmarket
                )
            )
        else:
            results.append(
                _no_bar_mark(candidate, close_target, detection_ts, "no postmarket close bar")
            )

    open_target = targets[NEXT_SESSION_OPEN]
    if next_open_final:
        opening_bar = next(
            (
                bar
                for bar in next_rth
                if _aware_utc(bar.ts, "next open bar") == next_open and bar.volume > 0
            ),
            None,
        )
        if opening_bar is not None:
            results.append(
                _available_mark(
                    candidate,
                    open_target,
                    detection_ts,
                    opening_bar,
                    all_bars,
                    use_open=True,
                )
            )
        else:
            results.append(
                _no_bar_mark(candidate, open_target, detection_ts, "no next-session open bar")
            )

    close_target = targets[NEXT_SESSION_CLOSE]
    if next_close_final:
        closing_bar = next(
            (
                bar
                for bar in reversed(next_rth)
                if bar_close_ts(bar).astimezone(timezone.utc) == next_close
                and bar.volume > 0
            ),
            None,
        )
        if closing_bar is not None:
            results.append(
                _available_mark(candidate, close_target, detection_ts, closing_bar, all_bars)
            )
        else:
            results.append(
                _no_bar_mark(candidate, close_target, detection_ts, "no next-session close bar")
            )
    return tuple(results)


def _event_id(mark: OutcomeMark, *, data_feed: str, provider: str, timeframe: str) -> str:
    semantic = {
        **asdict(mark),
        "data_feed": data_feed,
        "market_data_provider": provider,
        "bar_timeframe": timeframe,
    }
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def record_outcome_marks(
    conn: sqlite3.Connection,
    marks: Sequence[OutcomeMark],
    *,
    data_feed: str,
    market_data_provider: str,
    bar_timeframe: str,
    code_version: str | None,
    run_id: str,
    recorded_at_utc: datetime,
) -> int:
    """Append semantic changes and ignore exact replays."""
    if not data_feed or not market_data_provider or not bar_timeframe or not run_id:
        raise ValueError("provenance and run_id must not be empty")
    recorded_at = _aware_utc(recorded_at_utc, "recorded_at_utc").isoformat()
    ensure_quality_schema(conn)
    written = 0
    with conn:
        for mark in marks:
            if mark.status not in MARK_STATUSES:
                raise ValueError(f"invalid mark status: {mark.status}")
            if mark.checkpoint not in CHECKPOINTS:
                raise ValueError(f"invalid mark checkpoint: {mark.checkpoint}")
            if mark.status == MARK_STATUS_AVAILABLE:
                if mark.price is None or not _valid_price(mark.price):
                    raise ValueError("available marks require a valid price")
            elif any(
                value is not None
                for value in (
                    mark.price,
                    mark.directional_return_pct,
                    mark.mfe_pct,
                    mark.mae_pct,
                    mark.time_to_mfe_minutes,
                )
            ):
                raise ValueError("NO_BAR marks cannot contain outcome values")
            event_id = _event_id(
                mark,
                data_feed=data_feed,
                provider=market_data_provider,
                timeframe=bar_timeframe,
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO postmarket_candidate_mark_events
                    (event_id,quality_version,candidate_stream,candidate_id,session,
                     symbol,direction,checkpoint,target_ts_utc,status,
                     detection_ts_utc,baseline_price,observed_bar_open_ts_utc,
                     observed_at_utc,price,directional_return_pct,mfe_pct,mae_pct,
                     time_to_mfe_minutes,bars_examined,data_feed,
                     market_data_provider,bar_timeframe,code_version,run_id,
                     recorded_at_utc,detail_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    QUALITY_VERSION,
                    mark.candidate_stream,
                    mark.candidate_id,
                    mark.session,
                    mark.symbol,
                    mark.direction,
                    mark.checkpoint,
                    mark.target_ts_utc,
                    mark.status,
                    mark.detection_ts_utc,
                    mark.baseline_price,
                    mark.observed_bar_open_ts_utc,
                    mark.observed_at_utc,
                    mark.price,
                    mark.directional_return_pct,
                    mark.mfe_pct,
                    mark.mae_pct,
                    mark.time_to_mfe_minutes,
                    mark.bars_examined,
                    data_feed,
                    market_data_provider,
                    bar_timeframe,
                    code_version,
                    run_id,
                    recorded_at,
                    json.dumps(mark.detail, sort_keys=True, separators=(",", ":")),
                ),
            )
            written += int(cursor.rowcount > 0)
    return written


def candidate_quality_report(
    conn: sqlite3.Connection,
    *,
    candidate_stream: str,
    session: date,
    checkpoint: str,
    minimum_sample: int = MIN_QUALITY_SAMPLE,
) -> CandidateQualityReport:
    """Aggregate only the latest event per candidate/checkpoint.

    Metrics stay unavailable until every candidate has a resolved mark and the
    fixed sample floor is met. Coverage counts remain visible below the floor.
    """
    table = STREAM_TABLES.get(candidate_stream)
    if table is None:
        raise ValueError("candidate_stream must be marketwide or scheduled")
    if minimum_sample <= 0:
        raise ValueError("minimum_sample must be positive")
    if checkpoint not in CHECKPOINTS:
        raise ValueError(f"invalid mark checkpoint: {checkpoint}")
    ensure_quality_schema(conn)
    session_text = session.isoformat()
    total = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE session=?", (session_text,)
    ).fetchone()[0]
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT *,ROW_NUMBER() OVER (
                PARTITION BY candidate_stream,candidate_id,checkpoint
                ORDER BY seq DESC
            ) AS row_number
            FROM postmarket_candidate_mark_events
            WHERE candidate_stream=? AND session=? AND checkpoint=?
        )
        SELECT status,directional_return_pct,mfe_pct,mae_pct,time_to_mfe_minutes
        FROM ranked WHERE row_number=1
        """,
        (candidate_stream, session_text, checkpoint),
    ).fetchall()
    available = [row for row in rows if row[0] == MARK_STATUS_AVAILABLE]
    no_bar = sum(row[0] == MARK_STATUS_NO_BAR for row in rows)
    missing = max(0, total - len(available))
    eligible = total > 0 and len(available) == total and len(available) >= minimum_sample
    returns = [float(row[1]) for row in available]
    mfes = [float(row[2]) for row in available if row[2] is not None]
    maes = [float(row[3]) for row in available if row[3] is not None]
    times = [float(row[4]) for row in available if row[4] is not None]
    return CandidateQualityReport(
        candidate_stream=candidate_stream,
        session=session_text,
        checkpoint=checkpoint,
        total_candidates=total,
        available_marks=len(available),
        no_bar_marks=no_bar,
        missing_marks=missing,
        minimum_sample=minimum_sample,
        evidence_eligible=eligible,
        continuation_rate=(
            sum(value > 0 for value in returns) / len(returns) if eligible else None
        ),
        average_directional_return_pct=(sum(returns) / len(returns) if eligible else None),
        median_directional_return_pct=(statistics.median(returns) if eligible else None),
        average_mfe_pct=(sum(mfes) / len(mfes) if eligible and mfes else None),
        average_mae_pct=(sum(maes) / len(maes) if eligible and maes else None),
        median_time_to_mfe_minutes=(statistics.median(times) if eligible and times else None),
    )
