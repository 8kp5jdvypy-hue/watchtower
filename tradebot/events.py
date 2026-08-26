"""Scheduled events as suppression or context — never an alert source.
This module never publishes anything, never races to be first with a
headline, and never fabricates a date it doesn't actually have. It answers
exactly one question for the rest of the pipeline: "is `symbol` inside a
known event window right now, and if so, how should routing, explanation,
and track-record cohorts treat it?"

Three severities, in ascending priority when windows overlap:
    context    — worth tagging (Similar Setups doesn't apply), not worth blocking
    downgrade  — a HIGH alert becomes MEDIUM; still worth a look, not full confidence
    suppress   — don't publish at all; the technical read isn't trustworthy here

Real sources only:
    - EDGAR filings: tradebot.vendors.sec_edgar (real fetch) + the EDGAR
      classification section below (pure, fixture-testable).
    - Earnings dates: tradebot.vendors.nasdaq_earnings (real fetch, an
      undocumented-but-public endpoint — the only free forward-looking
      earnings source found) + the earnings classification section below.
    - FOMC/CPI/NFP: no free structured API exists for these at all — see
      seed_macro_event()'s docstring for the manual path from the
      official calendars. Never guessed, never hardcoded from memory.
    - EIA petroleum status report: released on a real, deterministic
      weekly schedule — see eia_report_window(), a pure computation, not
      a fetch.

No live "breaking news" polling loop exists in this module and none should
be added here. Earnings dates provide full-tier context and stats
exclusion; macro windows can suppress genuinely broken tape. A separate
reaction observer may use these scheduled facts for candidate admission,
but the calendar itself is never a price signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SEVERITY_RANK = {"context": 1, "downgrade": 2, "suppress": 3}


@dataclass(frozen=True)
class EventWindow:
    id: int
    symbol: str | None  # None = market-wide (macro prints affect everyone, esp. index ETFs)
    kind: str  # "8-K" | "13D" | "13G" | "form4" | "earnings" | "fomc" | "cpi" | "nfp" | "eia"
    start_utc: datetime
    end_utc: datetime
    severity: str
    source: str  # "sec_edgar" | "nasdaq_earnings" | "eia_schedule" | "manual"
    detail: str | None
    event_date: date | None = None
    event_timing: str | None = None


_COLUMNS = "id, symbol, kind, start_utc, end_utc, severity, source, detail, event_date, event_timing"


def _row_to_window(row) -> EventWindow:
    id_, symbol, kind, start_utc, end_utc, severity, source, detail, event_date, event_timing = row
    return EventWindow(
        id=id_, symbol=symbol, kind=kind,
        start_utc=datetime.fromisoformat(start_utc), end_utc=datetime.fromisoformat(end_utc),
        severity=severity, source=source, detail=detail,
        event_date=date.fromisoformat(event_date) if event_date else None,
        event_timing=event_timing,
    )


def add_event_window(
    conn, *, symbol: str | None, kind: str, start_utc: datetime, end_utc: datetime,
    severity: str, source: str, detail: str | None = None,
    event_date: date | None = None, event_timing: str | None = None,
) -> int | None:
    """Idempotent — re-running the same ingestion (or the same manual
    seed) twice does not duplicate the row. Returns the new row id, or
    None if this exact window already existed."""
    if severity not in SEVERITY_RANK:
        raise ValueError(f"unknown severity: {severity!r} (expected one of {sorted(SEVERITY_RANK)})")
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO event_windows
            (symbol, kind, start_utc, end_utc, severity, source, detail,
             event_date, event_timing, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, kind, start_utc.isoformat(), end_utc.isoformat(), severity, source, detail,
         event_date.isoformat() if event_date else None, event_timing,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def overlapping_windows(conn, symbol: str, when: datetime) -> list[EventWindow]:
    """Every window covering `when`, for this symbol OR market-wide.

    `when` must be tz-aware UTC — the overlap check is a plain string
    comparison against ISO-8601 timestamps (all written as UTC via
    .isoformat() elsewhere in this module), which only sorts correctly
    when every timestamp being compared shares the same offset
    representation. A naive or non-UTC `when` won't raise here; it will
    silently compare wrong. Every caller in this codebase already has a
    tz-aware UTC datetime in hand (see runner.py's `result["ts"]`) — this
    isn't defensively re-validated, consistent with how guard.py's `now`
    parameter is trusted rather than re-checked."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM event_windows "
        "WHERE (symbol = ? OR symbol IS NULL) AND start_utc <= ? AND end_utc >= ?",
        (symbol, when.isoformat(), when.isoformat()),
    ).fetchall()
    return [_row_to_window(r) for r in rows]


def active_event_window(conn, symbol: str, when: datetime) -> EventWindow | None:
    """The single highest-severity window covering `when`, or None. This
    is what runner.py checks before publishing a HIGH alert."""
    windows = overlapping_windows(conn, symbol, when)
    if not windows:
        return None
    return max(windows, key=lambda w: SEVERITY_RANK[w.severity])


def is_news_driven(conn, symbol: str, when: datetime) -> bool:
    """Any overlap at all, regardless of severity — even a context-only
    window (e.g. a routine Form 4) means Similar Setups' continuation
    stats, built on clean technical moves, don't transfer here."""
    return len(overlapping_windows(conn, symbol, when)) > 0


def events_for_date(conn, session_date) -> list[EventWindow]:
    """Every window whose start falls on this calendar date (ET) —
    what the pre-open card and /events show."""
    day_start = datetime.combine(session_date, datetime.min.time(), tzinfo=ET).astimezone(timezone.utc)
    day_end = datetime.combine(session_date, datetime.max.time(), tzinfo=ET).astimezone(timezone.utc)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM event_windows WHERE start_utc <= ? AND start_utc >= ? ORDER BY start_utc",
        (day_end.isoformat(), day_start.isoformat()),
    ).fetchall()
    return [_row_to_window(r) for r in rows]


def has_earnings_before(conn, symbol: str, start_date, end_date) -> bool | None:
    """Whether `symbol` has an ingested earnings event starting anywhere
    in [start_date, end_date] (inclusive, ET calendar days) — used by
    costs.select_contract's earnings_check_fn to blackout an expiry that
    would hold through an earnings print.

    Returns None if no earnings data has been ingested at all (see
    refresh_earnings_events) — unknown never means blackout; see
    costs.py's earnings_check_fn docstring for why treating unknown as
    blackout would be worse than not checking. This replaces runner.py's
    old has_earnings_before(), which read an always-empty table in
    tradebot.telegram_bot.db; earnings data now lives here, ingested from
    a real vendor (tradebot.vendors.nasdaq_earnings)."""
    any_loaded = conn.execute("SELECT 1 FROM event_windows WHERE kind = 'earnings' LIMIT 1").fetchone() is not None
    if not any_loaded:
        return None
    day_start = datetime.combine(start_date, datetime.min.time(), tzinfo=ET).astimezone(timezone.utc)
    day_end = datetime.combine(end_date, datetime.max.time(), tzinfo=ET).astimezone(timezone.utc)
    row = conn.execute(
        "SELECT 1 FROM event_windows WHERE kind = 'earnings' AND symbol = ? "
        "AND start_utc <= ? AND start_utc >= ? LIMIT 1",
        (symbol, day_end.isoformat(), day_start.isoformat()),
    ).fetchone()
    return row is not None


def scheduled_after_hours_earnings_symbols(conn, report_date: date) -> list[str]:
    """Active-ledger symbols scheduled to report after this session.

    Reads structured provider facts, never parses card copy. DISTINCT is
    required because one earnings event intentionally creates two context
    windows (report session and reaction session).
    """
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT symbol
            FROM event_windows
            WHERE kind = 'earnings' AND source = 'nasdaq_earnings'
              AND event_date = ? AND event_timing = 'after-hours'
              AND symbol IS NOT NULL
            ORDER BY symbol
            """,
            (report_date.isoformat(),),
        ).fetchall()
    ]


# --------------------------------------------------------------------------
# EDGAR filing classification — the one place that decides what a given
# form type means for the alert pipeline. tradebot.vendors.sec_edgar only
# fetches; it deliberately has no opinion on severity (see its module
# docstring). Pure functions, no network I/O — every real fetch happens
# in the vendor adapter and gets handed in here as plain Filing objects,
# so this logic is fully testable against fixtures, not live SEC calls.
# --------------------------------------------------------------------------

# 13D (active stake, intent to influence) and 13G (passive stake) are
# graded the same here — both disclose a real, material crossing of >5%
# ownership, even though 13G filers are passive. Form 4 (insider
# transactions) is far too frequent/routine on an actively-traded name to
# blanket-suppress on — it's tagged as context (Similar Setups doesn't
# apply) without blacking out the alert entirely.
EDGAR_FORM_KIND = {"8-K": "8-K", "SC 13D": "13D", "SC 13G": "13G", "4": "form4"}
EDGAR_FORM_SEVERITY = {"8-K": "suppress", "SC 13D": "suppress", "SC 13G": "suppress", "4": "context"}


def classify_filing(filing) -> tuple:
    """(kind, severity) for a real tradebot.vendors.sec_edgar.Filing, or
    None if its form_type isn't one this project has an opinion on
    (fetch_all_filings only requests tracked types, so this should only
    ever be None if the vendor's form list and this map drift apart —
    fail closed by skipping, not by guessing a severity)."""
    if filing.form_type not in EDGAR_FORM_SEVERITY:
        return None
    return EDGAR_FORM_KIND[filing.form_type], EDGAR_FORM_SEVERITY[filing.form_type]


def session_window_for_date(filing_date: date, calendar) -> tuple:
    """The [open, close) UTC of the trading session a filing on
    filing_date should blackout. EDGAR gives a filing DATE, not a
    timestamp (see sec_edgar.py) — a full session is the honest window,
    not a guessed intraday slice. If filing_date itself isn't a trading
    session (a weekend/holiday filing — EDGAR accepts filings any day),
    advances to the next real session: that's the first time the market
    can actually react to it."""
    from tradebot.runner import session_bounds

    d = filing_date
    for _ in range(10):
        if calendar.is_session(d):
            return session_bounds(d, calendar=calendar)
        d += timedelta(days=1)
    raise ValueError(f"no trading session found within 10 days of {filing_date.isoformat()}")


def ingest_filings(conn, symbol: str, filings: list, calendar, min_filing_date: date | None = None) -> int:
    """Classifies and stores event windows for Filing objects already
    fetched for `symbol` (see sec_edgar.fetch_all_filings — this function
    takes no network action itself). min_filing_date skips anything
    older: EDGAR's feed returns the most recent N filings ever, which for
    a frequent Form-4 filer can span months, and re-classifying ancient
    filings on every refresh is wasted work (dedup means it's harmless,
    just pointless). Returns the number of NEW windows actually created —
    0 on a re-run over the same filings, since add_event_window is
    idempotent."""
    created = 0
    for filing in filings:
        if min_filing_date is not None and filing.filing_date < min_filing_date:
            continue
        classified = classify_filing(filing)
        if classified is None:
            continue
        kind, severity = classified
        start, end = session_window_for_date(filing.filing_date, calendar)
        detail = f"{filing.form_type} filed {filing.filing_date.isoformat()} (acc# {filing.accession_number})"
        if filing.items_desc:
            detail += f" — {filing.items_desc}"
        row_id = add_event_window(
            conn, symbol=symbol, kind=kind, start_utc=start, end_utc=end,
            severity=severity, source="sec_edgar", detail=detail,
        )
        if row_id is not None:
            created += 1
    return created


def refresh_edgar_events(conn, symbol: str, cik_map: dict, calendar, today: date, lookback_days: int = 14) -> int:
    """The orchestration entrypoint: fetch, classify, store — for one
    symbol. Meant to run once per symbol per session (see runner.py's
    daily refresh), never in a tight loop; this module's whole point is
    that news here is context for the NEXT alert, not something to poll
    for in real time (see the module docstring's rule against a
    breaking-news feed)."""
    from tradebot.vendors.sec_edgar import fetch_all_filings

    filings = fetch_all_filings(symbol, cik_map)
    return ingest_filings(conn, symbol, filings, calendar, min_filing_date=today - timedelta(days=lookback_days))


def _adjacent_session(d: date, calendar, direction: int) -> date:
    """The next (direction=+1) or previous (direction=-1) real trading
    session relative to d, NOT including d itself."""
    step = timedelta(days=1 if direction > 0 else -1)
    cursor = d + step
    for _ in range(10):
        if calendar.is_session(cursor):
            return cursor
        cursor += step
    raise ValueError(f"no trading session found near {d.isoformat()}")


# --------------------------------------------------------------------------
# Earnings classification — tradebot.vendors.nasdaq_earnings only fetches
# who's reporting and when (pre-market/after-hours/unspecified); this is
# the one place that turns that into windows. Two per event: the session
# immediately before the print and the session pricing in the number.
# Both are context. Owner decision 2026-08-17: earnings are signal and
# context, never suppression; full-tier alerts remain eligible while
# Similar Setups excludes the event-driven population.
# --------------------------------------------------------------------------


def classify_earnings_event(event, calendar) -> list:
    """[(session_date, severity), ...] — always exactly 2 entries."""
    if event.timing == "after-hours":
        reaction_session = _adjacent_session(event.report_date, calendar, +1)
        report_session = (
            event.report_date if calendar.is_session(event.report_date)
            else _adjacent_session(event.report_date, calendar, -1)
        )
    else:  # "pre-market" or "unspecified"
        reaction_session = (
            event.report_date if calendar.is_session(event.report_date)
            else _adjacent_session(event.report_date, calendar, +1)
        )
        report_session = _adjacent_session(reaction_session, calendar, -1)
    return [(reaction_session, "context"), (report_session, "context")]


def ingest_earnings(conn, events: list, calendar) -> int:
    """Classifies and stores event windows for EarningsEvent objects
    already fetched (see nasdaq_earnings.fetch_earnings_for_symbols —
    this function takes no network action itself). Returns the number of
    NEW windows created."""
    from tradebot.runner import session_bounds

    created = 0
    for event in events:
        for session_date, severity in classify_earnings_event(event, calendar):
            start, end = session_bounds(session_date, calendar=calendar)
            detail = f"{event.symbol} earnings ({event.timing}), reported {event.report_date.isoformat()}"
            row_id = add_event_window(
                conn, symbol=event.symbol, kind="earnings", start_utc=start, end_utc=end,
                severity=severity, source="nasdaq_earnings", detail=detail,
                event_date=event.report_date, event_timing=event.timing,
            )
            if row_id is not None:
                created += 1
    return created


def _record_event_ingestion_run(
    conn, *, provider: str, kind: str, report_date: date,
    attempted_at: datetime, status: str, universe_scope: str,
    requested_symbols: int, fetched_events: int | None,
    matched_events: int | None, windows_created: int | None,
    error: str | None, code_version: str | None, run_mode: str | None,
    run_id: str | None, completed_at: datetime | None = None,
) -> int:
    """Append one provider attempt without changing its meaning later."""
    completed_at = completed_at or datetime.now(timezone.utc)
    cur = conn.execute(
        """
        INSERT INTO event_ingestion_runs
            (provider, kind, report_date, attempted_at, completed_at, status,
             universe_scope, requested_symbols, fetched_events, matched_events,
             windows_created, error, code_version, run_mode, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider, kind, report_date.isoformat(), attempted_at.isoformat(),
            completed_at.isoformat(), status, universe_scope, requested_symbols,
            fetched_events, matched_events, windows_created, error,
            code_version, run_mode, run_id,
        ),
    )
    conn.commit()
    return cur.lastrowid


def earnings_ingestion_succeeded(
    conn, report_date: date, *, universe_scope: str = "market",
) -> bool:
    """Whether this exact date/scope already has an attributable success."""
    row = conn.execute(
        """
        SELECT 1 FROM event_ingestion_runs
        WHERE provider = 'nasdaq_earnings' AND kind = 'earnings'
          AND report_date = ? AND universe_scope = ? AND status = 'success'
        LIMIT 1
        """,
        (report_date.isoformat(), universe_scope),
    ).fetchone()
    return row is not None


def refresh_earnings_events(
    conn, symbols: set, report_date: date, calendar, *,
    universe_scope: str = "market", code_version: str | None = None,
    run_mode: str | None = None, run_id: str | None = None, fetch_fn=None,
) -> int:
    """Fetch, filter, classify, store, and permanently record the attempt.

    ``symbols`` is the complete eligible universe supplied by the caller,
    not a fixed watchlist. A successful zero-event response and a provider
    failure receive different ledger rows. Failures are recorded and then
    re-raised so the runner can log/page without blocking the session.
    """
    from tradebot.vendors.nasdaq_earnings import fetch_earnings_calendar

    attempted_at = datetime.now(timezone.utc)
    fetch_fn = fetch_fn or fetch_earnings_calendar
    requested_symbols = len(symbols)
    fetched_count = matched_count = created = None
    try:
        if not symbols:
            raise ValueError("eligible earnings universe is empty")
        fetched = fetch_fn(report_date)
        fetched_count = len(fetched)
        matched = [event for event in fetched if event.symbol in symbols]
        matched_count = len(matched)
        created = ingest_earnings(conn, matched, calendar)
    except Exception as exc:
        _record_event_ingestion_run(
            conn, provider="nasdaq_earnings", kind="earnings",
            report_date=report_date, attempted_at=attempted_at, status="failed",
            universe_scope=universe_scope, requested_symbols=requested_symbols,
            fetched_events=fetched_count, matched_events=matched_count,
            windows_created=created, error=f"{type(exc).__name__}: {exc}"[:1000],
            code_version=code_version, run_mode=run_mode, run_id=run_id,
        )
        raise

    _record_event_ingestion_run(
        conn, provider="nasdaq_earnings", kind="earnings",
        report_date=report_date, attempted_at=attempted_at, status="success",
        universe_scope=universe_scope, requested_symbols=requested_symbols,
        fetched_events=fetched_count, matched_events=matched_count,
        windows_created=created, error=None, code_version=code_version,
        run_mode=run_mode, run_id=run_id,
    )
    return created


# --------------------------------------------------------------------------
# EIA Weekly Petroleum Status Report — real, deterministic schedule
# (released Wednesdays 10:30 ET), not a fetch. Relevant to USO
# specifically, so this always seeds a market-wide=False, USO-scoped
# window rather than a market-wide one.
# --------------------------------------------------------------------------

EIA_RELEASE_TIME_ET = time(10, 30)
EIA_SUPPRESS_WINDOW_MINUTES = 15


def eia_report_window(week_of: date, calendar) -> tuple:
    """(release_datetime_utc, is_shifted_to_thursday) for the EIA Weekly
    Petroleum Status Report covering the week containing week_of.
    Released Wednesday 10:30 ET, EXCEPT the report shifts to Thursday
    when that week's Monday is a federal holiday (EIA staff need the
    extra day).

    Known imprecision: this uses NYSE's holiday calendar as a proxy for
    "federal holiday," since there's no free federal-holiday API either.
    NYSE observes most federal holidays but stays OPEN on Columbus Day
    and Veterans Day, which the federal government (and EIA) does
    observe — so a week containing one of those two as its Monday will
    compute Wednesday here when the real report actually shifts to
    Thursday. If you know a specific week hits that edge case, override
    it with a manual add_event_window call rather than trust this
    function for that week."""
    monday = week_of - timedelta(days=week_of.weekday())
    is_shifted = not calendar.is_session(monday)
    release_date = monday + timedelta(days=3 if is_shifted else 2)  # Thursday or Wednesday
    release_utc = datetime.combine(release_date, EIA_RELEASE_TIME_ET, tzinfo=ET).astimezone(timezone.utc)
    return release_utc, is_shifted


def seed_eia_event(conn, week_of: date, calendar, symbol: str = "USO") -> int | None:
    """Seeds the suppress window around one week's real, computed EIA
    release time. Safe to call every session — dedup means re-seeding
    the same week is free."""
    release_utc, is_shifted = eia_report_window(week_of, calendar)
    start = release_utc - timedelta(minutes=EIA_SUPPRESS_WINDOW_MINUTES)
    end = release_utc + timedelta(minutes=EIA_SUPPRESS_WINDOW_MINUTES)
    detail = f"EIA Weekly Petroleum Status Report {release_utc.astimezone(ET).strftime('%Y-%m-%d %H:%M')} ET"
    if is_shifted:
        detail += " (shifted from Wednesday — that week's Monday is a market holiday)"
    return add_event_window(conn, symbol=symbol, kind="eia", start_utc=start, end_utc=end, severity="suppress", source="eia_schedule", detail=detail)


# --------------------------------------------------------------------------
# FOMC / CPI / NFP — no free structured calendar API exists for any of
# these (the Fed and BLS publish HTML calendars, not JSON feeds). Rather
# than scrape fragile, ToS-uncertain HTML or hardcode specific dates from
# memory (this project's core rule: never fabricate), this is a manual
# seed function. Pull real dates from:
#   FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#   CPI/NFP: https://www.bls.gov/schedule/news_release/
# and call this once per date you've confirmed.
# --------------------------------------------------------------------------

MACRO_RELEASE_TIME_ET = {
    "fomc": time(14, 0),  # FOMC statement
    "cpi": time(8, 30),   # BLS CPI release
    "nfp": time(8, 30),   # BLS Employment Situation
}
MACRO_SUPPRESS_WINDOW_MINUTES = 15


def seed_macro_event(conn, kind: str, event_date: date, calendar) -> tuple:
    """Seeds BOTH a tight suppress window around the real, official
    release time and a wider same-session downgrade window, for a date
    YOU provide from the official calendar (see module docstring for
    URLs) — this function does not know or guess any dates itself; it
    only knows what time of day each kind is released. Market-wide
    (symbol=None): FOMC/CPI/NFP move the whole market, not one name.
    Returns (suppress_row_id, downgrade_row_id), each None if that exact
    window already existed."""
    from tradebot.runner import session_bounds

    if kind not in MACRO_RELEASE_TIME_ET:
        raise ValueError(f"unknown macro event kind: {kind!r} (expected one of {sorted(MACRO_RELEASE_TIME_ET)})")
    release_time = MACRO_RELEASE_TIME_ET[kind]
    release_utc = datetime.combine(event_date, release_time, tzinfo=ET).astimezone(timezone.utc)
    suppress_start = release_utc - timedelta(minutes=MACRO_SUPPRESS_WINDOW_MINUTES)
    suppress_end = release_utc + timedelta(minutes=MACRO_SUPPRESS_WINDOW_MINUTES)
    suppress_id = add_event_window(
        conn, symbol=None, kind=kind, start_utc=suppress_start, end_utc=suppress_end,
        severity="suppress", source="manual", detail=f"{kind.upper()} release {release_time.strftime('%H:%M')} ET",
    )

    session_start, session_end = session_bounds(event_date, calendar=calendar)
    downgrade_id = add_event_window(
        conn, symbol=None, kind=kind, start_utc=session_start, end_utc=session_end,
        severity="downgrade", source="manual", detail=f"{kind.upper()} release day — elevated market-wide volatility",
    )
    return suppress_id, downgrade_id
