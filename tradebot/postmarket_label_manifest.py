"""Strict, blinded, append-only empirical label-manifest ingestion."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tradebot.postmarket_empirical import (
    CALENDAR,
    CLASSIFICATIONS,
    LABEL_METHODS,
    EligibilityRule,
    ensure_empirical_schema,
    locked_eligibility_rule,
    record_independent_label,
)


ROOT_FIELDS = {
    "schema_version", "status", "manifest_version", "session",
    "created_at_utc", "labeler", "label_method",
    "blinded_to_observer_output", "eligibility", "artifacts", "labels",
}
ELIGIBILITY_FIELDS = {"move_pct", "min_cumulative_notional", "persistence_bars"}
ARTIFACT_FIELDS = {"provider", "feed", "endpoint", "acquired_at_utc", "sha256"}
LABEL_FIELDS = {
    "symbol", "classification", "direction", "eligible_at_utc",
    "max_abs_move_pct", "persistence_bars_observed", "cumulative_notional",
    "reason_code", "rationale",
}


LABEL_MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_empirical_label_manifests (
    label_manifest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    session TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    labeler TEXT NOT NULL,
    label_method TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    manifest_json BLOB NOT NULL,
    label_count INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    code_version TEXT,
    status TEXT NOT NULL CHECK(status='locked')
);
CREATE TABLE IF NOT EXISTS postmarket_empirical_label_manifest_rows (
    label_manifest_id INTEGER NOT NULL,
    label_id INTEGER NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    PRIMARY KEY(label_manifest_id,symbol)
);
CREATE TRIGGER IF NOT EXISTS postmarket_empirical_label_manifests_no_update
BEFORE UPDATE ON postmarket_empirical_label_manifests BEGIN
    SELECT RAISE(ABORT, 'postmarket_empirical_label_manifests is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_empirical_label_manifests_no_delete
BEFORE DELETE ON postmarket_empirical_label_manifests BEGIN
    SELECT RAISE(ABORT, 'postmarket_empirical_label_manifests is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_empirical_label_manifest_rows_no_update
BEFORE UPDATE ON postmarket_empirical_label_manifest_rows BEGIN
    SELECT RAISE(ABORT, 'postmarket_empirical_label_manifest_rows is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_empirical_label_manifest_rows_no_delete
BEFORE DELETE ON postmarket_empirical_label_manifest_rows BEGIN
    SELECT RAISE(ABORT, 'postmarket_empirical_label_manifest_rows is append-only');
END;
"""


@dataclass(frozen=True)
class LabelArtifact:
    provider: str
    feed: str
    endpoint: str
    acquired_at_utc: datetime
    sha256: str


@dataclass(frozen=True)
class IndependentLabel:
    symbol: str
    classification: str
    direction: str | None
    eligible_at_utc: datetime | None
    max_abs_move_pct: float
    persistence_bars_observed: int
    cumulative_notional: float
    reason_code: str
    rationale: str


@dataclass(frozen=True)
class LabelManifest:
    manifest_version: str
    session: date
    created_at_utc: datetime
    observed_at_utc: datetime
    labeler: str
    label_method: str
    eligibility: EligibilityRule
    artifacts: tuple[LabelArtifact, ...]
    labels: tuple[IndependentLabel, ...]
    manifest_sha256: str


def ensure_label_manifest_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LABEL_MANIFEST_SCHEMA)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"label manifest contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise ValueError(
            f"{name} fields did not match contract; "
            f"missing={sorted(fields-set(value))} extra={sorted(set(value)-fields)}"
        )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _utc(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a JSON number")
    parsed = float(value)
    if not math.isfinite(parsed) or (parsed <= 0 if positive else parsed < 0):
        comparator = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be finite and {comparator}")
    return parsed


def _integer(value: object, name: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _sha256(value: object, name: str) -> str:
    digest = _text(value, name).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{name} must be a 64-character hexadecimal digest")
    return digest


def parse_label_manifest(raw: bytes, *, observed_at: datetime) -> LabelManifest:
    try:
        payload = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("label manifest was not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("label manifest root must be an object")
    _exact(payload, ROOT_FIELDS, "label manifest")
    if (
        not isinstance(payload["schema_version"], int)
        or isinstance(payload["schema_version"], bool)
        or payload["schema_version"] != 1
    ):
        raise ValueError("unsupported label manifest schema_version")
    if payload["status"] != "locked":
        raise ValueError("label manifest status must be locked")
    if payload["blinded_to_observer_output"] is not True:
        raise ValueError("labels must be blinded to observer output")
    try:
        session = date.fromisoformat(_text(payload["session"], "session"))
    except ValueError as exc:
        raise ValueError("session must be an ISO date") from exc
    if not CALENDAR.is_session(session):
        raise ValueError("session must be an XNYS session")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed = observed_at.astimezone(timezone.utc)
    created = _utc(payload["created_at_utc"], "created_at_utc")
    if created > observed:
        raise ValueError("label manifest cannot be observed before it was created")
    method = _text(payload["label_method"], "label_method")
    if method not in LABEL_METHODS:
        raise ValueError(f"label_method must be one of {sorted(LABEL_METHODS)}")
    raw_rule = payload["eligibility"]
    if not isinstance(raw_rule, Mapping):
        raise ValueError("eligibility must be an object")
    _exact(raw_rule, ELIGIBILITY_FIELDS, "eligibility")
    rule = EligibilityRule(
        _number(raw_rule["move_pct"], "eligibility.move_pct", positive=True),
        _number(
            raw_rule["min_cumulative_notional"],
            "eligibility.min_cumulative_notional", positive=True,
        ),
        _integer(raw_rule["persistence_bars"], "eligibility.persistence_bars", minimum=2),
    )
    raw_artifacts = payload["artifacts"]
    if not isinstance(raw_artifacts, list) or not 1 <= len(raw_artifacts) <= 100:
        raise ValueError("artifacts must contain between 1 and 100 rows")
    artifacts = []
    artifact_keys = set()
    for index, item in enumerate(raw_artifacts):
        if not isinstance(item, Mapping):
            raise ValueError(f"artifacts[{index}] must be an object")
        _exact(item, ARTIFACT_FIELDS, f"artifacts[{index}]")
        artifact = LabelArtifact(
            _text(item["provider"], f"artifacts[{index}].provider").lower(),
            _text(item["feed"], f"artifacts[{index}].feed").lower(),
            _text(item["endpoint"], f"artifacts[{index}].endpoint"),
            _utc(item["acquired_at_utc"], f"artifacts[{index}].acquired_at_utc"),
            _sha256(item["sha256"], f"artifacts[{index}].sha256"),
        )
        key = (artifact.provider, artifact.feed, artifact.endpoint, artifact.sha256)
        if key in artifact_keys:
            raise ValueError("artifact rows must be unique")
        if artifact.acquired_at_utc > created:
            raise ValueError("artifact cannot be acquired after manifest creation")
        artifact_keys.add(key)
        artifacts.append(artifact)
    if method == "multi_provider_reconciliation" and len({a.provider for a in artifacts}) < 2:
        raise ValueError("multi_provider_reconciliation requires two providers")
    raw_labels = payload["labels"]
    if not isinstance(raw_labels, list) or not 1 <= len(raw_labels) <= 20_000:
        raise ValueError("labels must contain between 1 and 20,000 rows")
    labels = []
    symbols = set()
    for index, item in enumerate(raw_labels):
        if not isinstance(item, Mapping):
            raise ValueError(f"labels[{index}] must be an object")
        _exact(item, LABEL_FIELDS, f"labels[{index}]")
        symbol = _text(item["symbol"], f"labels[{index}].symbol")
        if symbol != symbol.upper() or symbol in symbols:
            raise ValueError("label symbols must be unique canonical uppercase")
        symbols.add(symbol)
        classification = _text(item["classification"], f"labels[{index}].classification")
        if classification not in CLASSIFICATIONS:
            raise ValueError(f"classification must be one of {sorted(CLASSIFICATIONS)}")
        direction = item["direction"]
        eligible_raw = item["eligible_at_utc"]
        eligible_at = (
            None if eligible_raw is None
            else _utc(eligible_raw, f"labels[{index}].eligible_at_utc")
        )
        max_move = _number(item["max_abs_move_pct"], f"labels[{index}].max_abs_move_pct")
        persistence = _integer(
            item["persistence_bars_observed"],
            f"labels[{index}].persistence_bars_observed", minimum=0,
        )
        notional = _number(
            item["cumulative_notional"], f"labels[{index}].cumulative_notional"
        )
        meets_rule = (
            max_move >= rule.move_pct
            and notional >= rule.min_cumulative_notional
            and persistence >= rule.persistence_bars
        )
        if classification == "eligible":
            if direction not in {"up", "down"} or eligible_at is None:
                raise ValueError("eligible labels require direction and eligible_at_utc")
            if not meets_rule:
                raise ValueError("eligible label did not satisfy its locked eligibility rule")
        elif direction is not None or eligible_at is not None:
            raise ValueError("ineligible/ambiguous labels cannot declare direction or eligibility")
        elif classification == "ineligible" and meets_rule:
            raise ValueError("ineligible label contradicted its locked eligibility rule")
        labels.append(IndependentLabel(
            symbol, classification, direction, eligible_at, max_move, persistence,
            notional, _text(item["reason_code"], f"labels[{index}].reason_code"),
            _text(item["rationale"], f"labels[{index}].rationale"),
        ))
    return LabelManifest(
        _text(payload["manifest_version"], "manifest_version"), session, created,
        observed, _text(payload["labeler"], "labeler"), method, rule,
        tuple(artifacts), tuple(sorted(labels, key=lambda label: label.symbol)),
        hashlib.sha256(raw).hexdigest(),
    )


def ingest_label_manifest(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    experiment_id: str,
    observed_at: datetime,
    code_version: str | None,
    run_id: str,
) -> tuple[int, bool, int, LabelManifest]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("label manifest must be a regular non-symlink file")
    raw = source.read_bytes()
    manifest = parse_label_manifest(raw, observed_at=observed_at)
    if not run_id.strip():
        raise ValueError("run_id must be non-empty")
    ensure_empirical_schema(conn)
    ensure_label_manifest_schema(conn)
    locked_rule = locked_eligibility_rule(conn, experiment_id)
    if asdict(locked_rule) != asdict(manifest.eligibility):
        raise ValueError("label eligibility did not match the locked experiment")
    experiment = conn.execute(
        "SELECT label_method FROM postmarket_rank_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if experiment is None or experiment[0] != manifest.label_method:
        raise ValueError("label method did not match the locked experiment")
    existing = conn.execute(
        """SELECT label_manifest_id,experiment_id
           FROM postmarket_empirical_label_manifests WHERE manifest_sha256=?""",
        (manifest.manifest_sha256,),
    ).fetchone()
    if existing is not None:
        if existing[1] != experiment_id:
            raise ValueError("label manifest digest is already bound to another experiment")
        return int(existing[0]), False, 0, manifest
    artifact_digest = manifest.manifest_sha256
    providers = tuple(sorted({artifact.provider for artifact in manifest.artifacts}))
    feeds = tuple(sorted({artifact.feed for artifact in manifest.artifacts}))
    acquired_at = max(artifact.acquired_at_utc for artifact in manifest.artifacts)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_empirical_label_manifests
                (experiment_id,session,manifest_version,created_at_utc,
                 observed_at_utc,labeler,label_method,manifest_sha256,label_count,
                 manifest_json,run_id,code_version,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'locked')
            """,
            (
                experiment_id, manifest.session.isoformat(), manifest.manifest_version,
                manifest.created_at_utc.isoformat(), manifest.observed_at_utc.isoformat(),
                manifest.labeler, manifest.label_method, manifest.manifest_sha256,
                len(manifest.labels), raw, run_id, code_version,
            ),
        )
        manifest_id = int(cursor.lastrowid)
        for label in manifest.labels:
            label_id = record_independent_label(
                conn,
                experiment_id=experiment_id,
                session=manifest.session,
                symbol=label.symbol,
                classification=label.classification,
                direction=label.direction,
                eligible_at=label.eligible_at_utc,
                labeler=manifest.labeler,
                reason_code=label.reason_code,
                rationale=label.rationale,
                artifact_sha256=artifact_digest,
                artifact_providers=providers,
                artifact_feeds=feeds,
                artifact_acquired_at=acquired_at,
                recorded_at=manifest.observed_at_utc,
                _ensure_schema=False,
                _manage_transaction=False,
            )
            conn.execute(
                """INSERT INTO postmarket_empirical_label_manifest_rows
                   (label_manifest_id,label_id,symbol) VALUES (?,?,?)""",
                (manifest_id, label_id, label.symbol),
            )
    return manifest_id, True, len(manifest.labels), manifest
