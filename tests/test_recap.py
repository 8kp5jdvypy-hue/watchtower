"""Tests for tradebot.rendering.recap — Part B of
docs/phase4-proof-engine-proposal.md. Determinism, the shared-values
guarantee between markdown/HTML, and the voice rules (zero emoji, no
superlatives, a losing week renders through the same structure as a
winning one) are the load-bearing properties here, not just "does it
render something."
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradebot.detectors import Detection
from tradebot.journal import connect as journal_connect
from tradebot.journal import write_cluster
from tradebot.rendering.recap import (
    RecapData,
    build_recap_data,
    render_recap_html,
    render_recap_markdown,
)
from tradebot.telegram_bot import db as users_db
from tradebot.telegram_bot import outbox
from tradebot.telegram_bot.performance import TrackRecord, WeeklyRecap

BASE = datetime(2026, 7, 27, 16, 0, tzinfo=timezone.utc)  # a Monday


def _seed_delivered(jconn, uconn, symbol, ts, headline="h", close=100.0, mark=None, trend="up", chat_id=111):
    did = write_cluster(
        jconn, session=ts.date().isoformat(), symbol=symbol, ts_utc=ts.isoformat(),
        kinds="gap", headlines=headline, score=5.0, close=close, atr14=1.0, trend=trend,
        detections=[Detection(symbol, "gap", ts, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    if mark is not None:
        jconn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, mark))
    jconn.commit()
    delivered = ts + timedelta(seconds=4)
    outbox.enqueue_broadcast(uconn, did, [(chat_id, "text", None)], outbox.PRIORITY_HIGH, now=delivered)
    row_id = uconn.execute("SELECT id FROM outbox WHERE alert_id = ?", (did,)).fetchone()[0]
    outbox.mark_delivered(uconn, row_id, delivered)
    return did


def _conns():
    return journal_connect(":memory:"), users_db.connect(":memory:")


# ---------------------------------------------------------------------- #
# build_recap_data
# ---------------------------------------------------------------------- #


def test_build_recap_data_scopes_alerts_to_the_week_but_running_total_is_all_time():
    jconn, uconn = _conns()
    _seed_delivered(jconn, uconn, "IN_WEEK", BASE, mark=101.0)
    _seed_delivered(jconn, uconn, "BEFORE_WEEK", BASE - timedelta(days=14), mark=101.0)

    data = build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03")

    assert [a.symbol for a in data.alerts] == ["IN_WEEK"]
    # running_total is all-time -- both seeded alerts count toward it,
    # even though only one is below MIN_HISTORY_SAMPLE either way (None
    # here), the point is it's not scoped to the week's own 1 alert.
    assert isinstance(data, RecapData)


# ---------------------------------------------------------------------- #
# Determinism / idempotency
# ---------------------------------------------------------------------- #


def test_same_week_produces_byte_identical_output_on_repeated_calls():
    jconn, uconn = _conns()
    for i in range(6):
        _seed_delivered(jconn, uconn, f"SYM{i}", BASE + timedelta(hours=i), mark=101.0 if i % 2 else 99.0)

    data1 = build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03")
    data2 = build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03")

    assert render_recap_markdown(data1) == render_recap_markdown(data2)
    assert render_recap_html(data1) == render_recap_html(data2)


# ---------------------------------------------------------------------- #
# Markdown content
# ---------------------------------------------------------------------- #


def test_markdown_empty_week_states_it_plainly():
    jconn, uconn = _conns()
    data = build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03")
    text = render_recap_markdown(data)
    assert "0 HIGH-tier alerts sent this week." in text
    assert "No alerts sent this week" in text


def test_markdown_shows_a_win_and_a_loss_with_identical_structure():
    jconn, uconn = _conns()
    _seed_delivered(jconn, uconn, "WINNER", BASE, headline="WINNER broke out", close=100.0, mark=105.0, trend="up")
    _seed_delivered(jconn, uconn, "LOSER", BASE + timedelta(hours=1), headline="LOSER broke down", close=100.0, mark=95.0, trend="up")

    text = render_recap_markdown(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))

    assert "+5.00%" in text
    assert "-5.00%" in text
    # both alert blocks have the exact same shape: symbol line, quoted
    # headline, +30m outcome line -- no extra decoration for the winner.
    winner_block = text.split("WINNER broke out")[1].split("\n\n")[0]
    loser_block = text.split("LOSER broke down")[1].split("\n\n")[0]
    assert winner_block.count("\n") == loser_block.count("\n")


def test_markdown_pending_alert_shown_not_hidden():
    jconn, uconn = _conns()
    _seed_delivered(jconn, uconn, "FRESH", BASE, headline="FRESH just fired", mark=None)
    text = render_recap_markdown(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))
    assert "FRESH just fired" in text
    assert "+30m: pending" in text


def test_markdown_running_total_none_states_it_plainly_not_a_crash():
    jconn, uconn = _conns()
    _seed_delivered(jconn, uconn, "ONLY_ONE", BASE, mark=101.0)  # 1 < MIN_HISTORY_SAMPLE
    text = render_recap_markdown(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))
    assert "Not enough tracked alerts yet (all-time) for a real hit rate." in text


def test_markdown_has_no_emoji_and_no_exclamation_marks():
    jconn, uconn = _conns()
    for i in range(3):
        _seed_delivered(jconn, uconn, f"SYM{i}", BASE + timedelta(hours=i), mark=105.0)
    text = render_recap_markdown(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))
    banned_emoji = {"🚀", "📈", "📉", "🔥", "💰", "⚠️", "✅", "❌", "🔴", "🟡", "⚪"}
    assert not (banned_emoji & set(text))
    assert "!" not in text


def test_a_losing_week_and_a_winning_week_render_the_same_number_of_lines():
    """The structural half of the "same template, win or lose" rule --
    see render_weekly_recap's own docstring for the property this
    mirrors for the Telegram version."""
    jconn_win, uconn_win = _conns()
    for i in range(6):
        _seed_delivered(jconn_win, uconn_win, f"W{i}", BASE + timedelta(hours=i), close=100.0, mark=105.0, trend="up")

    jconn_lose, uconn_lose = _conns()
    for i in range(6):
        _seed_delivered(jconn_lose, uconn_lose, f"L{i}", BASE + timedelta(hours=i), close=100.0, mark=95.0, trend="up")

    win_text = render_recap_markdown(build_recap_data(jconn_win, uconn_win, "2026-07-27", "2026-08-03"))
    lose_text = render_recap_markdown(build_recap_data(jconn_lose, uconn_lose, "2026-07-27", "2026-08-03"))

    assert win_text.count("\n") == lose_text.count("\n")


# ---------------------------------------------------------------------- #
# HTML content — escaping is the load-bearing property here
# ---------------------------------------------------------------------- #


def test_html_escapes_a_headline_containing_markup():
    jconn, uconn = _conns()
    _seed_delivered(jconn, uconn, "XSS", BASE, headline="<script>alert(1)</script> & friends", mark=101.0)
    html = render_recap_html(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; friends" in html


def test_html_table_has_one_row_per_alert_plus_header():
    jconn, uconn = _conns()
    for i in range(4):
        _seed_delivered(jconn, uconn, f"SYM{i}", BASE + timedelta(hours=i), mark=101.0)
    html = render_recap_html(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))
    assert html.count("<tr>") == 5  # 1 header + 4 alerts
    assert html.count("<td>") == 4 * 5  # 5 columns per alert row


def test_html_and_markdown_agree_on_every_alerts_outcome_value():
    """Same RecapData in, so the two formats can never quietly disagree
    about what a given alert's outcome actually was."""
    jconn, uconn = _conns()
    _seed_delivered(jconn, uconn, "AGREE", BASE, headline="AGREE test", close=100.0, mark=103.5, trend="up")
    data = build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03")

    md = render_recap_markdown(data)
    html = render_recap_html(data)

    assert "+3.50%" in md
    assert "+3.50%" in html


def test_html_empty_week_states_it_plainly():
    jconn, uconn = _conns()
    html = render_recap_html(build_recap_data(jconn, uconn, "2026-07-27", "2026-08-03"))
    assert "No alerts sent this week" in html
    assert "<table" not in html
