"""Deterministic, decomposable shadow rank for postmarket candidates.

The score is an evidence-ordering heuristic, not a probability, confidence,
profit forecast, recommendation, or delivery decision.  Every component,
penalty, exclusion, source row, and version is persisted append-only.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from tradebot.postmarket_lifecycle import (
    STATE_CONFIRMED,
    STATE_FADING,
    STATE_REQUALIFIED,
    STATE_STRENGTHENING,
)


RANK_VERSION = 1
MAX_OBSERVATION_AGE_SECONDS = 420
MAX_RANKABLE_SPREAD_BPS = 300
COMPONENT_WEIGHTS = {
    "volatility_normalized_move": 20.0,
    "market_relative_excess": 15.0,
    "rth_dollar_liquidity": 15.0,
    "postmarket_notional": 10.0,
    "quote_spread": 15.0,
    "quoted_depth": 5.0,
    "verified_catalyst": 10.0,
    "lifecycle": 10.0,
}
RANKABLE_STATES = {STATE_CONFIRMED, STATE_STRENGTHENING, STATE_REQUALIFIED}


RANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_rank_runs (
    rank_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    rank_version INTEGER NOT NULL,
    as_of_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    input_digest_sha256 TEXT NOT NULL,
    input_candidates INTEGER NOT NULL,
    rankable_candidates INTEGER NOT NULL,
    status TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    UNIQUE(session,rank_version,input_digest_sha256),
    CHECK (status IN ('complete','degraded'))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_rank_runs_session
    ON postmarket_rank_runs(session,rank_run_id);

CREATE TABLE IF NOT EXISTS postmarket_candidate_ranks (
    rank_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rank_run_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    context_id INTEGER,
    transition_id INTEGER,
    observation_seq INTEGER,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    lifecycle_state TEXT,
    rankable INTEGER NOT NULL,
    ordinal_rank INTEGER,
    evidence_score REAL NOT NULL,
    raw_component_score REAL NOT NULL,
    penalty_total REAL NOT NULL,
    evidence_coverage_pct REAL NOT NULL,
    components_json TEXT NOT NULL,
    penalties_json TEXT NOT NULL,
    exclusion_reasons_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    UNIQUE(rank_run_id,candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_ranks_candidate
    ON postmarket_candidate_ranks(candidate_id,rank_run_id);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_ranks_run_rank
    ON postmarket_candidate_ranks(rank_run_id,ordinal_rank);

CREATE TRIGGER IF NOT EXISTS postmarket_rank_runs_no_update
BEFORE UPDATE ON postmarket_rank_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_runs_no_delete
BEFORE DELETE ON postmarket_rank_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidate_ranks_no_update
BEFORE UPDATE ON postmarket_candidate_ranks BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_ranks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidate_ranks_no_delete
BEFORE DELETE ON postmarket_candidate_ranks BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_ranks is append-only');
END;
"""


@dataclass(frozen=True)
class RankEvidence:
    candidate_id: int
    session: str
    symbol: str
    direction: str
    context_id: int | None
    context_status: str | None
    volatility_status: str | None
    move_atr_units: float | None
    market_relative_status: str | None
    directional_market_excess_pct: float | None
    quote_status: str | None
    spread_bps: float | None
    quoted_depth_notional: float | None
    liquidity_status: str | None
    rth_dollar_volume: float | None
    postmarket_notional: float
    asset_status: str | None
    tradable: bool | None
    catalyst_status: str | None
    transition_id: int | None
    lifecycle_state: str | None
    actionability: str | None
    observation_seq: int | None
    observation_recorded_at: datetime | None
    evidence_bar_open_ts: datetime | None
    observation_outcome: str | None


@dataclass(frozen=True)
class RankScore:
    candidate_id: int
    symbol: str
    rankable: bool
    evidence_score: float
    raw_component_score: float
    penalty_total: float
    evidence_coverage_pct: float
    components: dict[str, float]
    penalties: dict[str, float]
    exclusion_reasons: tuple[str, ...]
    explanation: tuple[str, ...]
    ordinal_rank: int | None = None


@dataclass(frozen=True)
class RankRunResult:
    rank_run_id: int | None
    session: str | None
    created: bool
    input_candidates: int
    rankable_candidates: int
    top_candidates: tuple[tuple[int, str, float], ...]
    status: str


def ensure_rank_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(RANK_SCHEMA)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _log_component(value: float | None, floor_power: float, span: float, weight: float) -> float:
    if value is None or value <= 0 or not math.isfinite(value):
        return 0.0
    return weight * _clamp((math.log10(value) - floor_power) / span, 0.0, 1.0)


def score_candidate(evidence: RankEvidence, *, as_of: datetime) -> RankScore:
    """Return a named heuristic decomposition with hard eligibility reasons."""
    current = _aware_utc(as_of, "as_of")
    components = {
        "volatility_normalized_move": (
            COMPONENT_WEIGHTS["volatility_normalized_move"]
            * _clamp((evidence.move_atr_units or 0) / 5.0, 0.0, 1.0)
            if evidence.volatility_status == "AVAILABLE" else 0.0
        ),
        "market_relative_excess": (
            _clamp(
                evidence.directional_market_excess_pct or 0,
                0.0,
                COMPONENT_WEIGHTS["market_relative_excess"],
            )
            if evidence.market_relative_status == "AVAILABLE" else 0.0
        ),
        "rth_dollar_liquidity": _log_component(
            evidence.rth_dollar_volume, 6.0, 3.0,
            COMPONENT_WEIGHTS["rth_dollar_liquidity"],
        ),
        "postmarket_notional": _log_component(
            evidence.postmarket_notional, 5.0, 3.0,
            COMPONENT_WEIGHTS["postmarket_notional"],
        ),
        "quote_spread": (
            COMPONENT_WEIGHTS["quote_spread"]
            * _clamp(1 - (evidence.spread_bps or 0) / MAX_RANKABLE_SPREAD_BPS, 0.0, 1.0)
            if evidence.quote_status == "AVAILABLE" and evidence.spread_bps is not None
            else 0.0
        ),
        "quoted_depth": (
            COMPONENT_WEIGHTS["quoted_depth"]
            * _clamp((evidence.quoted_depth_notional or 0) / 1_000_000, 0.0, 1.0)
            if evidence.quoted_depth_notional is not None else 0.0
        ),
        "verified_catalyst": (
            COMPONENT_WEIGHTS["verified_catalyst"]
            if evidence.catalyst_status == "VERIFIED" else 0.0
        ),
        "lifecycle": {
            STATE_STRENGTHENING: 10.0,
            STATE_CONFIRMED: 8.0,
            STATE_REQUALIFIED: 7.0,
            STATE_FADING: 2.0,
        }.get(evidence.lifecycle_state, 0.0),
    }
    available_weight = 0.0
    if evidence.volatility_status == "AVAILABLE":
        available_weight += 20
    if evidence.market_relative_status == "AVAILABLE":
        available_weight += 15
    if evidence.liquidity_status == "AVAILABLE":
        available_weight += 15
    if evidence.postmarket_notional > 0:
        available_weight += 10
    if evidence.quote_status == "AVAILABLE" and evidence.spread_bps is not None:
        available_weight += 15
    if evidence.quoted_depth_notional is not None:
        available_weight += 5
    if evidence.catalyst_status is not None:
        available_weight += 10
    if evidence.lifecycle_state is not None:
        available_weight += 10

    penalties: dict[str, float] = {}
    if evidence.context_status == "degraded":
        penalties["degraded_context"] = -20.0
    if evidence.spread_bps is not None and evidence.spread_bps > 100:
        penalties["wide_spread"] = -min(15.0, (evidence.spread_bps - 100) / 20)
    if evidence.rth_dollar_volume is not None and evidence.rth_dollar_volume < 5_000_000:
        penalties["thin_rth_liquidity"] = -10.0
    if evidence.catalyst_status == "NO_VERIFIED_CATALYST":
        penalties["unexplained_catalyst"] = -5.0
    if evidence.lifecycle_state == STATE_FADING:
        penalties["fading"] = -15.0
    if evidence.lifecycle_state == STATE_REQUALIFIED:
        penalties["requalified_after_failure"] = -3.0
    if evidence.volatility_status != "AVAILABLE":
        penalties["volatility_unavailable"] = -5.0
    if evidence.market_relative_status != "AVAILABLE":
        penalties["benchmark_unavailable"] = -5.0

    exclusions: list[str] = []
    if evidence.context_id is None:
        exclusions.append("MISSING_CONTEXT")
    elif evidence.context_status != "complete":
        exclusions.append("CONTEXT_DEGRADED")
    if evidence.transition_id is None or evidence.lifecycle_state is None:
        exclusions.append("MISSING_LIFECYCLE")
    elif evidence.lifecycle_state not in RANKABLE_STATES:
        exclusions.append(f"STATE_{evidence.lifecycle_state}_NOT_RANKABLE")
    if evidence.observation_seq is None or evidence.evidence_bar_open_ts is None:
        exclusions.append("MISSING_COMPLETED_BAR_OBSERVATION")
        observation_age = None
    else:
        observation_age = (
            current - (evidence.evidence_bar_open_ts + timedelta(minutes=5))
        ).total_seconds()
        if observation_age < 0:
            exclusions.append("OBSERVATION_FROM_FUTURE")
        elif observation_age > MAX_OBSERVATION_AGE_SECONDS:
            exclusions.append("OBSERVATION_STALE")
            penalties["stale_observation"] = -25.0
    if evidence.asset_status != "AVAILABLE":
        exclusions.append("ASSET_UNAVAILABLE")
    elif evidence.tradable is not True:
        exclusions.append("ASSET_NOT_TRADABLE")
    if evidence.quote_status != "AVAILABLE":
        exclusions.append(f"QUOTE_{evidence.quote_status or 'MISSING'}")
    elif evidence.spread_bps is None:
        exclusions.append("SPREAD_MISSING")
    elif evidence.spread_bps > MAX_RANKABLE_SPREAD_BPS:
        exclusions.append("SPREAD_TOO_WIDE")

    rounded_components = {key: round(value, 6) for key, value in components.items()}
    rounded_penalties = {key: round(value, 6) for key, value in penalties.items()}
    raw = round(sum(rounded_components.values()), 6)
    penalty_total = round(sum(rounded_penalties.values()), 6)
    score = round(_clamp(raw + penalty_total, 0.0, 100.0), 6)
    explanation = tuple(
        [f"{key}={value:+.2f}" for key, value in rounded_components.items()]
        + [f"penalty:{key}={value:+.2f}" for key, value in rounded_penalties.items()]
        + [f"excluded:{reason}" for reason in exclusions]
    )
    return RankScore(
        candidate_id=evidence.candidate_id,
        symbol=evidence.symbol,
        rankable=not exclusions,
        evidence_score=score,
        raw_component_score=raw,
        penalty_total=penalty_total,
        evidence_coverage_pct=round(available_weight, 6),
        components=rounded_components,
        penalties=rounded_penalties,
        exclusion_reasons=tuple(exclusions),
        explanation=explanation,
    )


def _load_evidence(conn: sqlite3.Connection, session: str) -> tuple[RankEvidence, ...]:
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH latest_context AS (
                SELECT candidate_id,MAX(context_id) AS context_id
                FROM postmarket_candidate_context GROUP BY candidate_id
            ), latest_transition AS (
                SELECT candidate_id,MAX(transition_id) AS transition_id
                FROM postmarket_candidate_lifecycle GROUP BY candidate_id
            ), latest_observation AS (
                SELECT candidate_id,MAX(seq) AS seq
                FROM postmarket_candidate_lifecycle_observations GROUP BY candidate_id
            )
            SELECT c.candidate_id,c.session,c.symbol,c.direction,
                   ctx.context_id,ctx.status AS context_status,
                   ctx.volatility_status,ctx.move_atr_units,
                   ctx.market_relative_status,ctx.directional_market_excess_pct,
                   ctx.quote_status,ctx.spread_bps,ctx.quoted_depth_notional,
                   ctx.liquidity_status,ctx.rth_dollar_volume,
                   COALESCE(ctx.postmarket_notional,c.cumulative_notional) AS pm_notional,
                   ctx.asset_status,ctx.tradable,ctx.catalyst_status,
                   tr.transition_id,tr.state AS lifecycle_state,tr.actionability,
                   obs.seq AS observation_seq,obs.observed_at_utc,
                   obs.evidence_bar_open_ts_utc,obs.evaluation_outcome
            FROM postmarket_discovery_candidates c
            LEFT JOIN latest_context lc ON lc.candidate_id=c.candidate_id
            LEFT JOIN postmarket_candidate_context ctx ON ctx.context_id=lc.context_id
            LEFT JOIN latest_transition lt ON lt.candidate_id=c.candidate_id
            LEFT JOIN postmarket_candidate_lifecycle tr ON tr.transition_id=lt.transition_id
            LEFT JOIN latest_observation lo ON lo.candidate_id=c.candidate_id
            LEFT JOIN postmarket_candidate_lifecycle_observations obs ON obs.seq=lo.seq
            WHERE c.session=? ORDER BY c.candidate_id
            """,
            (session,),
        ).fetchall()
    finally:
        conn.row_factory = original
    return tuple(
        RankEvidence(
            candidate_id=int(row["candidate_id"]),
            session=row["session"],
            symbol=row["symbol"],
            direction=row["direction"],
            context_id=row["context_id"],
            context_status=row["context_status"],
            volatility_status=row["volatility_status"],
            move_atr_units=row["move_atr_units"],
            market_relative_status=row["market_relative_status"],
            directional_market_excess_pct=row["directional_market_excess_pct"],
            quote_status=row["quote_status"],
            spread_bps=row["spread_bps"],
            quoted_depth_notional=row["quoted_depth_notional"],
            liquidity_status=row["liquidity_status"],
            rth_dollar_volume=row["rth_dollar_volume"],
            postmarket_notional=float(row["pm_notional"]),
            asset_status=row["asset_status"],
            tradable=None if row["tradable"] is None else bool(row["tradable"]),
            catalyst_status=row["catalyst_status"],
            transition_id=row["transition_id"],
            lifecycle_state=row["lifecycle_state"],
            actionability=row["actionability"],
            observation_seq=row["observation_seq"],
            observation_recorded_at=(
                _aware_utc(datetime.fromisoformat(row["observed_at_utc"]), "observed_at")
                if row["observed_at_utc"] else None
            ),
            evidence_bar_open_ts=(
                _aware_utc(
                    datetime.fromisoformat(row["evidence_bar_open_ts_utc"]),
                    "evidence_bar_open_ts",
                )
                if row["evidence_bar_open_ts_utc"] else None
            ),
            observation_outcome=row["evaluation_outcome"],
        )
        for row in rows
    )


def _freshness_state(row: RankEvidence, as_of: datetime) -> str:
    if row.evidence_bar_open_ts is None:
        return "MISSING"
    age = (as_of - (row.evidence_bar_open_ts + timedelta(minutes=5))).total_seconds()
    if age < 0:
        return "FUTURE"
    if age > MAX_OBSERVATION_AGE_SECONDS:
        return "STALE"
    return "FRESH"


def _input_digest(evidence: Sequence[RankEvidence], as_of: datetime) -> str:
    identity = [
        {
            "candidate_id": row.candidate_id,
            "context_id": row.context_id,
            "transition_id": row.transition_id,
            "observation_seq": row.observation_seq,
            "freshness_state": _freshness_state(row, as_of),
        }
        for row in evidence
    ]
    payload = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def run_rank_snapshot(
    conn: sqlite3.Connection,
    *,
    session: str,
    as_of: datetime,
    code_version: str | None,
    run_id: str,
) -> RankRunResult:
    current = _aware_utc(as_of, "as_of")
    ensure_rank_schema(conn)
    evidence = _load_evidence(conn, session)
    if not evidence:
        return RankRunResult(None, session, False, 0, 0, (), "current")
    digest = _input_digest(evidence, current)
    existing = conn.execute(
        """
        SELECT rank_run_id,rankable_candidates,status FROM postmarket_rank_runs
        WHERE session=? AND rank_version=? AND input_digest_sha256=?
        """,
        (session, RANK_VERSION, digest),
    ).fetchone()
    if existing is not None:
        top = tuple(
            (row[0], row[1], row[2])
            for row in conn.execute(
                """
                SELECT ordinal_rank,symbol,evidence_score
                FROM postmarket_candidate_ranks
                WHERE rank_run_id=? AND ordinal_rank IS NOT NULL
                ORDER BY ordinal_rank LIMIT 5
                """,
                (existing[0],),
            ).fetchall()
        )
        return RankRunResult(
            int(existing[0]), session, False, len(evidence), int(existing[1]), top,
            existing[2],
        )
    scored = [score_candidate(row, as_of=current) for row in evidence]
    ordered = sorted(
        (row for row in scored if row.rankable),
        key=lambda row: (-row.evidence_score, -row.evidence_coverage_pct, row.symbol, row.candidate_id),
    )
    ordinals = {row.candidate_id: index for index, row in enumerate(ordered, 1)}
    scored = [
        RankScore(**{**row.__dict__, "ordinal_rank": ordinals.get(row.candidate_id)})
        for row in scored
    ]
    status = "complete" if all(
        row.context_id is not None and row.context_status == "complete"
        and row.transition_id is not None
        and row.observation_seq is not None
        for row in evidence
    ) else "degraded"
    thresholds = {
        "max_observation_age_seconds": MAX_OBSERVATION_AGE_SECONDS,
        "max_rankable_spread_bps": MAX_RANKABLE_SPREAD_BPS,
        "score_semantics": "heuristic_evidence_ordering_not_probability",
    }
    evidence_by_id = {row.candidate_id: row for row in evidence}
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_rank_runs
                (session,rank_version,as_of_utc,recorded_at_utc,code_version,
                 run_id,input_digest_sha256,input_candidates,rankable_candidates,
                 status,weights_json,thresholds_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session, RANK_VERSION, current.isoformat(), current.isoformat(),
                code_version, run_id, digest, len(evidence), len(ordered), status,
                json.dumps(COMPONENT_WEIGHTS, separators=(",", ":"), sort_keys=True),
                json.dumps(thresholds, separators=(",", ":"), sort_keys=True),
            ),
        )
        rank_run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO postmarket_candidate_ranks
                (rank_run_id,candidate_id,context_id,transition_id,observation_seq,
                 session,symbol,direction,lifecycle_state,rankable,ordinal_rank,
                 evidence_score,raw_component_score,penalty_total,
                 evidence_coverage_pct,components_json,penalties_json,
                 exclusion_reasons_json,explanation_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    rank_run_id, score.candidate_id, evidence_by_id[score.candidate_id].context_id,
                    evidence_by_id[score.candidate_id].transition_id,
                    evidence_by_id[score.candidate_id].observation_seq, session,
                    score.symbol, evidence_by_id[score.candidate_id].direction,
                    evidence_by_id[score.candidate_id].lifecycle_state,
                    int(score.rankable), score.ordinal_rank, score.evidence_score,
                    score.raw_component_score, score.penalty_total,
                    score.evidence_coverage_pct,
                    json.dumps(score.components, separators=(",", ":"), sort_keys=True),
                    json.dumps(score.penalties, separators=(",", ":"), sort_keys=True),
                    json.dumps(score.exclusion_reasons, separators=(",", ":")),
                    json.dumps(score.explanation, separators=(",", ":")),
                )
                for score in scored
            ],
        )
    top = tuple(
        (row.ordinal_rank, row.symbol, row.evidence_score)
        for row in sorted(scored, key=lambda item: item.ordinal_rank or 10**9)
        if row.ordinal_rank is not None
    )[:5]
    return RankRunResult(
        rank_run_id, session, True, len(evidence), len(ordered), top, status
    )


def _rank_top(conn: sqlite3.Connection, rank_run_id: int) -> list[dict]:
    return [
        {"rank": rank, "symbol": symbol, "evidence_score": score}
        for rank, symbol, score in conn.execute(
            """
            SELECT ordinal_rank,symbol,evidence_score
            FROM postmarket_candidate_ranks
            WHERE rank_run_id=? AND ordinal_rank IS NOT NULL
            ORDER BY ordinal_rank LIMIT 5
            """,
            (rank_run_id,),
        ).fetchall()
    ]


def _rank_exclusion_counts(conn: sqlite3.Connection, rank_run_id: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for (raw_reasons,) in conn.execute(
        """
        SELECT exclusion_reasons_json FROM postmarket_candidate_ranks
        WHERE rank_run_id=? AND rankable=0
        """,
        (rank_run_id,),
    ).fetchall():
        try:
            reasons = json.loads(raw_reasons)
        except (json.JSONDecodeError, TypeError):
            reasons = None
        if not isinstance(reasons, list) or any(
            not isinstance(reason, str) or not reason for reason in reasons
        ):
            counts["MALFORMED_EXCLUSION_EVIDENCE"] += 1
            continue
        counts.update(reasons)
    return dict(sorted(counts.items()))


def latest_rank_summary(conn: sqlite3.Connection) -> dict | None:
    """Expose current rankability separately from historical session capability."""
    ensure_rank_schema(conn)
    row = conn.execute(
        """
        SELECT rank_run_id,session,as_of_utc,input_candidates,
               rankable_candidates,status
        FROM postmarket_rank_runs ORDER BY rank_run_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    rank_run_id = int(row[0])
    session = row[1]
    session_stats = conn.execute(
        """
        SELECT COUNT(*),
               SUM(rankable_candidates>0),
               MAX(rankable_candidates),
               MIN(CASE WHEN rankable_candidates>0 THEN as_of_utc END),
               MAX(CASE WHEN rankable_candidates>0 THEN as_of_utc END)
        FROM postmarket_rank_runs WHERE session=?
        """,
        (session,),
    ).fetchone()
    latest_rankable = conn.execute(
        """
        SELECT rank_run_id,as_of_utc,rankable_candidates
        FROM postmarket_rank_runs
        WHERE session=? AND rankable_candidates>0
        ORDER BY rank_run_id DESC LIMIT 1
        """,
        (session,),
    ).fetchone()
    latest_rankable_snapshot = (
        {
            "rank_run_id": int(latest_rankable[0]),
            "as_of_utc": latest_rankable[1],
            "rankable_candidates": int(latest_rankable[2]),
            "top": _rank_top(conn, int(latest_rankable[0])),
        }
        if latest_rankable is not None
        else None
    )
    return {
        "rank_run_id": rank_run_id,
        "session": session,
        "as_of_utc": row[2],
        "input_candidates": int(row[3]),
        "rankable_candidates": int(row[4]),
        "unrankable_candidates": int(row[3]) - int(row[4]),
        "status": row[5],
        "top": _rank_top(conn, rank_run_id),
        "latest_exclusion_counts": _rank_exclusion_counts(conn, rank_run_id),
        "session_runs": int(session_stats[0]),
        "session_rankable_runs": int(session_stats[1] or 0),
        "session_peak_rankable_candidates": int(session_stats[2] or 0),
        "first_rankable_as_of_utc": session_stats[3],
        "latest_rankable_as_of_utc": session_stats[4],
        "latest_rankable_snapshot": latest_rankable_snapshot,
        "semantics": "heuristic_evidence_ordering_not_probability",
    }
