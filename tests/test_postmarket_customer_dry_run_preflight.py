import gzip
import hashlib
import io
import json
import sqlite3
import subprocess
import tarfile
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import postmarket_customer_dry_run_preflight as preflight_module
from scripts.postmarket_customer_dry_run_preflight import (
    evaluate_customer_dry_run_preflight,
    write_preflight_atomic,
)
from tradebot.postmarket_customer_dry_run_campaign import (
    POLICY_FIELDS,
    lock_customer_dry_run_campaign,
)
from tradebot import postmarket_customer_dry_run_campaign as campaign_module
from tradebot.postmarket_customer_dry_run_gate import REQUIRED_CONTROL_KINDS
from tradebot.postmarket_delivery_readiness import (
    ACKNOWLEDGEMENT,
    parse_delivery_policy,
)
from tradebot.screening_archive import archive_screening_session
from tradebot.universe import connect as connect_universe


NOW = datetime(2026, 8, 29, 1, tzinfo=timezone.utc)
STAMP = "20260829T003000Z"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "preflight@example.invalid")
    _git(repo, "config", "user.name", "Preflight")
    (repo / "source.txt").write_text("customer dry run\n")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-m", "fixture")
    revision = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", revision)
    return repo, revision


def _control(path, kind, revision):
    payload = {
        "schema_version": 1, "kind": kind, "status": "passed",
        "revision": revision, "completed_at_utc": (NOW - timedelta(minutes=5)).isoformat(),
        "checks": [{"name": "fixture", "passed": True, "evidence": "passed"}],
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _backup(
    root, *, policy, authorization, campaign, controls, upstream_set, upstream_gate
):
    root.mkdir()
    files = []
    for name in ("journal", "users", "evaluations", "postmarket_shadow", "universe"):
        path = root / f"{name}_{STAMP}.db.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(name.encode())
        files.append(path)
    artifacts = root / f"postmarket_artifacts_{STAMP}.tar.gz"
    screening_source = root.parent / "screening-source.db"
    universe = connect_universe(screening_source)
    universe.execute(
        """
        INSERT INTO screening_ticks
          (session,tick_utc,run_id,run_mode,screen_version,code_version,
           audit_mode,universe_count,thresholds_json,counts_json,invariant_ok,
           promotion_limit,latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "2026-08-28", "2026-08-28T20:00:00+00:00", "customer-preflight",
            "live", 2, "abcdef1", 0, 1, "{}", '{"quiet":1}', 1, 25, 10,
        ),
    )
    universe.commit()
    universe.close()
    screening_report = archive_screening_session(
        screening_source,
        root.parent / "screening_archives",
        session="2026-08-28",
        now=NOW,
    )
    with tarfile.open(artifacts, "w:gz") as archive:
        for name in ("postmarket_audits/audit.json", "postmarket_evidence/control.json"):
            raw = b"{}\n"
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        screening_path = Path(screening_report.path)
        archive.add(
            screening_path,
            arcname=f"screening_archives/{screening_path.name}",
        )
        for name, path in (
            ("postmarket_customer_delivery_policy.json", policy),
            ("postmarket_customer_delivery_authorization.json", authorization),
            ("postmarket_customer_dry_run_campaign.json", campaign),
        ):
            raw = path.read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        for index, path in enumerate(controls):
            raw = path.read_bytes()
            member = tarfile.TarInfo(
                f"postmarket_evidence/customer-controls/{index}.json"
            )
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        for name, path in (
            ("postmarket_evidence/upstream/discovery_evidence_set.json", upstream_set),
            ("postmarket_evidence/upstream/discovery_evidence_gate.json", upstream_gate),
        ):
            raw = path.read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    files.append(artifacts)
    manifest = root / f"manifest_{STAMP}.sha256"
    manifest.write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in files
    ))
    return manifest


def _fixture(tmp_path, monkeypatch):
    repo, full_revision = _repo(tmp_path)
    revision = full_revision[:7]
    env = tmp_path / ".env"
    env.write_text("POSTMARKET_CUSTOMER_DRY_RUN_ENABLED=0\n")
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("""
services:
  postmarket-customer-dry-run:
    environment:
      POSTMARKET_CUSTOMER_DRY_RUN_ENABLED: ${POSTMARKET_CUSTOMER_DRY_RUN_ENABLED:-0}
    command: python -m tradebot.postmarket_delivery_dry_run_shadow
    healthcheck:
      test: ["CMD", "python", "-m", "tradebot.postmarket_delivery_dry_run_health"]
  api:
    image: api
""")
    controls = []
    control_digests = []
    control_root = tmp_path / "controls"
    control_root.mkdir()
    for index, kind in enumerate(sorted(REQUIRED_CONTROL_KINDS)):
        path = control_root / f"{index}.json"
        controls.append(path)
        control_digests.append(_control(path, kind, revision))
    policy_payload = {
        "delivery_policy_version": 2, "router_revision": revision,
        "evidence_set_sha256": "1" * 64, "evidence_gate_sha256": "2" * 64,
        "rank_version": 1, "minimum_evidence_score": 70,
        "calibration_version": 1, "calibration_model_sha256": "c" * 64,
        "minimum_calibrated_quality": 0.70,
        "maximum_ordinal_rank": 10, "minimum_evidence_coverage_pct": 95,
        "maximum_data_age_seconds": 330, "allowed_states": ["CONFIRMED"],
        "allowed_evidence_revisions": [revision], "allowed_providers": ["alpaca"],
        "allowed_calibration_revisions": [revision],
        "allowed_feeds": ["sip"],
    }
    policy = parse_delivery_policy(policy_payload)
    upstream_set = tmp_path / "upstream-evidence-set.json"
    upstream_gate = tmp_path / "upstream-evidence-gate.json"
    upstream_set.write_text("{}\n")
    upstream_gate.write_text("{}\n")
    upstream = SimpleNamespace(
        evidence_set_sha256=policy.evidence_set_sha256,
        gate_artifact_sha256=policy.evidence_gate_sha256,
        gate_code_version=revision,
        evaluated_at_utc=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
        calibration_artifact_sha256="7" * 64,
        calibration_model_sha256="c" * 64,
        calibration_version=1,
        calibration_evaluated_at_utc=datetime(
            2026, 8, 26, 12, tzinfo=timezone.utc
        ),
        report=SimpleNamespace(rank_version=1),
    )
    monkeypatch.setattr(
        campaign_module, "verify_discovery_gate_artifact", lambda *args: upstream
    )
    monkeypatch.setattr(
        preflight_module, "verify_discovery_gate_artifact", lambda *args: upstream
    )
    auth_payload = {
        "schema_version": 1, "release_id": "release-1",
        "approved_by": "owner@example.com",
        "approved_at_utc": "2026-08-28T12:00:00+00:00",
        "expires_at_utc": "2026-10-01T00:00:00+00:00",
        "policy_sha256": policy.sha256,
        "evidence_set_sha256": policy.evidence_set_sha256,
        "evidence_gate_sha256": policy.evidence_gate_sha256,
        "router_revision": revision, "acknowledgement": ACKNOWLEDGEMENT,
        "dry_run_readiness_approved": True,
    }
    policy_path = tmp_path / "policy.json"
    auth_path = tmp_path / "authorization.json"
    policy_path.write_text(json.dumps(policy_payload))
    auth_path.write_text(json.dumps(auth_payload))
    campaign_policy = {
        "min_clean_sessions": 10, "min_eligible_decisions": 20,
        "min_independently_reviewed_cases": 20, "min_distinct_reviewed_symbols": 10,
        "min_owner_review_approval_rate": 0.9, "min_session_coverage_pct": 100,
        "max_scheduled_lag_seconds": 30, "max_tick_latency_seconds": 10,
        "allowed_audit_versions": [2], "allowed_audit_code_versions": [revision],
        "allowed_runtime_router_revisions": [revision],
        **{name: True for name in POLICY_FIELDS if name.startswith("require_")},
    }
    campaign = tmp_path / "campaign.json"
    lock_customer_dry_run_campaign(
        campaign, campaign_id="campaign-1", locked_at=NOW - timedelta(hours=1),
        coverage_start=date(2026, 8, 31), coverage_end=date(2026, 9, 18),
        delivery_policy_payload=policy_payload,
        owner_authorization_payload=auth_payload,
        upstream_discovery_evidence_set_path=upstream_set,
        upstream_discovery_evidence_gate_path=upstream_gate,
        control_evidence_sha256s=tuple(control_digests), policy=campaign_policy,
    )
    database = tmp_path / "postmarket_shadow.db"
    conn = sqlite3.connect(database)
    for table in (
        "postmarket_rank_runs", "postmarket_candidate_ranks",
        "postmarket_candidate_lifecycle", "postmarket_candidate_lifecycle_observations",
        "postmarket_rank_calibration_runs", "postmarket_rank_calibrators",
        "postmarket_rank_calibration_projections",
    ):
        conn.execute(f"CREATE TABLE {table} (id INTEGER)")
    conn.commit()
    conn.close()
    return {
        "repo_root": repo, "expected_revision": full_revision,
        "env_file": env, "compose_file": compose, "campaign_path": campaign,
        "upstream_discovery_evidence_set_path": upstream_set,
        "upstream_discovery_evidence_gate_path": upstream_gate,
        "delivery_policy_path": policy_path, "owner_authorization_path": auth_path,
        "control_paths": tuple(controls), "database_path": database,
        "backup_manifest": _backup(
            tmp_path / "backups", policy=policy_path,
            authorization=auth_path, campaign=campaign, controls=controls,
            upstream_set=upstream_set, upstream_gate=upstream_gate,
        ), "now": NOW,
        "max_backup_age_seconds": 7200, "min_free_bytes": 0,
    }


def test_complete_preflight_is_safe_but_does_not_enable_anything(tmp_path, monkeypatch):
    report = evaluate_customer_dry_run_preflight(**_fixture(tmp_path, monkeypatch))
    assert report.safe_to_begin_customer_dry_run_campaign is True
    assert report.customer_delivery_enabled is False
    assert all(check.passed for check in report.checks)


def test_enabled_switch_or_started_campaign_fails_closed(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    args["env_file"].write_text("POSTMARKET_CUSTOMER_DRY_RUN_ENABLED=1\n")
    args["now"] = datetime(2026, 8, 31, 15, tzinfo=timezone.utc)
    report = evaluate_customer_dry_run_preflight(**args)
    assert not report.safe_to_begin_customer_dry_run_campaign
    failed = {check.name for check in report.checks if not check.passed}
    assert {"customer_dry_run_switch_off", "campaign_not_started"} <= failed


def test_partial_schema_or_dirty_revision_fails_closed(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    conn = sqlite3.connect(args["database_path"])
    conn.execute("CREATE TABLE postmarket_delivery_dry_runs (id INTEGER)")
    conn.commit()
    conn.close()
    (args["repo_root"] / "dirty.txt").write_text("dirty\n")
    report = evaluate_customer_dry_run_preflight(**args)
    failed = {check.name for check in report.checks if not check.passed}
    assert {"dry_run_schema_absent_or_complete", "clean_worktree"} <= failed


def test_preflight_artifact_is_immutable_and_false_activation(tmp_path, monkeypatch):
    report = evaluate_customer_dry_run_preflight(**_fixture(tmp_path, monkeypatch))
    output = tmp_path / "preflight.json"
    digest = write_preflight_atomic(output, report)
    assert len(digest) == 64
    assert json.loads(output.read_text())["customer_delivery_enabled"] is False
    schema = json.loads(Path(
        "truth/postmarket_customer_dry_run_preflight_v1.schema.json"
    ).read_text())
    assert set(asdict(report)) == set(schema["required"])
    with pytest.raises(FileExistsError):
        write_preflight_atomic(output, report)


def test_preflight_fails_when_upstream_gate_binding_changes(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight_module,
        "verify_discovery_gate_artifact",
        lambda *unused: SimpleNamespace(
            evidence_set_sha256="f" * 64,
            gate_artifact_sha256="2" * 64,
            gate_code_version=args["expected_revision"][:7],
            evaluated_at_utc=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
            calibration_artifact_sha256="7" * 64,
            calibration_model_sha256="c" * 64,
            calibration_version=1,
            calibration_evaluated_at_utc=datetime(
                2026, 8, 26, 12, tzinfo=timezone.utc
            ),
            report=SimpleNamespace(rank_version=1),
        ),
    )
    report = evaluate_customer_dry_run_preflight(**args)
    assert not report.safe_to_begin_customer_dry_run_campaign
    assert "upstream_discovery_evidence_exact" in {
        check.name for check in report.checks if not check.passed
    }
