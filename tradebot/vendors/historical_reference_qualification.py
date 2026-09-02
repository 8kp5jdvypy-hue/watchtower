"""Immutable operator qualification for an independent recall provider.

Technical adapter support is not purchasing authority.  A provider may enter
the full-universe recall proof only after an operator has archived and hashed
every legal, lifecycle, coverage, provenance, and production-acceptance item
required by the provider RFQ.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_STATUS = "qualified"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
REFERENCE_PROVIDER_QUALIFICATION_ENV = (
    "POSTMARKET_REFERENCE_QUALIFICATION_MANIFEST"
)
REQUIRED_QUALIFICATION_PROOFS = frozenset(
    {
        "commercial_internal_validation_rights",
        "derived_output_retention",
        "raw_data_lifecycle",
        "completed_intraday_bars",
        "full_universe_snapshot",
        "postmarket_coverage",
        "immutable_object_provenance",
        "correction_semantics",
        "production_qualification",
        "startup_price",
    }
)


@dataclass(frozen=True)
class QualificationProof:
    kind: str
    reference: str
    sha256: str


@dataclass(frozen=True)
class HistoricalReferenceQualification:
    schema_version: int
    status: str
    provider: str
    dataset: str
    approved_at_utc: str
    approved_by: str
    license_reference: str
    proofs: tuple[QualificationProof, ...]
    manifest_sha256: str


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def parse_historical_reference_qualification(
    raw: bytes,
    *,
    observed_at: datetime,
) -> HistoricalReferenceQualification:
    """Parse a strict, digest-bound provider qualification manifest."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    try:
        payload = _object(json.loads(raw.decode("utf-8")), "manifest")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification manifest must be valid UTF-8 JSON") from exc
    expected_root = {
        "schema_version",
        "status",
        "provider",
        "dataset",
        "approved_at_utc",
        "approved_by",
        "license_reference",
        "proofs",
    }
    if set(payload) != expected_root:
        raise ValueError("qualification manifest fields are not exact")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != QUALIFICATION_SCHEMA_VERSION
    ):
        raise ValueError("unsupported qualification schema_version")
    if payload["status"] != QUALIFICATION_STATUS:
        raise ValueError("qualification status must be qualified")
    provider = _text(payload["provider"], "provider")
    if not PROVIDER_PATTERN.fullmatch(provider):
        raise ValueError("provider must be a canonical lowercase identifier")
    dataset = _text(payload["dataset"], "dataset")
    approved_by = _text(payload["approved_by"], "approved_by")
    license_reference = _text(payload["license_reference"], "license_reference")
    approved_raw = _text(payload["approved_at_utc"], "approved_at_utc")
    try:
        approved = datetime.fromisoformat(approved_raw)
    except ValueError as exc:
        raise ValueError("approved_at_utc must be ISO-8601") from exc
    if approved.tzinfo is None or approved.utcoffset() is None:
        raise ValueError("approved_at_utc must be timezone-aware")
    approved = approved.astimezone(timezone.utc)
    if approved > observed_at.astimezone(timezone.utc):
        raise ValueError("approved_at_utc cannot be in the future")

    proof_rows = payload["proofs"]
    if not isinstance(proof_rows, list):
        raise ValueError("proofs must be an array")
    proofs: list[QualificationProof] = []
    kinds: set[str] = set()
    for index, item in enumerate(proof_rows):
        proof = _object(item, f"proofs[{index}]")
        if set(proof) != {"kind", "reference", "sha256"}:
            raise ValueError(f"proofs[{index}] fields are not exact")
        kind = _text(proof["kind"], f"proofs[{index}].kind")
        if kind not in REQUIRED_QUALIFICATION_PROOFS:
            raise ValueError(f"proofs[{index}].kind is unsupported")
        if kind in kinds:
            raise ValueError(f"duplicate qualification proof: {kind}")
        reference = _text(proof["reference"], f"proofs[{index}].reference")
        digest = _text(proof["sha256"], f"proofs[{index}].sha256")
        if not SHA256_PATTERN.fullmatch(digest):
            raise ValueError(f"proofs[{index}].sha256 must be a lowercase SHA-256")
        kinds.add(kind)
        proofs.append(QualificationProof(kind, reference, digest))
    missing = sorted(REQUIRED_QUALIFICATION_PROOFS - kinds)
    if missing:
        raise ValueError(
            "qualification proofs are incomplete: " + ", ".join(missing)
        )
    return HistoricalReferenceQualification(
        QUALIFICATION_SCHEMA_VERSION,
        QUALIFICATION_STATUS,
        provider,
        dataset,
        approved.isoformat(),
        approved_by,
        license_reference,
        tuple(sorted(proofs, key=lambda item: item.kind)),
        hashlib.sha256(raw).hexdigest(),
    )


def load_historical_reference_qualification(
    path: str | Path,
    *,
    observed_at: datetime | None = None,
) -> HistoricalReferenceQualification:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError(
            "qualification manifest must be a regular non-symlink file"
        )
    return parse_historical_reference_qualification(
        source.read_bytes(),
        observed_at=observed_at or datetime.now(timezone.utc),
    )
