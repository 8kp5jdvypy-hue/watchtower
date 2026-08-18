"""Tests for scripts/generate_weekly_recap.py — the thin CLI wiring
only (argument parsing, --format selection, --out-dir file writing).
The actual rendering logic is tradebot.rendering.recap's, already
covered in tests/test_recap.py.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import generate_weekly_recap as script
from tradebot.detectors import Detection
from tradebot.journal import connect as journal_connect
from tradebot.journal import write_cluster
from tradebot.telegram_bot import db as users_db
from tradebot.telegram_bot import outbox

BASE = datetime(2026, 7, 27, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def dbs(tmp_path):
    journal_path = tmp_path / "journal.db"
    users_path = tmp_path / "users.db"
    jconn = journal_connect(journal_path)
    uconn = users_db.connect(users_path)

    did = write_cluster(
        jconn, session=BASE.date().isoformat(), symbol="TEST", ts_utc=BASE.isoformat(),
        kinds="gap", headlines="TEST fired", score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("TEST", "gap", BASE, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    jconn.execute("INSERT INTO marks (detection_id, offset_min, price) VALUES (?, 30, ?)", (did, 102.0))
    jconn.commit()
    delivered = BASE + timedelta(seconds=4)
    outbox.enqueue_broadcast(uconn, did, [(111, "text", None)], outbox.PRIORITY_HIGH, now=delivered)
    row_id = uconn.execute("SELECT id FROM outbox WHERE alert_id = ?", (did,)).fetchone()[0]
    outbox.mark_delivered(uconn, row_id, delivered)

    return journal_path, users_path


def test_default_format_writes_both_files(dbs, tmp_path, monkeypatch, capsys):
    journal_path, users_path = dbs
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv",
        [
            "generate_weekly_recap.py", "--week-start", "2026-07-27",
            "--db-path", str(journal_path), "--users-db-path", str(users_path), "--out-dir", str(out_dir),
        ],
    )
    script.main()

    md_path = out_dir / "recap_2026-07-27.md"
    html_path = out_dir / "recap_2026-07-27.html"
    assert md_path.exists()
    assert html_path.exists()
    assert "TEST fired" in md_path.read_text()
    assert "TEST fired" in html_path.read_text()


def test_format_markdown_only_writes_only_the_md_file(dbs, tmp_path, monkeypatch):
    journal_path, users_path = dbs
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv",
        [
            "generate_weekly_recap.py", "--week-start", "2026-07-27", "--format", "markdown",
            "--db-path", str(journal_path), "--users-db-path", str(users_path), "--out-dir", str(out_dir),
        ],
    )
    script.main()

    assert (out_dir / "recap_2026-07-27.md").exists()
    assert not (out_dir / "recap_2026-07-27.html").exists()


def test_no_out_dir_prints_to_stdout(dbs, monkeypatch, capsys):
    journal_path, users_path = dbs
    monkeypatch.setattr(
        sys, "argv",
        [
            "generate_weekly_recap.py", "--week-start", "2026-07-27",
            "--db-path", str(journal_path), "--users-db-path", str(users_path),
        ],
    )
    script.main()

    out = capsys.readouterr().out
    assert "=== md ===" in out
    assert "=== html ===" in out
    assert "TEST fired" in out


def test_week_end_is_seven_days_after_week_start(dbs, tmp_path, monkeypatch):
    """A detection exactly 7 days after week-start must fall in the
    NEXT week, not this one -- week_end is exclusive, same convention
    weekly_recap() itself documents."""
    journal_path, users_path = dbs
    jconn = journal_connect(journal_path)
    uconn = users_db.connect(users_path)
    boundary = BASE + timedelta(days=7)
    did = write_cluster(
        jconn, session=boundary.date().isoformat(), symbol="BOUNDARY", ts_utc=boundary.isoformat(),
        kinds="gap", headlines="BOUNDARY fired", score=5.0, close=100.0, atr14=1.0, trend="up",
        detections=[Detection("BOUNDARY", "gap", boundary, 5.0, "h", {})], code_version_str="abc", alerted=True,
    )
    jconn.commit()
    outbox.enqueue_broadcast(uconn, did, [(111, "text", None)], outbox.PRIORITY_HIGH, now=boundary)
    row_id = uconn.execute("SELECT id FROM outbox WHERE alert_id = ?", (did,)).fetchone()[0]
    outbox.mark_delivered(uconn, row_id, boundary)

    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv",
        [
            "generate_weekly_recap.py", "--week-start", "2026-07-27", "--format", "markdown",
            "--db-path", str(journal_path), "--users-db-path", str(users_path), "--out-dir", str(out_dir),
        ],
    )
    script.main()

    text = (out_dir / "recap_2026-07-27.md").read_text()
    assert "TEST fired" in text
    assert "BOUNDARY fired" not in text
