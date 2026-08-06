"""Tests for tradebot.events — event-window storage, queries, and the
EDGAR/earnings/macro classification. No live network calls: ingestion is
tested against synthetic Filing/EarningsEvent objects, exactly what the
real vendor adapters hand back.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import exchange_calendars as ecals
import pytest

from tradebot.events import (
    EIA_RELEASE_TIME_ET,
    MACRO_RELEASE_TIME_ET,
    _adjacent_session,
    active_event_window,
    add_event_window,
    classify_earnings_event,
    classify_filing,
    eia_report_window,
    events_for_date,
    has_earnings_before,
    ingest_earnings,
    ingest_filings,
    is_news_driven,
    overlapping_windows,
    refresh_earnings_events,
    refresh_edgar_events,
    seed_eia_event,
    seed_macro_event,
    session_window_for_date,
)
from tradebot.journal import connect
from tradebot.vendors.nasdaq_earnings import EarningsEvent
from tradebot.vendors.sec_edgar import Filing

CALENDAR = ecals.get_calendar("XNYS")


def _conn():
    return connect(":memory:")


# ---------------------------------------------------------------------- #
# Storage: add_event_window, overlap queries, severity precedence
# ---------------------------------------------------------------------- #


def test_add_event_window_is_idempotent():
    conn = _conn()
    kwargs = dict(
        symbol="GOOGL", kind="8-K", start_utc=datetime(2026, 7, 22, 13, 30, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc), severity="suppress", source="sec_edgar",
    )
    first = add_event_window(conn, **kwargs)
    second = add_event_window(conn, **kwargs)
    assert first is not None
    assert second is None
    count = conn.execute("SELECT COUNT(*) FROM event_windows").fetchone()[0]
    assert count == 1


def test_add_event_window_rejects_an_unknown_severity():
    conn = _conn()
    with pytest.raises(ValueError):
        add_event_window(
            conn, symbol="GOOGL", kind="8-K", start_utc=datetime(2026, 7, 22, tzinfo=timezone.utc),
            end_utc=datetime(2026, 7, 22, tzinfo=timezone.utc), severity="nonsense", source="manual",
        )


def test_market_wide_window_dedupes_across_repeated_macro_ingestion():
    conn = _conn()
    kwargs = dict(
        symbol=None, kind="fomc", start_utc=datetime(2026, 7, 23, 17, 55, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 23, 18, 10, tzinfo=timezone.utc), severity="suppress", source="manual",
    )
    add_event_window(conn, **kwargs)
    add_event_window(conn, **kwargs)  # e.g. a second daily refresh re-seeding the same date
    count = conn.execute("SELECT COUNT(*) FROM event_windows").fetchone()[0]
    assert count == 1


def test_overlapping_windows_matches_symbol_specific_and_market_wide():
    conn = _conn()
    when = datetime(2026, 7, 23, 18, 0, tzinfo=timezone.utc)
    add_event_window(conn, symbol="GOOGL", kind="8-K", start_utc=when, end_utc=when, severity="suppress", source="sec_edgar")
    add_event_window(conn, symbol=None, kind="fomc", start_utc=when, end_utc=when, severity="suppress", source="manual")
    add_event_window(conn, symbol="TSLA", kind="8-K", start_utc=when, end_utc=when, severity="suppress", source="sec_edgar")

    googl_windows = overlapping_windows(conn, "GOOGL", when)
    assert {w.kind for w in googl_windows} == {"8-K", "fomc"}  # its own + market-wide, not TSLA's

    aapl_windows = overlapping_windows(conn, "AAPL", when)
    assert {w.kind for w in aapl_windows} == {"fomc"}  # only the market-wide one


def test_active_event_window_picks_the_highest_severity_when_several_overlap():
    conn = _conn()
    when = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    start, end = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc), datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
    add_event_window(conn, symbol="GOOGL", kind="form4", start_utc=start, end_utc=end, severity="context", source="sec_edgar")
    add_event_window(conn, symbol="GOOGL", kind="8-K", start_utc=start, end_utc=end, severity="suppress", source="sec_edgar")

    active = active_event_window(conn, "GOOGL", when)
    assert active.kind == "8-K" and active.severity == "suppress"


def test_active_event_window_none_outside_any_window():
    conn = _conn()
    add_event_window(
        conn, symbol="GOOGL", kind="8-K", start_utc=datetime(2026, 7, 22, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 22, 23, 59, tzinfo=timezone.utc), severity="suppress", source="sec_edgar",
    )
    assert active_event_window(conn, "GOOGL", datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)) is None


def test_is_news_driven_true_even_for_context_only_severity():
    conn = _conn()
    when = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    add_event_window(
        conn, symbol="GOOGL", kind="form4", start_utc=when, end_utc=when, severity="context", source="sec_edgar",
    )
    assert is_news_driven(conn, "GOOGL", when) is True
    assert is_news_driven(conn, "AAPL", when) is False


def test_events_for_date_filters_by_et_calendar_day():
    conn = _conn()
    add_event_window(
        conn, symbol="GOOGL", kind="8-K",
        start_utc=datetime(2026, 7, 22, 13, 30, tzinfo=timezone.utc), end_utc=datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc),
        severity="suppress", source="sec_edgar",
    )
    add_event_window(
        conn, symbol="TSLA", kind="8-K",
        start_utc=datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc), end_utc=datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc),
        severity="suppress", source="sec_edgar",
    )
    assert [w.symbol for w in events_for_date(conn, date(2026, 7, 22))] == ["GOOGL"]
    assert [w.symbol for w in events_for_date(conn, date(2026, 7, 23))] == ["TSLA"]


def test_has_earnings_before_returns_none_when_no_earnings_data_loaded_at_all():
    """Unknown never means blackout — see costs.select_contract's
    earnings_check_fn docstring. Even with other event kinds loaded, no
    'earnings' kind row at all means the day's ingest simply hasn't run."""
    conn = _conn()
    add_event_window(
        conn, symbol="TSLA", kind="8-K", start_utc=datetime(2026, 7, 22, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 22, 23, 0, tzinfo=timezone.utc), severity="suppress", source="sec_edgar",
    )
    assert has_earnings_before(conn, "TSLA", date(2026, 7, 22), date(2026, 7, 29)) is None


def test_has_earnings_before_true_when_earnings_falls_inside_the_expiry_window():
    conn = _conn()
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=datetime(2026, 7, 25, 20, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc), severity="suppress", source="nasdaq_earnings",
    )
    assert has_earnings_before(conn, "TSLA", date(2026, 7, 22), date(2026, 7, 29)) is True


def test_has_earnings_before_false_when_earnings_data_loaded_but_none_for_this_symbol_or_range():
    conn = _conn()
    # earnings data IS loaded (so this isn't the "nothing to check" case) —
    # just not for TSLA in the window being checked.
    add_event_window(
        conn, symbol="GOOGL", kind="earnings", start_utc=datetime(2026, 7, 25, 20, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc), severity="suppress", source="nasdaq_earnings",
    )
    assert has_earnings_before(conn, "TSLA", date(2026, 7, 22), date(2026, 7, 29)) is False


def test_has_earnings_before_excludes_a_report_date_outside_the_range():
    conn = _conn()
    add_event_window(
        conn, symbol="TSLA", kind="earnings", start_utc=datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc), severity="suppress", source="nasdaq_earnings",
    )
    # TSLA does have earnings loaded, just not before this (much earlier) expiry
    assert has_earnings_before(conn, "TSLA", date(2026, 7, 22), date(2026, 7, 29)) is False


# ---------------------------------------------------------------------- #
# EDGAR classification — pure, fixture-driven, no network
# ---------------------------------------------------------------------- #


def test_classify_filing_maps_every_tracked_form_type():
    assert classify_filing(Filing("8-K", date(2026, 7, 22), "a1", None)) == ("8-K", "suppress")
    assert classify_filing(Filing("SC 13D", date(2026, 7, 22), "a2", None)) == ("13D", "suppress")
    assert classify_filing(Filing("SC 13G", date(2026, 7, 22), "a3", None)) == ("13G", "suppress")
    assert classify_filing(Filing("4", date(2026, 7, 22), "a4", None)) == ("form4", "context")


def test_classify_filing_returns_none_for_an_untracked_form_type():
    assert classify_filing(Filing("10-K", date(2026, 7, 22), "a5", None)) is None


def test_session_window_for_date_on_a_regular_trading_day():
    start, end = session_window_for_date(date(2026, 7, 22), CALENDAR)  # a Wednesday
    assert start.date() == date(2026, 7, 22)
    assert start < end


def test_session_window_for_date_rolls_forward_from_a_weekend_filing():
    # EDGAR accepts filings any day; the market can't react until it's next open
    start, end = session_window_for_date(date(2026, 7, 25), CALENDAR)  # a Saturday
    assert start.date() == date(2026, 7, 27)  # the following Monday


class _NeverOpenCalendar:
    """A calendar that never has a session — used to exercise
    _adjacent_session's safety bound, not reachable with the real XNYS
    calendar (which never has a 10-day closure)."""

    def is_session(self, d):
        return False


def test_adjacent_session_raises_when_no_session_found_within_the_search_bound():
    with pytest.raises(ValueError):
        _adjacent_session(date(2026, 7, 22), _NeverOpenCalendar(), +1)


# ---------------------------------------------------------------------- #
# ingest_filings — the orchestration step, still no network (filings
# are handed in directly, exactly as fetch_all_filings would return them)
# ---------------------------------------------------------------------- #


def test_ingest_filings_creates_windows_for_tracked_types_only():
    conn = _conn()
    filings = [
        Filing("8-K", date(2026, 7, 22), "acc-8k", "items 2.02"),
        Filing("4", date(2026, 7, 20), "acc-form4", None),
        Filing("10-K", date(2026, 7, 22), "acc-10k", None),  # untracked
    ]
    created = ingest_filings(conn, "GOOGL", filings, CALENDAR, min_filing_date=date(2026, 7, 1))
    assert created == 2
    kinds = {r[0] for r in conn.execute("SELECT kind FROM event_windows").fetchall()}
    assert kinds == {"8-K", "form4"}


def test_ingest_filings_skips_anything_older_than_min_filing_date():
    conn = _conn()
    filings = [Filing("8-K", date(2026, 6, 1), "acc-old", None)]
    created = ingest_filings(conn, "GOOGL", filings, CALENDAR, min_filing_date=date(2026, 7, 1))
    assert created == 0
    assert conn.execute("SELECT COUNT(*) FROM event_windows").fetchone()[0] == 0


def test_ingest_filings_is_idempotent_on_a_rerun():
    conn = _conn()
    filings = [Filing("8-K", date(2026, 7, 22), "acc-8k", None)]
    first = ingest_filings(conn, "GOOGL", filings, CALENDAR, min_filing_date=date(2026, 7, 1))
    second = ingest_filings(conn, "GOOGL", filings, CALENDAR, min_filing_date=date(2026, 7, 1))
    assert first == 1
    assert second == 0
    assert conn.execute("SELECT COUNT(*) FROM event_windows").fetchone()[0] == 1


def test_ingest_filings_stores_a_readable_detail_including_items_desc():
    conn = _conn()
    filings = [Filing("8-K", date(2026, 7, 22), "acc-8k", "items 2.02 and 9.01")]
    ingest_filings(conn, "GOOGL", filings, CALENDAR, min_filing_date=date(2026, 7, 1))
    detail = conn.execute("SELECT detail FROM event_windows").fetchone()[0]
    assert "acc-8k" in detail and "items 2.02 and 9.01" in detail


def test_ingested_window_actually_gates_active_event_window():
    conn = _conn()
    filings = [Filing("8-K", date(2026, 7, 22), "acc-8k", None)]
    ingest_filings(conn, "GOOGL", filings, CALENDAR, min_filing_date=date(2026, 7, 1))
    during_session = active_event_window(conn, "GOOGL", datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc))
    assert during_session is not None and during_session.kind == "8-K"
    next_day = active_event_window(conn, "GOOGL", datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc))
    assert next_day is None


def test_refresh_edgar_events_fetches_classifies_and_stores(monkeypatch):
    """The orchestration entrypoint runner.py actually calls: fetches via
    the real vendor adapter (lazy-imported, monkeypatched here so this
    test makes no network call), then runs the exact same
    classify+store path as ingest_filings."""
    from tradebot.vendors import sec_edgar

    filings = [Filing("8-K", date(2026, 7, 22), "acc-8k", None)]
    monkeypatch.setattr(sec_edgar, "fetch_all_filings", lambda symbol, cik_map, count_per_type=20: filings)

    conn = _conn()
    created = refresh_edgar_events(conn, "GOOGL", cik_map={"GOOGL": "0001652044"}, calendar=CALENDAR, today=date(2026, 7, 23))
    assert created == 1
    assert active_event_window(conn, "GOOGL", datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)) is not None


def test_refresh_edgar_events_respects_the_lookback_window(monkeypatch):
    from tradebot.vendors import sec_edgar

    filings = [Filing("8-K", date(2026, 6, 1), "acc-old", None)]  # well outside a 14-day lookback
    monkeypatch.setattr(sec_edgar, "fetch_all_filings", lambda symbol, cik_map, count_per_type=20: filings)

    conn = _conn()
    created = refresh_edgar_events(
        conn, "GOOGL", cik_map={"GOOGL": "0001652044"}, calendar=CALENDAR, today=date(2026, 7, 23), lookback_days=14,
    )
    assert created == 0


# ---------------------------------------------------------------------- #
# Earnings classification — fixture-driven, no network
# ---------------------------------------------------------------------- #


def test_classify_pre_market_earnings_suppresses_report_day_downgrades_day_before():
    event = EarningsEvent(symbol="DDOG", report_date=date(2026, 8, 6), timing="pre-market")  # a Thursday
    windows = classify_earnings_event(event, CALENDAR)
    assert windows == [(date(2026, 8, 6), "suppress"), (date(2026, 8, 5), "downgrade")]


def test_classify_after_hours_earnings_suppresses_next_session_downgrades_report_day():
    event = EarningsEvent(symbol="PBR", report_date=date(2026, 8, 6), timing="after-hours")
    windows = classify_earnings_event(event, CALENDAR)
    assert windows == [(date(2026, 8, 7), "suppress"), (date(2026, 8, 6), "downgrade")]


def test_classify_unspecified_timing_treated_like_pre_market():
    event = EarningsEvent(symbol="XYZ", report_date=date(2026, 8, 6), timing="unspecified")
    windows = classify_earnings_event(event, CALENDAR)
    assert windows == [(date(2026, 8, 6), "suppress"), (date(2026, 8, 5), "downgrade")]


def test_classify_earnings_rolls_over_a_weekend_report_date():
    # a report_date that isn't itself a trading session (rare, but EDGAR-
    # style data feeds don't guarantee a business day) should still
    # resolve to real sessions on both sides
    event = EarningsEvent(symbol="ZZZ", report_date=date(2026, 8, 8), timing="pre-market")  # a Saturday
    windows = classify_earnings_event(event, CALENDAR)
    suppress_date, downgrade_date = windows[0][0], windows[1][0]
    assert CALENDAR.is_session(suppress_date) and CALENDAR.is_session(downgrade_date)
    assert downgrade_date < suppress_date


def test_ingest_earnings_creates_both_windows_and_gates_correctly():
    conn = _conn()
    events = [EarningsEvent(symbol="DDOG", report_date=date(2026, 8, 6), timing="pre-market")]
    created = ingest_earnings(conn, events, CALENDAR)
    assert created == 2

    report_day = active_event_window(conn, "DDOG", datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc))
    assert report_day.severity == "suppress"

    day_before = active_event_window(conn, "DDOG", datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc))
    assert day_before.severity == "downgrade"

    two_days_before = active_event_window(conn, "DDOG", datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc))
    assert two_days_before is None


def test_ingest_earnings_is_idempotent():
    conn = _conn()
    events = [EarningsEvent(symbol="DDOG", report_date=date(2026, 8, 6), timing="pre-market")]
    first = ingest_earnings(conn, events, CALENDAR)
    second = ingest_earnings(conn, events, CALENDAR)
    assert first == 2 and second == 0


def test_refresh_earnings_events_fetches_classifies_and_stores(monkeypatch):
    """The orchestration entrypoint runner.py actually calls: fetches via
    the real vendor adapter (lazy-imported, monkeypatched here so this
    test makes no network call), then runs the exact same
    classify+store path as ingest_earnings."""
    from tradebot.vendors import nasdaq_earnings

    events = [EarningsEvent(symbol="DDOG", report_date=date(2026, 8, 6), timing="pre-market")]
    monkeypatch.setattr(nasdaq_earnings, "fetch_earnings_for_symbols", lambda report_date, symbols: events)

    conn = _conn()
    created = refresh_earnings_events(conn, symbols={"DDOG", "TSLA"}, report_date=date(2026, 8, 6), calendar=CALENDAR)
    assert created == 2
    assert active_event_window(conn, "DDOG", datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)) is not None


# ---------------------------------------------------------------------- #
# EIA weekly schedule — deterministic, no network
# ---------------------------------------------------------------------- #


def test_eia_report_window_normal_week_lands_on_wednesday_at_the_right_time():
    from zoneinfo import ZoneInfo
    release_utc, shifted = eia_report_window(date(2026, 7, 22), CALENDAR)  # week of July 20, 2026
    et = release_utc.astimezone(ZoneInfo("America/New_York"))
    assert not shifted
    assert et.weekday() == 2  # Wednesday
    assert et.time() == EIA_RELEASE_TIME_ET


def test_eia_report_window_shifts_to_thursday_on_a_holiday_monday():
    # Labor Day 2026 is Monday September 7 -> report week shifts to Thursday
    from zoneinfo import ZoneInfo
    release_utc, shifted = eia_report_window(date(2026, 9, 9), CALENDAR)
    et = release_utc.astimezone(ZoneInfo("America/New_York"))
    assert shifted is True
    assert et.weekday() == 3  # Thursday
    assert et.time() == EIA_RELEASE_TIME_ET


def test_seed_eia_event_creates_a_uso_scoped_suppress_window():
    conn = _conn()
    seed_eia_event(conn, date(2026, 7, 22), CALENDAR)
    release_utc, _ = eia_report_window(date(2026, 7, 22), CALENDAR)
    w = active_event_window(conn, "USO", release_utc)
    assert w is not None and w.kind == "eia" and w.severity == "suppress"
    assert active_event_window(conn, "AAPL", release_utc) is None  # USO-scoped, not market-wide


def test_seed_eia_event_is_idempotent():
    conn = _conn()
    first = seed_eia_event(conn, date(2026, 7, 22), CALENDAR)
    second = seed_eia_event(conn, date(2026, 7, 22), CALENDAR)
    assert first is not None and second is None


# ---------------------------------------------------------------------- #
# Manual FOMC/CPI/NFP seeding — real dates the operator supplies
# ---------------------------------------------------------------------- #


def test_seed_macro_event_rejects_an_unknown_kind():
    conn = _conn()
    with pytest.raises(ValueError):
        seed_macro_event(conn, "unknown_kind", date(2026, 9, 17), CALENDAR)


def test_seed_macro_event_creates_a_tight_suppress_and_a_wide_downgrade_window():
    conn = _conn()
    sid, did = seed_macro_event(conn, "fomc", date(2026, 9, 17), CALENDAR)
    assert sid is not None and did is not None

    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    release_utc = datetime.combine(date(2026, 9, 17), MACRO_RELEASE_TIME_ET["fomc"], tzinfo=et).astimezone(timezone.utc)

    at_release = active_event_window(conn, "SPY", release_utc)
    assert at_release.severity == "suppress"

    same_day_elsewhere = active_event_window(conn, "SPY", release_utc - timedelta(hours=4))
    assert same_day_elsewhere.severity == "downgrade"

    assert active_event_window(conn, "SPY", release_utc + timedelta(days=1)) is None


def test_seed_macro_event_is_market_wide_not_symbol_scoped():
    conn = _conn()
    seed_macro_event(conn, "cpi", date(2026, 9, 10), CALENDAR)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    release_utc = datetime.combine(date(2026, 9, 10), MACRO_RELEASE_TIME_ET["cpi"], tzinfo=et).astimezone(timezone.utc)
    for symbol in ("SPY", "TSLA", "USO", "BE"):
        w = active_event_window(conn, symbol, release_utc)
        assert w is not None and w.kind == "cpi"


def test_seed_macro_event_is_idempotent():
    conn = _conn()
    first = seed_macro_event(conn, "nfp", date(2026, 9, 4), CALENDAR)
    second = seed_macro_event(conn, "nfp", date(2026, 9, 4), CALENDAR)
    assert first == (None, None) or all(x is not None for x in first)
    assert second == (None, None)
