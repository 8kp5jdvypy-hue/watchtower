"""Leakage-resistant calibration for the interpretable postmarket quality rank.

The live evidence score remains a decomposable ordering heuristic.  This
offline module may translate that score into an observed-quality estimate only
after a development-only monotonic calibrator is frozen before the first
holdout session opens and then passes an explicitly unblinded holdout.  It has
no delivery, vendor, alert, broker, or order path.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tradebot.postmarket_empirical import CALENDAR, ensure_empirical_schema


CALIBRATION_VERSION = 1
CALIBRATION_ARTIFACT_VERSION = 1
SPLITS = {"development", "holdout"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


CALIBRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_rank_calibrators (
    calibration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL UNIQUE,
    calibration_version INTEGER NOT NULL,
    fitted_at_utc TEXT NOT NULL,
    code_version TEXT NOT NULL,
    method TEXT NOT NULL,
    development_input_sha256 TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    model_json TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    definitive_labels INTEGER NOT NULL,
    positive_labels INTEGER NOT NULL,
    negative_labels INTEGER NOT NULL,
    training_brier_score REAL NOT NULL,
    training_expected_calibration_error REAL NOT NULL,
    CHECK (method='isotonic_pav')
);
CREATE TABLE IF NOT EXISTS postmarket_rank_calibration_runs (
    calibration_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    calibration_id INTEGER NOT NULL,
    experiment_id TEXT NOT NULL,
    split TEXT NOT NULL,
    evaluated_at_utc TEXT NOT NULL,
    code_version TEXT NOT NULL,
    input_digest_sha256 TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    UNIQUE(calibration_id,split,input_digest_sha256),
    CHECK (split IN ('development','holdout'))
);
CREATE TRIGGER IF NOT EXISTS postmarket_rank_calibrators_no_update
BEFORE UPDATE ON postmarket_rank_calibrators BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_calibrators is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_calibrators_no_delete
BEFORE DELETE ON postmarket_rank_calibrators BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_calibrators is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_calibration_runs_no_update
BEFORE UPDATE ON postmarket_rank_calibration_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_calibration_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_calibration_runs_no_delete
BEFORE DELETE ON postmarket_rank_calibration_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_calibration_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_development_labels_frozen_after_calibration
BEFORE INSERT ON postmarket_independent_labels
WHEN EXISTS (
    SELECT 1
    FROM postmarket_rank_calibrators c
    JOIN postmarket_rank_experiments e ON e.experiment_id=c.experiment_id
    JOIN json_each(e.development_sessions_json) s
      ON s.value=NEW.session
    WHERE c.experiment_id=NEW.experiment_id
)
BEGIN
    SELECT RAISE(ABORT, 'development labels are frozen after calibration');
END;
"""


@dataclass(frozen=True)
class CalibrationPolicy:
    min_training_labels: int
    min_training_positive_labels: int
    min_training_negative_labels: int
    min_holdout_labels: int
    min_holdout_positive_labels: int
    min_holdout_negative_labels: int
    minimum_bin_labels: int
    max_brier_score: float
    max_expected_calibration_error: float


@dataclass(frozen=True)
class CalibrationSegment:
    minimum_score: float
    maximum_score: float
    calibrated_quality: float
    development_labels: int
    development_positives: int


@dataclass(frozen=True)
class FrozenCalibrator:
    calibration_id: int
    experiment_id: str
    calibration_version: int
    fitted_at_utc: str
    code_version: str
    method: str
    development_input_sha256: str
    policy: CalibrationPolicy
    segments: tuple[CalibrationSegment, ...]
    model_sha256: str
    definitive_labels: int
    positive_labels: int
    negative_labels: int
    training_brier_score: float
    training_expected_calibration_error: float
    created: bool


@dataclass(frozen=True)
class ReliabilityBin:
    minimum_score: float
    maximum_score: float
    predicted_quality: float
    labels: int
    positives: int
    observed_quality: float
    absolute_error: float
    wilson_lower_95: float
    wilson_upper_95: float


@dataclass(frozen=True)
class CalibrationReport:
    calibration_version: int
    calibration_id: int
    experiment_id: str
    split: str
    sessions: tuple[str, ...]
    code_version: str
    model_sha256: str
    development_input_sha256: str
    policy: CalibrationPolicy
    definitive_labels: int
    positive_labels: int
    negative_labels: int
    ambiguous_labels: int
    unmatched_rank_labels: int
    brier_score: float | None
    expected_calibration_error: float | None
    reliability_bins: tuple[ReliabilityBin, ...]
    holdout_unblinded: bool
    calibrated_quality_claim_valid: bool
    blocking_reasons: tuple[str, ...]
    input_digest_sha256: str


@dataclass(frozen=True)
class WrittenCalibrationArtifact:
    path: str
    sha256: str
    created: bool
    experiment_id: str
    split: str
    input_digest_sha256: str
    report_sha256: str
    model_sha256: str


def ensure_calibration_schema(conn: sqlite3.Connection) -> None:
    ensure_empirical_schema(conn)
    conn.executescript(CALIBRATION_SCHEMA)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _revision(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not REVISION_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be an attributable 7-40 character Git SHA")
    return value


def _experiment(conn: sqlite3.Connection, experiment_id: str) -> sqlite3.Row:
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM postmarket_rank_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
    finally:
        conn.row_factory = previous
    if row is None:
        raise ValueError("unknown experiment_id")
    return row


def _validate_policy(policy: CalibrationPolicy) -> None:
    integer_fields = (
        policy.min_training_labels,
        policy.min_training_positive_labels,
        policy.min_training_negative_labels,
        policy.min_holdout_labels,
        policy.min_holdout_positive_labels,
        policy.min_holdout_negative_labels,
        policy.minimum_bin_labels,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in integer_fields):
        raise ValueError("calibration label floors must be positive integers")
    if policy.min_training_labels < (
        policy.min_training_positive_labels + policy.min_training_negative_labels
    ):
        raise ValueError("training label floor cannot be below positive plus negative floors")
    if policy.min_holdout_labels < (
        policy.min_holdout_positive_labels + policy.min_holdout_negative_labels
    ):
        raise ValueError("holdout label floor cannot be below positive plus negative floors")
    for value, name in (
        (policy.max_brier_score, "max_brier_score"),
        (policy.max_expected_calibration_error, "max_expected_calibration_error"),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if not 0 < float(value) < 1:
            raise ValueError(f"{name} must be in (0,1)")


def _latest_labels(
    conn: sqlite3.Connection, experiment_id: str, sessions: tuple[str, ...],
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in sessions)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            f"""
            WITH latest AS (
              SELECT experiment_id,session,symbol,MAX(revision) AS revision
              FROM postmarket_independent_labels
              WHERE experiment_id=? AND session IN ({placeholders})
              GROUP BY experiment_id,session,symbol
            )
            SELECT l.* FROM postmarket_independent_labels l JOIN latest x
              ON x.experiment_id=l.experiment_id AND x.session=l.session
             AND x.symbol=l.symbol AND x.revision=l.revision
            ORDER BY l.session,l.symbol
            """,
            (experiment_id, *sessions),
        ).fetchall()
    finally:
        conn.row_factory = previous


def _first_rank_rows(
    conn: sqlite3.Connection, sessions: tuple[str, ...], rank_version: int,
) -> dict[tuple[str, str], sqlite3.Row]:
    placeholders = ",".join("?" for _ in sessions)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            WITH ordered AS (
              SELECT c.session,c.symbol,c.direction,r.evidence_score,r.rank_id,
                     x.rank_run_id,x.as_of_utc,
                     ROW_NUMBER() OVER (
                       PARTITION BY c.session,c.symbol
                       ORDER BY x.as_of_utc,r.rank_id
                     ) AS ordinal
              FROM postmarket_discovery_candidates c
              JOIN postmarket_candidate_ranks r ON r.candidate_id=c.candidate_id
              JOIN postmarket_rank_runs x ON x.rank_run_id=r.rank_run_id
              WHERE c.session IN ({placeholders})
                AND x.rank_version=? AND r.rankable=1
            )
            SELECT * FROM ordered WHERE ordinal=1 ORDER BY session,symbol
            """,
            (*sessions, rank_version),
        ).fetchall()
    finally:
        conn.row_factory = previous
    return {(row["session"], row["symbol"]): row for row in rows}


def _examples(
    conn: sqlite3.Connection,
    experiment_id: str,
    sessions: tuple[str, ...],
    rank_version: int,
) -> tuple[list[dict[str, Any]], int, int, str, datetime | None]:
    labels = _latest_labels(conn, experiment_id, sessions)
    ranks = _first_rank_rows(conn, sessions, rank_version)
    examples: list[dict[str, Any]] = []
    ambiguous = unmatched = 0
    latest_recorded: datetime | None = None
    inventory_labels = []
    for row in labels:
        recorded = _utc(datetime.fromisoformat(row["recorded_at_utc"]), "recorded_at_utc")
        latest_recorded = recorded if latest_recorded is None else max(latest_recorded, recorded)
        key = (row["session"], row["symbol"])
        rank = ranks.get(key)
        inventory_labels.append({
            "session": row["session"],
            "symbol": row["symbol"],
            "revision": int(row["revision"]),
            "classification": row["classification"],
            "direction": row["direction"],
            "artifact_sha256": row["artifact_sha256"],
            "rank": None if rank is None else {
                "rank_run_id": int(rank["rank_run_id"]),
                "rank_id": int(rank["rank_id"]),
                "as_of_utc": rank["as_of_utc"],
                "direction": rank["direction"],
                "evidence_score": float(rank["evidence_score"]),
            },
        })
        if row["classification"] == "ambiguous":
            ambiguous += 1
            continue
        if rank is None:
            unmatched += 1
            continue
        positive = int(
            row["classification"] == "eligible" and row["direction"] == rank["direction"]
        )
        examples.append({
            "session": row["session"],
            "symbol": row["symbol"],
            "score": float(rank["evidence_score"]),
            "positive": positive,
        })
    return examples, ambiguous, unmatched, _digest(inventory_labels), latest_recorded


def _fit_isotonic(examples: Iterable[dict[str, Any]]) -> tuple[CalibrationSegment, ...]:
    grouped: list[dict[str, float | int]] = []
    for example in sorted(examples, key=lambda row: (row["score"], row["session"], row["symbol"])):
        score = float(example["score"])
        positive = int(example["positive"])
        if grouped and grouped[-1]["maximum_score"] == score:
            grouped[-1]["labels"] = int(grouped[-1]["labels"]) + 1
            grouped[-1]["positives"] = int(grouped[-1]["positives"]) + positive
        else:
            grouped.append({
                "minimum_score": score,
                "maximum_score": score,
                "labels": 1,
                "positives": positive,
            })
    blocks: list[dict[str, float | int]] = []
    for group in grouped:
        blocks.append(dict(group))
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            left_rate = int(left["positives"]) / int(left["labels"])
            right_rate = int(right["positives"]) / int(right["labels"])
            if left_rate <= right_rate:
                break
            blocks[-2:] = [{
                "minimum_score": float(left["minimum_score"]),
                "maximum_score": float(right["maximum_score"]),
                "labels": int(left["labels"]) + int(right["labels"]),
                "positives": int(left["positives"]) + int(right["positives"]),
            }]
    return tuple(
        CalibrationSegment(
            minimum_score=float(block["minimum_score"]),
            maximum_score=float(block["maximum_score"]),
            calibrated_quality=int(block["positives"]) / int(block["labels"]),
            development_labels=int(block["labels"]),
            development_positives=int(block["positives"]),
        )
        for block in blocks
    )


def _predict(segments: tuple[CalibrationSegment, ...], score: float) -> tuple[int, float]:
    for index, segment in enumerate(segments):
        if score <= segment.maximum_score:
            return index, segment.calibrated_quality
    return len(segments) - 1, segments[-1].calibrated_quality


def _scores(
    examples: list[dict[str, Any]], segments: tuple[CalibrationSegment, ...],
) -> tuple[float | None, float | None, tuple[ReliabilityBin, ...]]:
    if not examples:
        return None, None, ()
    buckets: dict[int, list[int]] = {}
    squared_error = 0.0
    for example in examples:
        index, predicted = _predict(segments, float(example["score"]))
        positive = int(example["positive"])
        squared_error += (predicted - positive) ** 2
        bucket = buckets.setdefault(index, [0, 0])
        bucket[0] += 1
        bucket[1] += positive
    bins = []
    ece = 0.0
    for index in sorted(buckets):
        labels, positives = buckets[index]
        segment = segments[index]
        observed = positives / labels
        error = abs(segment.calibrated_quality - observed)
        ece += labels / len(examples) * error
        center = (observed + 1.96**2 / (2 * labels)) / (1 + 1.96**2 / labels)
        margin = 1.96 * math.sqrt(
            (observed * (1 - observed) + 1.96**2 / (4 * labels)) / labels
        ) / (1 + 1.96**2 / labels)
        bins.append(ReliabilityBin(
            segment.minimum_score,
            segment.maximum_score,
            segment.calibrated_quality,
            labels,
            positives,
            observed,
            error,
            max(0.0, center - margin),
            min(1.0, center + margin),
        ))
    return squared_error / len(examples), ece, tuple(bins)


def fit_rank_calibrator(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    fitted_at: datetime,
    code_version: str,
    policy: CalibrationPolicy,
) -> FrozenCalibrator:
    """Freeze a development-only isotonic calibrator before holdout opens."""
    ensure_calibration_schema(conn)
    _validate_policy(policy)
    revision = _revision(code_version, "code_version")
    experiment = _experiment(conn, experiment_id)
    fitted = _utc(fitted_at, "fitted_at")
    created = _utc(datetime.fromisoformat(experiment["created_at_utc"]), "experiment created_at")
    holdout_sessions = tuple(json.loads(experiment["holdout_sessions_json"]))
    first_open = CALENDAR.session_open(date.fromisoformat(min(holdout_sessions))).to_pydatetime()
    if fitted < created:
        raise ValueError("calibrator cannot predate the locked experiment")
    if fitted >= first_open:
        raise ValueError("calibrator must be frozen before the first holdout session opens")
    development_sessions = tuple(json.loads(experiment["development_sessions_json"]))
    examples, ambiguous, unmatched, input_digest, latest_recorded = _examples(
        conn, experiment_id, development_sessions, int(experiment["rank_version"])
    )
    if latest_recorded is None:
        raise ValueError("development calibration requires independently recorded labels")
    if fitted < latest_recorded:
        raise ValueError("calibrator cannot predate its latest development label")
    if ambiguous:
        raise ValueError("development labels contain ambiguous classifications")
    if unmatched:
        raise ValueError("development labels are missing rankable score evidence")
    positives = sum(int(row["positive"]) for row in examples)
    negatives = len(examples) - positives
    if len(examples) < policy.min_training_labels:
        raise ValueError("minimum training labels not met")
    if positives < policy.min_training_positive_labels:
        raise ValueError("minimum training positive labels not met")
    if negatives < policy.min_training_negative_labels:
        raise ValueError("minimum training negative labels not met")
    segments = _fit_isotonic(examples)
    brier, ece, _ = _scores(examples, segments)
    assert brier is not None and ece is not None
    model = {
        "calibration_version": CALIBRATION_VERSION,
        "experiment_id": experiment_id,
        "method": "isotonic_pav",
        "scope": "first_rankable_score_same_direction_quality",
        "development_input_sha256": input_digest,
        "policy": asdict(policy),
        "segments": [asdict(segment) for segment in segments],
    }
    model_raw = _canonical(model)
    model_sha256 = hashlib.sha256(model_raw.encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM postmarket_rank_calibrators WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    was_created = existing is None
    if existing is not None:
        if existing[9] != model_sha256:
            raise ValueError("experiment already has a different frozen calibrator")
        calibration_id = int(existing[0])
    else:
        with conn:
            calibration_id = int(conn.execute(
                """
                INSERT INTO postmarket_rank_calibrators
                    (experiment_id,calibration_version,fitted_at_utc,code_version,method,
                     development_input_sha256,policy_json,model_json,model_sha256,
                     definitive_labels,positive_labels,negative_labels,
                     training_brier_score,training_expected_calibration_error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    experiment_id, CALIBRATION_VERSION, fitted.isoformat(), revision,
                    "isotonic_pav", input_digest, _canonical(asdict(policy)), model_raw,
                    model_sha256, len(examples), positives, negatives, brier, ece,
                ),
            ).lastrowid)
    return FrozenCalibrator(
        calibration_id, experiment_id, CALIBRATION_VERSION, fitted.isoformat(), revision,
        "isotonic_pav", input_digest, policy, segments, model_sha256, len(examples),
        positives, negatives, brier, ece, was_created,
    )


def _load_calibrator(conn: sqlite3.Connection, experiment_id: str) -> FrozenCalibrator:
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM postmarket_rank_calibrators WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
    finally:
        conn.row_factory = previous
    if row is None:
        raise ValueError("experiment has no frozen calibrator")
    model_raw = row["model_json"]
    if hashlib.sha256(model_raw.encode()).hexdigest() != row["model_sha256"]:
        raise ValueError("stored calibrator digest does not match model JSON")
    model = json.loads(model_raw)
    if _canonical(model) != model_raw:
        raise ValueError("stored calibrator model is not canonical JSON")
    return FrozenCalibrator(
        int(row["calibration_id"]), row["experiment_id"], int(row["calibration_version"]),
        row["fitted_at_utc"], row["code_version"], row["method"],
        row["development_input_sha256"], CalibrationPolicy(**json.loads(row["policy_json"])),
        tuple(CalibrationSegment(**item) for item in model["segments"]),
        row["model_sha256"], int(row["definitive_labels"]),
        int(row["positive_labels"]), int(row["negative_labels"]),
        float(row["training_brier_score"]),
        float(row["training_expected_calibration_error"]), False,
    )


def evaluate_rank_calibration(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    split: str,
    evaluated_at: datetime,
    code_version: str,
) -> CalibrationReport:
    """Evaluate a frozen mapping; only an unblinded holdout may validate it."""
    ensure_calibration_schema(conn)
    if split not in SPLITS:
        raise ValueError(f"split must be one of {sorted(SPLITS)}")
    revision = _revision(code_version, "code_version")
    calibrator = _load_calibrator(conn, experiment_id)
    experiment = _experiment(conn, experiment_id)
    unblinded = conn.execute(
        "SELECT 1 FROM postmarket_holdout_unblinds WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone() is not None
    if split == "holdout" and not unblinded:
        raise ValueError("holdout is sealed; record an explicit unblind event first")
    sessions = tuple(json.loads(experiment[f"{split}_sessions_json"]))
    examples, ambiguous, unmatched, label_rank_digest, _ = _examples(
        conn, experiment_id, sessions, int(experiment["rank_version"])
    )
    positives = sum(int(row["positive"]) for row in examples)
    negatives = len(examples) - positives
    brier, ece, bins = _scores(examples, calibrator.segments)
    policy = calibrator.policy
    blockers: list[str] = []
    minimum_labels = (
        policy.min_holdout_labels if split == "holdout" else policy.min_training_labels
    )
    minimum_positives = (
        policy.min_holdout_positive_labels
        if split == "holdout" else policy.min_training_positive_labels
    )
    minimum_negatives = (
        policy.min_holdout_negative_labels
        if split == "holdout" else policy.min_training_negative_labels
    )
    if len(examples) < minimum_labels:
        blockers.append("MIN_CALIBRATION_LABELS_NOT_MET")
    if positives < minimum_positives:
        blockers.append("MIN_CALIBRATION_POSITIVES_NOT_MET")
    if negatives < minimum_negatives:
        blockers.append("MIN_CALIBRATION_NEGATIVES_NOT_MET")
    if ambiguous:
        blockers.append("AMBIGUOUS_LABELS_PRESENT")
    if unmatched:
        blockers.append("UNMATCHED_RANK_EVIDENCE_PRESENT")
    if bins and any(item.labels < policy.minimum_bin_labels for item in bins):
        blockers.append("MIN_RELIABILITY_BIN_LABELS_NOT_MET")
    if brier is None or brier > policy.max_brier_score:
        blockers.append("BRIER_SCORE_FLOOR_NOT_MET")
    if ece is None or ece > policy.max_expected_calibration_error:
        blockers.append("EXPECTED_CALIBRATION_ERROR_FLOOR_NOT_MET")
    if split != "holdout":
        blockers.append("HOLDOUT_VALIDATION_REQUIRED")
    inventory = {
        "calibration_id": calibrator.calibration_id,
        "model_sha256": calibrator.model_sha256,
        "split": split,
        "sessions": sessions,
        "label_rank_digest": label_rank_digest,
    }
    input_digest = _digest(inventory)
    report = CalibrationReport(
        CALIBRATION_VERSION, calibrator.calibration_id, experiment_id, split, sessions,
        revision, calibrator.model_sha256, calibrator.development_input_sha256, policy,
        len(examples), positives, negatives, ambiguous, unmatched, brier, ece, bins,
        split == "holdout" and unblinded, split == "holdout" and not blockers,
        tuple(blockers), input_digest,
    )
    raw = _canonical(asdict(report))
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO postmarket_rank_calibration_runs
                (calibration_id,experiment_id,split,evaluated_at_utc,code_version,
                 input_digest_sha256,report_json,report_sha256)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                calibrator.calibration_id, experiment_id, split,
                _utc(evaluated_at, "evaluated_at").isoformat(), revision, input_digest,
                raw, hashlib.sha256(raw.encode()).hexdigest(),
            ),
        )
    return report


def export_calibration_report(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    split: str,
    input_digest_sha256: str,
    output_dir: Path | str,
) -> WrittenCalibrationArtifact:
    """Export one exact calibration run as a no-replace digest-bound artifact."""
    ensure_calibration_schema(conn)
    digest = input_digest_sha256.strip().lower()
    if split not in SPLITS or not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("split and input digest must identify one calibration run")
    row = conn.execute(
        """
        SELECT r.calibration_run_id,r.evaluated_at_utc,r.code_version,r.report_json,
               r.report_sha256,c.model_sha256
        FROM postmarket_rank_calibration_runs r
        JOIN postmarket_rank_calibrators c ON c.calibration_id=r.calibration_id
        WHERE r.experiment_id=? AND r.split=? AND r.input_digest_sha256=?
        """,
        (experiment_id, split, digest),
    ).fetchone()
    if row is None:
        raise ValueError("calibration run does not exist for the exact input digest")
    report_raw = row[3]
    report_sha = hashlib.sha256(report_raw.encode()).hexdigest()
    if report_sha != row[4] or _canonical(json.loads(report_raw)) != report_raw:
        raise ValueError("calibration run report is corrupt or non-canonical")
    payload = {
        "schema_version": CALIBRATION_ARTIFACT_VERSION,
        "artifact_type": "postmarket_rank_calibration",
        "calibration_run_id": int(row[0]),
        "experiment_id": experiment_id,
        "split": split,
        "evaluated_at_utc": _utc(
            datetime.fromisoformat(row[1]), "evaluated_at_utc"
        ).isoformat(),
        "code_version": _revision(row[2], "calibration run code_version"),
        "input_digest_sha256": digest,
        "report_sha256": report_sha,
        "model_sha256": row[5],
        "report": json.loads(report_raw),
    }
    raw = (_canonical(payload) + "\n").encode()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    experiment_key = hashlib.sha256(experiment_id.encode()).hexdigest()[:12]
    destination = directory / (
        f"postmarket_rank_calibration_{experiment_key}_{split}_{digest[:16]}_v"
        f"{CALIBRATION_ARTIFACT_VERSION}.json"
    )
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != raw:
            raise ValueError("existing calibration artifact does not match exact run")
        return WrittenCalibrationArtifact(
            str(destination), artifact_sha256, False, experiment_id, split, digest,
            report_sha, row[5],
        )
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o444)
        try:
            os.link(temp_name, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != raw:
                raise ValueError("existing calibration artifact does not match exact run")
            return WrittenCalibrationArtifact(
                str(destination), artifact_sha256, False, experiment_id, split, digest,
                report_sha, row[5],
            )
        os.chmod(destination, 0o444)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return WrittenCalibrationArtifact(
        str(destination), artifact_sha256, True, experiment_id, split, digest,
        report_sha, row[5],
    )
