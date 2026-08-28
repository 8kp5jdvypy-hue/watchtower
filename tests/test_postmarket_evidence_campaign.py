"""Prospective campaign locking is immutable and fail closed."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

import tradebot.postmarket_evidence_campaign as campaign_module
from tradebot.postmarket_evidence_campaign import lock_evidence_campaign, main


def _policy() -> dict:
    return {
        "min_clean_sessions": 10,
        "min_definitive_labels": 20,
        "min_positive_labels": 10,
        "min_recall": 0.95,
        "min_precision": 0.90,
        "max_detection_latency_seconds": 330,
        "allowed_data_feeds": ["sip"],
        "allowed_market_data_providers": ["alpaca"],
        "allowed_audit_versions": [1],
        "allowed_observer_versions": [1],
        "allowed_audit_code_versions": ["audit123"],
        "allowed_observer_code_versions": ["observer123"],
        "require_zero_dirty_sessions": True,
        "require_zero_direction_mismatches": True,
        "require_complete_session_inventory": True,
    }


def test_lock_writes_read_only_campaign_before_first_session(tmp_path):
    output = tmp_path / "campaign.json"

    digest, payload = lock_evidence_campaign(
        output,
        campaign_id="campaign-1",
        locked_at=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
        coverage_start=date(2026, 8, 27),
        coverage_end=date(2026, 9, 10),
        policy=_policy(),
    )

    assert len(digest) == 64
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert output.stat().st_mode & 0o777 == 0o444
    assert list(tmp_path.glob("*.tmp")) == []


def test_existing_campaign_is_never_overwritten_or_removed(tmp_path):
    output = tmp_path / "campaign.json"
    original = b"already locked\n"
    output.write_bytes(original)

    with pytest.raises(FileExistsError):
        lock_evidence_campaign(
            output,
            campaign_id="campaign-1",
            locked_at=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
            coverage_start=date(2026, 8, 27),
            coverage_end=date(2026, 9, 10),
            policy=_policy(),
        )

    assert output.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_campaign_cannot_be_locked_after_coverage_starts(tmp_path):
    with pytest.raises(ValueError, match="before its first session opens"):
        lock_evidence_campaign(
            tmp_path / "campaign.json",
            campaign_id="campaign-1",
            locked_at=datetime(2026, 8, 27, 14, tzinfo=timezone.utc),
            coverage_start=date(2026, 8, 27),
            coverage_end=date(2026, 9, 10),
            policy=_policy(),
        )


def test_campaign_range_must_be_capable_of_meeting_clean_session_floor(tmp_path):
    with pytest.raises(ValueError, match="fewer XNYS sessions"):
        lock_evidence_campaign(
            tmp_path / "campaign.json",
            campaign_id="campaign-1",
            locked_at=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
            coverage_start=date(2026, 8, 27),
            coverage_end=date(2026, 9, 4),
            policy=_policy(),
        )


def test_container_module_cli_locks_campaign_with_current_utc_time(
    tmp_path, monkeypatch, capsys
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 26, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(campaign_module, "datetime", FrozenDateTime)
    output = tmp_path / "campaign.json"

    result = main(
        [
            str(output),
            "--campaign-id", "campaign-1",
            "--coverage-start", "2026-08-27",
            "--coverage-end", "2026-09-10",
            "--min-clean-sessions", "10",
            "--min-definitive-labels", "20",
            "--min-positive-labels", "10",
            "--min-recall", "0.95",
            "--min-precision", "0.90",
            "--max-detection-latency-seconds", "330",
            "--allowed-data-feed", "sip",
            "--allowed-market-data-provider", "alpaca",
            "--allowed-audit-version", "1",
            "--allowed-observer-version", "1",
            "--allowed-audit-code-version", "audit123",
            "--allowed-observer-code-version", "observer123",
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output.exists()
    assert summary["campaign_id"] == "campaign-1"
    assert len(summary["campaign_sha256"]) == 64
