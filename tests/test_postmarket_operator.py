from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tradebot.postmarket_context import ensure_context_schema
from tradebot.postmarket_discovery import connect as connect_discovery
from tradebot.postmarket_lifecycle import ensure_lifecycle_schema
from tradebot.postmarket_operator import (
    load_session_opportunities,
    operator_alert_id,
    render_operator_opportunity,
    run_operator_cycle,
    validate_operator_chat,
)
from tradebot.postmarket_operator_health import evaluate_operator_health
from tradebot.postmarket_operator_shadow import operator_alerts_enabled, operator_chat_id
from tradebot.postmarket_rank import ensure_rank_schema
from tradebot.telegram_bot.db import connect as connect_users


SESSION = date(2026, 8, 31)
NOW = datetime(2026, 8, 31, 20, 16, tzinfo=timezone.utc)


def _shadow(tmp_path: Path):
    conn = connect_discovery(tmp_path / "postmarket.db")
    ensure_lifecycle_schema(conn)
    ensure_context_schema(conn)
    ensure_rank_schema(conn)
    return conn


def _users(tmp_path: Path, *, admin: bool = True):
    conn = connect_users(tmp_path / "users.db")
    conn.execute(
        "INSERT INTO users (telegram_user_id,chat_id,created_at,is_admin) VALUES (?,?,?,?)",
        (1234, 9876, NOW.isoformat(), int(admin)),
    )
    conn.commit()
    return conn


def _candidate(
    conn,
    *,
    symbol: str = "GPRO",
    run_id: str = "run-1",
    first_detected_at: str = "2026-08-31T20:15:00+00:00",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO postmarket_discovery_candidates
          (session,symbol,event_date,direction,discovery_version,first_detected_at,
           bar_open_ts_utc,rth_close,close,move_pct,cumulative_volume,
           cumulative_notional,sources_json,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), symbol, SESSION.isoformat(), "up", 2,
            first_detected_at, "2026-08-31T20:10:00+00:00", 0.8762, 1.4722,
            68.02, 220_002_730, 244_000_000.0, '["market_gainer"]', "sip",
            "alpaca", "5Min", "abc1234", run_id,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_switch_and_chat_id_are_fail_closed():
    for raw in (None, "", "0", "false", "off"):
        assert operator_alerts_enabled(raw) is False
    for raw in ("1", "true", "yes", "on"):
        assert operator_alerts_enabled(raw) is True
    with pytest.raises(ValueError):
        operator_alerts_enabled("maybe")
    with pytest.raises(ValueError):
        operator_chat_id("")
    with pytest.raises(ValueError):
        operator_chat_id("not-an-id")
    assert operator_chat_id("-100123") == -100123


def test_owner_cycle_enqueues_one_deterministic_admin_only_alert(tmp_path):
    shadow = _shadow(tmp_path)
    users = _users(tmp_path)
    candidate_id = _candidate(shadow)

    first = run_operator_cycle(
        shadow, users, session=SESSION, chat_id=9876, now=NOW,
    )
    second = run_operator_cycle(
        shadow, users, session=SESSION, chat_id=9876, now=NOW,
    )

    assert first.alerts_enqueued == 1
    assert first.eligible_candidates == 1
    assert second.alerts_enqueued == 0
    assert second.alerts_deduplicated == 1
    row = users.execute(
        "SELECT alert_id,chat_id,priority,text,status FROM outbox"
    ).fetchone()
    assert row[0] == operator_alert_id(candidate_id)
    assert row[1:3] == (9876, 0)
    assert "GPRO · +68.02% from RTH close" in row[3]
    assert "LOW_PRICE, EXTREME_MOVE, CONTEXT_PENDING" in row[3]
    assert "Owner-only shadow intelligence — not advice. No order was placed." in row[3]
    assert row[4] == "pending"


def test_cycle_does_not_starve_later_candidates_after_deduplication(tmp_path):
    shadow = _shadow(tmp_path)
    users = _users(tmp_path)
    first_id = _candidate(shadow, symbol="XYZ", run_id="run-1")
    second_id = _candidate(
        shadow, symbol="GPRO", run_id="run-2",
        first_detected_at="2026-08-31T20:15:01+00:00",
    )
    users.execute(
        "INSERT INTO outbox (id,alert_id,chat_id,priority,text,status,attempts,next_attempt_at,created_at) "
        "VALUES ('old',?,9876,0,'old','delivered',0,?,?)",
        (operator_alert_id(first_id), NOW.isoformat(), NOW.isoformat()),
    )
    users.commit()

    result = run_operator_cycle(
        shadow, users, session=SESSION, chat_id=9876, now=NOW, limit=1,
    )
    assert result.alerts_enqueued == 1
    assert result.alerts_deduplicated == 1
    assert users.execute(
        "SELECT COUNT(*) FROM outbox WHERE alert_id=?", (operator_alert_id(second_id),)
    ).fetchone()[0] == 1


def test_stale_candidate_and_non_admin_fail_closed(tmp_path):
    shadow = _shadow(tmp_path)
    _candidate(shadow, first_detected_at="2026-08-31T19:00:00+00:00")
    users = _users(tmp_path, admin=False)
    with pytest.raises(ValueError, match="administrator"):
        validate_operator_chat(users, 9876)
    users.execute("UPDATE users SET is_admin=1")
    users.commit()
    result = run_operator_cycle(
        shadow, users, session=SESSION, chat_id=9876, now=NOW,
    )
    assert result.alerts_enqueued == 0
    assert result.stale_candidates == 1


def test_render_uses_persisted_candidate_provenance(tmp_path):
    shadow = _shadow(tmp_path)
    _candidate(shadow)
    candidate = load_session_opportunities(shadow, session=SESSION)[0]
    rendered = render_operator_opportunity(candidate, now=NOW)
    assert "alpaca/sip · 5Min completed bars" in rendered
    assert "Postmarket notional: $244.00M" in rendered
    assert "First detected: 16:15:00 ET · age 60s" in rendered


def test_operator_health_requires_fresh_exact_revision_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({
        "ts_utc": NOW.isoformat(),
        "status": "ok",
        "enabled": True,
        "observer": "postmarket-owner-operator-shadow",
        "code_version": "abc1234",
    }))
    result = evaluate_operator_health(
        heartbeat, enabled=True, expected_revision="abc1234", now=NOW,
    )
    assert result.healthy is True
    assert evaluate_operator_health(
        heartbeat, enabled=True, expected_revision="other", now=NOW,
    ).healthy is False
    assert evaluate_operator_health(
        tmp_path / "missing.json", enabled=False, expected_revision="abc1234", now=NOW,
    ).healthy is True


def test_compose_service_is_owner_only_default_off():
    compose = Path("docker-compose.yml").read_text()
    block = compose.split("  postmarket-operator:", 1)[1].split(
        "\n  postmarket-customer-dry-run:", 1
    )[0]
    assert "POSTMARKET_OPERATOR_ALERTS_ENABLED:-0" in block
    assert "POSTMARKET_OPERATOR_CHAT_ID:-" in block
    assert "tradebot.postmarket_operator_shadow" in block
    assert "tradebot.postmarket_operator_health" in block
    assert "- worker" in block
    assert "postmarket-discovery" not in block
    assert "\n      - bot" not in block
