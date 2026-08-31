"""Daily market-wide discovery audit coverage and integrity tests."""
from __future__ import annotations

import ast
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradebot.detectors import Bar
from tradebot.marketdata import MarketScreenEntry, MarketWideScreen
from tradebot.postmarket import thresholds
from tradebot.postmarket_discovery import connect
from tradebot.postmarket_discovery_audit import (
    AUDIT_VERSION,
    _audit_ready_at,
    _session_window,
    audit_discovery_session,
    report_json,
    write_completed_discovery_audits,
    write_report_atomic,
)
from tradebot import postmarket_discovery_shadow as discovery_shadow


SESSION = date(2026, 8, 27)
START = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
AUDIT_READY = END + timedelta(minutes=5)
ENDPOINTS = ("market_movers", "most_actives_volume", "most_actives_trades")


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _times(start=START, end=END):
    values = []
    current = start
    while current <= end:
        values.append(current)
        current += timedelta(minutes=1)
    return values


def _seed_session(
    conn,
    *,
    times=None,
    source_age_seconds=0,
    code_version="abc123",
    missing_fetch=False,
    malformed_rank=False,
    candidate_directions=None,
    include_timing=True,
):
    times = times or _times()
    directions = candidate_directions or ["up"] * len(times)
    if len(directions) != len(times):
        raise ValueError("candidate_directions must match times")
    first_direction_indexes = {}
    for index, tick_utc in enumerate(times):
        direction = directions[index]
        first_direction_indexes.setdefault(direction, index)
        candidate_source = "market_gainer" if direction == "up" else "market_loser"
        candidate_close = 109.0 if direction == "up" else 91.0
        candidate_move = 9.0 if direction == "up" else -9.0
        screen_move = 12.0 if direction == "up" else -12.0
        source_updated = tick_utc - timedelta(seconds=source_age_seconds)
        source_updates = {name: source_updated.isoformat() for name in ENDPOINTS}
        fetched = 1 if missing_fetch else 2
        error_count = 1 if missing_fetch else 0
        rank_value = "one" if malformed_rank else 1
        cursor = conn.execute(
            """
            INSERT INTO postmarket_discovery_ticks
                (session,tick_utc,completed_utc,run_id,run_mode,discovery_version,
                 code_version,data_feed,market_data_provider,bar_timeframe,
                 discovery_scope,endpoints_json,source_updates_json,requested_top_n,
                 universe_symbols,screen_rows,screen_unique_symbols,excluded_symbols,
                 discovered_symbols,not_returned_symbols,fetched_symbols,
                 evaluated_symbols,candidate_observations,new_candidates,invariant_ok,
                 thresholds_json,latency_ms,error_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                SESSION.isoformat(),
                tick_utc.isoformat(),
                (tick_utc + timedelta(milliseconds=100)).isoformat(),
                "run-1",
                "postmarket-marketwide-shadow",
                1,
                code_version,
                "sip",
                "alpaca",
                "5Min",
                "alpaca_top_movers_and_actives",
                _compact(ENDPOINTS),
                _compact(source_updates),
                50,
                10,
                200,
                3,
                1,
                2,
                8,
                fetched,
                2,
                1,
                int(first_direction_indexes[direction] == index),
                1,
                _compact(thresholds()),
                100,
                error_count,
            ),
        )
        tick_id = cursor.lastrowid
        if include_timing:
            if index == 0:
                missed_cycles = int((tick_utc - START).total_seconds() // 60)
            else:
                missed_cycles = max(
                    0,
                    int((tick_utc - times[index - 1]).total_seconds() // 60) - 1,
                )
            conn.execute(
                """
                INSERT INTO postmarket_discovery_timing
                    (tick_id,session,scheduled_tick_utc,actual_start_utc,
                     completed_utc,scheduled_lag_ms,missed_cycles,
                     screen_latency_ms,selection_latency_ms,bar_fetch_latency_ms,
                     evaluation_latency_ms,persistence_observations,
                     persistence_span_avg_seconds,persistence_span_max_seconds,
                     total_latency_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tick_id,
                    SESSION.isoformat(),
                    tick_utc.isoformat(),
                    tick_utc.isoformat(),
                    (tick_utc + timedelta(milliseconds=100)).isoformat(),
                    0,
                    missed_cycles,
                    20,
                    5,
                    50,
                    25,
                    1,
                    300.0,
                    300.0,
                    100,
                ),
            )
        common = (
            SESSION.isoformat(),
            tick_utc.isoformat(),
            100.0,
            candidate_close,
            candidate_close,
            candidate_close,
            candidate_close,
            1000,
            1000,
            int(candidate_close * 1000),
            candidate_move,
            direction,
            2,
            10.0,
            "sip",
            "alpaca",
            "5Min",
        )
        conn.execute(
            """
            INSERT INTO postmarket_discovery_observations
                (tick_id,symbol,sources_json,ranks_json,screen_evidence_json,
                 screen_move_pct,outcome,reason,event_date,bar_open_ts_utc,
                 rth_close,open,high,low,close,volume,cumulative_volume,
                 cumulative_notional,move_pct,direction,persistence_bars,
                 data_age_seconds,data_feed,market_data_provider,bar_timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tick_id,
                "CAND",
                _compact([candidate_source]),
                _compact([[candidate_source, rank_value]]),
                _compact(
                    [
                        {
                            "source": candidate_source,
                            "rank": rank_value,
                            "source_updated_at": source_updated.isoformat(),
                            "move_pct": screen_move,
                            "price": candidate_close,
                            "volume": None,
                            "trade_count": None,
                        }
                    ]
                ),
                screen_move,
                "CANDIDATE",
                "qualified",
                *common,
            ),
        )
        quiet_outcome = "FETCH_ERROR" if missing_fetch else "BELOW_MOVE"
        conn.execute(
            """
            INSERT INTO postmarket_discovery_observations
                (tick_id,symbol,sources_json,ranks_json,screen_evidence_json,
                 screen_move_pct,outcome,reason,event_date,bar_open_ts_utc,
                 rth_close,open,high,low,close,volume,cumulative_volume,
                 cumulative_notional,move_pct,direction,persistence_bars,
                 data_age_seconds,data_feed,market_data_provider,bar_timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tick_id,
                "QUIET",
                _compact(["most_active_volume", "scheduled_earnings"]),
                _compact([["most_active_volume", 1]]),
                _compact(
                    [
                        {
                            "source": "most_active_volume",
                            "rank": 1,
                            "source_updated_at": source_updated.isoformat(),
                            "move_pct": None,
                            "price": None,
                            "volume": 1_000_000,
                            "trade_count": 10_000,
                        }
                    ]
                ),
                None,
                quiet_outcome,
                "quiet" if not missing_fetch else "missing bulk response",
                SESSION.isoformat(),
                tick_utc.isoformat() if not missing_fetch else None,
                100.0 if not missing_fetch else None,
                101.0 if not missing_fetch else None,
                101.0 if not missing_fetch else None,
                101.0 if not missing_fetch else None,
                101.0 if not missing_fetch else None,
                1000 if not missing_fetch else None,
                1000 if not missing_fetch else None,
                101_000.0 if not missing_fetch else None,
                1.0 if not missing_fetch else None,
                "up" if not missing_fetch else None,
                0,
                10.0 if not missing_fetch else None,
                "sip",
                "alpaca",
                "5Min",
            ),
        )
    for direction, index in first_direction_indexes.items():
        candidate_close = 109.0 if direction == "up" else 91.0
        candidate_move = 9.0 if direction == "up" else -9.0
        candidate_source = "market_gainer" if direction == "up" else "market_loser"
        conn.execute(
            """
            INSERT INTO postmarket_discovery_candidates
                (session,symbol,event_date,direction,discovery_version,
                 first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
                 cumulative_volume,cumulative_notional,sources_json,data_feed,
                 market_data_provider,bar_timeframe,code_version,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                SESSION.isoformat(),
                "CAND",
                SESSION.isoformat(),
                direction,
                1,
                (times[index] + timedelta(milliseconds=100)).isoformat(),
                times[index].isoformat(),
                100.0,
                candidate_close,
                candidate_move,
                1000,
                candidate_close * 1000,
                _compact([candidate_source]),
                "sip",
                "alpaca",
                "5Min",
                code_version,
                "run-1",
            ),
        )
    conn.commit()


def test_complete_discovery_session_is_operationally_eligible(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(conn)

    report = audit_discovery_session(
        conn,
        SESSION,
        database="shadow.db",
        audit_code_version="audit123",
    )

    assert report.operational_clean is True
    assert report.session_evidence_eligible is True
    assert report.operational.ticks == 241
    assert report.operational.window_coverage_pct == 100.0
    assert report.operational.max_tick_gap_seconds == 60
    assert report.operational.timing_rows == 241
    assert report.operational.missed_cycles == 0
    assert report.operational.max_scheduled_lag_ms == 0
    assert report.operational.max_stage_latency_ms == {
        "screen": 20,
        "selection": 5,
        "bar_fetch": 50,
        "evaluation": 25,
    }
    assert report.operational.max_persistence_span_seconds == 300
    assert report.operational.max_source_age_seconds == 0
    assert report.operational.unique_candidates == 1
    assert report.operational.scheduled_overlap_symbols == 1
    assert report.operational.source_observations == {
        "market_gainer": 241,
        "most_active_volume": 241,
        "scheduled_earnings": 241,
    }
    assert report.candidates[0].symbol == "CAND"
    assert report.candidates[0].observation_ticks == 241
    assert report.near_miss_symbols == ()
    assert report.issues == ()
    assert json.loads(report_json(report))["operational_clean"] is True


def test_candidate_direction_reversal_reconciles_each_ledger_entry(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    times = _times()
    _seed_session(
        conn,
        times=times,
        candidate_directions=["down", "down"] + ["up"] * (len(times) - 2),
    )

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")

    assert report.operational_clean is True
    assert report.operational.unique_candidates == 2
    assert {(candidate.symbol, candidate.direction) for candidate in report.candidates} == {
        ("CAND", "down"),
        ("CAND", "up"),
    }
    assert "CANDIDATE_DETECTION_TIME_MISMATCH" not in {
        issue.code for issue in report.issues
    }


def test_partial_session_is_explicitly_ineligible(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(
        conn,
        times=_times(START + timedelta(minutes=15), START + timedelta(minutes=20)),
    )

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    codes = {issue.code for issue in report.issues}

    assert report.operational_clean is False
    assert report.session_evidence_eligible is False
    assert report.operational.window_coverage_pct < 3
    assert {"COVERAGE_STARTED_LATE", "COVERAGE_ENDED_EARLY"} <= codes
    assert "MISSED_CYCLES" in codes


def test_missing_stage_timing_is_a_blocker(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(conn, include_timing=False)

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    codes = {issue.code for issue in report.issues}

    assert report.operational_clean is False
    assert report.operational.timing_rows == 0
    assert {"TIMING_EVIDENCE_MISSING", "TIMING_TICK_SET_MISMATCH"} <= codes


def test_tick_gap_names_preceding_slowest_stage(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(conn, times=[START, START + timedelta(minutes=3)])

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    issue = next(issue for issue in report.issues if issue.code == "TICK_GAP")

    assert "180s" in issue.detail
    assert "slowest stage was bar_fetch (50ms)" in issue.detail
    assert report.operational.missed_cycles == 2


def test_stale_provider_timestamps_and_fetch_errors_are_blockers(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(conn, source_age_seconds=181, missing_fetch=True)

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    codes = {issue.code for issue in report.issues}

    assert report.operational_clean is False
    assert report.operational.fetch_errors == 241
    assert report.operational.max_source_age_seconds == 181
    assert {"SOURCE_TIMESTAMP_STALE", "FETCH_ERRORS"} <= codes


def test_malformed_rank_is_reported_without_crashing_audit(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(conn, malformed_rank=True)

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    codes = {issue.code for issue in report.issues}

    assert report.operational_clean is False
    assert {"SCREEN_EVIDENCE_RANK_INVALID", "RANK_EVIDENCE_MISMATCH"} <= codes


def test_v2_audit_accepts_one_exact_deterministic_full_universe_sweep_cycle(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    active = {"A", "B", "C", "D", "E"}

    def bars(symbol):
        return [Bar(symbol, START - timedelta(minutes=5), 100, 100, 100, 100, 1_000)]

    for minute in range(5):
        now = START + timedelta(minutes=minute)
        screen = MarketWideScreen(
            entries=(
                MarketScreenEntry(
                    symbol="A",
                    source="market_gainer",
                    rank=1,
                    source_updated_at=now,
                    move_pct=1.0,
                    price=100.0,
                ),
            ),
            requested_top_n=50,
            provider="alpaca",
            feed="sip",
            endpoints=ENDPOINTS,
            source_updates=tuple((endpoint, now) for endpoint in ENDPOINTS),
        )
        discovery_shadow.run_discovery_tick(
            conn,
            active_universe=active,
            scheduled_earnings=set(),
            now=now,
            run_id=f"sweep-{minute}",
            version="sweep123",
            data_feed="sip",
            screen_fetch=lambda top, screen=screen: screen,
            bars_fetch=lambda symbols, session: {
                symbol: bars(symbol) for symbol in symbols
            },
            sweep_bars_fetch=lambda symbols, start, end: {
                symbol: bars(symbol) for symbol in symbols
            },
            sweep_cycle_ticks=5,
        )

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    codes = {issue.code for issue in report.issues}

    assert not {code for code in codes if code.startswith("SWEEP_")}
    assert report.operational.source_observations["full_universe_sweep"] == 5
    assert "COVERAGE_ENDED_EARLY" in codes

    conn.execute("DROP TRIGGER postmarket_discovery_observations_no_delete")
    conn.execute(
        """
        DELETE FROM postmarket_discovery_observations
        WHERE seq=(
            SELECT seq FROM postmarket_discovery_observations
            WHERE sources_json='["full_universe_sweep"]'
            ORDER BY seq LIMIT 1
        )
        """
    )
    damaged = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    assert "SWEEP_POSITION_COVERAGE_MISMATCH" in {
        issue.code for issue in damaged.issues
    }


def test_v2_audit_conserves_sweep_no_bars_without_fetch_error(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    active = {"A", "B", "C", "D", "E"}

    for minute in range(5):
        now = START + timedelta(minutes=minute)
        screen = MarketWideScreen(
            entries=(
                MarketScreenEntry(
                    symbol="A",
                    source="market_gainer",
                    rank=1,
                    source_updated_at=now,
                    move_pct=1.0,
                    price=100.0,
                ),
            ),
            requested_top_n=50,
            provider="alpaca",
            feed="sip",
            endpoints=ENDPOINTS,
            source_updates=tuple((endpoint, now) for endpoint in ENDPOINTS),
        )
        discovery_shadow.run_discovery_tick(
            conn,
            active_universe=active,
            scheduled_earnings=set(),
            now=now,
            run_id=f"no-bars-{minute}",
            version="sweep123",
            data_feed="sip",
            screen_fetch=lambda top, screen=screen: screen,
            bars_fetch=lambda symbols, session: {
                "A": [
                    Bar("A", START - timedelta(minutes=5), 100, 100, 100, 100, 1_000)
                ]
            },
            sweep_bars_fetch=lambda symbols, start, end: {},
            sweep_cycle_ticks=5,
        )

    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    codes = {issue.code for issue in report.issues}

    assert report.operational.fetch_errors == 0
    assert report.operational.outcome_counts["NO_BARS_RETURNED"] == 4
    assert "MISSING_FETCH_ERRORS" not in codes
    assert "FETCH_ERRORS" not in codes
    assert "NO_BARS_EVIDENCE_INVALID" not in codes


def test_session_window_uses_actual_early_close_and_est_postmarket_end():
    window = _session_window(date(2026, 11, 27))

    assert window == (
        datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc),
    )
    assert _audit_ready_at(date(2026, 11, 27)) == datetime(
        2026, 11, 28, 1, 5, tzinfo=timezone.utc
    )


def test_completed_audit_write_is_immutable_idempotent_and_after_window(tmp_path):
    db_path = tmp_path / "shadow.db"
    conn = connect(db_path)
    _seed_session(conn)
    output = tmp_path / "audits"
    output.mkdir()
    legacy_path = output / "postmarket_discovery_audit_2026-08-27_v1.json"
    legacy_path.write_text('{"legacy":true}\n', encoding="utf-8")

    assert write_completed_discovery_audits(
        db_path,
        output,
        now=AUDIT_READY,
        audit_code_version="audit123",
    ) == ()
    first = write_completed_discovery_audits(
        db_path,
        output,
        now=AUDIT_READY + timedelta(seconds=1),
        audit_code_version="audit123",
    )
    second = write_completed_discovery_audits(
        db_path,
        output,
        now=AUDIT_READY + timedelta(minutes=1),
        audit_code_version="different",
    )

    assert len(first) == 1
    assert second == ()
    path = output / f"postmarket_discovery_audit_2026-08-27_v{AUDIT_VERSION}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == {"legacy": True}
    assert payload["audit_code_version"] == "audit123"
    assert payload["session_evidence_eligible"] is True


def test_concurrent_audit_publication_creates_exactly_one_report(tmp_path):
    conn = connect(tmp_path / "shadow.db")
    _seed_session(conn)
    report = audit_discovery_session(conn, SESSION, audit_code_version="audit123")
    destination = tmp_path / "audit.json"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: write_report_atomic(destination, report), range(20))
        )

    assert results.count(True) == 1
    assert results.count(False) == 19
    assert json.loads(destination.read_text(encoding="utf-8"))["session"] == "2026-08-27"


def test_auditor_has_no_provider_delivery_or_trading_dependency():
    source_path = (
        Path(__file__).parents[1] / "tradebot" / "postmarket_discovery_audit.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = (
        "tradebot.vendors",
        "tradebot.alerts",
        "tradebot.telegram_bot",
        "tradebot.order",
        "tradebot.broker",
    )
    assert not any(module.startswith(forbidden) for module in imports)


def test_discovery_heartbeat_surfaces_latest_immutable_audit(monkeypatch):
    latest = {
        "session": "2026-08-27",
        "operational_clean": False,
        "session_evidence_eligible": False,
        "issue_codes": ["COVERAGE_STARTED_LATE"],
    }
    monkeypatch.setattr(
        discovery_shadow,
        "write_due_discovery_audits",
        lambda now: ({"session": "2026-08-27"},),
    )
    monkeypatch.setattr(
        discovery_shadow,
        "latest_discovery_audit_summary",
        lambda: latest,
    )

    fields = discovery_shadow.discovery_audit_heartbeat_fields(END + timedelta(minutes=1))

    assert fields == {
        "audit_status": "written",
        "audits_written": 1,
        "latest_audit": latest,
    }


def test_discovery_heartbeat_makes_audit_failure_visible(monkeypatch):
    def fail(now):
        raise ValueError("malformed evidence")

    monkeypatch.setattr(discovery_shadow, "write_due_discovery_audits", fail)

    fields = discovery_shadow.discovery_audit_heartbeat_fields(END + timedelta(minutes=1))

    assert fields["audit_status"] == "error"
    assert "malformed evidence" in fields["audit_error"]
