"""Seal one explicit, gate-passing market-wide discovery evidence set.

This offline writer never searches for artifacts or chooses a "latest" report.
Every input path is supplied by the operator, must live below the destination
directory, and is SHA-256 pinned. The manifest is published immutably only
after the aggregate evidence gate returns ELIGIBLE_FOR_OWNER_REVIEW.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from tradebot.postmarket_discovery_evidence_gate import (
    CAMPAIGN_FIELDS,
    EVIDENCE_SCHEMA_VERSION,
    REQUIRED_CONTROL_KINDS,
    VERDICT_OWNER_REVIEW,
    DiscoveryEvidenceGateReport,
    _exact_fields,
    _expected_sessions,
    _iso_date,
    _parse_policy,
    evaluate_discovery_evidence_gate,
    load_discovery_evidence_manifest,
)


@dataclass(frozen=True)
class SealedDiscoveryEvidenceSet:
    path: Path
    sha256: str
    report: DiscoveryEvidenceGateReport


def _read_json(path: Path, context: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{context} cannot be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload


def _artifact_reference(root: Path, path: Path | str, context: str) -> dict[str, str]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{context} cannot be resolved: {exc}") from exc
    if not resolved.is_file():
        raise ValueError(f"{context} must be a regular file")
    resolved_root = root.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"{context} must stay inside the evidence-set directory")
    relative = resolved.relative_to(resolved_root).as_posix()
    return {
        "path": relative,
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def _session_inventory(
    root: Path,
    raw: Iterable[tuple[date, Path | str]],
    *,
    context: str,
    expected_sessions: tuple[date, ...],
) -> list[dict[str, str]]:
    items = tuple(raw)
    sessions = tuple(session for session, _ in items)
    if len(sessions) != len(set(sessions)):
        raise ValueError(f"{context} contains duplicate sessions")
    if set(sessions) != set(expected_sessions):
        missing = sorted(set(expected_sessions) - set(sessions))
        extra = sorted(set(sessions) - set(expected_sessions))
        raise ValueError(
            f"{context} must match the exact campaign sessions; "
            f"missing={[value.isoformat() for value in missing]!r} "
            f"extra={[value.isoformat() for value in extra]!r}"
        )
    by_session = dict(items)
    return [
        {
            "session": session.isoformat(),
            **_artifact_reference(
                root,
                by_session[session],
                f"{context} {session.isoformat()}",
            ),
        }
        for session in expected_sessions
    ]


def _control_inventory(
    root: Path,
    raw: Iterable[tuple[str, Path | str]],
) -> list[dict[str, str]]:
    items = tuple(raw)
    kinds = tuple(kind for kind, _ in items)
    if len(kinds) != len(set(kinds)):
        raise ValueError("control artifacts contain duplicate kinds")
    if set(kinds) != REQUIRED_CONTROL_KINDS:
        raise ValueError(
            "control artifacts must match the exact required kinds; "
            f"observed={sorted(kinds)!r} required={sorted(REQUIRED_CONTROL_KINDS)!r}"
        )
    by_kind = dict(items)
    inventory = []
    for kind in sorted(REQUIRED_CONTROL_KINDS):
        path = Path(by_kind[kind])
        payload = _read_json(path, f"control {kind}")
        if payload.get("kind") != kind:
            raise ValueError(f"control {kind} payload kind does not match its inventory key")
        inventory.append(
            {
                "kind": kind,
                **_artifact_reference(root, path, f"control {kind}"),
                "revision": payload.get("revision"),
                "completed_at_utc": payload.get("completed_at_utc"),
            }
        )
    return inventory


def seal_discovery_evidence_set(
    output_path: Path | str,
    *,
    evidence_set_version: str,
    created_at: datetime,
    campaign_path: Path | str,
    discovery_audits: Iterable[tuple[date, Path | str]],
    recall_census_reports: Iterable[tuple[date, Path | str]],
    provider_proof_reports: Iterable[tuple[date, Path | str]],
    empirical_artifact: Path | str,
    calibration_artifact: Path | str,
    control_artifacts: Iterable[tuple[str, Path | str]],
) -> SealedDiscoveryEvidenceSet:
    """Publish an immutable manifest only for an explicitly passing package."""
    if not isinstance(evidence_set_version, str) or not evidence_set_version.strip():
        raise ValueError("evidence_set_version must be non-empty")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    created = created_at.astimezone(timezone.utc)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or os.path.lexists(output):
        raise FileExistsError(f"refusing to replace existing evidence set: {output}")
    root = output.parent

    campaign_payload = _read_json(Path(campaign_path), "campaign artifact")
    _exact_fields(campaign_payload, CAMPAIGN_FIELDS, "campaign")
    coverage_start = _iso_date(campaign_payload["coverage_start"], "campaign.coverage_start")
    coverage_end = _iso_date(campaign_payload["coverage_end"], "campaign.coverage_end")
    policy = _parse_policy(campaign_payload["policy"])
    expected_sessions = _expected_sessions(coverage_start, coverage_end)
    if not expected_sessions:
        raise ValueError("campaign coverage contains no XNYS sessions")

    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": "locked",
        "evidence_set_version": evidence_set_version.strip(),
        "created_at_utc": created.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "campaign_artifact": _artifact_reference(
            root, Path(campaign_path), "campaign artifact"
        ),
        "policy": asdict(policy),
        "discovery_audits": _session_inventory(
            root,
            discovery_audits,
            context="discovery audits",
            expected_sessions=expected_sessions,
        ),
        "recall_census_reports": _session_inventory(
            root,
            recall_census_reports,
            context="recall census reports",
            expected_sessions=expected_sessions,
        ),
        "provider_proof_reports": _session_inventory(
            root,
            provider_proof_reports,
            context="provider proof reports",
            expected_sessions=expected_sessions,
        ),
        "empirical_artifact": _artifact_reference(
            root, Path(empirical_artifact), "empirical artifact"
        ),
        "calibration_artifact": _artifact_reference(
            root, Path(calibration_artifact), "calibration artifact"
        ),
        "control_artifacts": _control_inventory(root, control_artifacts),
    }
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=root
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        report = evaluate_discovery_evidence_gate(
            load_discovery_evidence_manifest(temporary_path)
        )
        if report.verdict != VERDICT_OWNER_REVIEW:
            failed = [check.code for check in report.checks if not check.passed]
            raise ValueError(
                "refusing to seal a discovery evidence set that is not eligible "
                f"for owner review; failed_checks={failed!r}"
            )
        os.chmod(temporary_path, 0o444)
        os.link(temporary_path, output, follow_symlinks=False)
        temporary_path.unlink()
        temporary_path = None
        directory_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return SealedDiscoveryEvidenceSet(
        output,
        hashlib.sha256(raw).hexdigest(),
        report,
    )


def _dated_path(raw: str, context: str) -> tuple[date, Path]:
    session_text, separator, path_text = raw.partition("=")
    if not separator or not path_text:
        raise argparse.ArgumentTypeError(f"{context} must use SESSION=PATH")
    try:
        session = date.fromisoformat(session_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{context} session must use YYYY-MM-DD") from exc
    return session, Path(path_text)


def _control_path(raw: str) -> tuple[str, Path]:
    kind, separator, path_text = raw.partition("=")
    if not separator or not path_text:
        raise argparse.ArgumentTypeError("control must use KIND=PATH")
    return kind, Path(path_text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--evidence-set-version", required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument(
        "--discovery-audit",
        action="append",
        type=lambda raw: _dated_path(raw, "discovery audit"),
        required=True,
    )
    parser.add_argument(
        "--recall-census",
        action="append",
        type=lambda raw: _dated_path(raw, "recall census"),
        required=True,
    )
    parser.add_argument(
        "--provider-proof",
        action="append",
        type=lambda raw: _dated_path(raw, "provider proof"),
        required=True,
    )
    parser.add_argument("--empirical-artifact", type=Path, required=True)
    parser.add_argument("--calibration-artifact", type=Path, required=True)
    parser.add_argument("--control", action="append", type=_control_path, required=True)
    args = parser.parse_args(argv)
    try:
        sealed = seal_discovery_evidence_set(
            args.output,
            evidence_set_version=args.evidence_set_version,
            created_at=datetime.now(timezone.utc),
            campaign_path=args.campaign,
            discovery_audits=args.discovery_audit,
            recall_census_reports=args.recall_census,
            provider_proof_reports=args.provider_proof,
            empirical_artifact=args.empirical_artifact,
            calibration_artifact=args.calibration_artifact,
            control_artifacts=args.control,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(
        json.dumps(
            {
                "artifact_count": len(sealed.report.artifact_digests),
                "campaign_id": sealed.report.campaign_id,
                "path": str(sealed.path),
                "sha256": sealed.sha256,
                "verdict": sealed.report.verdict,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
