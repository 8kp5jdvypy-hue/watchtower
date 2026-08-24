"""Stage 1 observability: screening_ticks / screening_events.

"Why did Perch miss this mover?" had no answer for the widest part of the
funnel -- thousands of symbols down to at most 25 -- because nothing
recorded what Stage 1 did with a symbol it screened out. The counts
existed (runner._log_broad_scan_shadow_counts derives them) but only as a
log line: aggregate, ephemeral, and not per-symbol, so "which bucket did
AAPL fall into at 14:30?" was unanswerable.

These cover the classification (pure), the persistence, and -- the point
of the whole exercise -- a query that explains a specific missed mover.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

import pytest

from tradebot import universe as universe_mod
from tradebot.broad_scan import (
    MIN_HISTORY_BARS,
    OUTCOME_CANDIDATE_NOT_PROMOTED,
    OUTCOME_INSUFFICIENT_HISTORY,
    OUTCOME_INVALID_BASELINE,
    OUTCOME_MISSING_FROM_FETCH,
    OUTCOME_PROMOTED,
    OUTCOME_QUIET,
    OUTCOME_UNEXPECTED_FROM_FETCH,
    RVOL_THRESHOLD,
    SCREEN_VERSION,
    Snapshot,
    classify_screen_outcomes,
    promote_candidates,
    screen_snapshot,
    screen_thresholds,
)
from tradebot.marketdata import AssetInfo, Bar
import tradebot.runner as runner_mod

SESSION = date(2026, 8, 24)
TICK = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(symbol, *, volume=1000, avg_volume=1000.0, close=100.0, prior_close=100.0,
              open_=100.0, high=100.5, low=99.5):
    return Snapshot(symbol=symbol, open=open_, high=high, low=low, close=close,
                    prior_close=prior_close, volume=volume, avg_volume=avg_volume)


def _loud(symbol, multiple):
    """A snapshot that clears the rvol gate by `multiple` -- higher
    multiple, higher screen_score, so rank is controllable."""
    return _snapshot(symbol, volume=int(RVOL_THRESHOLD * 1000 * multiple))


def _classify(snapshots, requested=None, bars=None, promotion_limit=25, verbose_audit=False):
    requested = requested if requested is not None else [s.symbol for s in snapshots]
    bars = bars if bars is not None else {s.symbol: [object()] * 7 for s in snapshots}
    scores = [c for c in (screen_snapshot(s) for s in snapshots) if c is not None]
    promoted = promote_candidates(scores)
    selected = promoted[:promotion_limit]
    return classify_screen_outcomes(
        requested, bars, snapshots, promoted, selected, promotion_limit,
        verbose_audit=verbose_audit,
    )


def _record(conn, tick, events, *, audit_mode=False):
    return universe_mod.record_screening_tick(
        conn, tick, events, session=SESSION.isoformat(), tick_utc=TICK.isoformat(),
        run_id="run-1", run_mode="live", screen_version=SCREEN_VERSION,
        code_version="test-sha", audit_mode=audit_mode,
    )


# ---------------------------------------------------------------------------
# Classification is pure and total
# ---------------------------------------------------------------------------


def test_every_symbol_lands_in_exactly_one_bucket_and_the_invariant_holds():
    """The conservation property the whole table rests on: with quiet
    aggregated, "was X screened and quiet?" is answered by subtraction,
    and that subtraction is only valid if the counts add up."""
    snapshots = [
        _loud("LOUD", 2.0),
        _snapshot("QUIET"),
        _snapshot("INVALID", avg_volume=0.0),
    ]
    tick, events = _classify(
        snapshots,
        requested=["LOUD", "QUIET", "INVALID", "SHORT", "MISSING"],
        bars={"LOUD": [1] * 7, "QUIET": [1] * 7, "INVALID": [1] * 7, "SHORT": [1] * 3},
        promotion_limit=25,
    )

    assert tick.invariant_ok
    assert tick.universe_count == 5
    assert tick.counts["missing_from_fetch"] == 1
    assert tick.counts["insufficient_history"] == 1
    assert tick.counts["invalid_baseline"] == 1
    assert tick.counts["quiet"] == 1
    assert tick.counts["candidate"] == 1

    by_symbol = {e.symbol: e.outcome for e in events}
    assert by_symbol["LOUD"] == OUTCOME_PROMOTED
    assert by_symbol["MISSING"] == OUTCOME_MISSING_FROM_FETCH
    assert by_symbol["SHORT"] == OUTCOME_INSUFFICIENT_HISTORY
    assert by_symbol["INVALID"] == OUTCOME_INVALID_BASELINE
    assert "QUIET" not in by_symbol  # aggregated, not written


def test_quiet_symbols_are_counted_but_not_written_by_default():
    tick, events = _classify([_snapshot(f"Q{i}") for i in range(50)])

    assert tick.counts["quiet"] == 50
    assert events == []


def test_verbose_audit_writes_quiet_rows_as_well():
    tick, events = _classify([_snapshot(f"Q{i}") for i in range(50)], verbose_audit=True)

    assert tick.counts["quiet"] == 50
    assert len([e for e in events if e.outcome == OUTCOME_QUIET]) == 50


def test_the_promotion_cap_produces_a_distinct_bucket():
    """The bucket that exists nowhere today: cleared the screen, lost to
    the cap. Indistinguishable from 'never screened' before this."""
    snapshots = [_loud(f"L{i}", 2.0 + i) for i in range(5)]

    tick, events = _classify(snapshots, promotion_limit=2)

    outcomes = {e.symbol: e.outcome for e in events}
    assert sum(1 for o in outcomes.values() if o == OUTCOME_PROMOTED) == 2
    assert sum(1 for o in outcomes.values() if o == OUTCOME_CANDIDATE_NOT_PROMOTED) == 3
    assert tick.promotion_limit == 2


def test_candidates_are_ranked_strongest_first():
    snapshots = [_loud("WEAK", 1.1), _loud("STRONG", 5.0), _loud("MID", 2.0)]

    _, events = _classify(snapshots, promotion_limit=25)

    ranked = sorted((e for e in events if e.rank), key=lambda e: e.rank)
    assert [e.symbol for e in ranked] == ["STRONG", "MID", "WEAK"]
    assert ranked[0].screen_score > ranked[-1].screen_score


def test_an_unrequested_vendor_symbol_is_recorded_not_silently_folded_in():
    tick, events = _classify(
        [_snapshot("WANTED")], requested=["WANTED"],
        bars={"WANTED": [1] * 7, "SURPRISE": [1] * 7},
    )

    assert tick.counts["unexpected_from_fetch"] == 1
    assert {e.symbol: e.outcome for e in events}["SURPRISE"] == OUTCOME_UNEXPECTED_FROM_FETCH


def test_detail_records_raw_snapshot_inputs_not_recomputed_ratios():
    """Raw inputs, so nothing here can disagree with what screen_snapshot
    actually compared -- and a reader can still derive any ratio."""
    snapshots = [_loud("LOUD", 2.0)]

    _, events = _classify(snapshots)

    detail = events[0].detail
    assert detail["volume"] == snapshots[0].volume
    assert detail["avg_volume"] == snapshots[0].avg_volume
    assert detail["prior_close"] == snapshots[0].prior_close
    assert "rvol" not in detail  # never a second copy of the formula


def test_classification_mutates_none_of_its_inputs():
    snapshots = [_loud("LOUD", 2.0), _snapshot("QUIET")]
    requested = ["LOUD", "QUIET"]
    bars = {"LOUD": [1] * 7, "QUIET": [1] * 7}
    before = (list(requested), dict(bars), list(snapshots))

    _classify(snapshots, requested=requested, bars=bars)

    assert (requested, bars, snapshots) == before


def test_thresholds_are_read_from_the_module_not_copied():
    tick, _ = _classify([_snapshot("Q")])

    assert tick.thresholds == screen_thresholds()
    assert tick.thresholds["rvol"] == RVOL_THRESHOLD
    assert tick.thresholds["min_history_bars"] == MIN_HISTORY_BARS


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_a_tick_and_its_events_round_trip():
    conn = universe_mod.connect(":memory:")
    tick, events = _classify([_loud("LOUD", 2.0), _snapshot("INVALID", avg_volume=0.0)])

    tick_id = _record(conn, tick, events)

    row = conn.execute(
        "SELECT session, tick_utc, run_id, run_mode, screen_version, code_version, "
        "audit_mode, universe_count, invariant_ok, promotion_limit FROM screening_ticks"
    ).fetchone()
    assert row == (SESSION.isoformat(), TICK.isoformat(), "run-1", "live",
                   SCREEN_VERSION, "test-sha", 0, 2, 1, 25)
    assert conn.execute(
        "SELECT COUNT(*) FROM screening_events WHERE tick_id = ?", (tick_id,)
    ).fetchone()[0] == len(events)


def test_counts_and_thresholds_are_stored_as_queryable_json():
    """JSON1 is available, so these are queryable columns rather than
    opaque blobs -- json_extract works against them directly."""
    conn = universe_mod.connect(":memory:")
    tick, events = _classify([_snapshot("Q1"), _snapshot("Q2")])
    _record(conn, tick, events)

    quiet, rvol = conn.execute(
        "SELECT json_extract(counts_json, '$.quiet'), json_extract(thresholds_json, '$.rvol') "
        "FROM screening_ticks"
    ).fetchone()
    assert quiet == 2
    assert rvol == RVOL_THRESHOLD


def test_reasons_are_stored_as_a_json_array():
    conn = universe_mod.connect(":memory:")
    tick, events = _classify([_loud("LOUD", 2.0)])
    _record(conn, tick, events)

    stored = conn.execute("SELECT reasons_json FROM screening_events WHERE symbol='LOUD'").fetchone()[0]
    assert isinstance(json.loads(stored), list)
    assert "unusual_volume" in json.loads(stored)


def test_the_screen_score_column_is_named_to_avoid_detector_confusion():
    """Stage 1 units are not detector units. The column name is the guard
    against the one misreading that would matter."""
    conn = universe_mod.connect(":memory:")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(screening_events)")}

    assert "screen_score" in columns
    assert "score" not in columns


def test_audit_mode_is_recorded_on_the_tick():
    """Without it, a 200-row session and a 185,000-row one are
    indistinguishable except by guessing."""
    conn = universe_mod.connect(":memory:")
    tick, events = _classify([_snapshot("Q")], verbose_audit=True)

    _record(conn, tick, events, audit_mode=True)

    assert conn.execute("SELECT audit_mode FROM screening_ticks").fetchone()[0] == 1


def test_oversized_detail_is_dropped_rather_than_truncated():
    from tradebot.broad_scan import ScreeningOutcome

    conn = universe_mod.connect(":memory:")
    tick, _ = _classify([_snapshot("Q")])
    huge = ScreeningOutcome(symbol="BIG", outcome=OUTCOME_PROMOTED, detail={"x": "y" * 5000})

    _record(conn, tick, [huge])

    assert conn.execute("SELECT detail_json FROM screening_events WHERE symbol='BIG'").fetchone()[0] is None


# ---------------------------------------------------------------------------
# THE POINT: explaining a specific missed mover
# ---------------------------------------------------------------------------


def test_a_missed_mover_can_be_explained_as_candidate_not_promoted():
    """The investigation this table exists for.

    MOVER cleared the Stage 1 screen but ranked below the promotion cap,
    so it was never handed to Stage 2, never evaluated by a detector, and
    produced no detection -- previously leaving zero trace anywhere. The
    query must now say which bucket it fell into, how strong it was, what
    it ranked, and what cap it lost to."""
    conn = universe_mod.connect(":memory:")
    snapshots = [_loud("BIG1", 9.0), _loud("BIG2", 8.0), _loud("MOVER", 4.0)]
    tick, events = _classify(snapshots, promotion_limit=2)
    _record(conn, tick, events)

    history = universe_mod.screening_history_for_symbol(conn, "MOVER", SESSION.isoformat())

    assert len(history) == 1
    entry = history[0]
    assert entry["outcome"] == OUTCOME_CANDIDATE_NOT_PROMOTED
    assert entry["rank"] == 3                  # ...ranked third
    assert entry["promotion_limit"] == 2       # ...against a cap of two
    assert entry["screen_score"] > 0           # ...and it really did clear the screen
    assert "unusual_volume" in entry["reasons"]
    assert entry["detail"]["volume"] == snapshots[2].volume
    assert entry["tick_utc"] == TICK.isoformat()
    # The context needed to trust the answer.
    assert entry["invariant_ok"] is True
    assert entry["screen_version"] == SCREEN_VERSION
    assert entry["run_mode"] == "live"


def test_a_symbol_absent_from_the_vendor_fetch_is_explained_differently():
    """The same question, a different answer -- the distinction that did
    not exist before: not promoted because we never had its data, rather
    than because it lost a ranking."""
    conn = universe_mod.connect(":memory:")
    tick, events = _classify(
        [_loud("LOUD", 2.0)], requested=["LOUD", "GHOST"],
        bars={"LOUD": [1] * 7}, promotion_limit=25,
    )
    _record(conn, tick, events)

    [entry] = universe_mod.screening_history_for_symbol(conn, "GHOST", SESSION.isoformat())
    assert entry["outcome"] == OUTCOME_MISSING_FROM_FETCH
    assert entry["screen_score"] is None


def test_history_is_scoped_to_the_symbol_and_session():
    conn = universe_mod.connect(":memory:")
    tick, events = _classify([_loud("A", 2.0), _loud("B", 3.0)])
    _record(conn, tick, events)

    assert len(universe_mod.screening_history_for_symbol(conn, "A", SESSION.isoformat())) == 1
    assert universe_mod.screening_history_for_symbol(conn, "A", "2026-08-25") == []
    assert universe_mod.screening_history_for_symbol(conn, "NOPE", SESSION.isoformat()) == []


def test_history_across_several_ticks_is_returned_in_tick_order():
    """A symbol's Stage 1 story for a session -- promoted at one tick,
    beaten by the cap at another."""
    conn = universe_mod.connect(":memory:")
    for hour, limit in ((14, 25), (15, 1)):
        snaps = [_loud("RIVAL", 9.0), _loud("MOVER", 4.0)]
        tick, events = _classify(snaps, promotion_limit=limit)
        universe_mod.record_screening_tick(
            conn, tick, events, session=SESSION.isoformat(),
            tick_utc=datetime(2026, 8, 24, hour, 30, tzinfo=timezone.utc).isoformat(),
            run_id="run-1", run_mode="live", screen_version=SCREEN_VERSION,
        )

    history = universe_mod.screening_history_for_symbol(conn, "MOVER", SESSION.isoformat())
    assert [e["outcome"] for e in history] == [OUTCOME_PROMOTED, OUTCOME_CANDIDATE_NOT_PROMOTED]


# ---------------------------------------------------------------------------
# Wiring into run_broad_scan, and behavior-neutrality
# ---------------------------------------------------------------------------


def _universe_with(conn, symbols):
    universe_mod.refresh_universe(
        conn,
        lambda: [AssetInfo(s, "NASDAQ", s, True, True, None, ()) for s in symbols],
        datetime(2026, 8, 8, tzinfo=timezone.utc),
    )


def _bars(symbol, *, days=7, last_volume=1000):
    bars = [Bar(symbol, datetime(2026, 8, d, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, 1000)
            for d in range(1, days)]
    bars.append(Bar(symbol, datetime(2026, 8, days, tzinfo=timezone.utc), 100, 100.5, 99.5, 100, last_volume))
    return bars


def test_run_broad_scan_persists_a_tick_and_still_returns_the_same_selection():
    conn = universe_mod.connect(":memory:")
    _universe_with(conn, ["LOUD", "QUIET"])

    def fake_bars(symbols, lookback_days):
        return {"LOUD": _bars("LOUD", last_volume=int(RVOL_THRESHOLD * 1000 * 3)),
                "QUIET": _bars("QUIET")}

    promoted = runner_mod.run_broad_scan(
        conn, fetch_bars_fn=fake_bars, session_date=SESSION, tick_utc=TICK,
        run_id="run-1", run_mode="live",
    )

    assert promoted == ["LOUD"]  # the real, unaffected selection
    [entry] = universe_mod.screening_history_for_symbol(conn, "LOUD", SESSION.isoformat())
    assert entry["outcome"] == OUTCOME_PROMOTED
    assert conn.execute("SELECT COUNT(*) FROM screening_ticks").fetchone()[0] == 1


def test_a_persistence_failure_never_touches_the_returned_selection(monkeypatch, caplog):
    """Instrumentation must never be able to change what Stage 2 gets."""
    conn = universe_mod.connect(":memory:")
    _universe_with(conn, ["LOUD"])

    def _broken(*args, **kwargs):
        raise RuntimeError("screening store is on fire")

    monkeypatch.setattr(universe_mod, "record_screening_tick", _broken)

    def fake_bars(symbols, lookback_days):
        return {"LOUD": _bars("LOUD", last_volume=int(RVOL_THRESHOLD * 1000 * 3))}

    with caplog.at_level(logging.ERROR, logger="watchtower.runner"):
        promoted = runner_mod.run_broad_scan(conn, fetch_bars_fn=fake_bars)

    assert promoted == ["LOUD"]
    assert any("screening_events persistence failed" in r.getMessage() for r in caplog.records)


def test_the_persisted_counts_agree_with_the_logged_shadow_counts(caplog):
    """The two derivations are deliberately separate --
    _log_broad_scan_shadow_counts is proven-inert and was left untouched
    -- so this pins that they agree. A future edit to either that made
    them disagree fails here instead of drifting silently."""
    conn = universe_mod.connect(":memory:")
    _universe_with(conn, ["LOUD", "QUIET", "SHORT", "MISSING"])

    def fake_bars(symbols, lookback_days):
        return {"LOUD": _bars("LOUD", last_volume=int(RVOL_THRESHOLD * 1000 * 3)),
                "QUIET": _bars("QUIET"),
                "SHORT": _bars("SHORT", days=3)}

    with caplog.at_level(logging.INFO, logger="watchtower.runner"):
        runner_mod.run_broad_scan(conn, fetch_bars_fn=fake_bars)

    [record] = [r for r in caplog.records if "broad_scan_shadow_counts" in r.getMessage()]
    logged = record.getMessage()
    counts = json.loads(conn.execute("SELECT counts_json FROM screening_ticks").fetchone()[0])

    for key, logged_key in (
        ("requested", "requested"), ("missing_from_fetch", "missing_from_fetch"),
        ("insufficient_history", "insufficient_history"), ("invalid_baseline", "invalid_baseline"),
        ("quiet", "evaluated_quiet"), ("candidate", "candidate"),
        ("selected_top_n", "selected_top_n"), ("unexpected_from_fetch", "unexpected_from_fetch"),
    ):
        assert f"{logged_key}={counts[key]}" in logged, f"{key} disagrees with the shadow log"


def test_verbose_audit_is_off_unless_the_env_var_is_set(monkeypatch):
    monkeypatch.delenv("WATCHTOWER_SCREEN_AUDIT", raising=False)
    assert runner_mod._screening_audit_enabled() is False

    monkeypatch.setenv("WATCHTOWER_SCREEN_AUDIT", "1")
    assert runner_mod._screening_audit_enabled() is True


def test_verbose_audit_env_var_writes_quiet_rows_end_to_end(monkeypatch):
    conn = universe_mod.connect(":memory:")
    _universe_with(conn, ["QUIET1", "QUIET2"])
    monkeypatch.setenv("WATCHTOWER_SCREEN_AUDIT", "on")

    runner_mod.run_broad_scan(
        conn, fetch_bars_fn=lambda s, d: {"QUIET1": _bars("QUIET1"), "QUIET2": _bars("QUIET2")},
    )

    quiet_rows = conn.execute(
        "SELECT COUNT(*) FROM screening_events WHERE outcome = ?", (OUTCOME_QUIET,)
    ).fetchone()[0]
    assert quiet_rows == 2
    assert conn.execute("SELECT audit_mode FROM screening_ticks").fetchone()[0] == 1


def test_an_unattributed_scan_says_so_rather_than_claiming_live():
    """run_broad_scan called without run attribution -- a test or a
    script -- must not produce rows that read as live."""
    conn = universe_mod.connect(":memory:")
    _universe_with(conn, ["QUIET1"])

    runner_mod.run_broad_scan(conn, fetch_bars_fn=lambda s, d: {"QUIET1": _bars("QUIET1")})

    run_mode, run_id = conn.execute("SELECT run_mode, run_id FROM screening_ticks").fetchone()
    assert run_mode != "live"
    assert run_id == "unattributed"
