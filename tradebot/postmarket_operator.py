"""Owner-only shadow notifications for qualified postmarket candidates.

This module is deliberately downstream of discovery.  It never fetches market
data, changes candidate state, routes to customers, or places an order.  The
only write is an idempotent row in the existing Telegram outbox for one
explicitly configured administrator chat.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from tradebot.telegram_bot import outbox


OPERATOR_ALERT_VERSION = 1
MAX_CANDIDATE_AGE_SECONDS = 15 * 60
MAX_ALERTS_PER_CYCLE = 5
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OperatorOpportunity:
    candidate_id: int
    session: str
    symbol: str
    direction: str
    first_detected_at: datetime
    bar_open_ts_utc: datetime
    rth_close: float
    close: float
    move_pct: float
    cumulative_volume: int
    cumulative_notional: float
    sources_json: str
    data_feed: str
    market_data_provider: str
    bar_timeframe: str
    lifecycle_state: str | None
    actionability: str | None
    context_status: str | None
    catalyst_status: str | None
    catalyst_category: str | None
    spread_bps: float | None
    rankable: bool | None
    ordinal_rank: int | None
    evidence_score: float | None
    evidence_coverage_pct: float | None


@dataclass(frozen=True)
class OperatorCycleResult:
    session: str
    candidates_seen: int
    eligible_candidates: int
    alerts_enqueued: int
    alerts_deduplicated: int
    stale_candidates: int


def _aware(value: str, field: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def operator_alert_id(candidate_id: int) -> str:
    if candidate_id <= 0:
        raise ValueError("candidate_id must be positive")
    return f"postmarket-operator:v{OPERATOR_ALERT_VERSION}:candidate:{candidate_id}"


def validate_operator_chat(users_conn: sqlite3.Connection, chat_id: int) -> None:
    if chat_id == 0:
        raise ValueError("operator chat id must be nonzero")
    row = users_conn.execute(
        "SELECT COUNT(*) FROM users WHERE chat_id=? AND is_admin=1",
        (chat_id,),
    ).fetchone()
    if row is None or int(row[0]) != 1:
        raise ValueError("operator chat must identify exactly one configured administrator")


def load_session_opportunities(
    shadow_conn: sqlite3.Connection,
    *,
    session: date,
) -> tuple[OperatorOpportunity, ...]:
    original_factory = shadow_conn.row_factory
    shadow_conn.row_factory = sqlite3.Row
    try:
        rows = shadow_conn.execute(
            """
            WITH latest_transition AS (
                SELECT candidate_id,MAX(transition_id) AS transition_id
                FROM postmarket_candidate_lifecycle GROUP BY candidate_id
            ),
            latest_context AS (
                SELECT candidate_id,MAX(context_id) AS context_id
                FROM postmarket_candidate_context GROUP BY candidate_id
            ),
            latest_rank AS (
                SELECT candidate_id,MAX(rank_id) AS rank_id
                FROM postmarket_candidate_ranks GROUP BY candidate_id
            )
            SELECT
                c.candidate_id,c.session,c.symbol,c.direction,c.first_detected_at,
                c.bar_open_ts_utc,c.rth_close,c.close,c.move_pct,
                c.cumulative_volume,c.cumulative_notional,c.sources_json,
                c.data_feed,c.market_data_provider,c.bar_timeframe,
                tr.state AS lifecycle_state,tr.actionability,
                ctx.status AS context_status,ctx.catalyst_status,
                ctx.catalyst_category,ctx.spread_bps,
                r.rankable,r.ordinal_rank,r.evidence_score,r.evidence_coverage_pct
            FROM postmarket_discovery_candidates c
            LEFT JOIN latest_transition lt ON lt.candidate_id=c.candidate_id
            LEFT JOIN postmarket_candidate_lifecycle tr
              ON tr.transition_id=lt.transition_id
            LEFT JOIN latest_context lc ON lc.candidate_id=c.candidate_id
            LEFT JOIN postmarket_candidate_context ctx ON ctx.context_id=lc.context_id
            LEFT JOIN latest_rank lr ON lr.candidate_id=c.candidate_id
            LEFT JOIN postmarket_candidate_ranks r ON r.rank_id=lr.rank_id
            WHERE c.session=?
            ORDER BY c.first_detected_at,c.candidate_id
            """,
            (session.isoformat(),),
        ).fetchall()
    finally:
        shadow_conn.row_factory = original_factory
    return tuple(
        OperatorOpportunity(
            candidate_id=int(row["candidate_id"]),
            session=str(row["session"]),
            symbol=str(row["symbol"]),
            direction=str(row["direction"]),
            first_detected_at=_aware(row["first_detected_at"], "first_detected_at"),
            bar_open_ts_utc=_aware(row["bar_open_ts_utc"], "bar_open_ts_utc"),
            rth_close=float(row["rth_close"]),
            close=float(row["close"]),
            move_pct=float(row["move_pct"]),
            cumulative_volume=int(row["cumulative_volume"]),
            cumulative_notional=float(row["cumulative_notional"]),
            sources_json=str(row["sources_json"]),
            data_feed=str(row["data_feed"]),
            market_data_provider=str(row["market_data_provider"]),
            bar_timeframe=str(row["bar_timeframe"]),
            lifecycle_state=row["lifecycle_state"],
            actionability=row["actionability"],
            context_status=row["context_status"],
            catalyst_status=row["catalyst_status"],
            catalyst_category=row["catalyst_category"],
            spread_bps=(float(row["spread_bps"]) if row["spread_bps"] is not None else None),
            rankable=(bool(row["rankable"]) if row["rankable"] is not None else None),
            ordinal_rank=(int(row["ordinal_rank"]) if row["ordinal_rank"] is not None else None),
            evidence_score=(
                float(row["evidence_score"]) if row["evidence_score"] is not None else None
            ),
            evidence_coverage_pct=(
                float(row["evidence_coverage_pct"])
                if row["evidence_coverage_pct"] is not None else None
            ),
        )
        for row in rows
    )


def _candidate_is_sound(candidate: OperatorOpportunity, now: datetime) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(timezone.utc)
    numeric = (
        candidate.rth_close,
        candidate.close,
        candidate.move_pct,
        candidate.cumulative_notional,
    )
    return (
        all(math.isfinite(value) for value in numeric)
        and candidate.rth_close > 0
        and candidate.close > 0
        and candidate.cumulative_volume > 0
        and candidate.cumulative_notional > 0
        and candidate.first_detected_at <= current
        and candidate.bar_open_ts_utc <= current
        and candidate.direction in {"up", "down"}
        and candidate.data_feed != ""
        and candidate.market_data_provider != ""
        and candidate.bar_timeframe != ""
    )


def candidate_age_seconds(candidate: OperatorOpportunity, now: datetime) -> float:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return (now.astimezone(timezone.utc) - candidate.first_detected_at).total_seconds()


def _money(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def render_operator_opportunity(candidate: OperatorOpportunity, *, now: datetime) -> str:
    age = candidate_age_seconds(candidate, now)
    signed_move = f"{candidate.move_pct:+.2f}%"
    first_et = candidate.first_detected_at.astimezone(ET).strftime("%H:%M:%S ET")
    flags: list[str] = []
    if candidate.close < 5:
        flags.append("LOW_PRICE")
    if abs(candidate.move_pct) >= 25:
        flags.append("EXTREME_MOVE")
    if candidate.spread_bps is not None and candidate.spread_bps > 300:
        flags.append("WIDE_SPREAD")
    if candidate.context_status is None:
        flags.append("CONTEXT_PENDING")
    elif candidate.context_status != "complete":
        flags.append("CONTEXT_DEGRADED")
    lifecycle = candidate.lifecycle_state or "PENDING"
    rank = (
        f"#{candidate.ordinal_rank} · score {candidate.evidence_score:.1f} "
        f"· coverage {candidate.evidence_coverage_pct:.0f}%"
        if candidate.rankable and candidate.ordinal_rank is not None
        and candidate.evidence_score is not None
        and candidate.evidence_coverage_pct is not None
        else "pending or not rankable"
    )
    catalyst = candidate.catalyst_status or "PENDING"
    if candidate.catalyst_category:
        catalyst = f"{catalyst} · {candidate.catalyst_category}"
    lines = [
        "POSTMARKET SHADOW · NEW CANDIDATE",
        "",
        f"{candidate.symbol} · {signed_move} from RTH close",
        f"Price: ${candidate.close:.4f} · RTH close: ${candidate.rth_close:.4f}",
        f"Postmarket notional: {_money(candidate.cumulative_notional)}",
        f"Cumulative volume: {candidate.cumulative_volume:,}",
        f"First detected: {first_et} · age {age:.0f}s",
        f"Lifecycle: {lifecycle}",
        f"Rank: {rank}",
        f"Catalyst: {catalyst}",
        f"Data: {candidate.market_data_provider}/{candidate.data_feed} "
        f"· {candidate.bar_timeframe} completed bars",
        f"Risk flags: {', '.join(flags) if flags else 'none'}",
        "",
        "Owner-only shadow intelligence — not advice. No order was placed.",
    ]
    return "\n".join(lines)


def run_operator_cycle(
    shadow_conn: sqlite3.Connection,
    users_conn: sqlite3.Connection,
    *,
    session: date,
    chat_id: int,
    now: datetime,
    max_candidate_age_seconds: int = MAX_CANDIDATE_AGE_SECONDS,
    limit: int = MAX_ALERTS_PER_CYCLE,
) -> OperatorCycleResult:
    if max_candidate_age_seconds <= 0 or limit <= 0:
        raise ValueError("age and limit must be positive")
    validate_operator_chat(users_conn, chat_id)
    candidates = load_session_opportunities(shadow_conn, session=session)
    eligible: list[OperatorOpportunity] = []
    pending: list[OperatorOpportunity] = []
    stale = 0
    deduplicated = 0
    for candidate in candidates:
        if not _candidate_is_sound(candidate, now):
            continue
        age = candidate_age_seconds(candidate, now)
        if age > max_candidate_age_seconds:
            stale += 1
            continue
        eligible.append(candidate)
        existing = users_conn.execute(
            "SELECT 1 FROM outbox WHERE alert_id=? AND chat_id=?",
            (operator_alert_id(candidate.candidate_id), chat_id),
        ).fetchone()
        if existing is not None:
            deduplicated += 1
            continue
        pending.append(candidate)

    enqueued = 0
    for candidate in pending[:limit]:
        inserted = outbox.enqueue_broadcast(
            users_conn,
            operator_alert_id(candidate.candidate_id),
            [(chat_id, render_operator_opportunity(candidate, now=now), None)],
            outbox.PRIORITY_HIGH,
            now=now,
        )
        enqueued += inserted
        deduplicated += int(inserted == 0)
    return OperatorCycleResult(
        session=session.isoformat(),
        candidates_seen=len(candidates),
        eligible_candidates=len(eligible),
        alerts_enqueued=enqueued,
        alerts_deduplicated=deduplicated,
        stale_candidates=stale,
    )
