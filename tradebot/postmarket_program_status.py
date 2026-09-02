"""Read-only, fail-closed progress ledger for the signal-quality program.

This module does not create experiments, select evidence, tune thresholds,
contact providers, or enable delivery.  It inventories authoritative database
rows and immutable reports so process health cannot be mistaken for customer
readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tradebot.postmarket_customer_dry_run_gate import (
    evaluate_customer_dry_run_gate,
)
from tradebot.postmarket_customer_dry_run_campaign import (
    CAMPAIGN_FIELDS,
    CAMPAIGN_VERSION,
    parse_campaign_policy,
)


STATUS_VERSION = 1
MIN_CLEAN_SESSIONS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = REPO_ROOT / "data" / "postmarket_shadow.db"
DEFAULT_AUDIT_DIR = REPO_ROOT / "data" / "postmarket_audits"
DEFAULT_EVIDENCE_DIR = REPO_ROOT / "data" / "postmarket_evidence"
STATE_COMPLETE = "COMPLETE"
STATE_INCOMPLETE = "INCOMPLETE"
STATE_ERROR = "ERROR"


@dataclass(frozen=True)
class ProgramMilestone:
    code: str
    state: str
    observed: object
    required: object
    evidence: str


@dataclass(frozen=True)
class SignalQualityProgramStatus:
    status_version: int
    generated_at_utc: str
    database: str
    audit_directory: str
    evidence_directory: str
    database_integrity_ok: bool
    evidence_collection_complete: bool
    eligible_for_customer_delivery_review: bool
    customer_delivery_enabled: bool
    milestones: tuple[ProgramMilestone, ...]
    blockers: tuple[str, ...]
    next_action: str


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _regular_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"artifact cannot be a symlink: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"artifact must be a regular file: {path}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"artifact root must be an object: {path}")
    return payload


def _latest_session_reports(
    paths: Iterable[Path], *, version_field: str
) -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for path in sorted(paths):
        payload = _regular_json(path)
        session = payload.get("session")
        version = payload.get(version_field)
        if (
            not isinstance(session, str)
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
        ):
            raise ValueError(f"artifact session/version identity is invalid: {path}")
        try:
            parsed = datetime.strptime(session, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"artifact session is invalid: {path}") from exc
        if parsed.strftime("%Y-%m-%d") != session:
            raise ValueError(f"artifact session is not canonical: {path}")
        current = latest.get(session)
        if current is None or version > current[0]:
            latest[session] = (version, payload)
        elif version == current[0] and payload != current[1]:
            raise ValueError(
                f"conflicting artifacts share session/version {session}/v{version}"
            )
    return {session: item[1] for session, item in latest.items()}


def _latest_quality_reports(paths: Iterable[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for path in sorted(paths):
        payload = _regular_json(path)
        session = payload.get("session")
        stream = payload.get("candidate_stream")
        version = payload.get("report_version")
        if (
            not isinstance(session, str)
            or stream not in {"marketwide", "scheduled"}
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
        ):
            raise ValueError(f"quality report identity is invalid: {path}")
        key = (session, stream)
        current = latest.get(key)
        if current is None or version > current[0]:
            latest[key] = (version, payload)
        elif version == current[0] and payload != current[1]:
            raise ValueError(
                f"conflicting quality reports share identity {session}/{stream}/v{version}"
            )
    return {key: item[1] for key, item in latest.items()}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str, where: str = "1") -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])


def _validated_report(
    raw: object,
    stored_sha256: object,
    context: str,
) -> dict[str, Any]:
    if not isinstance(raw, str) or not isinstance(stored_sha256, str):
        raise ValueError(f"{context} report storage is invalid")
    if hashlib.sha256(raw.encode()).hexdigest() != stored_sha256:
        raise ValueError(f"{context} report digest does not match stored JSON")
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} report_json is malformed") from exc
    if not isinstance(report, dict):
        raise ValueError(f"{context} report_json must be an object")
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if canonical != raw:
        raise ValueError(f"{context} report_json is not canonical")
    return report


def _passed_empirical_holdouts(
    conn: sqlite3.Connection,
    eligible_experiment_ids: frozenset[str],
) -> frozenset[str]:
    if not _table_exists(conn, "postmarket_rank_empirical_runs"):
        return frozenset()
    passed: set[str] = set()
    for experiment_id, input_digest, raw, report_sha256 in conn.execute(
        """
        SELECT experiment_id,input_digest_sha256,report_json,report_sha256
        FROM postmarket_rank_empirical_runs WHERE split='holdout'
        """
    ).fetchall():
        report = _validated_report(raw, report_sha256, "empirical holdout")
        if (
            report.get("experiment_id") != experiment_id
            or report.get("input_digest_sha256") != input_digest
        ):
            raise ValueError("empirical holdout report identity does not match its row")
        if (
            experiment_id in eligible_experiment_ids
            and report.get("split") == "holdout"
            and report.get("holdout_unblinded") is True
            and report.get("passed_locked_policy") is True
            and report.get("blocking_reasons") == []
        ):
            passed.add(str(experiment_id))
    return frozenset(passed)


def _passed_calibration_holdouts(
    conn: sqlite3.Connection,
    eligible_experiment_ids: frozenset[str],
) -> frozenset[str]:
    if not _table_exists(conn, "postmarket_rank_calibration_runs"):
        return frozenset()
    passed: set[str] = set()
    for experiment_id, input_digest, raw, report_sha256 in conn.execute(
        """
        SELECT experiment_id,input_digest_sha256,report_json,report_sha256
        FROM postmarket_rank_calibration_runs WHERE split='holdout'
        """
    ).fetchall():
        report = _validated_report(raw, report_sha256, "calibration holdout")
        if (
            report.get("experiment_id") != experiment_id
            or report.get("input_digest_sha256") != input_digest
        ):
            raise ValueError("calibration holdout report identity does not match its row")
        if (
            experiment_id in eligible_experiment_ids
            and report.get("split") == "holdout"
            and report.get("holdout_unblinded") is True
            and report.get("calibrated_quality_claim_valid") is True
            and report.get("blocking_reasons") == []
        ):
            passed.add(str(experiment_id))
    return frozenset(passed)


def _holdout_label_progress(
    conn: sqlite3.Connection,
) -> tuple[int, int, frozenset[str]]:
    """Return best latest-label count, its floor, and every passing experiment."""
    required_tables = {
        "postmarket_rank_experiments",
        "postmarket_independent_labels",
    }
    if any(not _table_exists(conn, table) for table in required_tables):
        return 0, 0, frozenset()
    best_observed = best_required = 0
    passed_experiments: set[str] = set()
    for experiment_id, raw_sessions, raw_policy in conn.execute(
        """
        SELECT experiment_id,holdout_sessions_json,policy_json
        FROM postmarket_rank_experiments ORDER BY experiment_id
        """
    ).fetchall():
        try:
            sessions = json.loads(raw_sessions)
            policy = json.loads(raw_policy)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("locked experiment JSON is malformed") from exc
        if (
            not isinstance(sessions, list)
            or not sessions
            or not isinstance(policy, dict)
            or isinstance(policy.get("min_definitive_labels"), bool)
            or not isinstance(policy.get("min_definitive_labels"), int)
            or policy["min_definitive_labels"] <= 0
        ):
            raise ValueError("locked experiment label floor is invalid")
        placeholders = ",".join("?" for _ in sessions)
        observed = int(conn.execute(
            f"""
            WITH latest AS (
              SELECT session,symbol,MAX(revision) AS revision
              FROM postmarket_independent_labels
              WHERE experiment_id=? AND session IN ({placeholders})
              GROUP BY session,symbol
            )
            SELECT COUNT(*)
            FROM postmarket_independent_labels AS labels
            JOIN latest
              ON latest.session=labels.session
             AND latest.symbol=labels.symbol
             AND latest.revision=labels.revision
            WHERE labels.experiment_id=?
              AND labels.classification IN ('eligible','ineligible')
            """,
            (experiment_id, *sessions, experiment_id),
        ).fetchone()[0])
        required = int(policy["min_definitive_labels"])
        if best_required == 0 or observed > best_observed or (
            observed == best_observed and required < best_required
        ):
            best_observed, best_required = observed, required
        if observed >= required:
            passed_experiments.add(str(experiment_id))
    return best_observed, best_required, frozenset(passed_experiments)


def _customer_review_progress(
    conn: sqlite3.Connection,
    evidence_dir: Path,
) -> tuple[dict[str, object], object, bool]:
    """Count distinct independent cases against their exact locked campaign."""
    if not _table_exists(conn, "postmarket_customer_dry_run_reviews"):
        return (
            {"reviewed_cases": 0, "distinct_symbols": 0},
            "locked customer campaign floor unavailable",
            False,
        )
    campaigns: list[tuple[bool, float, dict[str, object], dict[str, int]]] = []
    if evidence_dir.exists():
        for path in sorted(evidence_dir.rglob("*.json")):
            payload = _regular_json(path)
            campaign_markers = {"campaign_id", "expected_sessions", "policy"}
            if not campaign_markers <= set(payload):
                continue
            if (
                set(payload) != CAMPAIGN_FIELDS
                or payload.get("schema_version") != CAMPAIGN_VERSION
                or payload.get("status") != "locked"
            ):
                raise ValueError(f"customer campaign contract is invalid: {path}")
            policy = parse_campaign_policy(payload["policy"])
            digest = hashlib.sha256(path.resolve(strict=True).read_bytes()).hexdigest()
            reviewed_cases, distinct_symbols = conn.execute(
                """
                SELECT COUNT(DISTINCT case_evidence_sha256),
                       COUNT(DISTINCT symbol)
                FROM postmarket_customer_dry_run_reviews
                WHERE campaign_sha256=?
                  AND reviewer_role='independent_market_reviewer'
                  AND independent_of_implementation=1
                  AND blinded_to_future_outcomes=1
                """,
                (digest,),
            ).fetchone()
            required = {
                "reviewed_cases": policy.min_independently_reviewed_cases,
                "distinct_symbols": policy.min_distinct_reviewed_symbols,
            }
            observed = {
                "campaign_id": payload["campaign_id"],
                "campaign_sha256": digest,
                "reviewed_cases": int(reviewed_cases),
                "distinct_symbols": int(distinct_symbols),
            }
            passed = (
                int(reviewed_cases) >= required["reviewed_cases"]
                and int(distinct_symbols) >= required["distinct_symbols"]
            )
            progress = min(
                int(reviewed_cases) / required["reviewed_cases"],
                int(distinct_symbols) / required["distinct_symbols"],
            )
            campaigns.append((passed, progress, observed, required))
    if not campaigns:
        return (
            {"reviewed_cases": 0, "distinct_symbols": 0},
            "locked customer campaign floor unavailable",
            False,
        )
    passed, _, observed, required = max(
        campaigns,
        key=lambda item: (
            item[0],
            item[1],
            item[2]["reviewed_cases"],
            item[2]["distinct_symbols"],
            item[2]["campaign_sha256"],
        ),
    )
    return observed, required, passed


def _verified_ready_customer_gates(
    evidence_dir: Path,
    audit_dir: Path,
    db_path: Path,
) -> int:
    """Reproduce every claimed-ready gate from its exact digest-bound inputs."""
    if not evidence_dir.exists():
        return 0
    paths = sorted(evidence_dir.rglob("*.json"))
    digest_index: dict[str, list[Path]] = {}
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"evidence artifact cannot be a symlink: {path}")
        raw = path.resolve(strict=True).read_bytes()
        digest_index.setdefault(hashlib.sha256(raw).hexdigest(), []).append(path)
        payloads.append((path, _regular_json(path)))

    def exact_path(digest: object, context: str) -> Path:
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"{context} digest is invalid")
        matches = digest_index.get(digest, [])
        if len(matches) != 1:
            raise ValueError(
                f"{context} digest must resolve to exactly one artifact; "
                f"matches={len(matches)}"
            )
        return matches[0]

    ready = 0
    for path, payload in payloads:
        if "ready_for_customer_delivery_review" not in payload:
            continue
        if payload.get("ready_for_customer_delivery_review") is not True:
            continue
        if payload.get("customer_delivery_enabled") is not False:
            continue
        upstream = payload.get("upstream_digests")
        controls = payload.get("control_digests")
        if not isinstance(upstream, list) or not isinstance(controls, list):
            raise ValueError(f"customer gate digest inventory is invalid: {path}")
        upstream_map = {
            item.get("kind"): item.get("sha256")
            for item in upstream
            if isinstance(item, dict)
        }
        campaign_path = exact_path(payload.get("campaign_sha256"), "campaign")
        evidence_set_path = exact_path(
            upstream_map.get("discovery_evidence_set"), "discovery evidence set"
        )
        discovery_gate_path = exact_path(
            upstream_map.get("discovery_evidence_gate"), "discovery gate"
        )
        control_paths = tuple(
            exact_path(item.get("sha256"), f"control[{index}]")
            for index, item in enumerate(controls)
            if isinstance(item, dict)
        )
        if len(control_paths) != 4:
            raise ValueError("customer gate must bind exactly four controls")
        try:
            evaluated_at = datetime.fromisoformat(payload["generated_at_utc"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("customer gate generated_at_utc is invalid") from exc
        recomputed = evaluate_customer_dry_run_gate(
            campaign_path=campaign_path,
            upstream_discovery_evidence_set_path=evidence_set_path,
            upstream_discovery_evidence_gate_path=discovery_gate_path,
            audit_dir=audit_dir,
            control_paths=control_paths,
            db_path=db_path,
            now=evaluated_at,
            gate_code_version=payload.get("gate_code_version"),
        )
        if asdict(recomputed) != payload:
            raise ValueError(f"customer gate report is not reproducible: {path}")
        if recomputed.ready_for_customer_delivery_review:
            ready += 1
    return ready


def _milestone(
    code: str,
    observed: object,
    required: object,
    passed: bool,
    evidence: str,
) -> ProgramMilestone:
    return ProgramMilestone(
        code,
        STATE_COMPLETE if passed else STATE_INCOMPLETE,
        observed,
        required,
        evidence,
    )


def build_program_status(
    db_path: Path | str,
    audit_dir: Path | str,
    evidence_dir: Path | str,
    *,
    generated_at: datetime,
) -> SignalQualityProgramStatus:
    generated = _utc(generated_at, "generated_at")
    database = Path(db_path).resolve()
    audits = Path(audit_dir).resolve()
    evidence = Path(evidence_dir).resolve()
    milestones: list[ProgramMilestone] = []
    blockers: list[str] = []
    integrity_ok = False

    try:
        if database.is_symlink() or not database.is_file():
            raise ValueError("postmarket database must be a regular non-symlink file")
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            integrity = [row[0] for row in conn.execute("PRAGMA quick_check")]
            integrity_ok = integrity == ["ok"]
            milestones.append(_milestone(
                "DATABASE_INTEGRITY",
                integrity,
                ["ok"],
                integrity_ok,
                "SQLite quick_check over the exact read-only database",
            ))
            if not integrity_ok:
                raise ValueError(f"SQLite quick_check failed: {integrity!r}")

            discovery = _latest_session_reports(
                audits.glob("postmarket_discovery_audit_*_v*.json"),
                version_field="audit_version",
            )
            clean_sessions = {
                session
                for session, report in discovery.items()
                if report.get("operational_clean") is True
                and report.get("session_evidence_eligible") is True
                and report.get("issues") == []
            }
            milestones.append(_milestone(
                "CLEAN_DISCOVERY_SESSIONS",
                len(clean_sessions),
                MIN_CLEAN_SESSIONS,
                len(clean_sessions) >= MIN_CLEAN_SESSIONS,
                f"eligible_sessions={sorted(clean_sessions)!r}",
            ))

            quality_reports = _latest_quality_reports(
                audits.glob("postmarket_quality_*_v*.json")
            )
            quality_sessions = {
                session
                for (session, stream), report in quality_reports.items()
                if stream == "marketwide"
                and report.get("operational_complete") is True
                and report.get("evidence_eligible") is True
                and report.get("issue_codes") == []
            }
            quality_clean_sessions = clean_sessions & quality_sessions
            milestones.append(_milestone(
                "APPEND_ONLY_OUTCOME_QUALITY",
                len(quality_clean_sessions),
                MIN_CLEAN_SESSIONS,
                len(quality_clean_sessions) >= MIN_CLEAN_SESSIONS,
                "eligible marketwide outcome reports intersected with clean sessions; "
                f"sessions={sorted(quality_clean_sessions)!r}",
            ))

            census_reports = _latest_session_reports(
                audits.glob("postmarket_recall_census_*_v*.json"),
                version_field="report_version",
            )
            census_sessions = {
                session
                for session, report in census_reports.items()
                if report.get("operational_complete") is True
                and report.get("evidence_eligible") is True
                and report.get("issue_codes") == []
            }
            census_clean_sessions = clean_sessions & census_sessions
            milestones.append(_milestone(
                "FULL_UNIVERSE_RECALL_CENSUS",
                len(census_clean_sessions),
                MIN_CLEAN_SESSIONS,
                len(census_clean_sessions) >= MIN_CLEAN_SESSIONS,
                "eligible retrospective censuses intersected with clean sessions; "
                f"sessions={sorted(census_clean_sessions)!r}",
            ))

            provider_reports = _latest_session_reports(
                audits.glob("postmarket_recall_provider_*_v*.json"),
                version_field="attempt",
            )
            provider_sessions = {
                session
                for session, report in provider_reports.items()
                if report.get("operational_complete") is True
                and report.get("evidence_eligible") is True
                and report.get("issue_codes") == []
            }
            covered_clean_sessions = clean_sessions & provider_sessions
            milestones.append(_milestone(
                "INDEPENDENT_PROVIDER_PROOFS",
                len(covered_clean_sessions),
                MIN_CLEAN_SESSIONS,
                len(covered_clean_sessions) >= MIN_CLEAN_SESSIONS,
                "counts only sessions that are both clean discovery evidence and "
                f"eligible provider proof; sessions={sorted(covered_clean_sessions)!r}",
            ))

            experiments = _count(conn, "postmarket_rank_experiments")
            milestones.append(_milestone(
                "LOCKED_EMPIRICAL_EXPERIMENT",
                experiments,
                1,
                experiments >= 1,
                "append-only postmarket_rank_experiments rows",
            ))
            context_rows = _count(conn, "postmarket_candidate_context")
            lifecycle_rows = _count(conn, "postmarket_candidate_lifecycle")
            rank_rows = _count(conn, "postmarket_candidate_ranks")
            feature_pipeline_complete = min(context_rows, lifecycle_rows, rank_rows) > 0
            milestones.append(_milestone(
                "CONTEXT_LIFECYCLE_RANK_EVIDENCE",
                {
                    "context_rows": context_rows,
                    "lifecycle_rows": lifecycle_rows,
                    "rank_rows": rank_rows,
                },
                "non-empty append-only rows in all three ledgers",
                feature_pipeline_complete,
                "versioned context features, lifecycle transitions, and decomposed ranks",
            ))

            (
                holdout_labels,
                holdout_floor,
                label_ready_experiments,
            ) = _holdout_label_progress(conn)
            milestones.append(_milestone(
                "BLINDED_HOLDOUT_LABELS",
                holdout_labels,
                holdout_floor or "locked policy floor unavailable",
                bool(label_ready_experiments),
                "definitive labels are compared with their exact locked experiment floor",
            ))

            empirical_experiments = _passed_empirical_holdouts(
                conn, label_ready_experiments
            )
            milestones.append(_milestone(
                "EMPIRICAL_HOLDOUT_PASS",
                len(empirical_experiments),
                1,
                bool(empirical_experiments),
                "digest-valid unblinded holdout pass linked to an experiment "
                "whose latest independent labels meet its locked floor",
            ))
            calibration_experiments = _passed_calibration_holdouts(
                conn, empirical_experiments
            )
            milestones.append(_milestone(
                "CALIBRATION_HOLDOUT_PASS",
                len(calibration_experiments),
                1,
                bool(calibration_experiments),
                "digest-valid holdout calibration linked to the same "
                "label-ready, empirically passing experiment",
            ))

            review_progress, review_floor, reviews_pass = _customer_review_progress(
                conn, evidence
            )
            milestones.append(_milestone(
                "INDEPENDENT_CUSTOMER_CASE_REVIEWS",
                review_progress,
                review_floor,
                reviews_pass,
                "distinct blinded independent cases and symbols are compared with "
                "their exact locked customer campaign floors; final gate still "
                "revalidates every review payload",
            ))
        finally:
            conn.close()

        ready_gates = _verified_ready_customer_gates(evidence, audits, database)
        milestones.append(_milestone(
            "CUSTOMER_DELIVERY_REVIEW_GATE",
            ready_gates,
            1,
            ready_gates >= 1,
            "v2 gate must reproduce from exact campaign, upstream package, controls, "
            "audits, reviews, and database while customer_delivery_enabled=false",
        ))
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        milestones.append(ProgramMilestone(
            "EVIDENCE_LEDGER_VALIDATION",
            STATE_ERROR,
            type(exc).__name__,
            "no validation errors",
            str(exc)[:1000],
        ))

    for item in milestones:
        if item.state != STATE_COMPLETE:
            blockers.append(item.code)
    evidence_complete = integrity_ok and not blockers
    eligible_review = evidence_complete and any(
        item.code == "CUSTOMER_DELIVERY_REVIEW_GATE"
        and item.state == STATE_COMPLETE
        for item in milestones
    )
    next_action_by_code = {
        "DATABASE_INTEGRITY": "Repair or restore the evidence database before collecting more evidence.",
        "CLEAN_DISCOVERY_SESSIONS": "Continue the unchanged shadow campaign until at least ten clean sessions exist.",
        "APPEND_ONLY_OUTCOME_QUALITY": "Finish next-session outcome resolution and immutable quality reports for every clean session.",
        "FULL_UNIVERSE_RECALL_CENSUS": "Run the finalized full-universe census for every clean session.",
        "INDEPENDENT_PROVIDER_PROOFS": "Complete licensing and run independent provider proofs for every clean session.",
        "LOCKED_EMPIRICAL_EXPERIMENT": "Prospectively lock development and holdout sessions before labeling results.",
        "CONTEXT_LIFECYCLE_RANK_EVIDENCE": "Collect complete context, lifecycle, and decomposed rank rows in shadow mode.",
        "BLINDED_HOLDOUT_LABELS": "Import independently produced, rank-blind holdout label manifests.",
        "EMPIRICAL_HOLDOUT_PASS": "Unblind once, then evaluate the frozen empirical holdout without retuning.",
        "CALIBRATION_HOLDOUT_PASS": "Fit on development only and pass the frozen calibration on holdout.",
        "INDEPENDENT_CUSTOMER_CASE_REVIEWS": "Run the isolated dry-run campaign and collect independent case reviews.",
        "CUSTOMER_DELIVERY_REVIEW_GATE": "Seal and independently reproduce the final customer-delivery review gate.",
        "EVIDENCE_LEDGER_VALIDATION": "Resolve malformed or conflicting evidence before interpreting progress.",
    }
    priority_blocker = (
        "EVIDENCE_LEDGER_VALIDATION"
        if "EVIDENCE_LEDGER_VALIDATION" in blockers
        else (blockers[0] if blockers else "")
    )
    next_action = (
        "Evidence is eligible for a separate owner review; customer delivery remains disabled."
        if eligible_review
        else next_action_by_code.get(
            priority_blocker, "No next action could be derived."
        )
    )
    return SignalQualityProgramStatus(
        STATUS_VERSION,
        generated.isoformat(),
        str(database),
        str(audits),
        str(evidence),
        integrity_ok,
        evidence_complete,
        eligible_review,
        False,
        tuple(milestones),
        tuple(blockers),
        next_action,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    args = parser.parse_args(argv)
    report = build_program_status(
        args.database,
        args.audit_dir,
        args.evidence_dir,
        generated_at=datetime.now(timezone.utc),
    )
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.eligible_for_customer_delivery_review else 1


if __name__ == "__main__":
    raise SystemExit(main())
