"""Tests for tradebot.status_page — the public status page's data layer
and renderer. Every number here must trace back to a real, reproducible
source (tradebot.incidents, tradebot.metrics, the journal) — see the
module docstring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradebot import incidents, metrics, status_page
from tradebot.detectors import Detection
from tradebot.journal import connect, set_no_trade, write_cluster

NOW = datetime(2026, 8, 6, 18, 0, tzinfo=timezone.utc)


def test_no_incidents_ever_reports_uptime_as_unknown_not_100_percent(tmp_path):
    """A brand-new install with zero tracked history must not fabricate
    a perfect uptime number it has no basis for."""
    conn = connect(":memory:")
    data = status_page.collect_status_data(
        conn, now=NOW, incidents_path=tmp_path / "incidents.jsonl", metrics_path=tmp_path / "metrics.json",
    )
    assert data.uptime_pct is None
    assert data.tracked_since is None


def test_uptime_reflects_incident_time_as_a_fraction_of_tracked_time(tmp_path):
    incidents_path = tmp_path / "incidents.jsonl"
    tracked_since = NOW - timedelta(hours=100)
    incidents.open_incident("heartbeat_stale", "feed down", tracked_since, path=incidents_path)
    incidents.close_incident("heartbeat_stale", tracked_since + timedelta(hours=1), path=incidents_path)

    conn = connect(":memory:")
    data = status_page.collect_status_data(
        conn, now=NOW, incidents_path=incidents_path, metrics_path=tmp_path / "metrics.json",
    )
    # 1 hour down out of 100 hours tracked -> 99% uptime
    assert abs(data.uptime_pct - 99.0) < 0.01
    assert data.tracked_since == tracked_since


def test_an_ongoing_incident_counts_downtime_up_to_now(tmp_path):
    incidents_path = tmp_path / "incidents.jsonl"
    started = NOW - timedelta(hours=10)
    incidents.open_incident("halt", "fixing a bug", started, path=incidents_path)

    conn = connect(":memory:")
    data = status_page.collect_status_data(
        conn, now=NOW, incidents_path=incidents_path, metrics_path=tmp_path / "metrics.json",
    )
    assert data.incidents[0]["ended_at"] is None
    # entire 10-hour tracked window is inside the still-open incident -> 0% uptime
    assert abs(data.uptime_pct - 0.0) < 0.01


def test_missed_alerts_are_read_from_the_real_validator_rejection_counters(tmp_path):
    metrics_path = tmp_path / "metrics.json"
    metrics.increment("validator_rejection", path=metrics_path, rule="stale_quote")
    metrics.increment("validator_rejection", path=metrics_path, rule="stale_quote")
    metrics.increment("validator_rejection", path=metrics_path, rule="crossed_quote")

    conn = connect(":memory:")
    data = status_page.collect_status_data(
        conn, now=NOW, incidents_path=tmp_path / "incidents.jsonl", metrics_path=metrics_path,
    )
    assert data.missed_alerts_by_rule == {"stale_quote": 2, "crossed_quote": 1}
    assert data.total_missed_alerts == 3


def test_operational_failure_table_surfaces_every_recorded_failure_family(tmp_path):
    metrics_path = tmp_path / "metrics.json"
    metrics.increment("dedup_check_failed", path=metrics_path)
    metrics.increment("decision_event_write_failed", path=metrics_path, stage="dedup")
    metrics.increment("evaluation_write_failed", path=metrics_path, outcome="NO_DETECTION")
    metrics.increment("screening_persist_failed", path=metrics_path)
    metrics.increment("contract_outcome_backfill_failed", path=metrics_path, stage="forward_mid")
    metrics.increment("data_health_suppression", path=metrics_path, reason="bar_gap")
    metrics.increment("duplicate_suppression", path=metrics_path, symbol="TSLA")
    metrics.increment("duplicate_suppression", path=metrics_path, symbol="AAPL")
    metrics.increment("event_window_suppression", path=metrics_path, kind="earnings")
    metrics.increment("event_window_downgrade", path=metrics_path, kind="earnings")
    metrics.increment("suppression", path=metrics_path, category="data_integrity")
    metrics.increment("plausibility_floor_rejection", path=metrics_path, stage="baseline")
    metrics.increment("provider_error", path=metrics_path, provider="example")
    metrics.increment("universe_stage1_runs", path=metrics_path)
    metrics.increment("validator_rejection", path=metrics_path, rule="stale_quote")

    conn = connect(":memory:")
    data = status_page.collect_status_data(
        conn,
        now=NOW,
        incidents_path=tmp_path / "incidents.jsonl",
        metrics_path=metrics_path,
    )

    assert data.operational_failures_by_family == {
        "contract_outcome_backfill_failed": 1,
        "data_health_suppression": 1,
        "decision_event_write_failed": 1,
        "dedup_check_failed": 1,
        "duplicate_suppression": 2,
        "evaluation_write_failed": 1,
        "event_window_downgrade": 1,
        "event_window_suppression": 1,
        "plausibility_floor_rejection": 1,
        "provider_error": 1,
        "screening_persist_failed": 1,
        "suppression": 1,
    }
    assert "universe_stage1_runs" not in data.operational_failures_by_family
    assert not any(
        key.startswith("validator_rejection")
        for key in data.operational_failures_by_family
    )
    assert data.missed_alerts_by_rule == {"stale_quote": 1}


def test_alerts_vs_no_trade_counter_matches_performance_track_record(tmp_path):
    conn = connect(":memory:")
    for i in range(6):
        did = write_cluster(
            conn, session="2026-08-06", symbol="TSLA", ts_utc=(NOW + timedelta(minutes=i)).isoformat(),
            kinds="gap", headlines="h", score=5.0, close=100.0, atr14=1.0, trend="up",
            detections=[Detection("TSLA", "gap", NOW, 5.0, "h", {})], code_version_str="abc", alerted=True,
        )
        set_no_trade(conn, did, i < 2)
        conn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, 101))
    conn.commit()

    data = status_page.collect_status_data(
        conn, now=NOW, incidents_path=tmp_path / "incidents.jsonl", metrics_path=tmp_path / "metrics.json",
    )
    assert data.total_alerts_published == 6
    assert data.total_no_trade == 2
    assert data.no_trade_tracked_count == 6


def test_render_status_page_includes_the_beta_label_and_core_sections():
    data = status_page.StatusPageData(
        generated_at=NOW, uptime_pct=99.5, tracked_since=NOW - timedelta(days=10), total_incident_seconds=3600,
        incidents=[{"kind": "halt", "detail": "fixing a bug", "started_at": NOW.isoformat(), "ended_at": None}],
        missed_alerts_by_rule={"stale_quote": 2}, total_missed_alerts=2,
        operational_failures_by_family={"dedup_check_failed": 1},
        total_alerts_published=10, total_no_trade=3, no_trade_tracked_count=10,
    )
    html_out = status_page.render_status_page(data)
    assert "BETA" in html_out
    assert "99.50%" in html_out
    assert "Incident log" in html_out
    assert "stale_quote" in html_out
    assert "Operational failures, suppressions, and downgrades" in html_out
    assert "dedup_check_failed" in html_out
    assert "not added into a fabricated incident total" in html_out
    assert "3 of 10" in html_out
    assert "ONGOING" in html_out


def test_render_status_page_never_fabricates_100_percent_uptime():
    data = status_page.StatusPageData(
        generated_at=NOW, uptime_pct=None, tracked_since=None, total_incident_seconds=0,
        incidents=[], missed_alerts_by_rule={}, total_missed_alerts=0,
        operational_failures_by_family={},
        total_alerts_published=0, total_no_trade=0, no_trade_tracked_count=0,
    )
    html_out = status_page.render_status_page(data)
    assert '<div class="stat">100' not in html_out
    assert "not enough history yet" in html_out


def test_generate_status_page_writes_a_real_file(tmp_path):
    conn = connect(":memory:")
    output_path = status_page.generate_status_page(
        conn, output_path=tmp_path / "status.html", now=NOW,
        incidents_path=tmp_path / "incidents.jsonl", metrics_path=tmp_path / "metrics.json",
    )
    assert output_path.exists()
    assert "<html" in output_path.read_text()
