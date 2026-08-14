"""Trade Journal: end-to-end tests through Flask's test client, same
discipline as test_api.py — the wire interface, not internal calls.

The non-negotiables this file exists to prove, per the feature review:
- User isolation at the QUERY level on every /journal route: account B
  can never read, edit, or delete account A's entries, and a wrong-owner
  id is indistinguishable from a nonexistent one (404, no oracle).
- Journal-created rows always carry a NEGATIVE sentinel
  telegram_user_id, so no legacy telegram-scoped query (list_trades,
  personal_stats, the /took//closed flows) can ever see them. This
  invariant regressed once during review (commit a614fbe wrote real
  telegram ids for linked accounts); the test here is the guard.
- ET day bucketing survives DST transitions and the ET/UTC midnight gap
  (the exact bug class docs/BACKLOG.md records for monthly_recap).
- The taken_at wire format: datetime.fromisoformat is
  version-sensitive ('Z' parses on 3.11+, not 3.9) — the documented
  wire format '+00:00' must work everywhere, and this file records the
  actual behavior of 'Z' on the running interpreter so a runtime
  upgrade that silently changes it is visible in the test run.
"""
from __future__ import annotations

import csv
import io
import sys
from datetime import datetime, timezone

import pytest

from tradebot.api.app import create_app
from tradebot.email_sender import DevEmailSender
from tradebot.telegram_bot import db as users_db


@pytest.fixture(autouse=True)
def _session_secret_key(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-only-secret-key-do-not-use-in-production")


@pytest.fixture
def app(tmp_path):
    application = create_app(
        users_db_path=tmp_path / "users.db",
        journal_db_path=tmp_path / "journal.db",
    )
    application.config["TESTING"] = True
    application.email_sender = DevEmailSender()
    return application


NOW_ISO = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc).isoformat()


def _make_account(app, account_id: str, email: str) -> str:
    app.users_conn.execute(
        "INSERT INTO accounts (id, email, plan, created_at) VALUES (?, ?, 'beta', ?)",
        (account_id, email, NOW_ISO),
    )
    app.users_conn.commit()
    return account_id


def _client_for(app, account_id: str):
    client = app.test_client()
    with client.session_transaction() as session:
        session["account_id"] = account_id
    return client


def _link_telegram(app, account_id: str, telegram_user_id: int) -> None:
    app.users_conn.execute(
        "INSERT INTO users (telegram_user_id, chat_id, created_at) VALUES (?, ?, ?)",
        (telegram_user_id, telegram_user_id, NOW_ISO),
    )
    app.users_conn.execute(
        "INSERT INTO linked_identities (account_id, provider, provider_user_id, linked_at) "
        "VALUES (?, 'telegram', ?, ?)",
        (account_id, str(telegram_user_id), NOW_ISO),
    )
    app.users_conn.commit()


def _seed_detection(app, detection_id: str, *, symbol: str = "NVDA", tier: str = "high") -> None:
    app.journal_conn.execute(
        "INSERT INTO detections (id, ts_utc, session, symbol, kinds, headlines, score, tier, trend) "
        "VALUES (?, ?, '2026-08-13', ?, 'vwap_reclaim', 'reclaimed VWAP on volume', 7.0, ?, 'up')",
        (detection_id, NOW_ISO, symbol, tier),
    )
    app.journal_conn.commit()


def _seed_delivered_alert(app, detection_id: str, chat_id: int, *, suffix: str = "") -> None:
    app.users_conn.execute(
        "INSERT INTO outbox (id, alert_id, chat_id, priority, text, status, next_attempt_at, "
        "created_at, delivered_at) VALUES (?, ?, ?, 1, 'alert text', 'delivered', ?, ?, ?)",
        (f"outbox-{detection_id}{suffix}-{chat_id}", f"{detection_id}{suffix}", chat_id, NOW_ISO, NOW_ISO, NOW_ISO),
    )
    app.users_conn.commit()


# ---------------------------------------------------------------------------
# Auth and isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/journal/summary"),
        ("get", "/journal/calendar?month=2026-08"),
        ("get", "/journal/trades"),
        ("post", "/journal/trades"),
        ("patch", "/journal/trades/some-id"),
        ("delete", "/journal/trades/some-id"),
        ("get", "/journal/export.csv"),
        ("get", "/journal/linkable-signals"),
    ],
)
def test_every_journal_route_requires_a_session(app, method, path):
    response = getattr(app.test_client(), method)(path)
    assert response.status_code == 401


def test_user_b_cannot_read_edit_or_delete_user_a_trades(app):
    _make_account(app, "acct-a", "a@example.com")
    _make_account(app, "acct-b", "b@example.com")
    client_a = _client_for(app, "acct-a")
    client_b = _client_for(app, "acct-b")

    trade = client_a.post(
        "/journal/trades", json={"symbol": "SPY", "direction": "long", "pnl_cents": 5000, "note": "mine"}
    ).get_json()["trade"]

    # B's views are empty; A's trade id resolves for B exactly like a
    # nonexistent id would (404, no oracle), and nothing changed for A.
    assert client_b.get("/journal/trades").get_json()["trades"] == []
    assert client_b.get("/journal/summary").get_json()["summary"]["all_time"]["trade_count"] == 0
    assert client_b.get("/journal/calendar?month=2026-08").get_json()["days"] == {}
    assert b"SPY" not in client_b.get("/journal/export.csv").data
    assert client_b.patch(f"/journal/trades/{trade['id']}", json={"note": "not mine"}).status_code == 404
    assert client_b.delete(f"/journal/trades/{trade['id']}").status_code == 404
    unknown = client_b.patch("/journal/trades/no-such-id", json={"note": "x"}).status_code
    assert unknown == 404  # wrong-owner and unknown are the same status
    refetched = client_a.get("/journal/trades").get_json()["trades"][0]
    assert refetched["note"] == "mine"


def test_isolation_is_enforced_in_the_sql_not_just_the_route(app):
    """Belt-and-suspenders proof at the layer below the API: the db
    functions themselves refuse a wrong account_id even when called
    directly, because the scope predicate lives in each statement's own
    WHERE clause."""
    _make_account(app, "acct-a", "a@example.com")
    _make_account(app, "acct-b", "b@example.com")
    trade = users_db.create_journal_trade(
        app.users_conn, "acct-a", symbol="SPY", taken_at=datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)
    )
    assert users_db.get_journal_trade(app.users_conn, "acct-b", trade.id) is None
    assert users_db.update_journal_trade(app.users_conn, "acct-b", trade.id, note="stolen") is None
    assert users_db.delete_journal_trade(app.users_conn, "acct-b", trade.id) is False
    assert users_db.get_journal_trade(app.users_conn, "acct-a", trade.id) is not None


# ---------------------------------------------------------------------------
# Sentinel invariant (the a614fbe regression guard)
# ---------------------------------------------------------------------------


def test_journal_rows_always_write_a_negative_sentinel_telegram_id(app):
    _make_account(app, "acct-web", "web@example.com")
    _make_account(app, "acct-linked", "linked@example.com")
    _link_telegram(app, "acct-linked", 555_111_222)

    for account_id in ("acct-web", "acct-linked"):
        client = _client_for(app, account_id)
        trade = client.post("/journal/trades", json={"symbol": "SPY"}).get_json()["trade"]
        row = app.users_conn.execute(
            "SELECT telegram_user_id FROM user_trades WHERE id = ?", (trade["id"],)
        ).fetchone()
        # The invariant: even for an account with a REAL linked telegram
        # id, a journal-native row must carry a negative sentinel --
        # writing the real id leaks web entries into every legacy
        # telegram-scoped aggregate.
        assert row[0] < 0, f"journal row for {account_id} wrote telegram_user_id={row[0]}"


def test_linked_accounts_web_entries_never_reach_legacy_telegram_queries(app):
    telegram_id = 555_111_222
    _make_account(app, "acct-linked", "linked@example.com")
    _link_telegram(app, "acct-linked", telegram_id)
    legacy = users_db.log_took(
        app.users_conn, telegram_id, detection_id=None, symbol="NVDA",
        taken_at=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc),
    )
    users_db.log_closed(
        app.users_conn, legacy.id, exit_price=110.0, closed_at=datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc)
    )

    client = _client_for(app, "acct-linked")
    client.post("/journal/trades", json={"symbol": "AAPL", "pnl_cents": 5000})
    client.post("/journal/trades", json={"symbol": "QQQ", "is_skip": True, "skip_reason": "chop"})

    legacy_trades = users_db.list_trades(app.users_conn, telegram_id)
    assert [t.symbol for t in legacy_trades] == ["NVDA"]
    assert users_db.personal_stats(app.users_conn, telegram_id)["total_trades"] == 1
    # while the journal view unifies both origins
    journal_symbols = {t["symbol"] for t in client.get("/journal/trades").get_json()["trades"]}
    assert journal_symbols == {"NVDA", "AAPL", "QQQ"}


# ---------------------------------------------------------------------------
# ET day bucketing: midnight gap and DST transitions
# ---------------------------------------------------------------------------


def test_late_evening_et_trade_stays_on_its_et_day(app):
    """22:00 ET on Aug 12 is 02:00 UTC on Aug 13 -- raw-UTC bucketing
    (monthly_recap's known bug) would put this on the 13th. (Timestamps
    here must be in the past: the API rejects future taken_at.)"""
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    client.post(
        "/journal/trades",
        json={"symbol": "SPY", "pnl_cents": 100, "taken_at": "2026-08-13T02:00:00+00:00"},
    )
    days = client.get("/journal/calendar?month=2026-08").get_json()["days"]
    assert list(days) == ["2026-08-12"]
    assert client.get("/journal/trades?date=2026-08-12").get_json()["trades"] != []
    assert client.get("/journal/trades?date=2026-08-13").get_json()["trades"] == []


def test_dst_spring_forward_buckets_to_the_correct_et_day(app):
    """2026-03-08: 02:00 EST jumps to 03:00 EDT. Instants on both sides
    of the gap belong to March 8 ET; a fixed -5 offset would push the
    post-transition instant's ET clock an hour off."""
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    # 06:59 UTC = 01:59 EST (pre-gap); 07:30 UTC = 03:30 EDT (post-gap).
    for ts in ("2026-03-08T06:59:00+00:00", "2026-03-08T07:30:00+00:00"):
        client.post("/journal/trades", json={"symbol": "SPY", "pnl_cents": 100, "taken_at": ts})
    # 03:30 UTC on March 9 = 23:30 EDT March 8: crosses UTC midnight too.
    client.post(
        "/journal/trades", json={"symbol": "QQQ", "pnl_cents": 100, "taken_at": "2026-03-09T03:30:00+00:00"}
    )
    days = client.get("/journal/calendar?month=2026-03").get_json()["days"]
    assert set(days) == {"2026-03-08"}
    assert days["2026-03-08"]["trade_count"] == 3


def test_dst_fall_back_buckets_to_the_correct_et_day(app):
    """2025-11-02: 02:00 EDT falls back to 01:00 EST -- the 1am hour
    happens twice. Both passes are November 2 ET, and late evening EST
    that lands past UTC midnight still belongs to November 2. (The 2025
    transition, not 2026's: November 2026 is in the future relative to
    the frozen product clock and the API rejects future taken_at.)"""
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    # 05:30 UTC = 01:30 EDT (first pass); 06:30 UTC = 01:30 EST (second).
    for ts in ("2025-11-02T05:30:00+00:00", "2025-11-02T06:30:00+00:00"):
        client.post("/journal/trades", json={"symbol": "SPY", "pnl_cents": 100, "taken_at": ts})
    # 04:59 UTC on Nov 3 = 23:59 EST Nov 2. Under the pre-transition -4
    # offset this instant would read as Nov 3 00:59 -- the exact
    # misbucket a fixed-offset conversion produces half the year.
    client.post(
        "/journal/trades", json={"symbol": "QQQ", "pnl_cents": 100, "taken_at": "2025-11-03T04:59:00+00:00"}
    )
    days = client.get("/journal/calendar?month=2025-11").get_json()["days"]
    assert set(days) == {"2025-11-02"}
    assert days["2025-11-02"]["trade_count"] == 3


# ---------------------------------------------------------------------------
# taken_at wire format and fromisoformat version sensitivity
# ---------------------------------------------------------------------------


def test_documented_wire_format_parses_everywhere(app):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    response = client.post(
        "/journal/trades", json={"symbol": "SPY", "taken_at": "2026-08-13T14:30:00+00:00"}
    )
    assert response.status_code == 201


def test_z_suffix_behavior_matches_the_running_interpreter(app):
    """datetime.fromisoformat accepts a trailing 'Z' only from 3.11.
    The frontend deliberately sends '+00:00' (journalFormat.toApiIso)
    because of this; this test pins the actual behavior per runtime so
    an interpreter upgrade changes a visible assertion, not silently."""
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    response = client.post("/journal/trades", json={"symbol": "SPY", "taken_at": "2026-08-13T14:30:00Z"})
    if sys.version_info >= (3, 11):
        assert response.status_code == 201
    else:
        assert response.status_code == 400


def test_naive_and_future_timestamps_are_rejected(app):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    naive = client.post("/journal/trades", json={"symbol": "SPY", "taken_at": "2026-08-13T14:30:00"})
    assert naive.status_code == 400
    future = client.post("/journal/trades", json={"symbol": "SPY", "taken_at": "2030-01-01T00:00:00+00:00"})
    assert future.status_code == 400


# ---------------------------------------------------------------------------
# Validation and money handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"symbol": ""}, "empty symbol"),
        ({"symbol": "WAYTOOLONGSYMBOL"}, "symbol too long"),
        ({"symbol": "SPY", "pnl_cents": 1.5}, "float money"),
        ({"symbol": "SPY", "pnl_cents": "100"}, "string money"),
        ({"symbol": "SPY", "pnl_cents": True}, "bool is not money"),
        ({"symbol": "SPY", "pnl_cents": 10**12}, "out of range"),
        ({"symbol": "SPY", "direction": "sideways"}, "unknown direction"),
        ({"symbol": "SPY", "source": "psychic"}, "unknown source"),
        ({"symbol": "SPY", "pnl_cent": 100}, "unknown field"),
        ({"symbol": "SPY", "detection_snapshot": {"tier": "high"}}, "client-supplied snapshot"),
    ],
)
def test_invalid_create_payloads_are_400(app, payload, reason):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    assert client.post("/journal/trades", json=payload).status_code == 400, reason


def test_patch_rejects_unknown_and_create_only_fields(app):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    trade = client.post("/journal/trades", json={"symbol": "SPY"}).get_json()["trade"]
    assert client.patch(f"/journal/trades/{trade['id']}", json={"hax": 1}).status_code == 400
    assert client.patch(f"/journal/trades/{trade['id']}", json={"is_skip": True}).status_code == 400
    assert client.patch(f"/journal/trades/{trade['id']}", json={}).status_code == 400


def test_pnl_is_signed_integer_cents_end_to_end(app):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    client.post("/journal/trades", json={"symbol": "WIN", "pnl_cents": 12550})
    client.post("/journal/trades", json={"symbol": "LOSS", "pnl_cents": -4000})
    client.post("/journal/trades", json={"symbol": "FLAT", "pnl_cents": 0})
    summary = client.get("/journal/summary").get_json()["summary"]["all_time"]
    assert summary == {"pnl_cents": 8550, "trade_count": 3, "wins": 1, "losses": 1}
    # zero P&L counts as a trade but neither a win nor a loss


# ---------------------------------------------------------------------------
# Pass (skip) entries
# ---------------------------------------------------------------------------


def test_a_pass_entry_records_reflection_without_pnl(app):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    response = client.post(
        "/journal/trades",
        json={"symbol": "QQQ", "is_skip": True, "skip_reason": "spread too wide", "pnl_cents": 999},
    )
    trade = response.get_json()["trade"]
    assert response.status_code == 201
    assert trade["status"] == "skipped"
    assert trade["is_skip"] is True
    assert trade["pnl_cents"] is None  # a pass never carries P&L, even if sent
    assert trade["skip_reason"] == "spread too wide"
    summary = client.get("/journal/summary").get_json()["summary"]["all_time"]
    assert summary["trade_count"] == 0  # passes don't count as priced trades


# ---------------------------------------------------------------------------
# Stats honesty, empty/one/many states
# ---------------------------------------------------------------------------


def test_stats_stay_honest_below_the_sample_floor(app):
    _make_account(app, "acct-a", "a@example.com")
    client = _client_for(app, "acct-a")
    empty = client.get("/journal/summary").get_json()["stats"]
    assert empty["meaningful"] is False and empty["win_rate"] is None

    client.post("/journal/trades", json={"symbol": "SPY", "pnl_cents": 100})
    one = client.get("/journal/summary").get_json()["stats"]
    assert one["sample_size"] == 1 and one["meaningful"] is False and one["win_rate"] is None

    for i in range(4):
        client.post("/journal/trades", json={"symbol": "SPY", "pnl_cents": 100 if i % 2 else -100})
    five = client.get("/journal/summary").get_json()["stats"]
    assert five["sample_size"] == 5 and five["meaningful"] is True
    assert five["win_rate"] == pytest.approx(3 / 5)
    assert five["avg_win_cents"] == 100 and five["avg_loss_cents"] == -100


# ---------------------------------------------------------------------------
# Signal linking
# ---------------------------------------------------------------------------


def test_snapshot_is_derived_server_side_at_link_time(app):
    _make_account(app, "acct-a", "a@example.com")
    _seed_detection(app, "det-1", symbol="NVDA", tier="high")
    client = _client_for(app, "acct-a")
    trade = client.post(
        "/journal/trades", json={"symbol": "NVDA", "detection_id": "det-1"}
    ).get_json()["trade"]
    snapshot = trade["detection_snapshot"]
    assert snapshot["id"] == "det-1" and snapshot["tier"] == "high" and snapshot["symbol"] == "NVDA"


def test_missing_and_log_tier_detections_link_without_a_snapshot(app):
    """The designed degradation for the users.db/journal.db atomicity
    gap: the id is kept (the user really was alerted), the snapshot is
    honestly absent, and nothing errors."""
    _make_account(app, "acct-a", "a@example.com")
    _seed_detection(app, "det-log", tier="log")
    client = _client_for(app, "acct-a")
    for detection_id in ("det-never-committed", "det-log"):
        trade = client.post(
            "/journal/trades", json={"symbol": "NVDA", "detection_id": detection_id}
        ).get_json()["trade"]
        assert trade["detection_id"] == detection_id
        assert trade["detection_snapshot"] is None


def test_patch_can_link_and_unlink_with_snapshot_lifecycle(app):
    _make_account(app, "acct-a", "a@example.com")
    _seed_detection(app, "det-1")
    client = _client_for(app, "acct-a")
    trade = client.post("/journal/trades", json={"symbol": "NVDA"}).get_json()["trade"]
    linked = client.patch(
        f"/journal/trades/{trade['id']}", json={"detection_id": "det-1"}
    ).get_json()["trade"]
    assert linked["detection_snapshot"]["id"] == "det-1"
    unlinked = client.patch(
        f"/journal/trades/{trade['id']}", json={"detection_id": None}
    ).get_json()["trade"]
    assert unlinked["detection_id"] is None
    assert unlinked["detection_snapshot"] is None  # never outlives its link


def test_linkable_signals_come_only_from_this_users_delivery_log(app):
    telegram_id = 777_001
    _make_account(app, "acct-a", "a@example.com")
    _make_account(app, "acct-b", "b@example.com")
    _link_telegram(app, "acct-a", telegram_id)
    _seed_detection(app, "det-1", symbol="NVDA")
    _seed_detection(app, "det-2", symbol="TSLA")
    _seed_delivered_alert(app, "det-1", telegram_id)
    _seed_delivered_alert(app, "det-1", telegram_id, suffix=":sizing")  # folds onto det-1
    _seed_delivered_alert(app, "det-2", 999_999)  # someone else's delivery

    payload = _client_for(app, "acct-a").get("/journal/linkable-signals").get_json()
    assert payload["delivery_history"] is True
    assert [s["detection_id"] for s in payload["signals"]] == ["det-1"]

    filtered = _client_for(app, "acct-a").get("/journal/linkable-signals?symbol=TSLA").get_json()
    assert filtered["signals"] == []

    web_only = _client_for(app, "acct-b").get("/journal/linkable-signals").get_json()
    assert web_only == {"signals": [], "delivery_history": False}


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def test_csv_export_contains_only_the_owners_rows_with_et_dates(app):
    _make_account(app, "acct-a", "a@example.com")
    _make_account(app, "acct-b", "b@example.com")
    client_a = _client_for(app, "acct-a")
    client_a.post(
        "/journal/trades",
        json={"symbol": "SPY", "pnl_cents": 12550, "taken_at": "2026-08-13T02:00:00+00:00"},
    )
    _client_for(app, "acct-b").post("/journal/trades", json={"symbol": "SECRET", "pnl_cents": 1})

    response = client_a.get("/journal/export.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["pnl_usd"] == "125.50"
    assert rows[0]["date_et"] == "2026-08-12"  # ET day, not the UTC day
    assert "SECRET" not in response.get_data(as_text=True)
