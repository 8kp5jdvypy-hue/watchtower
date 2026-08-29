"""Licensed reference manifests are strict, causal, and append-only."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.postmarket_reference_manifest import (
    candidate_reference,
    ingest_reference_manifest,
    parse_reference_manifest,
)


OBSERVED = datetime(2026, 8, 27, 19, 30, tzinfo=timezone.utc)


def _payload():
    return {
        "schema_version": 1,
        "status": "locked",
        "provider": "licensed-vendor",
        "dataset": "daily-sector-and-float-v1",
        "license_reference": "contract-2026-001",
        "effective_date": "2026-08-27",
        "published_at_utc": "2026-08-27T18:00:00+00:00",
        "created_at_utc": "2026-08-27T18:01:00+00:00",
        "classification_system": "GICS",
        "rows": [{
            "symbol": "ABC",
            "sector_code": "45",
            "sector_name": "Information Technology",
            "benchmark_symbol": "XLK",
            "float_shares": 1_000_000,
            "float_as_of_date": "2026-08-26",
        }, {
            "symbol": "XYZ",
            "sector_code": "40",
            "sector_name": "Financials",
            "benchmark_symbol": "XLF",
            "float_shares": None,
            "float_as_of_date": None,
        }],
    }


def _raw(payload=None):
    return json.dumps(payload or _payload(), separators=(",", ":"), sort_keys=True).encode()


def test_manifest_contract_is_exact_and_causal():
    manifest = parse_reference_manifest(_raw(), observed_at=OBSERVED)
    assert manifest.provider == "licensed-vendor"
    assert [row.symbol for row in manifest.rows] == ["ABC", "XYZ"]
    assert len(manifest.manifest_sha256) == 64

    extra = _payload()
    extra["unexpected"] = True
    with pytest.raises(ValueError, match="fields did not match"):
        parse_reference_manifest(_raw(extra), observed_at=OBSERVED)
    with pytest.raises(ValueError, match="not causal"):
        parse_reference_manifest(
            _raw(), observed_at=datetime(2026, 8, 27, 17, tzinfo=timezone.utc),
        )


def test_manifest_rejects_guessed_or_incomplete_reference_rows():
    payload = _payload()
    payload["license_reference"] = "unlicensed"
    with pytest.raises(ValueError, match="operator-reviewed"):
        parse_reference_manifest(_raw(payload), observed_at=OBSERVED)
    payload = _payload()
    payload["rows"][0]["benchmark_symbol"] = "SPY"
    with pytest.raises(ValueError, match="Select Sector"):
        parse_reference_manifest(_raw(payload), observed_at=OBSERVED)
    payload = _payload()
    payload["rows"][0]["float_as_of_date"] = None
    with pytest.raises(ValueError, match="supplied together"):
        parse_reference_manifest(_raw(payload), observed_at=OBSERVED)


def test_manifest_rejects_ambiguous_json_and_coerced_numbers():
    duplicate = _raw()[:-1] + b',"status":"locked"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_reference_manifest(duplicate, observed_at=OBSERVED)
    payload = _payload()
    payload["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        parse_reference_manifest(_raw(payload), observed_at=OBSERVED)
    payload = _payload()
    payload["rows"][0]["float_shares"] = "1000000"
    with pytest.raises(ValueError, match="JSON number"):
        parse_reference_manifest(_raw(payload), observed_at=OBSERVED)


def test_ingestion_is_digest_idempotent_append_only_and_point_in_time(tmp_path):
    path = tmp_path / "reference.json"
    path.write_bytes(_raw())
    conn = sqlite3.connect(":memory:")
    first_id, created, manifest = ingest_reference_manifest(
        conn, path, observed_at=OBSERVED, code_version="abc1234", run_id="run-1",
    )
    same_id, created_again, _ = ingest_reference_manifest(
        conn, path, observed_at=OBSERVED, code_version="abc1234", run_id="run-2",
    )
    assert created is True and created_again is False and same_id == first_id
    assert conn.execute("SELECT COUNT(*) FROM postmarket_reference_rows").fetchone()[0] == 2
    assert manifest.manifest_sha256 == conn.execute(
        "SELECT manifest_sha256 FROM postmarket_reference_manifests"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postmarket_reference_rows SET benchmark_symbol='XLF'")

    before_observation = candidate_reference(
        conn, symbol="ABC", session=date(2026, 8, 27),
        detected_at=OBSERVED - timedelta(minutes=1),
    )
    after_observation = candidate_reference(
        conn, symbol="ABC", session=date(2026, 8, 27),
        detected_at=OBSERVED + timedelta(minutes=1),
    )
    assert before_observation is None
    assert after_observation is not None
    assert after_observation.benchmark_symbol == "XLK"
    assert after_observation.float_shares == 1_000_000


def test_symlink_manifest_is_rejected(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(_raw())
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="non-symlink"):
        ingest_reference_manifest(
            sqlite3.connect(":memory:"), link, observed_at=OBSERVED,
            code_version="x", run_id="run",
        )
