"""Service-level conservation, isolation, kill-switch, and architecture tests."""
from __future__ import annotations

import ast
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import postmarket_shadow as shadow_module
from tradebot.detectors import Bar
from tradebot.events import add_event_window
from tradebot.journal import connect as connect_journal
from tradebot.marketdata import IntradaySessionBars
from tradebot.postmarket import connect as connect_shadow
from tradebot.postmarket_shadow import (
    audit_heartbeat_fields,
    idle_sleep_seconds,
    postmarket_is_active,
    postmarket_window,
    run_shadow_tick,
    shadow_enabled,
    write_heartbeat_atomic,
)


SESSION = date(2026, 8, 26)
CLOSE = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def _bar(symbol, ts, close, volume=1_000_000):
    return Bar(symbol, ts, close, close, close, close, volume)


class _MarketData:
    def __init__(self, symbol, session):
        if symbol == "BROKEN":
            raise RuntimeError("vendor down")
        self.symbol = symbol

    def intraday_snapshot(self, symbol, session):
        closes = [110, 111] if symbol == "MOVER" else [101, 102]
        return IntradaySessionBars(
            premarket=(),
            rth=(_bar(symbol, CLOSE - timedelta(minutes=5), 100),),
            postmarket=tuple(
                _bar(symbol, CLOSE + timedelta(minutes=5 * index), close)
                for index, close in enumerate(closes)
            ),
        )


def _journal_with_symbols(tmp_path, symbols):
    conn = connect_journal(tmp_path / "journal.db")
    for symbol in symbols:
        add_event_window(
            conn,
            symbol=symbol,
            kind="earnings",
            start_utc=CLOSE - timedelta(hours=6, minutes=30),
            end_utc=CLOSE,
            severity="context",
            source="nasdaq_earnings",
            detail=f"{symbol} earnings (after-hours), reported {SESSION}",
            event_date=SESSION,
            event_timing="after-hours",
        )
    return conn


def test_tick_conserves_every_symbol_and_isolates_vendor_failure(tmp_path):
    journal = _journal_with_symbols(tmp_path, ["MOVER", "QUIET", "BROKEN"])
    shadow = connect_shadow(tmp_path / "postmarket.db")

    result, evaluations = run_shadow_tick(
        journal,
        shadow,
        now=CLOSE + timedelta(minutes=10),
        run_id="run-1",
        version="abc123",
        data_feed="sip",
        market_data_factory=_MarketData,
    )

    assert result.scheduled_symbols == result.evaluated_symbols == 3
    assert result.candidate_observations == result.new_candidates == 1
    assert result.error_count == 1
    assert {evaluation.symbol for evaluation in evaluations} == {"MOVER", "QUIET", "BROKEN"}
    assert shadow.execute(
        "SELECT invariant_ok,scheduled_symbols,evaluated_symbols,error_count "
        "FROM postmarket_ticks WHERE tick_id=?",
        (result.tick_id,),
    ).fetchone() == (1, 3, 3, 1)
    assert dict(shadow.execute(
        "SELECT symbol,outcome FROM postmarket_observations WHERE tick_id=?",
        (result.tick_id,),
    ).fetchall()) == {
        "BROKEN": "FETCH_ERROR",
        "MOVER": "CANDIDATE",
        "QUIET": "BELOW_MOVE",
    }


def test_service_import_graph_has_no_delivery_dependency():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_shadow.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = ("tradebot.alerts", "tradebot.telegram_bot", "tradebot.order", "tradebot.broker")
    assert not any(module.startswith(forbidden) for module in imports)


def test_compose_wires_default_off_shadow_service_and_market_aware_health():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "command: python -m tradebot.postmarket_shadow" in compose
    assert "POSTMARKET_SHADOW_ENABLED: ${POSTMARKET_SHADOW_ENABLED:-0}" in compose
    assert "python\", \"-m\", \"tradebot.postmarket_health" in compose


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_kill_switch_true_values(raw):
    assert shadow_enabled(raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off", ""])
def test_kill_switch_false_values(raw):
    assert shadow_enabled(raw) is False


def test_kill_switch_rejects_ambiguous_configuration():
    with pytest.raises(ValueError, match="POSTMARKET_SHADOW_ENABLED"):
        shadow_enabled("maybe")


def test_real_calendar_window_includes_early_close_and_final_bar_grace():
    regular = datetime(2026, 8, 26, 20, 30, tzinfo=timezone.utc)
    early = datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)
    assert postmarket_is_active(regular) is True
    assert postmarket_is_active(early) is True
    session, close, end = postmarket_window(early)
    assert session == date(2026, 11, 27)
    assert close == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 28, 1, 5, tzinfo=timezone.utc)
    assert postmarket_is_active(end + timedelta(seconds=1)) is False


def test_idle_sleep_aligns_to_regular_and_early_close_without_crossing_start():
    regular_preclose = datetime(2026, 8, 26, 19, 58, tzinfo=timezone.utc)
    early_preclose = datetime(2026, 11, 27, 17, 59, 30, tzinfo=timezone.utc)
    far_from_close = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)

    assert idle_sleep_seconds(regular_preclose) == 120
    assert idle_sleep_seconds(early_preclose) == 30
    assert idle_sleep_seconds(far_from_close) == 300


def test_atomic_heartbeat_replaces_complete_json_and_leaves_no_temp(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text('{"old":true}', encoding="utf-8")
    payload = {"ts_utc": CLOSE.isoformat(), "status": "ok"}

    write_heartbeat_atomic(path, payload)

    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert list(tmp_path.glob(".heartbeat.json.*.tmp")) == []


def test_idle_heartbeat_keeps_latest_daily_audit_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_module, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(shadow_module, "write_due_audits", lambda now: ())
    (tmp_path / "postmarket_audit_2026-08-26_v1.json").write_text(
        json.dumps(
            {
                "session": "2026-08-26",
                "operational_clean": False,
                "issues": [{"code": "COVERAGE_STARTED_LATE"}],
            }
        ),
        encoding="utf-8",
    )

    fields = audit_heartbeat_fields(CLOSE + timedelta(hours=5))

    assert fields == {
        "audit_status": "current",
        "audits_written": 0,
        "latest_audit": {
            "session": "2026-08-26",
            "operational_clean": False,
            "issue_codes": ["COVERAGE_STARTED_LATE"],
        },
    }


def test_corrupt_daily_audit_is_loud_in_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_module, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(shadow_module, "write_due_audits", lambda now: ())
    (tmp_path / "postmarket_audit_2026-08-26_v1.json").write_text(
        "not-json", encoding="utf-8"
    )

    fields = audit_heartbeat_fields(CLOSE + timedelta(hours=5))

    assert fields["audit_status"] == "error"
    assert fields["audit_error"].startswith("JSONDecodeError:")
