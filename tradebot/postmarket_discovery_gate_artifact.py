"""Immutable, independently reproducible discovery-gate decision artifact.

This offline module binds one exact market-wide evidence-set manifest to the
gate report produced from it.  It has no delivery, alert, vendor, broker, or
order path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradebot.postmarket_discovery_evidence_gate import (
    VERDICT_OWNER_REVIEW,
    DiscoveryEvidenceGateReport,
    evaluate_discovery_evidence_gate,
    load_discovery_evidence_manifest,
)


ARTIFACT_VERSION = 1
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
ROOT_FIELDS = {
    "schema_version",
    "artifact_type",
    "evaluated_at_utc",
    "gate_code_version",
    "evidence_set_sha256",
    "report_sha256",
    "report",
}


@dataclass(frozen=True)
class VerifiedDiscoveryGateArtifact:
    evidence_set_path: Path
    evidence_set_sha256: str
    gate_artifact_path: Path
    gate_artifact_sha256: str
    gate_code_version: str
    evaluated_at_utc: datetime
    report: DiscoveryEvidenceGateReport


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _regular(path: Path | str, context: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{context} must be a regular file")
    return resolved


def _utc(value: datetime, context: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _revision(value: object, context: str) -> str:
    if not isinstance(value, str) or not REVISION_PATTERN.fullmatch(value):
        raise ValueError(f"{context} must be a concrete Git revision")
    return value


def _sha(value: object, context: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def write_discovery_gate_artifact(
    evidence_set_path: Path | str,
    output_path: Path | str,
    *,
    evaluated_at: datetime,
    gate_code_version: str,
) -> str:
    """Write one no-replace gate artifact only for a passing evidence set."""
    evidence_path = _regular(evidence_set_path, "discovery evidence set")
    evaluated = _utc(evaluated_at, "evaluated_at")
    revision = _revision(gate_code_version, "gate_code_version")
    manifest = load_discovery_evidence_manifest(evidence_path)
    if evaluated < manifest.created_at_utc:
        raise ValueError("gate evaluation cannot predate the evidence set")
    report = evaluate_discovery_evidence_gate(manifest)
    if report.verdict != VERDICT_OWNER_REVIEW:
        failed = [check.code for check in report.checks if not check.passed]
        raise ValueError(
            "discovery evidence is not eligible for owner review; "
            f"failed_checks={failed!r}"
        )
    report_payload = asdict(report)
    report_raw = _canonical(report_payload)
    evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    payload = {
        "schema_version": ARTIFACT_VERSION,
        "artifact_type": "postmarket_discovery_evidence_gate",
        "evaluated_at_utc": evaluated.isoformat(),
        "gate_code_version": revision,
        "evidence_set_sha256": evidence_sha256,
        "report_sha256": hashlib.sha256(report_raw.encode()).hexdigest(),
        "report": report_payload,
    }
    raw = (_canonical(payload) + "\n").encode()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or os.path.lexists(output):
        raise FileExistsError(f"refusing to replace discovery gate artifact: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
        os.chmod(output, 0o444)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def verify_discovery_gate_artifact(
    evidence_set_path: Path | str,
    gate_artifact_path: Path | str,
) -> VerifiedDiscoveryGateArtifact:
    """Reopen both exact files and independently reproduce the gate report."""
    evidence_path = _regular(evidence_set_path, "discovery evidence set")
    gate_path = _regular(gate_artifact_path, "discovery gate artifact")
    gate_raw = gate_path.read_bytes()
    payload = json.loads(gate_raw)
    if not isinstance(payload, dict) or set(payload) != ROOT_FIELDS:
        raise ValueError("discovery gate artifact fields are not exact")
    if (
        payload["schema_version"] != ARTIFACT_VERSION
        or isinstance(payload["schema_version"], bool)
        or payload["artifact_type"] != "postmarket_discovery_evidence_gate"
    ):
        raise ValueError("discovery gate artifact identity is invalid")
    revision = _revision(payload["gate_code_version"], "gate_code_version")
    evaluated = _utc(
        datetime.fromisoformat(payload["evaluated_at_utc"]), "evaluated_at_utc"
    )
    evidence_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    if _sha(payload["evidence_set_sha256"], "evidence_set_sha256") != evidence_digest:
        raise ValueError("discovery gate artifact does not bind the evidence set")
    report_payload = payload["report"]
    if not isinstance(report_payload, dict):
        raise ValueError("discovery gate report must be an object")
    report_raw = _canonical(report_payload)
    if _sha(payload["report_sha256"], "report_sha256") != hashlib.sha256(
        report_raw.encode()
    ).hexdigest():
        raise ValueError("discovery gate report digest does not match embedded report")
    manifest = load_discovery_evidence_manifest(evidence_path)
    if evaluated < manifest.created_at_utc:
        raise ValueError("discovery gate artifact predates the evidence set")
    report = evaluate_discovery_evidence_gate(manifest)
    if _canonical(asdict(report)) != report_raw:
        raise ValueError("embedded discovery gate report is not reproducible")
    if report.verdict != VERDICT_OWNER_REVIEW:
        raise ValueError("discovery gate artifact is not eligible for owner review")
    return VerifiedDiscoveryGateArtifact(
        evidence_path,
        evidence_digest,
        gate_path,
        hashlib.sha256(gate_raw).hexdigest(),
        revision,
        evaluated,
        report,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_set", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--gate-code-version", required=True)
    args = parser.parse_args(argv)
    try:
        digest = write_discovery_gate_artifact(
            args.evidence_set,
            args.output,
            evaluated_at=datetime.now(timezone.utc),
            gate_code_version=args.gate_code_version,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps({"path": str(args.output), "sha256": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
