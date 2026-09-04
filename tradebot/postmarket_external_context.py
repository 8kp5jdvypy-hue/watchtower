"""Point-in-time external facts for postmarket candidates.

Facts are append-only observations, never retroactive attributes.  Available
and unavailable states carry the provider, feed, endpoint, source/effective
time, observation time, and a canonical payload digest.  The module is
shadow-only and has no alert, order, or customer-delivery dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from tradebot.detectors import Bar
from tradebot.marketdata import NewsItem, OptionChain, Quote, XNYS
from tradebot.postmarket_context import CandidateFact
from tradebot.postmarket_reference_manifest import (
    CandidateReference,
    candidate_reference,
    ensure_reference_schema,
)
from tradebot.vendors.massive import TickerReference
from tradebot.vendors.nasdaq_halts import HaltRecord
from tradebot.vendors.openfigi import OpenFigiLookup
from tradebot.vendors.sec_companyfacts import PointInTimeSnapshot


EXTERNAL_CONTEXT_VERSION = 1
MAX_ATTEMPTS = 3
MAX_BATCH = 10
MAX_PROVIDER_CLOSE_DIFFERENCE_BPS = 50.0
FACT_KINDS = {
    "OPTIONS_EXPECTED_MOVE",
    "CURRENT_OPTION_MARKET_CONTEXT",
    "NEWS",
    "SECTOR_CLASSIFICATION",
    "FUNDAMENTALS",
    "FILING_INDUSTRY_CLASSIFICATION",
    "FILING_FUNDAMENTALS",
    "LICENSED_POINT_IN_TIME_REFERENCE",
    "HALT_STATE",
    "INDEPENDENT_PRICE_COMPARISON",
    "SECURITY_IDENTITY",
}
FACT_STATUSES = {
    "AVAILABLE",
    "AVAILABLE_DISAGREEMENT",
    "AVAILABLE_INDICATIVE",
    "AVAILABLE_NO_MATCHES",
    "UNAVAILABLE_UNCONFIGURED",
    "UNAVAILABLE_NO_DATA",
    "UNAVAILABLE_NOT_APPLICABLE",
    "FETCH_ERROR",
}


EXTERNAL_CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_pre_event_option_expectations (
    expectation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_context_version INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    session_close_utc TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    spot_reference REAL,
    spot_ts_utc TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    error_code TEXT,
    UNIQUE(session,symbol,external_context_version,attempt)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_pre_event_expectations_session
    ON postmarket_pre_event_option_expectations(session,symbol,attempt);
CREATE TABLE IF NOT EXISTS postmarket_external_fact_events (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_context_version INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    effective_at_utc TEXT,
    observed_at_utc TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    code_version TEXT,
    run_id TEXT NOT NULL,
    error_code TEXT,
    UNIQUE(candidate_id,external_context_version,fact_kind,attempt)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_external_fact_events_candidate
    ON postmarket_external_fact_events(candidate_id,fact_kind,attempt);
CREATE INDEX IF NOT EXISTS idx_postmarket_external_fact_events_session
    ON postmarket_external_fact_events(session,fact_kind,status);
CREATE TRIGGER IF NOT EXISTS postmarket_external_fact_events_no_update
BEFORE UPDATE ON postmarket_external_fact_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_external_fact_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_external_fact_events_no_delete
BEFORE DELETE ON postmarket_external_fact_events BEGIN
    SELECT RAISE(ABORT, 'postmarket_external_fact_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_pre_event_option_expectations_no_update
BEFORE UPDATE ON postmarket_pre_event_option_expectations BEGIN
    SELECT RAISE(ABORT, 'postmarket_pre_event_option_expectations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_pre_event_option_expectations_no_delete
BEFORE DELETE ON postmarket_pre_event_option_expectations BEGIN
    SELECT RAISE(ABORT, 'postmarket_pre_event_option_expectations is append-only');
END;
"""


@dataclass(frozen=True)
class ExternalFact:
    candidate_id: int
    session: str
    symbol: str
    fact_kind: str
    status: str
    effective_at_utc: str | None
    observed_at_utc: str
    provider: str
    feed: str
    endpoint: str
    payload: dict
    code_version: str | None
    run_id: str
    error_code: str | None = None


@dataclass(frozen=True)
class ExternalBackfillResult:
    candidates_planned: int
    facts_written: int
    available_facts: int
    fetch_errors: int
    latency_ms: int


@dataclass(frozen=True)
class PreEventExpectation:
    session: str
    symbol: str
    status: str
    captured_at_utc: str
    session_close_utc: str
    provider: str
    feed: str
    endpoint: str
    spot_reference: float | None
    spot_ts_utc: str | None
    payload: dict
    code_version: str | None
    run_id: str
    error_code: str | None = None


@dataclass(frozen=True)
class PreEventCaptureResult:
    scheduled_symbols: int
    symbols_planned: int
    expectations_written: int
    available_expectations: int
    fetch_errors: int
    latency_ms: int


def ensure_external_context_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(EXTERNAL_CONTEXT_SCHEMA)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


def record_external_fact(conn: sqlite3.Connection, fact: ExternalFact) -> int:
    ensure_external_context_schema(conn)
    if fact.fact_kind not in FACT_KINDS:
        raise ValueError(f"unsupported fact_kind: {fact.fact_kind}")
    if fact.status not in FACT_STATUSES:
        raise ValueError(f"unsupported external fact status: {fact.status}")
    if not fact.symbol or fact.symbol != fact.symbol.strip().upper():
        raise ValueError("symbol must be canonical uppercase")
    observed = _utc(datetime.fromisoformat(fact.observed_at_utc), "observed_at_utc")
    effective = None
    if fact.effective_at_utc is not None:
        effective = _utc(datetime.fromisoformat(fact.effective_at_utc), "effective_at_utc")
        if effective > observed:
            raise ValueError("effective_at_utc cannot be later than observed_at_utc")
    if not all(value.strip() for value in (fact.provider, fact.feed, fact.endpoint, fact.run_id)):
        raise ValueError("provider, feed, endpoint, and run_id must be non-empty")
    if fact.status == "FETCH_ERROR" and not fact.error_code:
        raise ValueError("FETCH_ERROR requires error_code")
    raw = _canonical(fact.payload)
    attempt = conn.execute(
        """SELECT COALESCE(MAX(attempt),0)+1 FROM postmarket_external_fact_events
           WHERE candidate_id=? AND external_context_version=? AND fact_kind=?""",
        (fact.candidate_id, EXTERNAL_CONTEXT_VERSION, fact.fact_kind),
    ).fetchone()[0]
    if attempt > MAX_ATTEMPTS:
        raise ValueError("maximum external fact attempts reached")
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_external_fact_events
                (external_context_version,candidate_id,session,symbol,fact_kind,
                 attempt,status,effective_at_utc,observed_at_utc,provider,feed,
                 endpoint,payload_json,payload_sha256,code_version,run_id,error_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                EXTERNAL_CONTEXT_VERSION, fact.candidate_id, fact.session, fact.symbol,
                fact.fact_kind, attempt, fact.status,
                effective.isoformat() if effective else None, observed.isoformat(),
                fact.provider, fact.feed, fact.endpoint, raw,
                hashlib.sha256(raw.encode()).hexdigest(), fact.code_version,
                fact.run_id, fact.error_code,
            ),
        )
    return int(cursor.lastrowid)


def unavailable_fact(
    candidate: CandidateFact,
    *,
    fact_kind: str,
    observed_at: datetime,
    provider: str,
    feed: str,
    endpoint: str,
    reason: str,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        fact_kind, "UNAVAILABLE_UNCONFIGURED", None,
        _utc(observed_at, "observed_at").isoformat(), provider, feed, endpoint,
        {"reason": reason}, code_version, run_id,
    )


def _option_snapshot(
    *, chain: OptionChain, spot: float, session: date, observed_at: datetime,
) -> tuple[str, datetime | None, dict]:
    """Return status/effective-time/payload without assigning temporal meaning."""
    observed = _utc(observed_at, "observed_at")
    pairs: dict[float, dict[str, object]] = {}
    for contract in chain.contracts:
        if contract.right not in {"call", "put"}:
            continue
        if not all(_finite_positive(value) for value in (contract.bid, contract.ask)):
            continue
        if contract.bid > contract.ask:
            continue
        pairs.setdefault(float(contract.strike), {})[contract.right] = contract
    valid = [(strike, pair) for strike, pair in pairs.items() if {"call", "put"} <= pair.keys()]
    if not valid or not _finite_positive(spot):
        return (
            "UNAVAILABLE_NO_DATA", None,
            {"expiry": chain.expiry.isoformat(), "reason": "NO_VALID_ATM_STRADDLE"},
        )
    strike, pair = min(valid, key=lambda item: (abs(item[0] - spot), item[0]))
    call = pair["call"]
    put = pair["put"]
    call_mid = (call.bid + call.ask) / 2
    put_mid = (put.bid + put.ask) / 2
    quote_times = [value.quote_ts for value in (call, put) if value.quote_ts is not None]
    effective = max(_utc(value, "option quote timestamp") for value in quote_times) if quote_times else None
    if effective is not None and effective > observed:
        raise ValueError("option quote timestamp is later than observation time")
    payload = {
        "expiry": chain.expiry.isoformat(),
        "days_to_expiry": (chain.expiry - session).days,
        "spot_reference": spot,
        "strike": strike,
        "call_symbol": call.symbol,
        "put_symbol": put.symbol,
        "call_mid": call_mid,
        "put_mid": put_mid,
        "straddle_mid": call_mid + put_mid,
        "straddle_pct_of_spot": (call_mid + put_mid) / spot * 100,
        "call_open_interest": call.open_interest,
        "put_open_interest": put.open_interest,
        "quote_time_available": effective is not None,
        "semantic": "indicative_atm_straddle_not_probability",
    }
    return "AVAILABLE_INDICATIVE", effective, payload


def current_option_context_fact(
    candidate: CandidateFact,
    *,
    chain: OptionChain,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    """Store a post-detection chain without calling it an expected baseline."""
    observed = _utc(observed_at, "observed_at")
    status, effective, payload = _option_snapshot(
        chain=chain, spot=candidate.close, session=candidate.session, observed_at=observed,
    )
    payload = {
        **payload,
        "spot_reference_kind": "candidate_completed_bar_close",
        "temporal_semantic": "post_detection_current_context_not_pre_event_expectation",
    }
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "CURRENT_OPTION_MARKET_CONTEXT", status,
        effective.isoformat() if effective else None, observed.isoformat(), "alpaca",
        "indicative", "option_chain_snapshot", payload, code_version, run_id,
    )


def record_pre_event_expectation(
    conn: sqlite3.Connection, expectation: PreEventExpectation,
) -> int:
    """Append a snapshot only when capture and quote facts precede the close."""
    ensure_external_context_schema(conn)
    captured = _utc(datetime.fromisoformat(expectation.captured_at_utc), "captured_at_utc")
    session_close = _utc(
        datetime.fromisoformat(expectation.session_close_utc), "session_close_utc"
    )
    if expectation.status not in FACT_STATUSES:
        raise ValueError(f"unsupported pre-event expectation status: {expectation.status}")
    if expectation.status.startswith("AVAILABLE") and captured >= session_close:
        raise ValueError("pre-event expectation must be captured before session close")
    if expectation.status == "FETCH_ERROR" and not expectation.error_code:
        raise ValueError("FETCH_ERROR requires error_code")
    if not all(value.strip() for value in (
        expectation.symbol, expectation.provider, expectation.feed,
        expectation.endpoint, expectation.run_id,
    )):
        raise ValueError("symbol, provider, feed, endpoint, and run_id must be non-empty")
    if expectation.symbol != expectation.symbol.strip().upper():
        raise ValueError("symbol must be canonical uppercase")
    spot_ts = None
    if expectation.spot_ts_utc is not None:
        spot_ts = _utc(datetime.fromisoformat(expectation.spot_ts_utc), "spot_ts_utc")
        if spot_ts > captured or spot_ts >= session_close:
            raise ValueError("pre-event spot timestamp must be knowable before close")
    raw = _canonical(expectation.payload)
    attempt = conn.execute(
        """SELECT COALESCE(MAX(attempt),0)+1
           FROM postmarket_pre_event_option_expectations
           WHERE session=? AND symbol=? AND external_context_version=?""",
        (expectation.session, expectation.symbol, EXTERNAL_CONTEXT_VERSION),
    ).fetchone()[0]
    if attempt > MAX_ATTEMPTS:
        raise ValueError("maximum pre-event expectation attempts reached")
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_pre_event_option_expectations
                (external_context_version,session,symbol,attempt,status,captured_at_utc,
                 session_close_utc,provider,feed,endpoint,spot_reference,spot_ts_utc,
                 payload_json,payload_sha256,code_version,run_id,error_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                EXTERNAL_CONTEXT_VERSION, expectation.session, expectation.symbol,
                attempt, expectation.status, captured.isoformat(), session_close.isoformat(),
                expectation.provider, expectation.feed, expectation.endpoint,
                expectation.spot_reference, spot_ts.isoformat() if spot_ts else None,
                raw, hashlib.sha256(raw.encode()).hexdigest(), expectation.code_version,
                expectation.run_id, expectation.error_code,
            ),
        )
    return int(cursor.lastrowid)


def build_pre_event_expectation(
    *,
    session: date,
    symbol: str,
    session_close: datetime,
    captured_at: datetime,
    spot: float,
    spot_ts: datetime,
    chain: OptionChain,
    code_version: str | None,
    run_id: str,
) -> PreEventExpectation:
    captured = _utc(captured_at, "captured_at")
    close = _utc(session_close, "session_close")
    if captured >= close:
        raise ValueError("pre-event expectation must be built before session close")
    status, effective, payload = _option_snapshot(
        chain=chain, spot=spot, session=session, observed_at=captured,
    )
    payload = {
        **payload,
        "spot_reference_kind": "pre_close_latest_quote_mid",
        "temporal_semantic": "pre_event_expected_move_baseline",
        "option_quote_ts_utc": effective.isoformat() if effective else None,
    }
    return PreEventExpectation(
        session.isoformat(), symbol, status, captured.isoformat(), close.isoformat(),
        "alpaca", "indicative", "option_chain_snapshot", spot,
        _utc(spot_ts, "spot_ts").isoformat(), payload, code_version, run_id,
    )


def run_pre_event_expectation_capture(
    conn: sqlite3.Connection,
    *,
    session: date,
    session_close: datetime,
    now: datetime,
    symbols: Sequence[str],
    code_version: str | None,
    run_id: str,
    quote_fetch: Callable[[list[str]], Mapping[str, Quote]],
    option_fetch: Callable[[str, date, float], OptionChain],
    completion_clock: Callable[[], datetime] | None = None,
    limit: int = MAX_BATCH,
) -> PreEventCaptureResult:
    """Capture scheduled-catalyst expectations before the official close."""
    ensure_external_context_schema(conn)
    current = _utc(now, "now")
    close = _utc(session_close, "session_close")
    if current >= close:
        raise ValueError("pre-event capture requires now before session_close")
    canonical = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
    pending = []
    for symbol in canonical:
        row = conn.execute(
            """SELECT status,attempt FROM postmarket_pre_event_option_expectations
               WHERE session=? AND symbol=? AND external_context_version=?
               ORDER BY attempt DESC LIMIT 1""",
            (session.isoformat(), symbol, EXTERNAL_CONTEXT_VERSION),
        ).fetchone()
        if row is None or (row[0] == "FETCH_ERROR" and int(row[1]) < MAX_ATTEMPTS):
            pending.append(symbol)
    pending = pending[:limit]
    if not pending:
        return PreEventCaptureResult(len(canonical), 0, 0, 0, 0, 0)
    started = clock.perf_counter()
    completion_clock = completion_clock or (lambda: datetime.now(timezone.utc))
    try:
        quotes = dict(quote_fetch(pending))
        quote_error = None
    except Exception as exc:
        quotes = {}
        quote_error = type(exc).__name__
    written = available = errors = 0
    for symbol in pending:
        try:
            quote = quotes.get(symbol)
            if quote_error:
                raise RuntimeError(f"QUOTE_FETCH_{quote_error}")
            if quote is None or not all(_finite_positive(value) for value in (quote.bid, quote.ask)):
                raise ValueError("QUOTE_UNAVAILABLE")
            quote_ts = _utc(quote.ts, "quote.ts")
            if quote_ts > current or quote_ts >= close:
                raise ValueError("QUOTE_NOT_KNOWABLE_PRE_CLOSE")
            spot = (quote.bid + quote.ask) / 2
            chain = option_fetch(symbol, session, spot)
            completed = _utc(completion_clock(), "completion_clock")
            expectation = build_pre_event_expectation(
                session=session, symbol=symbol, session_close=close, captured_at=completed,
                spot=spot, spot_ts=quote_ts,
                chain=chain,
                code_version=code_version, run_id=run_id,
            )
        except Exception as exc:
            errors += 1
            failed_at = _utc(completion_clock(), "completion_clock")
            expectation = PreEventExpectation(
                session.isoformat(), symbol, "FETCH_ERROR", failed_at.isoformat(),
                close.isoformat(), "alpaca", "indicative", "pre_event_option_capture",
                None, None, {}, code_version, run_id, type(exc).__name__,
            )
        record_pre_event_expectation(conn, expectation)
        written += 1
        available += int(expectation.status.startswith("AVAILABLE"))
    return PreEventCaptureResult(
        len(canonical), len(pending), written, available, errors,
        round((clock.perf_counter() - started) * 1000),
    )


def pre_event_expectation_fact(
    conn: sqlite3.Connection,
    candidate: CandidateFact,
    *,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    """Bind the latest successful pre-close snapshot to a later candidate."""
    ensure_external_context_schema(conn)
    row = conn.execute(
        """
        SELECT status,captured_at_utc,provider,feed,endpoint,payload_json
        FROM postmarket_pre_event_option_expectations
        WHERE session=? AND symbol=? AND external_context_version=?
          AND status LIKE 'AVAILABLE%'
        ORDER BY attempt DESC LIMIT 1
        """,
        (candidate.session.isoformat(), candidate.symbol, EXTERNAL_CONTEXT_VERSION),
    ).fetchone()
    observed = _utc(observed_at, "observed_at")
    if row is None:
        return ExternalFact(
            candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
            "OPTIONS_EXPECTED_MOVE", "UNAVAILABLE_NO_DATA", None,
            observed.isoformat(), "none", "none", "pre_event_snapshot",
            {"reason": "NO_PRE_EVENT_OPTION_SNAPSHOT"}, code_version, run_id,
        )
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "OPTIONS_EXPECTED_MOVE", row[0], row[1], observed.isoformat(), row[2],
        row[3], row[4], json.loads(row[5]), code_version, run_id,
    )


def news_fact(
    candidate: CandidateFact,
    *,
    items: Sequence[NewsItem],
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    observed = _utc(observed_at, "observed_at")
    included = []
    for item in items:
        created = _utc(item.created_at, "news.created_at")
        if created > observed or candidate.symbol not in item.symbols:
            continue
        included.append({
            "provider_id": item.provider_id,
            "headline": item.headline,
            "source": item.source,
            "url": item.url,
            "created_at_utc": created.isoformat(),
            "updated_at_utc": _utc(item.updated_at, "news.updated_at").isoformat(),
            "symbols": list(item.symbols),
        })
    included.sort(key=lambda row: (row["created_at_utc"], row["provider_id"]))
    latest = included[-1]["created_at_utc"] if included else None
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "NEWS", "AVAILABLE" if included else "AVAILABLE_NO_MATCHES", latest,
        observed.isoformat(), "alpaca", "benzinga", "historical_news",
        {"items": included, "match_count": len(included),
         "semantic": "provider_symbol_tag_match_not_causal_classification"},
        code_version, run_id,
    )


def independent_price_comparison_fact(
    candidate: CandidateFact,
    *,
    bars: Sequence[Bar],
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    """Reconcile the exact candidate and RTH-close bars across providers."""
    observed = _utc(observed_at, "observed_at")
    session_open = XNYS.session_open(candidate.session).to_pydatetime().astimezone(timezone.utc)
    session_close = XNYS.session_close(candidate.session).to_pydatetime().astimezone(timezone.utc)
    rth_bar_open = session_close - timedelta(minutes=5)
    expected = {rth_bar_open, _utc(candidate.bar_open_ts, "candidate.bar_open_ts")}
    by_ts: dict[datetime, Bar] = {}
    for bar in bars:
        ts = _utc(bar.ts, "independent bar timestamp")
        if bar.symbol != candidate.symbol:
            raise ValueError("independent bar symbol does not match candidate")
        if ts in by_ts:
            raise ValueError("independent bar timestamps are duplicated")
        by_ts[ts] = bar
    missing = sorted(ts.isoformat() for ts in expected if ts not in by_ts)
    if missing:
        return ExternalFact(
            candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
            "INDEPENDENT_PRICE_COMPARISON", "UNAVAILABLE_NO_DATA", None,
            observed.isoformat(), "massive", "stock_aggregate_trades",
            "v2_stock_custom_bars_5min",
            {
                "reason": "EXACT_COMPARISON_BAR_MISSING",
                "missing_bar_open_ts_utc": missing,
                "requested_start_utc": session_open.isoformat(),
                "requested_end_utc": candidate.bar_open_ts.isoformat(),
                "semantic": "missing_aggregate_is_not_filled_or_interpolated",
            },
            code_version, run_id,
        )
    independent_rth = by_ts[rth_bar_open]
    independent_candidate = by_ts[_utc(candidate.bar_open_ts, "candidate.bar_open_ts")]
    if min(independent_rth.close, independent_candidate.close) <= 0:
        raise ValueError("independent comparison closes must be positive")
    rth_difference_bps = (independent_rth.close / candidate.rth_close - 1) * 10_000
    candidate_difference_bps = (independent_candidate.close / candidate.close - 1) * 10_000
    independent_move_pct = (independent_candidate.close / independent_rth.close - 1) * 100
    direction = "up" if independent_move_pct > 0 else "down" if independent_move_pct < 0 else "flat"
    direction_agrees = direction == candidate.direction
    disagreement = (
        abs(rth_difference_bps) > MAX_PROVIDER_CLOSE_DIFFERENCE_BPS
        or abs(candidate_difference_bps) > MAX_PROVIDER_CLOSE_DIFFERENCE_BPS
        or not direction_agrees
    )
    effective = _utc(candidate.bar_open_ts, "candidate.bar_open_ts") + timedelta(minutes=5)
    if effective > observed:
        raise ValueError("independent candidate bar was not completed at observation time")
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "INDEPENDENT_PRICE_COMPARISON",
        "AVAILABLE_DISAGREEMENT" if disagreement else "AVAILABLE",
        effective.isoformat(), observed.isoformat(), "massive",
        "stock_aggregate_trades", "v2_stock_custom_bars_5min",
        {
            "primary_provider": candidate.bar_data_provider,
            "primary_feed": candidate.bar_data_feed,
            "independent_provider": "massive",
            "independent_feed": "stock_aggregate_trades",
            "bar_timeframe": candidate.bar_timeframe,
            "rth_close_bar_open_ts_utc": rth_bar_open.isoformat(),
            "candidate_bar_open_ts_utc": candidate.bar_open_ts.isoformat(),
            "primary_rth_close": candidate.rth_close,
            "independent_rth_close": independent_rth.close,
            "primary_candidate_close": candidate.close,
            "independent_candidate_close": independent_candidate.close,
            "primary_move_pct": candidate.move_pct,
            "independent_move_pct": independent_move_pct,
            "move_difference_percentage_points": independent_move_pct - candidate.move_pct,
            "rth_close_difference_bps": rth_difference_bps,
            "candidate_close_difference_bps": candidate_difference_bps,
            "direction_agrees": direction_agrees,
            "max_allowed_close_difference_bps": MAX_PROVIDER_CLOSE_DIFFERENCE_BPS,
            "comparison_rule": "exact_timestamp_unadjusted_completed_5min_bars_v1",
            "semantic": "post_detection_provider_reconciliation_not_signal_input",
        },
        code_version, run_id,
    )


def ticker_reference_facts(
    candidate: CandidateFact,
    *,
    reference: TickerReference,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> tuple[ExternalFact, ExternalFact]:
    """Split one dated reference response into explicit classification/fundamental facts.

    These are observed post-detection context.  They are deliberately marked
    as not replay-safe because a provider's historical reference date need not
    equal the date on which every underlying filing fact became public.
    """
    observed = _utc(observed_at, "observed_at")
    if reference.symbol != candidate.symbol or reference.as_of != candidate.session:
        raise ValueError("ticker reference identity does not match candidate")
    updated = reference.last_updated_utc
    if updated is not None:
        updated = _utc(updated, "reference.last_updated_utc")
        if updated > observed:
            raise ValueError("ticker reference timestamp is later than observation")
    common = {
        "as_of_date_requested": reference.as_of.isoformat(),
        "provider_last_updated_utc": updated.isoformat() if updated else None,
        "active": reference.active,
        "market": reference.market,
        "primary_exchange": reference.primary_exchange,
        "security_type": reference.security_type,
        "currency_name": reference.currency_name,
        "temporal_semantic": "post_detection_reference_context_not_replay_safe",
    }
    sector_available = bool(reference.sic_code or reference.sic_description)
    sector = ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "SECTOR_CLASSIFICATION", "AVAILABLE" if sector_available else "UNAVAILABLE_NO_DATA",
        updated.isoformat() if updated else None, observed.isoformat(), "massive",
        "ticker_reference", "v3_ticker_overview",
        {
            **common,
            "sic_code": reference.sic_code,
            "sic_description": reference.sic_description,
            "classification_system": "SEC_SIC",
            "semantic": "industry_classification_not_gics_sector_or_sector_etf_mapping",
        },
        code_version, run_id,
    )
    fundamentals_available = any(value is not None for value in (
        reference.market_cap,
        reference.share_class_shares_outstanding,
        reference.weighted_shares_outstanding,
    ))
    fundamentals = ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "FUNDAMENTALS", "AVAILABLE" if fundamentals_available else "UNAVAILABLE_NO_DATA",
        updated.isoformat() if updated else None, observed.isoformat(), "massive",
        "ticker_reference", "v3_ticker_overview",
        {
            **common,
            "market_cap": reference.market_cap,
            "share_class_shares_outstanding": reference.share_class_shares_outstanding,
            "weighted_shares_outstanding": reference.weighted_shares_outstanding,
            "float_status": "UNAVAILABLE_NOT_PROVIDED_BY_ENDPOINT",
            "semantic": "reference_snapshot_not_float_and_not_replay_rank_input",
        },
        code_version, run_id,
    )
    return sector, fundamentals


def filing_snapshot_facts(
    candidate: CandidateFact,
    *,
    snapshot: PointInTimeSnapshot,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> tuple[ExternalFact, ExternalFact]:
    """Convert accession-joined SEC evidence without retroactive leakage."""
    observed = _utc(observed_at, "observed_at")
    detected = _utc(candidate.detected_at, "candidate.detected_at")
    if snapshot.symbol != candidate.symbol or snapshot.candidate_cutoff_utc != detected:
        raise ValueError("SEC point-in-time snapshot identity did not match candidate")
    if snapshot.eligible_cutoff_utc > detected:
        raise ValueError("SEC eligible cutoff cannot follow candidate detection")
    if any(fact.accepted_at_utc > snapshot.eligible_cutoff_utc for fact in snapshot.facts):
        raise ValueError("SEC fact was accepted after the eligible cutoff")
    if (
        snapshot.classification_accepted_at_utc is not None
        and snapshot.classification_accepted_at_utc > snapshot.eligible_cutoff_utc
    ):
        raise ValueError("SEC classification filing was accepted after the eligible cutoff")
    common = {
        "cik": snapshot.cik,
        "candidate_cutoff_utc": detected.isoformat(),
        "eligible_cutoff_utc": snapshot.eligible_cutoff_utc.isoformat(),
        "dissemination_safety_lag_seconds": int(
            (detected - snapshot.eligible_cutoff_utc).total_seconds()
        ),
        "recent_submission_count": snapshot.recent_submission_count,
        "eligible_submission_count": snapshot.eligible_submission_count,
        "source_errors": list(snapshot.errors),
        "temporal_semantic": "accession_acceptance_bounded_with_conservative_api_lag",
    }
    classification_available = bool(snapshot.sic_code and snapshot.sic_description)
    classification = ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "FILING_INDUSTRY_CLASSIFICATION",
        "AVAILABLE" if classification_available else "UNAVAILABLE_NO_DATA",
        (
            snapshot.classification_accepted_at_utc.isoformat()
            if snapshot.classification_accepted_at_utc else None
        ),
        observed.isoformat(), "sec_edgar", "submissions_and_filing_header",
        "submissions_recent_plus_accession_index_headers",
        {
            **common,
            "accession_number": snapshot.classification_accession,
            "accepted_at_utc": (
                snapshot.classification_accepted_at_utc.isoformat()
                if snapshot.classification_accepted_at_utc else None
            ),
            "sic_code": snapshot.sic_code,
            "sic_description": snapshot.sic_description,
            "classification_system": "SEC_SIC",
            "semantic": "filing_time_industry_classification_not_gics_or_sector_etf",
        },
        code_version, run_id,
    )
    fact_rows = tuple({
        "name": fact.name,
        "taxonomy": fact.taxonomy,
        "concept": fact.concept,
        "unit": fact.unit,
        "value": fact.value,
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat(),
        "accession_number": fact.accession_number,
        "form": fact.form,
        "filed": fact.filed.isoformat(),
        "accepted_at_utc": fact.accepted_at_utc.isoformat(),
        "fiscal_year": fact.fiscal_year,
        "fiscal_period": fact.fiscal_period,
        "frame": fact.frame,
    } for fact in snapshot.facts)
    latest = max((fact.accepted_at_utc for fact in snapshot.facts), default=None)
    fundamentals = ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "FILING_FUNDAMENTALS", "AVAILABLE" if fact_rows else "UNAVAILABLE_NO_DATA",
        latest.isoformat() if latest else None, observed.isoformat(), "sec_edgar",
        "companyfacts_joined_to_submissions",
        "api_xbrl_companyfacts_plus_submissions_recent",
        {
            **common,
            "facts": list(fact_rows),
            "available_fact_names": sorted(row["name"] for row in fact_rows),
            "public_float_semantic": "dei_entity_public_float_is_usd_value_not_float_shares",
            "missing_concepts_are_not_zero": True,
            "semantic": "accepted_filing_facts_for_replay_context_not_yet_rank_input",
        },
        code_version, run_id,
    )
    return classification, fundamentals


def licensed_reference_fact(
    candidate: CandidateFact,
    *,
    reference: CandidateReference,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    """Bind a pre-detection licensed manifest row to one candidate."""
    observed = _utc(observed_at, "observed_at")
    detected = _utc(candidate.detected_at, "candidate.detected_at")
    published = _utc(datetime.fromisoformat(reference.published_at_utc), "published_at_utc")
    source_observed = _utc(
        datetime.fromisoformat(reference.source_observed_at_utc),
        "source_observed_at_utc",
    )
    if max(published, source_observed) > detected:
        raise ValueError("licensed reference was not observed before candidate detection")
    if date.fromisoformat(reference.effective_date) > candidate.session:
        raise ValueError("licensed reference effective date followed candidate session")
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "LICENSED_POINT_IN_TIME_REFERENCE", "AVAILABLE", published.isoformat(),
        observed.isoformat(), reference.provider, "licensed_reference_manifest",
        reference.dataset,
        {
            "reference_manifest_id": reference.reference_manifest_id,
            "manifest_sha256": reference.manifest_sha256,
            "license_reference": reference.license_reference,
            "effective_date": reference.effective_date,
            "published_at_utc": published.isoformat(),
            "source_observed_at_utc": source_observed.isoformat(),
            "classification_system": reference.classification_system,
            "sector_code": reference.sector_code,
            "sector_name": reference.sector_name,
            "benchmark_symbol": reference.benchmark_symbol,
            "float_shares": reference.float_shares,
            "float_as_of_date": reference.float_as_of_date,
            "float_status": (
                "AVAILABLE" if reference.float_shares is not None
                else "UNAVAILABLE_NOT_INCLUDED_IN_LICENSED_MANIFEST"
            ),
            "semantic": "operator_attested_licensed_point_in_time_reference_not_inferred",
        },
        code_version, run_id,
    )


def halt_state_fact(
    candidate: CandidateFact,
    *,
    records: Sequence[HaltRecord],
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    """Record official halt matches without inferring a halt from missing bars."""
    observed = _utc(observed_at, "observed_at")
    matches = []
    for record in records:
        if record.symbol != candidate.symbol:
            continue
        halted = _utc(record.halted_at, "halted_at")
        if halted > observed:
            raise ValueError("halt timestamp is later than observation")
        resume_quote = (
            _utc(record.resume_quote_at, "resume_quote_at")
            if record.resume_quote_at is not None else None
        )
        resume_trade = (
            _utc(record.resume_trade_at, "resume_trade_at")
            if record.resume_trade_at is not None else None
        )
        detected = _utc(candidate.detected_at, "candidate.detected_at")
        matches.append({
            "halted_at_utc": halted.isoformat(),
            "resume_quote_at_utc": resume_quote.isoformat() if resume_quote else None,
            "resume_trade_at_utc": resume_trade.isoformat() if resume_trade else None,
            "reason_code": record.reason_code,
            "market": record.market,
            "pause_threshold_price": record.pause_threshold_price,
            "halted_at_candidate_detection": (
                halted <= detected and (resume_trade is None or resume_trade > detected)
            ),
        })
    matches.sort(key=lambda row: (row["halted_at_utc"], row["reason_code"]))
    latest = matches[-1]["halted_at_utc"] if matches else None
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "HALT_STATE", "AVAILABLE" if matches else "AVAILABLE_NO_MATCHES",
        latest, observed.isoformat(), "nasdaq_trader", "trade_halt_rss",
        "rss_tradehalts_haltdate_and_resumedate",
        {
            "records": matches,
            "match_count": len(matches),
            "halted_at_candidate_detection": any(
                row["halted_at_candidate_detection"] for row in matches
            ),
            "semantic": "official_halt_record_observed_post_detection_not_missing_bar_inference",
            "coverage_limitation": (
                "date query covers halts begun or resumed on session; a halt begun earlier "
                "and not resumed on session requires separate carry-forward evidence"
            ),
        },
        code_version, run_id,
    )


def security_identity_fact(
    candidate: CandidateFact,
    *,
    lookup: OpenFigiLookup,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> ExternalFact:
    """Record current OpenFIGI mappings without guessing through ambiguity."""
    observed = _utc(observed_at, "observed_at")
    if lookup.symbol != candidate.symbol:
        raise ValueError("OpenFIGI lookup symbol did not match candidate")
    matches = [{
        "figi": match.figi,
        "name": match.name,
        "ticker": match.ticker,
        "exchange_code": match.exchange_code,
        "composite_figi": match.composite_figi,
        "share_class_figi": match.share_class_figi,
        "market_sector": match.market_sector,
        "security_type": match.security_type,
        "security_type2": match.security_type2,
        "security_description": match.security_description,
    } for match in lookup.matches]
    share_classes = {row["share_class_figi"] for row in matches if row["share_class_figi"]}
    composites = {row["composite_figi"] for row in matches if row["composite_figi"]}
    if not matches:
        resolution = "NO_MATCHES"
        resolved = {}
    elif len(matches) == 1:
        resolution = "RESOLVED_SINGLE_MATCH"
        resolved = {
            "figi": matches[0]["figi"],
            "composite_figi": matches[0]["composite_figi"],
            "share_class_figi": matches[0]["share_class_figi"],
        }
    elif len(share_classes) == 1 and all(row["share_class_figi"] for row in matches):
        resolution = "RESOLVED_SINGLE_SHARE_CLASS"
        resolved = {
            "figi": None,
            "composite_figi": next(iter(composites)) if len(composites) == 1 else None,
            "share_class_figi": next(iter(share_classes)),
        }
    else:
        resolution = "AMBIGUOUS_MULTIPLE_IDENTITIES"
        resolved = {}
    return ExternalFact(
        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
        "SECURITY_IDENTITY",
        "AVAILABLE" if matches else "AVAILABLE_NO_MATCHES",
        None, observed.isoformat(), "openfigi", "mapping", "v3_mapping",
        {
            "query": {
                "id_type": "TICKER",
                "id_value": candidate.symbol,
                "market_sector_description": "Equity",
                "exchange_code": "US",
            },
            "match_count": len(matches),
            "resolution": resolution,
            "resolved_identity": resolved,
            "matches": matches,
            "provider_warning": lookup.provider_warning,
            "temporal_semantic": (
                "current_identity_observed_after_candidate_detection_not_point_in_time"
            ),
            "usage_semantic": "identity_provenance_only_not_market_data_or_rank_input",
        },
        code_version, run_id,
    )


def latest_external_context_summary(conn: sqlite3.Connection) -> dict | None:
    ensure_external_context_schema(conn)
    session = conn.execute(
        "SELECT MAX(session) FROM postmarket_external_fact_events"
    ).fetchone()[0]
    expectation_session = conn.execute(
        "SELECT MAX(session) FROM postmarket_pre_event_option_expectations"
    ).fetchone()[0]
    if session is None and expectation_session is None:
        return None
    session = max(value for value in (session, expectation_session) if value is not None)
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT candidate_id,fact_kind,MAX(attempt) AS attempt
          FROM postmarket_external_fact_events WHERE session=?
          GROUP BY candidate_id,fact_kind
        )
        SELECT e.fact_kind,e.status,COUNT(*) FROM postmarket_external_fact_events e
        JOIN latest l ON l.candidate_id=e.candidate_id AND l.fact_kind=e.fact_kind
                     AND l.attempt=e.attempt
        WHERE e.session=? GROUP BY e.fact_kind,e.status ORDER BY e.fact_kind,e.status
        """, (session, session),
    ).fetchall()
    expectation_rows = conn.execute(
        """
        WITH latest AS (
          SELECT symbol,MAX(attempt) AS attempt
          FROM postmarket_pre_event_option_expectations WHERE session=? GROUP BY symbol
        )
        SELECT e.status,COUNT(*) FROM postmarket_pre_event_option_expectations e
        JOIN latest l ON l.symbol=e.symbol AND l.attempt=e.attempt
        WHERE e.session=? GROUP BY e.status ORDER BY e.status
        """, (session, session),
    ).fetchall()
    return {
        "session": session,
        "facts": {f"{kind}:{status}": count for kind, status, count in rows},
        "pre_event_expectations": {status: count for status, count in expectation_rows},
    }


def _pending_candidates(
    conn: sqlite3.Connection,
    limit: int,
    *,
    include_security_identity: bool = False,
) -> list[CandidateFact]:
    ensure_external_context_schema(conn)
    ensure_reference_schema(conn)
    rows = conn.execute(
        """
        SELECT c.candidate_id,c.session,c.symbol,c.direction,c.first_detected_at,
               c.bar_open_ts_utc,c.rth_close,c.close,c.move_pct,c.cumulative_notional,
               c.data_feed,c.market_data_provider,c.bar_timeframe
        FROM postmarket_discovery_candidates c
        WHERE EXISTS (
          SELECT 1 FROM (
            SELECT 'OPTIONS_EXPECTED_MOVE' AS kind
            UNION ALL SELECT 'CURRENT_OPTION_MARKET_CONTEXT'
            UNION ALL SELECT 'NEWS'
            UNION ALL SELECT 'SECTOR_CLASSIFICATION'
            UNION ALL SELECT 'FUNDAMENTALS'
            UNION ALL SELECT 'FILING_INDUSTRY_CLASSIFICATION'
            UNION ALL SELECT 'FILING_FUNDAMENTALS'
            UNION ALL SELECT 'HALT_STATE'
            UNION ALL SELECT 'INDEPENDENT_PRICE_COMPARISON'
          ) required
          WHERE NOT EXISTS (
            SELECT 1 FROM postmarket_external_fact_events e
            WHERE e.candidate_id=c.candidate_id
              AND e.external_context_version=? AND e.fact_kind=required.kind
              AND (e.status!='FETCH_ERROR' OR e.attempt>=?)
          )
        ) OR EXISTS (
          SELECT 1 FROM postmarket_reference_rows rr
          JOIN postmarket_reference_manifests rm
            ON rm.reference_manifest_id=rr.reference_manifest_id
          WHERE rr.symbol=c.symbol AND rm.effective_date<=c.session
            AND rm.published_at_utc<=c.first_detected_at
            AND rm.observed_at_utc<=c.first_detected_at
            AND NOT EXISTS (
              SELECT 1 FROM postmarket_external_fact_events e
              WHERE e.candidate_id=c.candidate_id
                AND e.external_context_version=?
                AND e.fact_kind='LICENSED_POINT_IN_TIME_REFERENCE'
                AND (e.status!='FETCH_ERROR' OR e.attempt>=?)
            )
        ) OR (
          ? AND NOT EXISTS (
            SELECT 1 FROM postmarket_external_fact_events e
            WHERE e.candidate_id=c.candidate_id
              AND e.external_context_version=?
              AND e.fact_kind='SECURITY_IDENTITY'
              AND (e.status!='FETCH_ERROR' OR e.attempt>=?)
          )
        )
        ORDER BY c.first_detected_at,c.candidate_id LIMIT ?
        """, (
            EXTERNAL_CONTEXT_VERSION, MAX_ATTEMPTS,
            EXTERNAL_CONTEXT_VERSION, MAX_ATTEMPTS,
            int(include_security_identity), EXTERNAL_CONTEXT_VERSION, MAX_ATTEMPTS,
            limit,
        ),
    ).fetchall()
    return [CandidateFact(
        int(row[0]), date.fromisoformat(row[1]), row[2], row[3],
        datetime.fromisoformat(row[4]), datetime.fromisoformat(row[5]),
        float(row[6]), float(row[7]), float(row[8]), float(row[9]),
        row[10], row[11], row[12],
    ) for row in rows]


def run_external_context_backfill(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    code_version: str | None,
    run_id: str,
    option_fetch: Callable[[str, date, float], OptionChain],
    news_fetch: Callable[[str, datetime, datetime], Sequence[NewsItem]],
    independent_fetch: Callable[[str, datetime, datetime], Sequence[Bar]] | None = None,
    reference_fetch: Callable[[str, date], TickerReference] | None = None,
    filing_context_fetch: Callable[[str, datetime], PointInTimeSnapshot] | None = None,
    halt_fetch: Callable[[date], Sequence[HaltRecord]] | None = None,
    identity_fetch: Callable[[str], OpenFigiLookup] | None = None,
    completion_clock: Callable[[], datetime] | None = None,
    limit: int = MAX_BATCH,
) -> ExternalBackfillResult:
    current = _utc(now, "now")
    if limit <= 0:
        raise ValueError("limit must be positive")
    started = clock.perf_counter()
    completion_clock = completion_clock or (lambda: datetime.now(timezone.utc))
    candidates = _pending_candidates(
        conn, limit, include_security_identity=identity_fetch is not None,
    )
    written = available = errors = 0
    halt_cache: dict[date, Sequence[HaltRecord] | Exception] = {}
    for candidate in candidates:
        completed = {
            row[0] for row in conn.execute(
                """SELECT fact_kind FROM postmarket_external_fact_events
                   WHERE candidate_id=? AND external_context_version=?
                     AND status!='FETCH_ERROR'""",
                (candidate.candidate_id, EXTERNAL_CONTEXT_VERSION),
            ).fetchall()
        }
        if "LICENSED_POINT_IN_TIME_REFERENCE" not in completed:
            reference = candidate_reference(
                conn, symbol=candidate.symbol, session=candidate.session,
                detected_at=candidate.detected_at,
            )
            if reference is not None:
                try:
                    fact = licensed_reference_fact(
                        candidate, reference=reference, observed_at=current,
                        code_version=code_version, run_id=run_id,
                    )
                except Exception as exc:
                    errors += 1
                    fact = ExternalFact(
                        candidate.candidate_id, candidate.session.isoformat(),
                        candidate.symbol, "LICENSED_POINT_IN_TIME_REFERENCE",
                        "FETCH_ERROR", None, current.isoformat(),
                        reference.provider, "licensed_reference_manifest",
                        reference.dataset, {}, code_version, run_id,
                        type(exc).__name__,
                    )
                record_external_fact(conn, fact)
                written += 1
                available += int(fact.status.startswith("AVAILABLE"))
        if "OPTIONS_EXPECTED_MOVE" not in completed:
            fact = pre_event_expectation_fact(
                conn, candidate, observed_at=current,
                code_version=code_version, run_id=run_id,
            )
            record_external_fact(conn, fact)
            written += 1
            available += int(fact.status.startswith("AVAILABLE"))
        if "CURRENT_OPTION_MARKET_CONTEXT" not in completed:
            try:
                fact = current_option_context_fact(
                    candidate, chain=option_fetch(candidate.symbol, candidate.session, candidate.close),
                    observed_at=current, code_version=code_version, run_id=run_id,
                )
            except Exception as exc:
                errors += 1
                fact = ExternalFact(
                    candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
                    "CURRENT_OPTION_MARKET_CONTEXT", "FETCH_ERROR", None, current.isoformat(),
                    "alpaca", "indicative", "option_chain_snapshot", {},
                    code_version, run_id, type(exc).__name__,
                )
            record_external_fact(conn, fact)
            written += 1
            available += int(fact.status.startswith("AVAILABLE"))
        if "NEWS" not in completed:
            start = candidate.detected_at - timedelta(hours=24)
            try:
                fact = news_fact(
                    candidate, items=news_fetch(candidate.symbol, start, current),
                    observed_at=current, code_version=code_version, run_id=run_id,
                )
            except Exception as exc:
                errors += 1
                fact = ExternalFact(
                    candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
                    "NEWS", "FETCH_ERROR", None, current.isoformat(), "alpaca",
                    "benzinga", "historical_news", {}, code_version, run_id,
                    type(exc).__name__,
                )
            record_external_fact(conn, fact)
            written += 1
            available += int(fact.status.startswith("AVAILABLE"))
        if any(kind not in completed for kind in ("SECTOR_CLASSIFICATION", "FUNDAMENTALS")):
            if reference_fetch is None:
                reference_facts = tuple(
                    unavailable_fact(
                        candidate, fact_kind=kind, observed_at=current, provider="none",
                        feed="none", endpoint="unconfigured",
                        reason="NO_TEMPORALLY_SAFE_REFERENCE_SOURCE_CONFIGURED",
                        code_version=code_version, run_id=run_id,
                    )
                    for kind in ("SECTOR_CLASSIFICATION", "FUNDAMENTALS")
                )
            else:
                try:
                    reference_facts = ticker_reference_facts(
                        candidate,
                        reference=reference_fetch(candidate.symbol, candidate.session),
                        observed_at=current, code_version=code_version, run_id=run_id,
                    )
                except Exception as exc:
                    errors += 1
                    reference_facts = tuple(
                        ExternalFact(
                            candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
                            kind, "FETCH_ERROR", None, current.isoformat(), "massive",
                            "ticker_reference", "v3_ticker_overview", {}, code_version,
                            run_id, type(exc).__name__,
                        )
                        for kind in ("SECTOR_CLASSIFICATION", "FUNDAMENTALS")
                    )
            for fact in reference_facts:
                if fact.fact_kind in completed:
                    continue
                record_external_fact(conn, fact)
                written += 1
                available += int(fact.status.startswith("AVAILABLE"))
        filing_kinds = (
            "FILING_INDUSTRY_CLASSIFICATION", "FILING_FUNDAMENTALS",
        )
        if any(kind not in completed for kind in filing_kinds):
            if filing_context_fetch is None:
                filing_facts = tuple(
                    unavailable_fact(
                        candidate, fact_kind=kind, observed_at=current,
                        provider="none", feed="none", endpoint="unconfigured",
                        reason="NO_ACCEPTANCE_BOUNDED_SEC_CONTEXT_CONFIGURED",
                        code_version=code_version, run_id=run_id,
                    )
                    for kind in filing_kinds
                )
            else:
                try:
                    snapshot = filing_context_fetch(
                        candidate.symbol,
                        _utc(candidate.detected_at, "candidate.detected_at"),
                    )
                    filing_observed = _utc(completion_clock(), "completion_clock")
                    filing_facts = filing_snapshot_facts(
                        candidate,
                        snapshot=snapshot, observed_at=filing_observed,
                        code_version=code_version, run_id=run_id,
                    )
                except Exception as exc:
                    errors += 1
                    failed_at = _utc(completion_clock(), "completion_clock")
                    filing_facts = tuple(
                        ExternalFact(
                            candidate.candidate_id, candidate.session.isoformat(),
                            candidate.symbol, kind, "FETCH_ERROR", None,
                            failed_at.isoformat(), "sec_edgar", "submissions_and_xbrl",
                            "point_in_time_filing_context", {}, code_version, run_id,
                            type(exc).__name__,
                        )
                        for kind in filing_kinds
                    )
            for fact in filing_facts:
                if fact.fact_kind in completed:
                    continue
                record_external_fact(conn, fact)
                written += 1
                available += int(fact.status.startswith("AVAILABLE"))
        if "INDEPENDENT_PRICE_COMPARISON" not in completed:
            if independent_fetch is None:
                fact = unavailable_fact(
                    candidate, fact_kind="INDEPENDENT_PRICE_COMPARISON",
                    observed_at=current, provider="none", feed="none",
                    endpoint="unconfigured",
                    reason="NO_SECOND_MARKET_DATA_PROVIDER_CONFIGURED",
                    code_version=code_version, run_id=run_id,
                )
            else:
                try:
                    session_open = XNYS.session_open(candidate.session).to_pydatetime().astimezone(timezone.utc)
                    fact = independent_price_comparison_fact(
                        candidate,
                        bars=independent_fetch(
                            candidate.symbol, session_open,
                            _utc(candidate.bar_open_ts, "candidate.bar_open_ts"),
                        ),
                        observed_at=current, code_version=code_version, run_id=run_id,
                    )
                except Exception as exc:
                    errors += 1
                    fact = ExternalFact(
                        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
                        "INDEPENDENT_PRICE_COMPARISON", "FETCH_ERROR", None,
                        current.isoformat(), "massive", "stock_aggregate_trades",
                        "v2_stock_custom_bars_5min", {}, code_version, run_id,
                        type(exc).__name__,
                    )
            record_external_fact(conn, fact)
            written += 1
            available += int(fact.status.startswith("AVAILABLE"))
        if "SECURITY_IDENTITY" not in completed and identity_fetch is not None:
            try:
                lookup = identity_fetch(candidate.symbol)
                identity_observed = _utc(completion_clock(), "completion_clock")
                fact = security_identity_fact(
                    candidate, lookup=lookup, observed_at=identity_observed,
                    code_version=code_version, run_id=run_id,
                )
            except Exception as exc:
                errors += 1
                failed_at = _utc(completion_clock(), "completion_clock")
                fact = ExternalFact(
                    candidate.candidate_id, candidate.session.isoformat(),
                    candidate.symbol, "SECURITY_IDENTITY", "FETCH_ERROR", None,
                    failed_at.isoformat(), "openfigi", "mapping", "v3_mapping",
                    {}, code_version, run_id, type(exc).__name__,
                )
            record_external_fact(conn, fact)
            written += 1
            available += int(fact.status.startswith("AVAILABLE"))
        if "HALT_STATE" not in completed:
            if halt_fetch is None:
                fact = unavailable_fact(
                    candidate, fact_kind="HALT_STATE", observed_at=current,
                    provider="none", feed="none", endpoint="unconfigured",
                    reason="NO_POINT_IN_TIME_HALT_SOURCE_CONFIGURED",
                    code_version=code_version, run_id=run_id,
                )
            else:
                if candidate.session not in halt_cache:
                    try:
                        halt_cache[candidate.session] = tuple(halt_fetch(candidate.session))
                    except Exception as exc:
                        halt_cache[candidate.session] = exc
                cached = halt_cache[candidate.session]
                if isinstance(cached, Exception):
                    errors += 1
                    fact = ExternalFact(
                        candidate.candidate_id, candidate.session.isoformat(), candidate.symbol,
                        "HALT_STATE", "FETCH_ERROR", None, current.isoformat(),
                        "nasdaq_trader", "trade_halt_rss",
                        "rss_tradehalts_haltdate_and_resumedate", {}, code_version,
                        run_id, type(cached).__name__,
                    )
                else:
                    fact = halt_state_fact(
                        candidate, records=cached, observed_at=current,
                        code_version=code_version, run_id=run_id,
                    )
            record_external_fact(conn, fact)
            written += 1
            available += int(fact.status.startswith("AVAILABLE"))
    latency = round((clock.perf_counter() - started) * 1000)
    return ExternalBackfillResult(len(candidates), written, available, errors, latency)
