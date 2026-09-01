from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot.evaluations import SCHEMA as EVALUATIONS_SCHEMA
from tradebot.journal import SCHEMA as JOURNAL_SCHEMA
from tradebot.postmarket import SCHEMA as POSTMARKET_SCHEMA
from tradebot.postmarket_context import CONTEXT_SCHEMA
from tradebot.postmarket_discovery import DISCOVERY_SCHEMA
from tradebot.postmarket_lifecycle import LIFECYCLE_SCHEMA
from tradebot.postmarket_quality import QUALITY_SCHEMA
from tradebot.postmarket_rank import RANK_SCHEMA
from tradebot.rth_missed_mover_census import CENSUS_SCHEMA
from tradebot.rth_momentum import RTH_SCHEMA
from tradebot.screening_archive import archive_screening_session
from tradebot.signal_incident_reconstruction import (
    STATUS_ARCHIVE_CORRUPT,
    STATUS_DATABASE_MISSING,
    STATUS_PRESENT,
    STATUS_PRESENT_WITH_ISSUES,
    STATUS_SCHEMA_INCOMPATIBLE,
    ReadonlyDatabase,
    ReconstructionPaths,
    _assess_rows,
    _conclusions,
    main,
    reconstruct_signal_incident,
    write_reconstruction_artifact,
)
from tradebot.telegram_bot.db import SCHEMA as USERS_SCHEMA
from tradebot.universe import SCHEMA as UNIVERSE_SCHEMA


SESSION = "2026-08-31"
SYMBOL = "GPRO"
GENERATED = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)


def _database(path: Path, schema: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    return connection


def _paths(root: Path) -> ReconstructionPaths:
    return ReconstructionPaths.from_data_directory(root)


def _seed_universe(path: Path, *, with_event: bool = True) -> None:
    connection = _database(path, UNIVERSE_SCHEMA)
    connection.execute(
        """
        INSERT INTO assets
          (symbol,exchange,name,tradable,options_enabled,overnight_eligible,
           attributes_json,is_active,first_seen_at,last_seen_at,delisted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SYMBOL,
            "NASDAQ",
            "GoPro, Inc.",
            1,
            1,
            1,
            "{}",
            1,
            "2026-08-01T00:00:00+00:00",
            "2026-08-31T12:00:00+00:00",
            None,
        ),
    )
    connection.execute(
        """
        INSERT INTO screening_ticks
          (session,tick_utc,run_id,run_mode,screen_version,code_version,
           audit_mode,universe_count,thresholds_json,counts_json,invariant_ok,
           promotion_limit,latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION,
            "2026-08-31T19:36:56+00:00",
            "screen-run",
            "live",
            2,
            "8aa7dd5",
            0,
            13_083,
            "{}",
            '{"selected_top_n":25}',
            1,
            25,
            1200,
        ),
    )
    if with_event:
        connection.execute(
            """
            INSERT INTO screening_events
              (tick_id,symbol,outcome,screen_score,rank,reasons_json,detail_json)
            VALUES (1,?,?,?,?,?,?)
            """,
            (
                SYMBOL,
                "CANDIDATE_NOT_PROMOTED",
                1.2,
                2412,
                '["outside_top_n"]',
                "{}",
            ),
        )
    connection.commit()
    connection.close()


def _seed_journal(path: Path) -> None:
    connection = _database(path, JOURNAL_SCHEMA)
    connection.execute(
        """
        INSERT INTO detections
          (id,ts_utc,session,symbol,kinds,headlines,score,tier,close,alerted,
           code_version,data_feed,origin,context_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "gpro-detection",
            "2026-08-31T19:55:00+00:00",
            SESSION,
            SYMBOL,
            "range_expansion",
            "GPRO expanded",
            4.2,
            "high",
            0.87,
            1,
            "8aa7dd5",
            "sip",
            "screening",
            "{}",
        ),
    )
    connection.execute(
        """
        INSERT INTO decision_events
          (detection_id,ts_utc,stage,decision,reason,detail_json,code_version,
           run_mode,run_id)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "gpro-detection",
            "2026-08-31T20:00:01+00:00",
            "alert_routing",
            "send",
            "high tier",
            "{}",
            "8aa7dd5",
            "live",
            "journal-run",
        ),
    )
    connection.execute("INSERT INTO marks VALUES (?,?,?)", ("gpro-detection", -1, 0.87))
    connection.commit()
    connection.close()


def _seed_evaluations(path: Path) -> None:
    connection = _database(path, EVALUATIONS_SCHEMA)
    connection.execute(
        """
        INSERT INTO evaluation_sessions
          (session,symbol,run_id,run_mode,evaluation_version,code_version,
           origin,anchors_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION,
            SYMBOL,
            "eval-run",
            "live",
            1,
            "8aa7dd5",
            "screening",
            "{}",
            "2026-08-31T19:40:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO bar_evaluations
          (eval_session_id,bar_ts_utc,outcome,open,high,low,close,volume,
           atr14,kinds,cluster_score,tier,detection_id,error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            "2026-08-31T19:55:00+00:00",
            "DETECTED",
            0.80,
            0.90,
            0.79,
            0.87,
            1_000_000,
            0.05,
            "range_expansion",
            4.2,
            "high",
            "gpro-detection",
            None,
        ),
    )
    connection.commit()
    connection.close()


def _seed_postmarket(path: Path) -> None:
    connection = sqlite3.connect(path)
    for schema in (
        POSTMARKET_SCHEMA,
        DISCOVERY_SCHEMA,
        RTH_SCHEMA,
        CENSUS_SCHEMA,
        LIFECYCLE_SCHEMA,
        CONTEXT_SCHEMA,
        RANK_SCHEMA,
        QUALITY_SCHEMA,
    ):
        connection.executescript(schema)
    connection.execute(
        """
        INSERT INTO rth_momentum_ticks
          (session,scheduled_tick_utc,tick_utc,completed_utc,window_start_utc,
           session_close_utc,momentum_version,run_mode,run_id,code_version,
           data_feed,market_data_provider,universe_symbols,provider_screen_rows,
           provider_screen_unique_symbols,screen_error,scheduled_symbols,
           sweep_universe_sha256,sweep_cycle_ticks,sweep_shard_index,
           sweep_shard_count,sweep_shard_size,sweep_shard_symbols,
           sweep_overlap_symbols,selected_symbols,intraday_symbols_fetched,
           daily_symbols_fetched,evaluated_symbols,candidate_observations,
           new_candidates,invariant_ok,error_count,missed_cycles,scheduled_lag_ms,
           screen_latency_ms,selection_latency_ms,bar_fetch_latency_ms,
           evaluation_latency_ms,total_latency_ms,thresholds_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION,
            "2026-08-31T19:55:00+00:00",
            "2026-08-31T19:55:00+00:00",
            "2026-08-31T19:55:02+00:00",
            "2026-08-31T19:30:00+00:00",
            "2026-08-31T20:00:00+00:00",
            1,
            "shadow",
            "rth-run",
            "1c09254",
            "sip",
            "alpaca",
            13_083,
            200,
            170,
            None,
            0,
            "a" * 64,
            5,
            0,
            5,
            2617,
            2617,
            40,
            2750,
            2000,
            2000,
            2750,
            1,
            1,
            1,
            0,
            0,
            0,
            100,
            50,
            1500,
            300,
            2000,
            "{}",
        ),
    )
    connection.execute(
        """
        INSERT INTO rth_momentum_observations
          (tick_id,session,symbol,sources_json,ranks_json,screen_evidence_json,
           outcome,reason,prior_close,bar_open_ts_utc,open,high,low,close,volume,
           cumulative_volume,cumulative_notional,move_pct,direction,
           persistence_bars,data_age_seconds,data_feed,market_data_provider,
           bar_timeframe)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            SESSION,
            SYMBOL,
            '["full_universe_rth_sweep"]',
            "[]",
            "[]",
            "CANDIDATE",
            "qualified",
            0.59,
            "2026-08-31T19:50:00+00:00",
            0.75,
            0.90,
            0.74,
            0.87,
            1_000_000,
            5_000_000,
            4_000_000,
            47.46,
            "up",
            2,
            2,
            "sip",
            "alpaca",
            "5Min",
        ),
    )
    connection.execute(
        """
        INSERT INTO rth_momentum_candidates
          (session,symbol,direction,momentum_version,first_detected_at,
           bar_open_ts_utc,prior_close,close,move_pct,cumulative_volume,
           cumulative_notional,sources_json,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION,
            SYMBOL,
            "up",
            1,
            "2026-08-31T19:55:02+00:00",
            "2026-08-31T19:50:00+00:00",
            0.59,
            0.87,
            47.46,
            5_000_000,
            4_000_000,
            '["full_universe_rth_sweep"]',
            "sip",
            "alpaca",
            "5Min",
            "1c09254",
            "rth-run",
        ),
    )
    connection.execute(
        """
        INSERT INTO postmarket_discovery_ticks
          (session,tick_utc,completed_utc,run_id,run_mode,discovery_version,
           code_version,data_feed,market_data_provider,bar_timeframe,
           discovery_scope,endpoints_json,source_updates_json,requested_top_n,
           universe_symbols,screen_rows,screen_unique_symbols,
           provider_screen_rows,provider_screen_unique_symbols,
           sweep_universe_sha256,sweep_cycle_ticks,sweep_shard_index,
           sweep_shard_count,sweep_shard_size,sweep_shard_symbols,
           sweep_overlap_symbols,excluded_symbols,discovered_symbols,
           not_returned_symbols,fetched_symbols,evaluated_symbols,
           candidate_observations,new_candidates,invariant_ok,thresholds_json,
           latency_ms,error_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION,
            "2026-08-31T20:05:00+00:00",
            "2026-08-31T20:05:03+00:00",
            "pm-run",
            "shadow",
            2,
            "8aa7dd5",
            "sip",
            "alpaca",
            "5Min",
            "full_universe",
            "[]",
            "{}",
            50,
            13_083,
            200,
            170,
            200,
            170,
            "a" * 64,
            5,
            0,
            5,
            2617,
            2617,
            40,
            0,
            2750,
            0,
            2000,
            2750,
            1,
            1,
            1,
            "{}",
            3000,
            0,
        ),
    )
    connection.execute(
        """
        INSERT INTO postmarket_discovery_observations
          (tick_id,symbol,sources_json,ranks_json,screen_evidence_json,
           screen_move_pct,outcome,reason,event_date,bar_open_ts_utc,rth_close,
           open,high,low,close,volume,cumulative_volume,cumulative_notional,
           move_pct,direction,persistence_bars,data_age_seconds,data_feed,
           market_data_provider,bar_timeframe)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            SYMBOL,
            '["full_universe_sweep"]',
            "[]",
            "[]",
            68.0,
            "CANDIDATE",
            "qualified",
            SESSION,
            "2026-08-31T20:00:00+00:00",
            0.87,
            1.00,
            1.50,
            0.99,
            1.47,
            10_000_000,
            10_000_000,
            12_000_000,
            68.97,
            "up",
            2,
            3,
            "sip",
            "alpaca",
            "5Min",
        ),
    )
    connection.execute(
        """
        INSERT INTO postmarket_discovery_candidates
          (session,symbol,event_date,direction,discovery_version,
           first_detected_at,bar_open_ts_utc,rth_close,close,move_pct,
           cumulative_volume,cumulative_notional,sources_json,data_feed,
           market_data_provider,bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION,
            SYMBOL,
            SESSION,
            "up",
            2,
            "2026-08-31T20:05:03+00:00",
            "2026-08-31T20:00:00+00:00",
            0.87,
            1.47,
            68.97,
            10_000_000,
            12_000_000,
            '["full_universe_sweep"]',
            "sip",
            "alpaca",
            "5Min",
            "8aa7dd5",
            "pm-run",
        ),
    )
    connection.execute(
        """
        INSERT INTO postmarket_candidate_lifecycle
          (candidate_id,lifecycle_version,session,symbol,direction,from_state,
           state,actionability,transition_at_utc,recorded_at_utc,
           evidence_bar_open_ts_utc,evaluation_outcome,reason,move_pct,
           peak_abs_move_pct,cumulative_notional,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            1,
            SESSION,
            SYMBOL,
            "up",
            None,
            "CONFIRMED",
            "QUALIFIED",
            "2026-08-31T20:05:03+00:00",
            "2026-08-31T20:05:04+00:00",
            "2026-08-31T20:00:00+00:00",
            "CANDIDATE",
            "confirmed",
            68.97,
            68.97,
            12_000_000,
            "sip",
            "alpaca",
            "5Min",
            "8aa7dd5",
            "life-run",
        ),
    )
    connection.execute(
        """
        INSERT INTO postmarket_candidate_mark_events
          (event_id,quality_version,candidate_stream,candidate_id,session,symbol,
           direction,checkpoint,target_ts_utc,status,detection_ts_utc,
           baseline_price,observed_bar_open_ts_utc,observed_at_utc,price,
           directional_return_pct,mfe_pct,mae_pct,time_to_mfe_minutes,
           bars_examined,data_feed,market_data_provider,bar_timeframe,
           code_version,run_id,recorded_at_utc,detail_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "mark-1",
            1,
            "marketwide",
            1,
            SESSION,
            SYMBOL,
            "up",
            "close",
            "2026-09-01T00:00:00+00:00",
            "AVAILABLE",
            "2026-08-31T20:05:03+00:00",
            1.47,
            "2026-08-31T23:55:00+00:00",
            "2026-09-01T00:00:01+00:00",
            1.60,
            8.84,
            10.0,
            -2.0,
            230,
            47,
            "sip",
            "alpaca",
            "5Min",
            "8aa7dd5",
            "mark-run",
            "2026-09-01T00:05:00+00:00",
            "{}",
        ),
    )
    connection.commit()
    connection.close()


def _seed_users(path: Path) -> None:
    connection = _database(path, USERS_SCHEMA)
    connection.execute(
        """
        INSERT INTO outbox
          (id,alert_id,chat_id,priority,text,reply_markup_json,status,attempts,
           next_attempt_at,leased_by,leased_at,created_at,delivered_at,last_error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "outbox-1",
            "postmarket-operator:v1:candidate:1",
            123,
            10,
            "owner only",
            None,
            "sent",
            1,
            "2026-08-31T20:05:05+00:00",
            None,
            None,
            "2026-08-31T20:05:05+00:00",
            "2026-08-31T20:05:06+00:00",
            None,
        ),
    )
    connection.commit()
    connection.close()


def test_complete_fixture_separates_observation_qualification_and_delivery(tmp_path):
    _seed_universe(tmp_path / "universe.db")
    _seed_journal(tmp_path / "journal.db")
    _seed_evaluations(tmp_path / "evaluations.db")
    _seed_postmarket(tmp_path / "postmarket_shadow.db")
    _seed_users(tmp_path / "users.db")

    report = reconstruct_signal_incident(
        symbol="gpro",
        session=SESSION,
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    assert report["symbol"] == SYMBOL
    assert report["market_session"]["is_session"] is True
    assert report["stages"]["universe_asset"]["status"] == STATUS_PRESENT
    assert report["stages"]["stage1_screening_events"]["rows"][0]["outcome"] == (
        "CANDIDATE_NOT_PROMOTED"
    )
    assert report["stages"]["rth_momentum_observations"]["rows"][0]["data_feed"] == "sip"
    assert report["stages"]["postmarket_discovery_candidates"]["rows"][0][
        "market_data_provider"
    ] == "alpaca"
    assert report["stages"]["postmarket_outcome_marks"]["rows"][0]["status"] == (
        "AVAILABLE"
    )
    assert report["conclusions"]["path_classification"] == (
        "QUALIFIED_SHADOW_CANDIDATE_RECORDED"
    )
    assert report["conclusions"]["delivery_classification"] == (
        "OWNER_OPERATOR_OUTBOX_EVIDENCE_PRESENT"
    )
    assert report["conclusions"]["caught_or_missed_verdict"] == (
        "BROAD_CAUGHT_OR_MISSED_CLAIM_NOT_PROVEN"
    )


def test_missing_databases_stay_unknown_instead_of_becoming_a_miss(tmp_path):
    report = reconstruct_signal_incident(
        symbol=SYMBOL,
        session=SESSION,
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    assert report["report_status"] == "degraded"
    assert report["stages"]["universe_asset"]["status"] == STATUS_DATABASE_MISSING
    assert report["conclusions"]["path_classification"] == (
        "UNKNOWN_NO_DURABLE_SYMBOL_PATH_EVIDENCE"
    )
    assert report["conclusions"]["caught_or_missed_verdict"] == (
        "BROAD_CAUGHT_OR_MISSED_CLAIM_NOT_PROVEN"
    )


def test_verified_screening_archive_is_used_when_live_ticks_are_absent(tmp_path):
    _seed_universe(tmp_path / "universe.db", with_event=False)
    archive_dir = tmp_path / "screening_archives"
    archived = archive_screening_session(
        tmp_path / "universe.db",
        archive_dir,
        session=SESSION,
        now=GENERATED,
    )
    (tmp_path / "universe.db").unlink()

    report = reconstruct_signal_incident(
        symbol=SYMBOL,
        session=SESSION,
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    stage = report["stages"]["stage1_screening"]
    assert stage["status"] == STATUS_PRESENT
    assert stage["source"]["sha256"] == archived.sha256
    assert stage["interpretations"][0]["interpretation"] == "QUIET_BY_INVARIANT"


def test_corrupt_archive_is_not_silently_treated_as_absent(tmp_path):
    archive_dir = tmp_path / "screening_archives"
    archive_dir.mkdir()
    corrupt = archive_dir / f"screening_{SESSION}_{'a' * 64}.jsonl.gz"
    corrupt.write_bytes(b"not gzip")

    report = reconstruct_signal_incident(
        symbol=SYMBOL,
        session=SESSION,
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    assert report["stages"]["stage1_screening"]["status"] == STATUS_ARCHIVE_CORRUPT


def test_market_calendar_records_early_close_and_rejects_holiday_claims(tmp_path):
    early_close = reconstruct_signal_incident(
        symbol=SYMBOL,
        session="2026-11-27",
        paths=_paths(tmp_path),
        generated_at=datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc),
    )
    holiday = reconstruct_signal_incident(
        symbol=SYMBOL,
        session="2026-08-30",
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    assert early_close["market_session"]["duration_minutes"] == 210
    assert early_close["market_session"]["is_final_at_generation"] is True
    assert holiday["market_session"]["is_session"] is False
    assert "market_session_not_xnys" in holiday["degraded_stages"]


def test_symlink_database_is_visible_as_unsafe(tmp_path):
    real = tmp_path / "real.db"
    sqlite3.connect(real).close()
    linked = tmp_path / "universe.db"
    linked.symlink_to(real)

    report = reconstruct_signal_incident(
        symbol=SYMBOL,
        session=SESSION,
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    assert report["stages"]["universe_asset"]["status"] == "DATABASE_UNSAFE"


def test_schema_drift_and_malformed_records_are_visible(tmp_path):
    universe = sqlite3.connect(tmp_path / "universe.db")
    universe.execute("CREATE TABLE assets (symbol TEXT PRIMARY KEY)")
    universe.execute("INSERT INTO assets VALUES (?)", (SYMBOL,))
    universe.commit()
    universe.close()

    postmarket = sqlite3.connect(tmp_path / "postmarket_shadow.db")
    postmarket.execute(
        """
        CREATE TABLE rth_momentum_observations (
          seq INTEGER, session TEXT, symbol TEXT, outcome TEXT,
          bar_open_ts_utc TEXT, sources_json TEXT, ranks_json TEXT,
          screen_evidence_json TEXT
        )
        """
    )
    postmarket.execute(
        "INSERT INTO rth_momentum_observations VALUES (?,?,?,?,?,?,?,?)",
        (1, SESSION, SYMBOL, "CANDIDATE", "naive-time", "{", "[]", "[]"),
    )
    postmarket.commit()
    postmarket.close()

    report = reconstruct_signal_incident(
        symbol=SYMBOL,
        session=SESSION,
        paths=_paths(tmp_path),
        generated_at=GENERATED,
    )

    assert report["stages"]["universe_asset"]["status"] == STATUS_SCHEMA_INCOMPATIBLE
    observation = report["stages"]["rth_momentum_observations"]
    assert observation["status"] == STATUS_PRESENT_WITH_ISSUES
    codes = {issue["code"] for issue in observation["quality"]["issues"]}
    assert "INVALID_OPTIONAL_UTC_TIMESTAMP" in codes
    assert "INVALID_JSON" in codes


def test_row_assessment_detects_duplicates_and_out_of_order_timestamps():
    quality = _assess_rows(
        [
            {"id": "same", "ts": "2026-08-31T20:10:00+00:00"},
            {"id": "same", "ts": "2026-08-31T20:05:00+00:00"},
        ],
        timestamp_fields=("ts",),
        identity_fields=("id",),
        ordered_timestamp_field="ts",
    )

    assert quality["duplicate_identity_count"] == 1
    assert quality["out_of_order_count"] == 1


def test_dirty_census_cannot_create_a_missed_direction_claim():
    stages = {
        "rth_missed_mover_census": {
            "status": STATUS_PRESENT,
            "rows": [
                {
                    "census_status": "degraded",
                    "invariant_ok": 0,
                    "data_status": "AVAILABLE",
                    "missed_directions_json": '["up"]',
                }
            ],
        }
    }

    result = _conclusions(stages)

    assert result["census_missed_directions"] == []
    assert result["untrusted_census_missed_directions"] == ["up"]
    assert result["caught_or_missed_verdict"] == (
        "BROAD_CAUGHT_OR_MISSED_CLAIM_NOT_PROVEN"
    )


def test_readonly_query_does_not_change_database_bytes(tmp_path):
    path = tmp_path / "source.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE evidence (id INTEGER, ts TEXT)")
    connection.execute(
        "INSERT INTO evidence VALUES (?,?)", (1, "2026-08-31T20:00:00+00:00")
    )
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with ReadonlyDatabase("evidence", path) as reader:
        stage = reader.query_stage(
            "evidence",
            scope="symbol",
            required={"evidence": ("id", "ts")},
            sql="SELECT * FROM evidence",
            timestamp_fields=("ts",),
            identity_fields=("id",),
        )
        assert reader.source["query_only"] is True
        assert stage["status"] == STATUS_PRESENT

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_artifact_publication_is_no_replace_and_digest_bound(tmp_path):
    report = {
        "generated_at_utc": "2026-09-01T05:00:00+00:00",
        "session": SESSION,
        "symbol": SYMBOL,
        "value": 1,
    }
    path, digest = write_reconstruction_artifact(report, tmp_path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert path.stat().st_mode & 0o222 == 0
    with pytest.raises(FileExistsError):
        write_reconstruction_artifact(report, tmp_path)


def test_cli_can_write_artifact_and_fail_closed_on_degraded_sources(tmp_path, capsys):
    output = tmp_path / "artifacts"
    exit_code = main(
        [
            SYMBOL,
            SESSION,
            "--data-dir",
            str(tmp_path / "missing"),
            "--output-dir",
            str(output),
            "--fail-on-degraded",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["report"]["report_status"] == "degraded"
    assert Path(payload["artifact"]).is_file()
