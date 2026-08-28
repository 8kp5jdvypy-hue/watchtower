"""Append-only completed-bar lifecycle for postmarket candidates.

Once Stage 1 creates a candidate, this module keeps observing it even when it
falls out of the bounded provider screen.  It records state transitions only;
it cannot route, alert, or trade.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as ecals

from tradebot.detectors import Bar, bar_close_ts
from tradebot.marketdata import partition_intraday_bars
from tradebot.postmarket import (
    BAR_TIMEFRAME,
    MARKET_DATA_PROVIDER,
    OUTCOME_AWAITING_PERSISTENCE,
    OUTCOME_BELOW_MOVE,
    OUTCOME_BELOW_NOTIONAL,
    OUTCOME_CANDIDATE,
    OUTCOME_FETCH_ERROR,
    OUTCOME_UNSTABLE_PRINT,
    ReactionEvaluation,
    evaluate_postmarket_reaction,
    fetch_error_evaluation,
)


LIFECYCLE_VERSION = 1
STRENGTHENING_DELTA_PCT = 1.0
FADING_FROM_PEAK_PCT = 2.0
RECOVERY_DELTA_PCT = 1.0
FINAL_BAR_GRACE = timedelta(minutes=5)
ET = ZoneInfo("America/New_York")
CALENDAR = ecals.get_calendar("XNYS")

STATE_NEW = "NEWLY_QUALIFYING"
STATE_CONFIRMED = "CONFIRMED"
STATE_STRENGTHENING = "STRENGTHENING"
STATE_FADING = "FADING"
STATE_DEQUALIFIED = "DEQUALIFIED"
STATE_REQUALIFIED = "REQUALIFIED"
STATE_CLOSED = "CLOSED"
STATES = {
    STATE_NEW,
    STATE_CONFIRMED,
    STATE_STRENGTHENING,
    STATE_FADING,
    STATE_DEQUALIFIED,
    STATE_REQUALIFIED,
    STATE_CLOSED,
}
DEQUALIFY_OUTCOMES = {
    OUTCOME_BELOW_MOVE,
    OUTCOME_AWAITING_PERSISTENCE,
    OUTCOME_BELOW_NOTIONAL,
    OUTCOME_UNSTABLE_PRINT,
}


LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_candidate_lifecycle (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    from_state TEXT,
    state TEXT NOT NULL,
    actionability TEXT NOT NULL,
    transition_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    evidence_bar_open_ts_utc TEXT,
    evaluation_outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    move_pct REAL,
    peak_abs_move_pct REAL NOT NULL,
    cumulative_notional REAL,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    UNIQUE(candidate_id,lifecycle_version,evidence_bar_open_ts_utc,state),
    CHECK (state IN (
        'NEWLY_QUALIFYING','CONFIRMED','STRENGTHENING','FADING',
        'DEQUALIFIED','REQUALIFIED','CLOSED'
    )),
    CHECK (actionability IN ('WATCH','QUALIFIED','NOT_ACTIONABLE','CLOSED'))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_lifecycle_candidate
    ON postmarket_candidate_lifecycle(candidate_id,lifecycle_version,transition_id);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_lifecycle_session
    ON postmarket_candidate_lifecycle(session,state,transition_id);

CREATE TABLE IF NOT EXISTS postmarket_candidate_lifecycle_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    evidence_bar_open_ts_utc TEXT NOT NULL,
    evaluation_outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    move_pct REAL,
    observed_direction TEXT,
    cumulative_notional REAL,
    data_age_seconds REAL,
    data_feed TEXT NOT NULL,
    market_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    UNIQUE(candidate_id,lifecycle_version,evidence_bar_open_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_lifecycle_observations_candidate
    ON postmarket_candidate_lifecycle_observations(candidate_id,seq);

CREATE TRIGGER IF NOT EXISTS postmarket_candidate_lifecycle_no_update
BEFORE UPDATE ON postmarket_candidate_lifecycle BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_lifecycle is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidate_lifecycle_no_delete
BEFORE DELETE ON postmarket_candidate_lifecycle BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_lifecycle is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_lifecycle_observations_no_update
BEFORE UPDATE ON postmarket_candidate_lifecycle_observations BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_lifecycle_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_lifecycle_observations_no_delete
BEFORE DELETE ON postmarket_candidate_lifecycle_observations BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_lifecycle_observations is append-only');
END;
"""


@dataclass(frozen=True)
class LifecycleCandidate:
    candidate_id: int
    session: date
    symbol: str
    direction: str
    first_detected_at: datetime
    initial_bar_open_ts: datetime
    initial_move_pct: float
    initial_notional: float
    data_feed: str
    market_data_provider: str
    bar_timeframe: str


@dataclass(frozen=True)
class LifecycleTransition:
    candidate_id: int
    session: str
    symbol: str
    direction: str
    from_state: str | None
    state: str
    actionability: str
    transition_at_utc: str
    recorded_at_utc: str
    evidence_bar_open_ts_utc: str | None
    evaluation_outcome: str
    reason: str
    move_pct: float | None
    peak_abs_move_pct: float
    cumulative_notional: float | None
    data_feed: str
    market_data_provider: str
    bar_timeframe: str
    code_version: str | None
    run_id: str


@dataclass(frozen=True)
class LifecyclePassResult:
    session: str | None
    tracked_candidates: int
    symbols_fetched: int
    observations_written: int
    transitions_written: int
    states_written: tuple[tuple[str, int], ...]
    error_count: int
    latency_ms: int


def ensure_lifecycle_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LIFECYCLE_SCHEMA)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def lifecycle_window(session: date) -> tuple[datetime, datetime]:
    if not CALENDAR.is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    close = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
    postmarket_end = datetime.combine(
        session, wall_time(20, 0), tzinfo=ET
    ).astimezone(timezone.utc)
    return close, postmarket_end + FINAL_BAR_GRACE


def _actionability(state: str) -> str:
    if state in {STATE_CONFIRMED, STATE_STRENGTHENING, STATE_REQUALIFIED}:
        return "QUALIFIED"
    if state in {STATE_NEW, STATE_FADING}:
        return "WATCH"
    if state == STATE_DEQUALIFIED:
        return "NOT_ACTIONABLE"
    return "CLOSED"


def _candidate_rows(conn: sqlite3.Connection, session: date) -> list[LifecycleCandidate]:
    ensure_lifecycle_schema(conn)
    return [
        LifecycleCandidate(
            candidate_id=int(row[0]),
            session=date.fromisoformat(row[1]),
            symbol=row[2],
            direction=row[3],
            first_detected_at=_aware_utc(datetime.fromisoformat(row[4]), "first_detected_at"),
            initial_bar_open_ts=_aware_utc(datetime.fromisoformat(row[5]), "bar_open_ts_utc"),
            initial_move_pct=float(row[6]),
            initial_notional=float(row[7]),
            data_feed=row[8],
            market_data_provider=row[9],
            bar_timeframe=row[10],
        )
        for row in conn.execute(
            """
            SELECT candidate_id,session,symbol,direction,first_detected_at,
                   bar_open_ts_utc,move_pct,cumulative_notional,data_feed,
                   market_data_provider,bar_timeframe
            FROM postmarket_discovery_candidates
            WHERE session=? ORDER BY candidate_id
            """,
            (session.isoformat(),),
        ).fetchall()
    ]


def _latest_transition(
    conn: sqlite3.Connection, candidate_id: int,
) -> tuple[str, float, float | None, str | None] | None:
    row = conn.execute(
        """
        SELECT state,peak_abs_move_pct,move_pct,evidence_bar_open_ts_utc
        FROM postmarket_candidate_lifecycle
        WHERE candidate_id=? AND lifecycle_version=?
        ORDER BY transition_id DESC LIMIT 1
        """,
        (candidate_id, LIFECYCLE_VERSION),
    ).fetchone()
    return None if row is None else (row[0], float(row[1]), row[2], row[3])


def _transition(
    candidate: LifecycleCandidate,
    *,
    from_state: str | None,
    state: str,
    transition_at: datetime,
    recorded_at: datetime,
    evaluation_outcome: str,
    reason: str,
    move_pct: float | None,
    peak_abs_move_pct: float,
    cumulative_notional: float | None,
    evidence_bar_open_ts: datetime | None,
    code_version: str | None,
    run_id: str,
) -> LifecycleTransition:
    if state not in STATES:
        raise ValueError(f"unknown lifecycle state: {state}")
    return LifecycleTransition(
        candidate_id=candidate.candidate_id,
        session=candidate.session.isoformat(),
        symbol=candidate.symbol,
        direction=candidate.direction,
        from_state=from_state,
        state=state,
        actionability=_actionability(state),
        transition_at_utc=_aware_utc(transition_at, "transition_at").isoformat(),
        recorded_at_utc=_aware_utc(recorded_at, "recorded_at").isoformat(),
        evidence_bar_open_ts_utc=(
            _aware_utc(evidence_bar_open_ts, "evidence_bar_open_ts").isoformat()
            if evidence_bar_open_ts else None
        ),
        evaluation_outcome=evaluation_outcome,
        reason=reason,
        move_pct=move_pct,
        peak_abs_move_pct=peak_abs_move_pct,
        cumulative_notional=cumulative_notional,
        data_feed=candidate.data_feed,
        market_data_provider=candidate.market_data_provider,
        bar_timeframe=candidate.bar_timeframe,
        code_version=code_version,
        run_id=run_id,
    )


def initial_transition(
    candidate: LifecycleCandidate, *, recorded_at: datetime,
    code_version: str | None, run_id: str,
) -> LifecycleTransition:
    return _transition(
        candidate,
        from_state=None,
        state=STATE_NEW,
        transition_at=candidate.first_detected_at,
        recorded_at=recorded_at,
        evidence_bar_open_ts=candidate.initial_bar_open_ts,
        evaluation_outcome=OUTCOME_CANDIDATE,
        reason="first completed-bar candidate qualification",
        move_pct=candidate.initial_move_pct,
        peak_abs_move_pct=abs(candidate.initial_move_pct),
        cumulative_notional=candidate.initial_notional,
        code_version=code_version,
        run_id=run_id,
    )


def plan_evaluation_transition(
    candidate: LifecycleCandidate,
    latest: tuple[str, float, float | None, str | None],
    evaluation: ReactionEvaluation,
    *,
    recorded_at: datetime,
    code_version: str | None,
    run_id: str,
) -> LifecycleTransition | None:
    state, peak, prior_move, prior_bar = latest
    if state == STATE_CLOSED or evaluation.bar is None:
        return None
    evidence_bar = _aware_utc(evaluation.bar.ts, "evaluation.bar.ts")
    if evidence_bar <= candidate.initial_bar_open_ts:
        return None
    if prior_bar is not None and evidence_bar <= _aware_utc(
        datetime.fromisoformat(prior_bar), "prior evidence bar"
    ):
        return None
    move = evaluation.move_pct
    absolute_move = abs(move) if move is not None else None
    next_state = None
    reason = evaluation.reason
    next_peak = peak
    qualifies_same_direction = (
        evaluation.outcome == OUTCOME_CANDIDATE
        and evaluation.direction == candidate.direction
        and absolute_move is not None
    )
    if evaluation.outcome == OUTCOME_CANDIDATE and not qualifies_same_direction:
        next_state = STATE_DEQUALIFIED
        reason = "candidate direction reversed"
    elif qualifies_same_direction:
        next_peak = max(peak, absolute_move)
        if state == STATE_NEW:
            next_state = STATE_CONFIRMED
            reason = "qualification persisted on a later completed bar"
        elif state == STATE_DEQUALIFIED:
            next_state = STATE_REQUALIFIED
            reason = "qualification returned on a later completed bar"
        elif absolute_move >= peak + STRENGTHENING_DELTA_PCT:
            next_state = STATE_STRENGTHENING
            reason = (
                f"absolute move expanded at least {STRENGTHENING_DELTA_PCT:.1f}pp "
                "beyond the prior peak"
            )
        elif absolute_move <= peak - FADING_FROM_PEAK_PCT and state != STATE_FADING:
            next_state = STATE_FADING
            reason = (
                f"absolute move retraced at least {FADING_FROM_PEAK_PCT:.1f}pp "
                "from the recorded peak"
            )
        elif (
            state == STATE_FADING and prior_move is not None
            and absolute_move >= abs(prior_move) + RECOVERY_DELTA_PCT
        ):
            next_state = STATE_CONFIRMED
            reason = "move recovered from fading while remaining qualified"
    elif evaluation.outcome in DEQUALIFY_OUTCOMES and state != STATE_DEQUALIFIED:
        next_state = STATE_DEQUALIFIED
    if next_state is None:
        return None
    return _transition(
        candidate,
        from_state=state,
        state=next_state,
        transition_at=bar_close_ts(evaluation.bar),
        recorded_at=recorded_at,
        evidence_bar_open_ts=evidence_bar,
        evaluation_outcome=evaluation.outcome,
        reason=reason,
        move_pct=move,
        peak_abs_move_pct=next_peak,
        cumulative_notional=evaluation.cumulative_notional,
        code_version=code_version,
        run_id=run_id,
    )


def closed_transition(
    candidate: LifecycleCandidate,
    latest: tuple[str, float, float | None, str | None],
    *,
    closed_at: datetime,
    recorded_at: datetime,
    code_version: str | None,
    run_id: str,
    evaluation: ReactionEvaluation | None = None,
) -> LifecycleTransition | None:
    state, peak, move, evidence_bar = latest
    if state == STATE_CLOSED:
        return None
    if evaluation is not None and evaluation.bar is not None:
        evidence_bar_value = evaluation.bar.ts
        move_value = evaluation.move_pct
        cumulative_notional = evaluation.cumulative_notional
    else:
        evidence_bar_value = datetime.fromisoformat(evidence_bar) if evidence_bar else None
        move_value = move
        cumulative_notional = None
    return _transition(
        candidate,
        from_state=state,
        state=STATE_CLOSED,
        transition_at=closed_at,
        recorded_at=recorded_at,
        evidence_bar_open_ts=evidence_bar_value,
        evaluation_outcome="WINDOW_CLOSED",
        reason="postmarket observation window closed",
        move_pct=move_value,
        peak_abs_move_pct=peak,
        cumulative_notional=cumulative_notional,
        code_version=code_version,
        run_id=run_id,
    )


def record_transition(conn: sqlite3.Connection, transition: LifecycleTransition) -> bool:
    ensure_lifecycle_schema(conn)
    with conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO postmarket_candidate_lifecycle
                (candidate_id,lifecycle_version,session,symbol,direction,
                 from_state,state,actionability,transition_at_utc,recorded_at_utc,
                 evidence_bar_open_ts_utc,evaluation_outcome,reason,move_pct,
                 peak_abs_move_pct,cumulative_notional,data_feed,
                 market_data_provider,bar_timeframe,code_version,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                transition.candidate_id, LIFECYCLE_VERSION, transition.session,
                transition.symbol, transition.direction, transition.from_state,
                transition.state, transition.actionability,
                transition.transition_at_utc, transition.recorded_at_utc,
                transition.evidence_bar_open_ts_utc, transition.evaluation_outcome,
                transition.reason, transition.move_pct,
                transition.peak_abs_move_pct, transition.cumulative_notional,
                transition.data_feed, transition.market_data_provider,
                transition.bar_timeframe, transition.code_version,
                transition.run_id,
            ),
        )
    return bool(cursor.rowcount)


def record_observation(
    conn: sqlite3.Connection,
    candidate: LifecycleCandidate,
    evaluation: ReactionEvaluation,
    *,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> bool:
    """Record each distinct completed candidate bar, even without a transition."""
    if evaluation.bar is None:
        return False
    ensure_lifecycle_schema(conn)
    with conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO postmarket_candidate_lifecycle_observations
                (candidate_id,lifecycle_version,session,symbol,observed_at_utc,
                 evidence_bar_open_ts_utc,evaluation_outcome,reason,move_pct,
                 observed_direction,cumulative_notional,data_age_seconds,
                 data_feed,market_data_provider,bar_timeframe,code_version,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.candidate_id, LIFECYCLE_VERSION,
                candidate.session.isoformat(), candidate.symbol,
                _aware_utc(observed_at, "observed_at").isoformat(),
                _aware_utc(evaluation.bar.ts, "evaluation.bar.ts").isoformat(),
                evaluation.outcome, evaluation.reason, evaluation.move_pct,
                evaluation.direction, evaluation.cumulative_notional,
                evaluation.data_age_seconds, candidate.data_feed,
                candidate.market_data_provider, candidate.bar_timeframe,
                code_version, run_id,
            ),
        )
    return bool(cursor.rowcount)


def latest_open_session(conn: sqlite3.Connection) -> date | None:
    """Newest candidate session with an absent or non-CLOSED lifecycle."""
    ensure_lifecycle_schema(conn)
    row = conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id,MAX(transition_id) AS transition_id
            FROM postmarket_candidate_lifecycle
            WHERE lifecycle_version=? GROUP BY candidate_id
        )
        SELECT MAX(c.session)
        FROM postmarket_discovery_candidates c
        LEFT JOIN latest l ON l.candidate_id=c.candidate_id
        LEFT JOIN postmarket_candidate_lifecycle x
          ON x.transition_id=l.transition_id
        WHERE x.state IS NULL OR x.state!='CLOSED'
        """,
        (LIFECYCLE_VERSION,),
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def run_lifecycle_pass(
    conn: sqlite3.Connection,
    *,
    session: date,
    session_close: datetime,
    window_end: datetime,
    now: datetime,
    code_version: str | None,
    run_id: str,
    data_feed: str,
    bars_fetch: Callable[[list[str], date], Mapping[str, Sequence[Bar]]],
    existing_evaluations: Sequence[ReactionEvaluation] = (),
) -> LifecyclePassResult:
    current = _aware_utc(now, "now")
    close = _aware_utc(session_close, "session_close")
    end = _aware_utc(window_end, "window_end")
    started = time.perf_counter()
    candidates = _candidate_rows(conn, session)
    if not candidates:
        return LifecyclePassResult(session.isoformat(), 0, 0, 0, 0, (), 0, 0)
    if any(candidate.data_feed != data_feed for candidate in candidates):
        raise ValueError("candidate lifecycle feed does not match the service feed")
    evaluation_by_symbol = {row.symbol: row for row in existing_evaluations}
    errors = sum(row.outcome == OUTCOME_FETCH_ERROR for row in existing_evaluations)
    open_candidates = [
        candidate for candidate in candidates
        if (_latest_transition(conn, candidate.candidate_id) or (None,))[0] != STATE_CLOSED
    ]
    missing_symbols = sorted(
        {candidate.symbol for candidate in open_candidates} - set(evaluation_by_symbol)
    )
    fetched = {}
    if missing_symbols:
        try:
            fetched = dict(bars_fetch(missing_symbols, session))
        except Exception as exc:
            errors += 1
            fetched = {}
            for symbol in missing_symbols:
                evaluation_by_symbol[symbol] = fetch_error_evaluation(symbol, session, exc)
        else:
            for symbol in missing_symbols:
                bars = fetched.get(symbol)
                if bars is None:
                    evaluation_by_symbol[symbol] = fetch_error_evaluation(
                        symbol, session, RuntimeError("missing from lifecycle bar response")
                    )
                    errors += 1
                    continue
                try:
                    snapshot = partition_intraday_bars(bars)
                    evaluation_by_symbol[symbol] = evaluate_postmarket_reaction(
                        symbol,
                        session,
                        snapshot.rth,
                        snapshot.postmarket,
                        session_close=close,
                        now=current,
                    )
                except Exception as exc:
                    evaluation_by_symbol[symbol] = fetch_error_evaluation(symbol, session, exc)
                    errors += 1
    states: dict[str, int] = {}
    written = 0
    observations_written = 0
    for candidate in open_candidates:
        latest = _latest_transition(conn, candidate.candidate_id)
        if latest is None:
            transition = initial_transition(
                candidate, recorded_at=current, code_version=code_version, run_id=run_id
            )
            if record_transition(conn, transition):
                written += 1
                states[transition.state] = states.get(transition.state, 0) + 1
            latest = _latest_transition(conn, candidate.candidate_id)
            assert latest is not None
        evaluation = evaluation_by_symbol.get(candidate.symbol)
        if evaluation is not None:
            observations_written += int(
                record_observation(
                    conn,
                    candidate,
                    evaluation,
                    observed_at=current,
                    code_version=code_version,
                    run_id=run_id,
                )
            )
            transition = plan_evaluation_transition(
                candidate,
                latest,
                evaluation,
                recorded_at=current,
                code_version=code_version,
                run_id=run_id,
            )
            if transition is not None and record_transition(conn, transition):
                written += 1
                states[transition.state] = states.get(transition.state, 0) + 1
                latest = _latest_transition(conn, candidate.candidate_id)
                assert latest is not None
        if current >= end:
            transition = closed_transition(
                candidate,
                latest,
                closed_at=end,
                recorded_at=current,
                code_version=code_version,
                run_id=run_id,
                evaluation=evaluation,
            )
            if transition is not None and record_transition(conn, transition):
                written += 1
                states[transition.state] = states.get(transition.state, 0) + 1
    return LifecyclePassResult(
        session=session.isoformat(),
        tracked_candidates=len(open_candidates),
        symbols_fetched=len(fetched),
        observations_written=observations_written,
        transitions_written=written,
        states_written=tuple(sorted(states.items())),
        error_count=errors,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


def lifecycle_summary(conn: sqlite3.Connection) -> dict | None:
    ensure_lifecycle_schema(conn)
    session_row = conn.execute(
        "SELECT MAX(session) FROM postmarket_discovery_candidates"
    ).fetchone()
    if not session_row or session_row[0] is None:
        return None
    session = session_row[0]
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id,MAX(transition_id) AS transition_id
            FROM postmarket_candidate_lifecycle
            WHERE lifecycle_version=? GROUP BY candidate_id
        )
        SELECT COALESCE(x.state,'MISSING') AS state,COUNT(*)
        FROM postmarket_discovery_candidates c
        LEFT JOIN latest l ON l.candidate_id=c.candidate_id
        LEFT JOIN postmarket_candidate_lifecycle x ON x.transition_id=l.transition_id
        WHERE c.session=? GROUP BY COALESCE(x.state,'MISSING') ORDER BY state
        """,
        (LIFECYCLE_VERSION, session),
    ).fetchall()
    counts = {state: count for state, count in rows}
    observation_row = conn.execute(
        """
        SELECT COUNT(*),MAX(observed_at_utc),MAX(evidence_bar_open_ts_utc)
        FROM postmarket_candidate_lifecycle_observations WHERE session=?
        """,
        (session,),
    ).fetchone()
    return {
        "session": session,
        "candidates": sum(counts.values()),
        "states": counts,
        "currently_qualified": sum(
            counts.get(state, 0)
            for state in {STATE_CONFIRMED, STATE_STRENGTHENING, STATE_REQUALIFIED}
        ),
        "closed": counts.get(STATE_CLOSED, 0),
        "missing": counts.get("MISSING", 0),
        "observations": int(observation_row[0] or 0),
        "latest_observed_at_utc": observation_row[1],
        "latest_evidence_bar_open_ts_utc": observation_row[2],
    }
