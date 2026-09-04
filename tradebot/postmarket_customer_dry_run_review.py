"""Export and append independent reviews of eligible customer dry-run cases.

Review cases contain only point-in-time evidence used by the router.  Outcome
marks and later bars are deliberately excluded.  Recording a review writes an
append-only attestation; it cannot send an alert or change a routing decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from tradebot.postmarket_customer_dry_run_campaign import (
    CAMPAIGN_FIELDS,
    CAMPAIGN_VERSION,
    parse_campaign_policy,
)
from tradebot.postmarket_customer_presentation import validate_customer_preview


CASE_VERSION = 3
REVIEW_VERSION = 1
REVIEW_ATTESTATION = (
    "I independently reviewed only the point-in-time evidence in this case, "
    "recorded every material concern, and did not use later outcomes."
)
RUBRIC_FIELDS = (
    "signal_relevance",
    "timeliness",
    "evidence_sufficiency",
    "explanation_clarity",
    "risk_disclosure",
)
REVIEWER_ROLES = frozenset({"owner", "owner_delegate", "independent_market_reviewer"})
EXCLUDED_EVIDENCE_CLASSES = ("outcome_marks", "later_bars", "later_headlines")


REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_customer_dry_run_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_version INTEGER NOT NULL,
    campaign_sha256 TEXT NOT NULL,
    case_evidence_sha256 TEXT NOT NULL,
    route_id INTEGER NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewer_role TEXT NOT NULL,
    reviewed_at_utc TEXT NOT NULL,
    independent_of_implementation INTEGER NOT NULL,
    blinded_to_future_outcomes INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    critical_finding INTEGER NOT NULL,
    notes TEXT NOT NULL,
    attestation TEXT NOT NULL,
    review_payload_sha256 TEXT NOT NULL UNIQUE,
    recorded_at_utc TEXT NOT NULL,
    UNIQUE(campaign_sha256,case_evidence_sha256,reviewer_id),
    CHECK (reviewer_role IN ('owner','owner_delegate','independent_market_reviewer')),
    CHECK (independent_of_implementation=1),
    CHECK (blinded_to_future_outcomes=1),
    CHECK (verdict IN ('APPROVE','REJECT')),
    CHECK (critical_finding IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_postmarket_customer_dry_run_reviews_campaign
    ON postmarket_customer_dry_run_reviews(campaign_sha256,review_id);
CREATE TRIGGER IF NOT EXISTS postmarket_customer_dry_run_reviews_no_update
BEFORE UPDATE ON postmarket_customer_dry_run_reviews BEGIN
    SELECT RAISE(ABORT, 'postmarket_customer_dry_run_reviews is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_customer_dry_run_reviews_no_delete
BEFORE DELETE ON postmarket_customer_dry_run_reviews BEGIN
    SELECT RAISE(ABORT, 'postmarket_customer_dry_run_reviews is append-only');
END;
"""


@dataclass(frozen=True)
class ReviewCaseEvidence:
    route_id: int
    session: str
    symbol: str
    direction: str
    decision: str
    presentation: str
    evaluated_at_utc: str
    candidate_id: int
    transition_id: int
    rank_run_id: int
    ordinal_rank: int | None
    evidence_score: float
    calibration_projection_id: int
    calibration_run_id: int
    calibration_version: int
    calibration_model_sha256: str
    calibrated_quality: float
    calibration_projected_at_utc: str
    calibration_code_version: str
    evidence_coverage_pct: float
    rank_components: dict[str, float]
    rank_penalties: dict[str, float]
    exclusion_reasons: tuple[str, ...]
    explanation: tuple[str, ...]
    lifecycle_state: str
    actionability: str
    transition_at_utc: str
    evidence_bar_open_ts_utc: str
    move_pct: float | None
    cumulative_notional: float | None
    data_age_seconds: float | None
    data_feed: str
    market_data_provider: str
    policy_sha256: str
    authorization_sha256: str
    runtime_router_revision: str
    customer_preview_payload: dict[str, object]
    customer_preview_sha256: str


@dataclass(frozen=True)
class RecordedReview:
    review_id: int
    verdict: str
    case_evidence_sha256: str
    review_payload_sha256: str


def _read_json_file(path: Path, context: str) -> tuple[dict[str, object], bytes]:
    if path.is_symlink():
        raise ValueError(f"{context} cannot be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{context} must be a regular file")
    raw = resolved.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{context} root must be an object")
    return payload, raw


def _campaign_scope(
    campaign: Mapping[str, object], campaign_sha256: str,
) -> tuple[tuple[str, ...], str, str, str]:
    if set(campaign) != CAMPAIGN_FIELDS or campaign["schema_version"] != CAMPAIGN_VERSION:
        raise ValueError("campaign contract is not exact")
    if campaign["status"] != "locked":
        raise ValueError("campaign must be locked")
    parse_campaign_policy(campaign["policy"])
    sessions = campaign["expected_sessions"]
    if not isinstance(sessions, list) or not sessions or any(
        not isinstance(item, str) for item in sessions
    ):
        raise ValueError("campaign expected_sessions are invalid")
    _sha256(campaign_sha256, "campaign_sha256")
    return (
        tuple(sessions),
        _sha256(campaign["delivery_policy_sha256"], "delivery_policy_sha256"),
        _sha256(
            campaign["owner_authorization_sha256"],
            "owner_authorization_sha256",
        ),
        _revision(
            campaign["policy"]["allowed_runtime_router_revisions"][0],
            "runtime_router_revision",
        ),
    )


def list_eligible_review_cases(
    conn: sqlite3.Connection,
    *,
    campaign: Mapping[str, object],
    campaign_sha256: str,
) -> tuple[dict[str, object], ...]:
    """List only point-in-time eligible routes in the locked campaign scope."""
    sessions, policy_sha, authorization_sha, revision = _campaign_scope(
        campaign, campaign_sha256
    )
    placeholders = ",".join("?" for _ in sessions)
    has_reviews = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='postmarket_customer_dry_run_reviews'"
    ).fetchone())
    review_select = "COUNT(r.review_id)" if has_reviews else "0"
    review_join = (
        "LEFT JOIN postmarket_customer_dry_run_reviews r "
        "ON r.route_id=d.route_id AND r.campaign_sha256=?"
        if has_reviews else ""
    )
    parameters: tuple[object, ...] = (
        (campaign_sha256,) if has_reviews else ()
    ) + (
        *sessions,
        policy_sha,
        authorization_sha,
        revision,
        campaign["calibration_model_sha256"],
    )
    rows = conn.execute(
        f"""
        SELECT d.route_id,d.session,d.symbol,d.direction,d.evaluated_at_utc,
               d.rank_run_id,d.candidate_id,d.transition_id,
               {review_select} AS review_count
        FROM postmarket_delivery_dry_runs d
        JOIN postmarket_delivery_dry_run_calibrations q ON q.route_id=d.route_id
        JOIN postmarket_customer_presentation_previews v ON v.route_id=d.route_id
        {review_join}
        WHERE d.session IN ({placeholders})
          AND d.decision='ELIGIBLE_FOR_DRY_RUN'
          AND d.presentation='ACTIONABLE'
          AND d.policy_sha256=? AND d.authorization_sha256=?
          AND d.runtime_router_revision=?
          AND q.model_sha256=?
        GROUP BY d.route_id
        ORDER BY d.session,d.evaluated_at_utc,d.route_id
        """,
        parameters,
    ).fetchall()
    return tuple({
        "route_id": int(row[0]), "session": row[1], "symbol": row[2],
        "direction": row[3], "evaluated_at_utc": row[4],
        "rank_run_id": int(row[5]), "candidate_id": int(row[6]),
        "transition_id": int(row[7]), "review_count": int(row[8]),
    } for row in rows)


def _digest(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object, name: str) -> str:
    if not isinstance(value, str) or not 7 <= len(value) <= 40 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{name} must be a concrete git revision")
    return value


def _json_object(raw: str, name: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _numeric_object(raw: str, name: str) -> dict[str, float]:
    value = _json_object(raw, name)
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value.values()
    ):
        raise ValueError(f"{name} values must be finite numbers")
    return {str(key): float(item) for key, item in value.items()}


def _json_strings(raw: str, name: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON string array")
    return tuple(value)


def ensure_review_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(REVIEW_SCHEMA)


def _validate_case_contract(case: Mapping[str, object]) -> dict[str, object]:
    case_fields = {
        "schema_version", "campaign_sha256", "exported_at_utc",
        "blinded_to_future_outcomes", "excluded_evidence_classes", "evidence",
        "case_evidence_sha256",
    }
    if set(case) != case_fields or case["schema_version"] != CASE_VERSION:
        raise ValueError("review case contract is not exact")
    if case["blinded_to_future_outcomes"] is not True:
        raise ValueError("review case must be blinded to future outcomes")
    if tuple(case["excluded_evidence_classes"]) != EXCLUDED_EVIDENCE_CLASSES:
        raise ValueError("review case excluded evidence classes are not exact")
    campaign = case["campaign_sha256"]
    _sha256(campaign, "campaign_sha256")
    _aware(case["exported_at_utc"], "exported_at_utc")
    evidence = case["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != set(ReviewCaseEvidence.__dataclass_fields__):
        raise ValueError("review case evidence fields are not exact")
    if _digest(evidence) != case["case_evidence_sha256"]:
        raise ValueError("review case evidence digest mismatch")
    return evidence


def build_review_case(
    conn: sqlite3.Connection,
    *,
    campaign_sha256: str,
    route_id: int,
    exported_at: datetime,
) -> dict[str, object]:
    """Build one blinded point-in-time case for an eligible route."""
    _sha256(campaign_sha256, "campaign_sha256")
    if exported_at.tzinfo is None or exported_at.utcoffset() is None:
        raise ValueError("exported_at must be timezone-aware")
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT d.route_id,d.session,d.symbol,d.direction,d.decision,d.presentation,
                   d.evaluated_at_utc,d.candidate_id,d.transition_id,d.rank_run_id,
                   d.policy_sha256,d.authorization_sha256,d.runtime_router_revision,
                   r.ordinal_rank,r.evidence_score,r.evidence_coverage_pct,
                   r.components_json,r.penalties_json,r.exclusion_reasons_json,
                   r.explanation_json,l.state,l.actionability,l.transition_at_utc,
                   o.evidence_bar_open_ts_utc,o.move_pct,o.cumulative_notional,
                   o.data_age_seconds,o.data_feed,o.market_data_provider,
                   q.projection_id,q.calibration_run_id,q.calibration_version,
                   q.model_sha256 AS calibration_model_sha256,
                   q.calibrated_quality,q.projected_at_utc,
                   q.code_version AS calibration_code_version,
                   v.payload_json AS customer_preview_payload_json,
                   v.payload_sha256 AS customer_preview_sha256,
                   v.presentation_version AS customer_preview_version,
                   v.license_semantic AS customer_preview_license_semantic,
                   v.generated_at_utc AS customer_preview_generated_at_utc
            FROM postmarket_delivery_dry_runs d
            JOIN postmarket_delivery_dry_run_calibrations q
              ON q.route_id=d.route_id
            JOIN postmarket_customer_presentation_previews v
              ON v.route_id=d.route_id
            JOIN postmarket_candidate_ranks r
              ON r.rank_run_id=d.rank_run_id AND r.candidate_id=d.candidate_id
            JOIN postmarket_candidate_lifecycle l ON l.transition_id=d.transition_id
            JOIN postmarket_candidate_lifecycle_observations o
              ON o.seq=r.observation_seq AND o.candidate_id=d.candidate_id
            JOIN postmarket_rank_calibration_projections p
              ON p.projection_id=q.projection_id
             AND p.rank_id=r.rank_id
             AND p.rank_run_id=d.rank_run_id
             AND p.candidate_id=d.candidate_id
             AND p.calibration_run_id=q.calibration_run_id
             AND p.calibration_version=q.calibration_version
             AND p.model_sha256=q.model_sha256
             AND p.calibrated_quality=q.calibrated_quality
             AND p.projected_at_utc=q.projected_at_utc
             AND p.code_version=q.code_version
            WHERE d.route_id=?
            """,
            (route_id,),
        ).fetchone()
    finally:
        conn.row_factory = original
    if row is None:
        raise ValueError("route does not have a complete point-in-time evidence join")
    if row["decision"] != "ELIGIBLE_FOR_DRY_RUN" or row["presentation"] != "ACTIONABLE":
        raise ValueError("only actionable eligible dry-run routes may be reviewed")
    evidence_score = float(row["evidence_score"])
    calibrated_quality = float(row["calibrated_quality"])
    evidence_coverage = float(row["evidence_coverage_pct"])
    if not math.isfinite(evidence_score):
        raise ValueError("evidence_score must be finite")
    if not math.isfinite(calibrated_quality) or not 0 <= calibrated_quality <= 1:
        raise ValueError("calibrated_quality must be between 0 and 1")
    if not math.isfinite(evidence_coverage) or not 0 <= evidence_coverage <= 100:
        raise ValueError("evidence_coverage_pct must be between 0 and 100")
    data_age = None if row["data_age_seconds"] is None else float(row["data_age_seconds"])
    if data_age is not None and (not math.isfinite(data_age) or data_age < 0):
        raise ValueError("data_age_seconds must be finite and non-negative")
    move_pct = None if row["move_pct"] is None else float(row["move_pct"])
    notional = None if row["cumulative_notional"] is None else float(row["cumulative_notional"])
    if move_pct is not None and not math.isfinite(move_pct):
        raise ValueError("move_pct must be finite")
    if notional is not None and (not math.isfinite(notional) or notional < 0):
        raise ValueError("cumulative_notional must be finite and non-negative")
    if row["direction"] not in {"up", "down"}:
        raise ValueError("direction must be up or down")
    customer_preview = validate_customer_preview(
        row["customer_preview_payload_json"],
        _sha256(row["customer_preview_sha256"], "customer_preview_sha256"),
    )
    if (
        customer_preview["symbol"] != str(row["symbol"]).strip().upper()
        or customer_preview["ordinal_rank"] != row["ordinal_rank"]
        or customer_preview["lifecycle"] != row["state"]
    ):
        raise ValueError("customer preview does not match route evidence")
    if (
        customer_preview["presentation_version"] != row["customer_preview_version"]
        or customer_preview["license_semantic"]
        != row["customer_preview_license_semantic"]
        or customer_preview["generated_at_utc"]
        != row["customer_preview_generated_at_utc"]
    ):
        raise ValueError("customer preview columns do not match its payload")
    expected_signal = (
        "POSTMARKET_STRENGTH" if row["direction"] == "up"
        else "POSTMARKET_WEAKNESS"
    )
    if customer_preview["signal"] != expected_signal:
        raise ValueError("customer preview direction does not match route evidence")
    evidence = ReviewCaseEvidence(
        route_id=int(row["route_id"]), session=row["session"], symbol=row["symbol"],
        direction=row["direction"], decision=row["decision"],
        presentation=row["presentation"],
        evaluated_at_utc=_aware(row["evaluated_at_utc"], "evaluated_at_utc").isoformat(),
        candidate_id=int(row["candidate_id"]), transition_id=int(row["transition_id"]),
        rank_run_id=int(row["rank_run_id"]),
        ordinal_rank=None if row["ordinal_rank"] is None else int(row["ordinal_rank"]),
        evidence_score=evidence_score,
        calibration_projection_id=int(row["projection_id"]),
        calibration_run_id=int(row["calibration_run_id"]),
        calibration_version=int(row["calibration_version"]),
        calibration_model_sha256=_sha256(
            row["calibration_model_sha256"], "calibration_model_sha256"
        ),
        calibrated_quality=calibrated_quality,
        calibration_projected_at_utc=_aware(
            row["projected_at_utc"], "calibration_projected_at_utc"
        ).isoformat(),
        calibration_code_version=_revision(
            row["calibration_code_version"], "calibration_code_version"
        ),
        evidence_coverage_pct=evidence_coverage,
        rank_components=_numeric_object(row["components_json"], "rank components"),
        rank_penalties=_numeric_object(row["penalties_json"], "rank penalties"),
        exclusion_reasons=_json_strings(row["exclusion_reasons_json"], "exclusions"),
        explanation=_json_strings(row["explanation_json"], "explanation"),
        lifecycle_state=row["state"], actionability=row["actionability"],
        transition_at_utc=_aware(row["transition_at_utc"], "transition_at_utc").isoformat(),
        evidence_bar_open_ts_utc=_aware(
            row["evidence_bar_open_ts_utc"], "evidence_bar_open_ts_utc"
        ).isoformat(),
        move_pct=move_pct,
        cumulative_notional=notional,
        data_age_seconds=data_age,
        data_feed=row["data_feed"], market_data_provider=row["market_data_provider"],
        policy_sha256=_sha256(row["policy_sha256"], "policy_sha256"),
        authorization_sha256=_sha256(
            row["authorization_sha256"], "authorization_sha256"
        ),
        runtime_router_revision=_revision(
            row["runtime_router_revision"], "runtime_router_revision"
        ),
        customer_preview_payload=customer_preview,
        customer_preview_sha256=_sha256(
            row["customer_preview_sha256"], "customer_preview_sha256"
        ),
    )
    evidence_payload = asdict(evidence)
    return {
        "schema_version": CASE_VERSION,
        "campaign_sha256": campaign_sha256,
        "exported_at_utc": exported_at.astimezone(timezone.utc).isoformat(),
        "blinded_to_future_outcomes": True,
        "excluded_evidence_classes": list(EXCLUDED_EVIDENCE_CLASSES),
        "evidence": evidence_payload,
        "case_evidence_sha256": _digest(evidence_payload),
    }


def write_review_case_atomic(path: Path | str, case: Mapping[str, object]) -> str:
    _validate_case_contract(case)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("review case output cannot be a symlink")
    raw = (json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def record_independent_review(
    conn: sqlite3.Connection,
    *,
    case: Mapping[str, object],
    assessment: Mapping[str, object],
    recorded_at: datetime | None = None,
) -> RecordedReview:
    """Validate and append one human attestation bound to the current case."""
    evidence = _validate_case_contract(case)
    rebuilt = build_review_case(
        conn,
        campaign_sha256=str(case["campaign_sha256"]),
        route_id=int(evidence["route_id"]),
        exported_at=_aware(case["exported_at_utc"], "exported_at_utc"),
    )
    if rebuilt["case_evidence_sha256"] != case["case_evidence_sha256"]:
        raise ValueError("review case no longer matches persisted point-in-time evidence")

    fields = {
        "schema_version", "reviewer_id", "reviewer_role", "reviewed_at_utc",
        "independent_of_implementation", "blinded_to_future_outcomes", "rubric",
        "critical_finding", "notes", "attestation",
    }
    if set(assessment) != fields or assessment["schema_version"] != REVIEW_VERSION:
        raise ValueError("review assessment contract is not exact")
    reviewer = assessment["reviewer_id"]
    role = assessment["reviewer_role"]
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer_id must be non-empty")
    if role not in REVIEWER_ROLES:
        raise ValueError("reviewer_role is invalid")
    if assessment["independent_of_implementation"] is not True:
        raise ValueError("reviewer must attest independence from implementation")
    if assessment["blinded_to_future_outcomes"] is not True:
        raise ValueError("reviewer must attest outcome blinding")
    if assessment["attestation"] != REVIEW_ATTESTATION:
        raise ValueError("review attestation is not exact")
    rubric = assessment["rubric"]
    if not isinstance(rubric, dict) or set(rubric) != set(RUBRIC_FIELDS) or any(
        value not in {"PASS", "FAIL"} for value in rubric.values()
    ):
        raise ValueError("review rubric must contain exact PASS/FAIL fields")
    if not isinstance(assessment["critical_finding"], bool):
        raise ValueError("critical_finding must be boolean")
    if not isinstance(assessment["notes"], str):
        raise ValueError("notes must be a string")
    reviewed_at = _aware(assessment["reviewed_at_utc"], "reviewed_at_utc")
    recorded = recorded_at or datetime.now(timezone.utc)
    if recorded.tzinfo is None or recorded.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    recorded = recorded.astimezone(timezone.utc)
    if reviewed_at < _aware(case["exported_at_utc"], "exported_at_utc"):
        raise ValueError("review cannot precede case export")
    if reviewed_at > recorded:
        raise ValueError("reviewed_at cannot be in the future")
    verdict = (
        "APPROVE"
        if not assessment["critical_finding"] and all(value == "PASS" for value in rubric.values())
        else "REJECT"
    )
    review_payload = {
        "review_version": REVIEW_VERSION,
        "campaign_sha256": case["campaign_sha256"],
        "case_evidence_sha256": case["case_evidence_sha256"],
        "route_id": evidence["route_id"],
        "reviewer_id": reviewer.strip(),
        "reviewer_role": role,
        "reviewed_at_utc": reviewed_at.isoformat(),
        "independent_of_implementation": True,
        "blinded_to_future_outcomes": True,
        "verdict": verdict,
        "rubric": rubric,
        "critical_finding": assessment["critical_finding"],
        "notes": assessment["notes"],
        "attestation": REVIEW_ATTESTATION,
    }
    payload_sha = _digest(review_payload)
    ensure_review_schema(conn)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO postmarket_customer_dry_run_reviews
              (review_version,campaign_sha256,case_evidence_sha256,route_id,session,
               symbol,direction,reviewer_id,reviewer_role,reviewed_at_utc,
               independent_of_implementation,blinded_to_future_outcomes,verdict,
               rubric_json,critical_finding,notes,attestation,review_payload_sha256,
               recorded_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                REVIEW_VERSION, case["campaign_sha256"], case["case_evidence_sha256"],
                evidence["route_id"], evidence["session"], evidence["symbol"],
                evidence["direction"], reviewer.strip(), role, reviewed_at.isoformat(),
                1, 1, verdict, json.dumps(rubric, sort_keys=True, separators=(",", ":")),
                int(assessment["critical_finding"]), assessment["notes"],
                REVIEW_ATTESTATION, payload_sha, recorded.isoformat(),
            ),
        )
    return RecordedReview(
        review_id=int(cursor.lastrowid), verdict=verdict,
        case_evidence_sha256=str(case["case_evidence_sha256"]),
        review_payload_sha256=payload_sha,
    )


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list eligible campaign cases")
    list_parser.add_argument("--db", required=True, type=Path)
    list_parser.add_argument("--campaign", required=True, type=Path)

    export_parser = subparsers.add_parser("export", help="export one blinded case")
    export_parser.add_argument("--db", required=True, type=Path)
    export_parser.add_argument("--campaign", required=True, type=Path)
    export_parser.add_argument("--route-id", required=True, type=int)
    export_parser.add_argument("--output", required=True, type=Path)

    record_parser = subparsers.add_parser("record", help="append one review")
    record_parser.add_argument("--db", required=True, type=Path)
    record_parser.add_argument("--case", required=True, type=Path)
    record_parser.add_argument("--assessment", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command in {"list", "export"}:
        campaign, campaign_raw = _read_json_file(args.campaign, "campaign")
        campaign_sha = hashlib.sha256(campaign_raw).hexdigest()
        conn = _readonly_connection(args.db)
        try:
            if args.command == "list":
                cases = list_eligible_review_cases(
                    conn, campaign=campaign, campaign_sha256=campaign_sha
                )
                print(json.dumps(cases, indent=2, sort_keys=True))
                return 0
            eligible = {
                item["route_id"] for item in list_eligible_review_cases(
                    conn, campaign=campaign, campaign_sha256=campaign_sha
                )
            }
            if args.route_id not in eligible:
                raise ValueError("route is not eligible in the locked campaign scope")
            case = build_review_case(
                conn,
                campaign_sha256=campaign_sha,
                route_id=args.route_id,
                exported_at=datetime.now(timezone.utc),
            )
        finally:
            conn.close()
        artifact_sha = write_review_case_atomic(args.output, case)
        print(json.dumps({
            "route_id": args.route_id,
            "case_evidence_sha256": case["case_evidence_sha256"],
            "artifact_sha256": artifact_sha,
            "path": str(args.output),
        }, sort_keys=True, separators=(",", ":")))
        return 0

    case, _ = _read_json_file(args.case, "review case")
    assessment, _ = _read_json_file(args.assessment, "review assessment")
    conn = sqlite3.connect(args.db, timeout=30)
    try:
        result = record_independent_review(
            conn, case=case, assessment=assessment
        )
    finally:
        conn.close()
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
