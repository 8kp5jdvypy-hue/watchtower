"""Append-only context and tradability evidence for postmarket candidates.

The candidate detector answers whether a completed-bar reaction qualified.
This module answers different questions without changing that decision: how
large the move was relative to prior volatility and SPY, whether the current
quote was usable, what asset/catalyst facts were known, and which desired
features were unavailable.  It has no alert or trading dependency.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from tradebot.detectors import Bar, atr, bar_close_ts
from tradebot.marketdata import Quote, partition_intraday_bars


CONTEXT_VERSION = 1
MAX_CONTEXT_ATTEMPTS = 3
MAX_CONTEXT_BATCH = 100
MAX_QUOTE_DISTANCE_SECONDS = 180
BENCHMARK_SYMBOL = "SPY"
ET = ZoneInfo("America/New_York")


CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_candidate_context (
    context_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    context_version INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    candidate_detected_at TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    code_version TEXT,
    bar_data_feed TEXT NOT NULL,
    bar_data_provider TEXT NOT NULL,
    bar_timeframe TEXT NOT NULL,
    quote_data_provider TEXT NOT NULL,
    quote_data_feed TEXT NOT NULL,
    status TEXT NOT NULL,
    volatility_status TEXT NOT NULL,
    prior_daily_bars INTEGER NOT NULL,
    atr14 REAL,
    atr_pct_of_rth_close REAL,
    move_atr_units REAL,
    implied_expected_move_status TEXT NOT NULL,
    market_relative_status TEXT NOT NULL,
    benchmark_symbol TEXT NOT NULL,
    benchmark_move_pct REAL,
    market_relative_move_pct REAL,
    directional_market_excess_pct REAL,
    sector_relative_status TEXT NOT NULL,
    sector_symbol TEXT,
    sector_move_pct REAL,
    sector_relative_move_pct REAL,
    quote_status TEXT NOT NULL,
    quote_ts_utc TEXT,
    quote_distance_seconds REAL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    spread_bps REAL,
    quoted_depth_notional REAL,
    liquidity_status TEXT NOT NULL,
    rth_volume INTEGER,
    rth_dollar_volume REAL,
    postmarket_notional REAL NOT NULL,
    asset_status TEXT NOT NULL,
    asset_observed_at_utc TEXT,
    exchange TEXT,
    tradable INTEGER,
    options_enabled INTEGER,
    overnight_eligible INTEGER,
    float_status TEXT NOT NULL,
    market_cap_status TEXT NOT NULL,
    halt_status TEXT NOT NULL,
    catalyst_status TEXT NOT NULL,
    catalyst_category TEXT NOT NULL,
    catalyst_sources_json TEXT NOT NULL,
    catalyst_details_json TEXT NOT NULL,
    catalyst_coverage_json TEXT NOT NULL,
    bar_quality_status TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    UNIQUE(candidate_id,context_version,attempt),
    CHECK (status IN ('complete','degraded'))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_context_session
    ON postmarket_candidate_context(session,symbol,context_id);
CREATE INDEX IF NOT EXISTS idx_postmarket_candidate_context_candidate
    ON postmarket_candidate_context(candidate_id,context_version,attempt);

CREATE TRIGGER IF NOT EXISTS postmarket_candidate_context_no_update
BEFORE UPDATE ON postmarket_candidate_context BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_context is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_candidate_context_no_delete
BEFORE DELETE ON postmarket_candidate_context BEGIN
    SELECT RAISE(ABORT, 'postmarket_candidate_context is append-only');
END;
"""


@dataclass(frozen=True)
class CandidateFact:
    candidate_id: int
    session: date
    symbol: str
    direction: str
    detected_at: datetime
    bar_open_ts: datetime
    rth_close: float
    close: float
    move_pct: float
    postmarket_notional: float
    bar_data_feed: str
    bar_data_provider: str
    bar_timeframe: str


@dataclass(frozen=True)
class AssetFact:
    status: str
    observed_at_utc: str | None = None
    exchange: str | None = None
    tradable: bool | None = None
    options_enabled: bool | None = None
    overnight_eligible: bool | None = None


@dataclass(frozen=True)
class CatalystFact:
    category: str
    source: str
    detail: str | None
    observed_at: str | None


@dataclass(frozen=True)
class ContextEvidence:
    candidate_id: int
    session: str
    symbol: str
    direction: str
    candidate_detected_at: str
    observed_at_utc: str
    code_version: str | None
    bar_data_feed: str
    bar_data_provider: str
    bar_timeframe: str
    quote_data_provider: str
    quote_data_feed: str
    status: str
    volatility_status: str
    prior_daily_bars: int
    atr14: float | None
    atr_pct_of_rth_close: float | None
    move_atr_units: float | None
    implied_expected_move_status: str
    market_relative_status: str
    benchmark_symbol: str
    benchmark_move_pct: float | None
    market_relative_move_pct: float | None
    directional_market_excess_pct: float | None
    sector_relative_status: str
    sector_symbol: str | None
    sector_move_pct: float | None
    sector_relative_move_pct: float | None
    quote_status: str
    quote_ts_utc: str | None
    quote_distance_seconds: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    spread_bps: float | None
    quoted_depth_notional: float | None
    liquidity_status: str
    rth_volume: int | None
    rth_dollar_volume: float | None
    postmarket_notional: float
    asset_status: str
    asset_observed_at_utc: str | None
    exchange: str | None
    tradable: bool | None
    options_enabled: bool | None
    overnight_eligible: bool | None
    float_status: str
    market_cap_status: str
    halt_status: str
    catalyst_status: str
    catalyst_category: str
    catalyst_sources: tuple[str, ...]
    catalyst_details: tuple[dict, ...]
    catalyst_coverage: dict[str, str]
    bar_quality_status: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class ContextBackfillResult:
    candidates_planned: int
    contexts_written: int
    degraded_contexts: int
    fetch_errors: int
    latency_ms: int


def ensure_context_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CONTEXT_SCHEMA)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _typical_notional(bars: Sequence[Bar]) -> float:
    return sum(
        ((bar.open + bar.high + bar.low + bar.close) / 4) * bar.volume
        for bar in bars
    )


def _benchmark_move(
    bars: Sequence[Bar], *, session: date, knowable_at: datetime,
) -> tuple[str, float | None]:
    snapshot = partition_intraday_bars(bars)
    rth = [bar for bar in snapshot.rth if bar.ts.astimezone(ET).date() == session]
    postmarket = [
        bar
        for bar in snapshot.postmarket
        if bar.ts.astimezone(ET).date() == session and bar_close_ts(bar) <= knowable_at
    ]
    if not rth or not _finite_positive(rth[-1].close):
        return "NO_RTH_CLOSE", None
    if not postmarket or not _finite_positive(postmarket[-1].close):
        return "NO_COMPLETED_POSTMARKET_BAR", None
    return "AVAILABLE", (postmarket[-1].close / rth[-1].close - 1) * 100


def _quote_features(
    quote: Quote | None, *, detected_at: datetime,
) -> tuple[str, dict, tuple[str, ...]]:
    if quote is None:
        return "UNAVAILABLE", {}, ("QUOTE_UNAVAILABLE",)
    quote_ts = _aware_utc(quote.ts, "quote.ts")
    distance = abs((quote_ts - detected_at).total_seconds())
    common = {
        "quote_ts_utc": quote_ts.isoformat(),
        "quote_distance_seconds": distance,
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
    }
    if not _finite_positive(quote.bid) or not _finite_positive(quote.ask):
        return "INVALID", common, ("QUOTE_INVALID",)
    if quote.bid > quote.ask:
        return "CROSSED", common, ("QUOTE_CROSSED",)
    mid = (quote.bid + quote.ask) / 2
    spread_bps = (quote.ask - quote.bid) / mid * 10_000
    depth = None
    if quote.bid_size is not None and quote.ask_size is not None:
        if quote.bid_size >= 0 and quote.ask_size >= 0:
            depth = quote.bid * quote.bid_size + quote.ask * quote.ask_size
    common.update(spread_bps=spread_bps, quoted_depth_notional=depth)
    if distance > MAX_QUOTE_DISTANCE_SECONDS:
        return "TEMPORALLY_MISMATCHED", common, ("QUOTE_TEMPORALLY_MISMATCHED",)
    issues = () if depth is not None else ("QUOTE_DEPTH_UNAVAILABLE",)
    return "AVAILABLE", common, issues


def _volatility_features(
    daily_bars: Sequence[Bar], candidate: CandidateFact,
) -> tuple[str, int, float | None, float | None, float | None]:
    prior = [
        bar for bar in daily_bars
        if bar.ts.astimezone(ET).date() < candidate.session
    ]
    atr14 = atr(prior)
    if atr14 is None:
        return "INSUFFICIENT_HISTORY", len(prior), None, None, None
    if not _finite_positive(atr14) or not _finite_positive(candidate.rth_close):
        return "INVALID", len(prior), None, None, None
    absolute_move = abs(candidate.close - candidate.rth_close)
    return (
        "AVAILABLE",
        len(prior),
        atr14,
        atr14 / candidate.rth_close * 100,
        absolute_move / atr14,
    )


def build_context_evidence(
    candidate: CandidateFact,
    *,
    observed_at: datetime,
    code_version: str | None,
    daily_bars: Sequence[Bar],
    intraday_bars: Sequence[Bar],
    benchmark_bars: Sequence[Bar],
    quote: Quote | None,
    asset: AssetFact,
    catalysts: Sequence[CatalystFact],
    operational_errors: Sequence[str] = (),
) -> ContextEvidence:
    observed = _aware_utc(observed_at, "observed_at")
    detected = _aware_utc(candidate.detected_at, "candidate.detected_at")
    knowable_at = max(
        detected,
        _aware_utc(candidate.bar_open_ts, "candidate.bar_open_ts")
        + timedelta(minutes=5),
    )
    volatility = _volatility_features(daily_bars, candidate)
    benchmark_status, benchmark_move = _benchmark_move(
        benchmark_bars, session=candidate.session, knowable_at=knowable_at
    )
    market_relative = (
        candidate.move_pct - benchmark_move
        if benchmark_move is not None else None
    )
    directional_excess = (
        market_relative * (1 if candidate.direction == "up" else -1)
        if market_relative is not None else None
    )
    quote_status, quote_values, quote_issues = _quote_features(
        quote, detected_at=detected
    )
    snapshot = partition_intraday_bars(intraday_bars)
    session_rth = [
        bar for bar in snapshot.rth if bar.ts.astimezone(ET).date() == candidate.session
    ]
    if session_rth:
        rth_volume = sum(bar.volume for bar in session_rth)
        rth_dollar_volume = _typical_notional(session_rth)
        liquidity_status = "AVAILABLE"
    else:
        rth_volume = None
        rth_dollar_volume = None
        liquidity_status = "NO_RTH_BARS"

    if catalysts:
        precedence = {
            "SCHEDULED_EARNINGS": 5,
            "SEC_FILING": 4,
            "MACRO": 3,
            "OTHER_VERIFIED": 2,
        }
        category = max(catalysts, key=lambda item: precedence.get(item.category, 1)).category
        catalyst_status = "VERIFIED"
    else:
        category = "UNEXPLAINED"
        catalyst_status = "NO_VERIFIED_CATALYST"
    sources = tuple(sorted({item.source for item in catalysts}))
    details = tuple(
        {
            "category": item.category,
            "source": item.source,
            "detail": item.detail,
            "observed_at": item.observed_at,
        }
        for item in sorted(catalysts, key=lambda item: (item.category, item.source, item.detail or ""))
    )
    coverage = {
        "earnings": "CONFIGURED",
        "filings": "CONFIGURED",
        "guidance": "UNCONFIGURED",
        "news": "UNCONFIGURED",
        "regulatory": "UNCONFIGURED",
        "analyst": "UNCONFIGURED",
    }
    issues = list(operational_errors)
    issues.extend(quote_issues)
    if volatility[0] != "AVAILABLE":
        issues.append(f"VOLATILITY_{volatility[0]}")
    if benchmark_status != "AVAILABLE":
        issues.append(f"BENCHMARK_{benchmark_status}")
    if liquidity_status != "AVAILABLE":
        issues.append(f"LIQUIDITY_{liquidity_status}")
    if asset.status != "AVAILABLE":
        issues.append(f"ASSET_{asset.status}")
    if asset.tradable is False:
        issues.append("ASSET_NOT_TRADABLE")
    if catalyst_status != "VERIFIED":
        issues.append("CATALYST_UNEXPLAINED")
    issues.extend(
        (
            "SECTOR_RELATIVE_UNAVAILABLE",
            "IMPLIED_EXPECTED_MOVE_UNAVAILABLE",
            "FLOAT_UNAVAILABLE",
            "MARKET_CAP_UNAVAILABLE",
            "HALT_STATUS_UNAVAILABLE",
        )
    )
    unique_issues = tuple(dict.fromkeys(issues))
    status = "degraded" if operational_errors else "complete"
    return ContextEvidence(
        candidate_id=candidate.candidate_id,
        session=candidate.session.isoformat(),
        symbol=candidate.symbol,
        direction=candidate.direction,
        candidate_detected_at=detected.isoformat(),
        observed_at_utc=observed.isoformat(),
        code_version=code_version,
        bar_data_feed=candidate.bar_data_feed,
        bar_data_provider=candidate.bar_data_provider,
        bar_timeframe=candidate.bar_timeframe,
        quote_data_provider="alpaca",
        quote_data_feed="sip",
        status=status,
        volatility_status=volatility[0],
        prior_daily_bars=volatility[1],
        atr14=volatility[2],
        atr_pct_of_rth_close=volatility[3],
        move_atr_units=volatility[4],
        implied_expected_move_status="UNAVAILABLE_NO_OPTIONS_EXPECTED_MOVE_SOURCE",
        market_relative_status=benchmark_status,
        benchmark_symbol=BENCHMARK_SYMBOL,
        benchmark_move_pct=benchmark_move,
        market_relative_move_pct=market_relative,
        directional_market_excess_pct=directional_excess,
        sector_relative_status="UNAVAILABLE_NO_SECTOR_CLASSIFICATION_SOURCE",
        sector_symbol=None,
        sector_move_pct=None,
        sector_relative_move_pct=None,
        quote_status=quote_status,
        quote_ts_utc=quote_values.get("quote_ts_utc"),
        quote_distance_seconds=quote_values.get("quote_distance_seconds"),
        bid=quote_values.get("bid"),
        ask=quote_values.get("ask"),
        bid_size=quote_values.get("bid_size"),
        ask_size=quote_values.get("ask_size"),
        spread_bps=quote_values.get("spread_bps"),
        quoted_depth_notional=quote_values.get("quoted_depth_notional"),
        liquidity_status=liquidity_status,
        rth_volume=rth_volume,
        rth_dollar_volume=rth_dollar_volume,
        postmarket_notional=candidate.postmarket_notional,
        asset_status=asset.status,
        asset_observed_at_utc=asset.observed_at_utc,
        exchange=asset.exchange,
        tradable=asset.tradable,
        options_enabled=asset.options_enabled,
        overnight_eligible=asset.overnight_eligible,
        float_status="UNAVAILABLE_NO_SOURCE",
        market_cap_status="UNAVAILABLE_NO_SOURCE",
        halt_status="UNAVAILABLE_NO_POINT_IN_TIME_SOURCE",
        catalyst_status=catalyst_status,
        catalyst_category=category,
        catalyst_sources=sources,
        catalyst_details=details,
        catalyst_coverage=coverage,
        bar_quality_status="PASSED_CANDIDATE_COMPLETED_BAR_GATES",
        issues=unique_issues,
    )


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _candidate_rows(conn: sqlite3.Connection, limit: int) -> list[CandidateFact]:
    ensure_context_schema(conn)
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id,MAX(attempt) AS attempt
            FROM postmarket_candidate_context
            WHERE context_version=? GROUP BY candidate_id
        )
        SELECT c.candidate_id,c.session,c.symbol,c.direction,c.first_detected_at,
               c.bar_open_ts_utc,c.rth_close,c.close,c.move_pct,
               c.cumulative_notional,c.data_feed,c.market_data_provider,
               c.bar_timeframe,COALESCE(x.attempt,0),ctx.status
        FROM postmarket_discovery_candidates c
        LEFT JOIN latest x ON x.candidate_id=c.candidate_id
        LEFT JOIN postmarket_candidate_context ctx
          ON ctx.candidate_id=c.candidate_id AND ctx.context_version=?
         AND ctx.attempt=x.attempt
        WHERE x.attempt IS NULL OR (ctx.status='degraded' AND x.attempt<?)
        ORDER BY c.first_detected_at,c.candidate_id LIMIT ?
        """,
        (CONTEXT_VERSION, CONTEXT_VERSION, MAX_CONTEXT_ATTEMPTS, limit),
    ).fetchall()
    return [
        CandidateFact(
            candidate_id=int(row[0]),
            session=date.fromisoformat(row[1]),
            symbol=row[2],
            direction=row[3],
            detected_at=_aware_utc(datetime.fromisoformat(row[4]), "first_detected_at"),
            bar_open_ts=_aware_utc(datetime.fromisoformat(row[5]), "bar_open_ts_utc"),
            rth_close=float(row[6]),
            close=float(row[7]),
            move_pct=float(row[8]),
            postmarket_notional=float(row[9]),
            bar_data_feed=row[10],
            bar_data_provider=row[11],
            bar_timeframe=row[12],
        )
        for row in rows
    ]


def _next_attempt(conn: sqlite3.Connection, candidate_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COALESCE(MAX(attempt),0)+1 FROM postmarket_candidate_context
            WHERE candidate_id=? AND context_version=?
            """,
            (candidate_id, CONTEXT_VERSION),
        ).fetchone()[0]
    )


def asset_facts(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, AssetFact]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""
        SELECT symbol,exchange,tradable,options_enabled,overnight_eligible,
               is_active,last_seen_at
        FROM assets WHERE symbol IN ({placeholders})
        """,
        tuple(symbols),
    ).fetchall()
    return {
        row[0]: AssetFact(
            status="AVAILABLE" if row[5] else "INACTIVE",
            observed_at_utc=row[6],
            exchange=row[1],
            tradable=bool(row[2]),
            options_enabled=bool(row[3]),
            overnight_eligible=None if row[4] is None else bool(row[4]),
        )
        for row in rows
    }


def catalyst_facts(conn: sqlite3.Connection, candidate: CandidateFact) -> tuple[CatalystFact, ...]:
    rows = conn.execute(
        """
        SELECT kind,source,detail,created_at
        FROM event_windows
        WHERE (symbol=? OR symbol IS NULL)
          AND (
            event_date=? OR
            (start_utc<=? AND end_utc>=?)
          )
        ORDER BY kind,source,created_at
        """,
        (
            candidate.symbol,
            candidate.session.isoformat(),
            candidate.detected_at.isoformat(),
            candidate.detected_at.isoformat(),
        ),
    ).fetchall()
    facts = []
    for kind, source, detail, created_at in rows:
        if kind == "earnings":
            category = "SCHEDULED_EARNINGS"
        elif kind in {"8-K", "13D", "13G", "form4"}:
            category = "SEC_FILING"
        elif kind in {"fomc", "cpi", "nfp", "eia"}:
            category = "MACRO"
        else:
            category = "OTHER_VERIFIED"
        facts.append(CatalystFact(category, source, detail, created_at))
    return tuple(facts)


def record_context(
    conn: sqlite3.Connection, evidence: ContextEvidence,
) -> int:
    ensure_context_schema(conn)
    attempt = _next_attempt(conn, evidence.candidate_id)
    if attempt > MAX_CONTEXT_ATTEMPTS:
        raise ValueError("maximum context attempts reached")
    values = (
        evidence.candidate_id, CONTEXT_VERSION, attempt, evidence.session,
        evidence.symbol, evidence.direction, evidence.candidate_detected_at,
        evidence.observed_at_utc, evidence.code_version, evidence.bar_data_feed,
        evidence.bar_data_provider, evidence.bar_timeframe,
        evidence.quote_data_provider, evidence.quote_data_feed, evidence.status,
        evidence.volatility_status, evidence.prior_daily_bars, evidence.atr14,
        evidence.atr_pct_of_rth_close, evidence.move_atr_units,
        evidence.implied_expected_move_status, evidence.market_relative_status,
        evidence.benchmark_symbol, evidence.benchmark_move_pct,
        evidence.market_relative_move_pct, evidence.directional_market_excess_pct,
        evidence.sector_relative_status, evidence.sector_symbol,
        evidence.sector_move_pct, evidence.sector_relative_move_pct,
        evidence.quote_status, evidence.quote_ts_utc,
        evidence.quote_distance_seconds, evidence.bid, evidence.ask,
        evidence.bid_size, evidence.ask_size, evidence.spread_bps,
        evidence.quoted_depth_notional, evidence.liquidity_status,
        evidence.rth_volume, evidence.rth_dollar_volume,
        evidence.postmarket_notional, evidence.asset_status,
        evidence.asset_observed_at_utc, evidence.exchange,
        None if evidence.tradable is None else int(evidence.tradable),
        None if evidence.options_enabled is None else int(evidence.options_enabled),
        None if evidence.overnight_eligible is None else int(evidence.overnight_eligible),
        evidence.float_status, evidence.market_cap_status, evidence.halt_status,
        evidence.catalyst_status, evidence.catalyst_category,
        _json(evidence.catalyst_sources), _json(evidence.catalyst_details),
        _json(evidence.catalyst_coverage), evidence.bar_quality_status,
        _json(evidence.issues),
    )
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_candidate_context
                (candidate_id,context_version,attempt,session,symbol,direction,
                 candidate_detected_at,observed_at_utc,code_version,bar_data_feed,
                 bar_data_provider,bar_timeframe,quote_data_provider,
                 quote_data_feed,status,
                 volatility_status,prior_daily_bars,atr14,atr_pct_of_rth_close,
                 move_atr_units,implied_expected_move_status,
                 market_relative_status,benchmark_symbol,benchmark_move_pct,
                 market_relative_move_pct,directional_market_excess_pct,
                 sector_relative_status,sector_symbol,sector_move_pct,
                 sector_relative_move_pct,quote_status,quote_ts_utc,
                 quote_distance_seconds,bid,ask,bid_size,ask_size,spread_bps,
                 quoted_depth_notional,liquidity_status,rth_volume,
                 rth_dollar_volume,postmarket_notional,asset_status,
                 asset_observed_at_utc,exchange,
                 tradable,options_enabled,overnight_eligible,float_status,
                 market_cap_status,halt_status,catalyst_status,catalyst_category,
                 catalyst_sources_json,catalyst_details_json,
                 catalyst_coverage_json,bar_quality_status,issues_json)
            VALUES ({})
            """.format(",".join("?" for _ in values)),
            values,
        )
    return int(cursor.lastrowid)


def latest_context_summary(conn: sqlite3.Connection) -> dict | None:
    """Coverage and availability for the newest candidate session."""
    ensure_context_schema(conn)
    session_row = conn.execute(
        "SELECT MAX(session) FROM postmarket_discovery_candidates"
    ).fetchone()
    if not session_row or session_row[0] is None:
        return None
    session = session_row[0]
    row = conn.execute(
        """
        WITH session_candidates AS (
            SELECT candidate_id FROM postmarket_discovery_candidates WHERE session=?
        ), latest AS (
            SELECT ctx.candidate_id,MAX(ctx.attempt) AS attempt
            FROM postmarket_candidate_context ctx
            JOIN session_candidates c ON c.candidate_id=ctx.candidate_id
            WHERE ctx.context_version=? GROUP BY ctx.candidate_id
        ), current AS (
            SELECT ctx.* FROM postmarket_candidate_context ctx
            JOIN latest l ON l.candidate_id=ctx.candidate_id AND l.attempt=ctx.attempt
            WHERE ctx.context_version=?
        )
        SELECT
            (SELECT COUNT(*) FROM session_candidates),
            COUNT(*),
            SUM(status='complete'),
            SUM(status='degraded'),
            SUM(volatility_status='AVAILABLE'),
            SUM(market_relative_status='AVAILABLE'),
            SUM(quote_status='AVAILABLE'),
            SUM(liquidity_status='AVAILABLE'),
            SUM(catalyst_status='VERIFIED')
        FROM current
        """,
        (session, CONTEXT_VERSION, CONTEXT_VERSION),
    ).fetchone()
    contexts = int(row[1] or 0)
    candidates = int(row[0] or 0)
    return {
        "session": session,
        "candidates": candidates,
        "contexts": contexts,
        "missing_contexts": candidates - contexts,
        "complete": int(row[2] or 0),
        "degraded": int(row[3] or 0),
        "volatility_available": int(row[4] or 0),
        "market_relative_available": int(row[5] or 0),
        "quote_available": int(row[6] or 0),
        "liquidity_available": int(row[7] or 0),
        "verified_catalysts": int(row[8] or 0),
    }


def run_context_backfill(
    shadow_conn: sqlite3.Connection,
    journal_conn: sqlite3.Connection,
    universe_conn: sqlite3.Connection,
    *,
    now: datetime,
    code_version: str | None,
    intraday_fetch: Callable[[list[str], date], Mapping[str, Sequence[Bar]]],
    daily_fetch: Callable[[list[str]], Mapping[str, Sequence[Bar]]],
    quote_fetch: Callable[[list[str]], Mapping[str, Quote]],
    limit: int = MAX_CONTEXT_BATCH,
) -> ContextBackfillResult:
    """Enrich a bounded set of candidates; provider failures stay explicit."""
    current = _aware_utc(now, "now")
    if limit <= 0:
        raise ValueError("limit must be positive")
    started = time.perf_counter()
    candidates = _candidate_rows(shadow_conn, limit)
    if not candidates:
        return ContextBackfillResult(0, 0, 0, 0, 0)
    symbols = sorted({candidate.symbol for candidate in candidates})
    assets = asset_facts(universe_conn, symbols)
    errors: list[str] = []
    try:
        daily = dict(daily_fetch(symbols))
    except Exception as exc:
        daily = {}
        errors.append(f"DAILY_FETCH_{type(exc).__name__}")
    try:
        quotes = dict(quote_fetch(symbols))
    except Exception as exc:
        quotes = {}
        errors.append(f"QUOTE_FETCH_{type(exc).__name__}")
    intraday_by_session: dict[date, dict[str, Sequence[Bar]]] = {}
    intraday_errors: dict[date, str] = {}
    for session in sorted({candidate.session for candidate in candidates}):
        session_symbols = sorted(
            {candidate.symbol for candidate in candidates if candidate.session == session}
            | {BENCHMARK_SYMBOL}
        )
        try:
            intraday_by_session[session] = dict(intraday_fetch(session_symbols, session))
        except Exception as exc:
            intraday_by_session[session] = {}
            intraday_errors[session] = (
                f"INTRADAY_FETCH_{session}_{type(exc).__name__}"
            )
    written = 0
    degraded = 0
    for candidate in candidates:
        session_bars = intraday_by_session[candidate.session]
        candidate_errors = list(errors)
        if candidate.session in intraday_errors:
            candidate_errors.append(intraday_errors[candidate.session])
        if candidate.symbol not in daily:
            candidate_errors.append("DAILY_BARS_UNAVAILABLE")
        if candidate.symbol not in session_bars:
            candidate_errors.append("INTRADAY_BARS_UNAVAILABLE")
        if BENCHMARK_SYMBOL not in session_bars:
            candidate_errors.append("BENCHMARK_BARS_UNAVAILABLE")
        if candidate.symbol not in quotes:
            candidate_errors.append("QUOTE_UNAVAILABLE")
        if candidate.symbol not in assets:
            candidate_errors.append("ASSET_UNAVAILABLE")
        evidence = build_context_evidence(
            candidate,
            observed_at=current,
            code_version=code_version,
            daily_bars=daily.get(candidate.symbol, ()),
            intraday_bars=session_bars.get(candidate.symbol, ()),
            benchmark_bars=session_bars.get(BENCHMARK_SYMBOL, ()),
            quote=quotes.get(candidate.symbol),
            asset=assets.get(candidate.symbol, AssetFact("UNAVAILABLE")),
            catalysts=catalyst_facts(journal_conn, candidate),
            operational_errors=candidate_errors,
        )
        record_context(shadow_conn, evidence)
        written += 1
        degraded += int(evidence.status == "degraded")
    return ContextBackfillResult(
        candidates_planned=len(candidates),
        contexts_written=written,
        degraded_contexts=degraded,
        fetch_errors=len(errors) + len(intraday_errors),
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
