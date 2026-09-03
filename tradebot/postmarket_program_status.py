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
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
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
from tradebot.postmarket_lifecycle import LIFECYCLE_VERSION
from tradebot.postmarket_empirical import (
    CALENDAR as EMPIRICAL_CALENDAR,
    EMPIRICAL_VERSION,
    ET as EMPIRICAL_ET,
    LABEL_METHODS,
    TECHNICAL_MIN_RECALL,
)
from tradebot.postmarket_rank import (
    COMPONENT_WEIGHTS,
    RANK_VERSION,
    REQUIRED_CONTEXT_VERSION,
    rank_contract_sha256,
    rank_thresholds,
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


def _provider_qualification_is_bound(report: dict[str, Any]) -> bool:
    source = report.get("source")
    if not isinstance(source, dict):
        return False
    digest = source.get("qualification_manifest_sha256")
    return bool(
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


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


_CONTEXT_FEATURE_COLUMNS = {
    "context_id",
    "candidate_id",
    "context_version",
    "session",
    "symbol",
    "direction",
    "lifecycle_observation_seq",
    "lifecycle_evidence_bar_open_ts_utc",
    "status",
    "volatility_status",
    "market_relative_status",
    "sector_relative_status",
    "liquidity_status",
    "catalyst_status",
    "catalyst_sources_json",
    "catalyst_details_json",
    "catalyst_coverage_json",
    "data_confidence_status",
    "data_confidence_coverage_pct",
    "data_confidence_components_json",
}
_LIFECYCLE_COLUMNS = {
    "transition_id",
    "candidate_id",
    "lifecycle_version",
    "session",
    "symbol",
    "direction",
    "state",
    "actionability",
    "evidence_bar_open_ts_utc",
}
_LIFECYCLE_OBSERVATION_COLUMNS = {
    "seq",
    "candidate_id",
    "lifecycle_version",
    "session",
    "symbol",
    "evidence_bar_open_ts_utc",
}
_RANK_COLUMNS = {
    "rank_id",
    "rank_run_id",
    "candidate_id",
    "context_id",
    "transition_id",
    "observation_seq",
    "session",
    "symbol",
    "direction",
    "lifecycle_state",
    "rankable",
    "ordinal_rank",
    "evidence_score",
    "raw_component_score",
    "penalty_total",
    "evidence_coverage_pct",
    "components_json",
    "penalties_json",
    "exclusion_reasons_json",
    "explanation_json",
}
_RANK_RUN_COLUMNS = {
    "rank_run_id",
    "rank_version",
    "rank_contract_sha256",
    "status",
    "weights_json",
    "thresholds_json",
}
_LOCKED_EXPERIMENT_COLUMNS = {
    "experiment_id",
    "empirical_version",
    "status",
    "created_at_utc",
    "created_by",
    "rank_version",
    "rank_contract_sha256",
    "label_method",
    "development_sessions_json",
    "holdout_sessions_json",
    "eligibility_rule_json",
    "selection_rule_json",
    "policy_json",
    "manifest_sha256",
}
_REQUIRED_RANK_COMPONENTS = set(COMPONENT_WEIGHTS)
_REQUIRED_CONFIDENCE_COMPONENTS = {
    "completed_bar_gate",
    "sip_bar_provenance",
    "operational_fetches",
    "quote_temporal_integrity",
    "volatility_history",
    "market_benchmark",
    "rth_liquidity",
    "asset_point_in_time",
}
_CATALYST_COVERAGE_FAMILIES = {
    "earnings",
    "filings",
    "guidance",
    "news",
    "regulatory",
    "analyst",
}
_RANKABLE_LIFECYCLE_STATES = {"CONFIRMED", "STRENGTHENING", "REQUALIFIED"}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _json_value(raw: object, expected: type) -> object | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, expected) else None


def _finite_number(raw: object) -> bool:
    return (
        not isinstance(raw, bool)
        and isinstance(raw, (int, float))
        and math.isfinite(float(raw))
    )


def _locked_experiment_progress(
    conn: sqlite3.Connection,
) -> tuple[dict[str, object], frozenset[str]]:
    """Validate immutable experiment contracts instead of counting rows."""
    table = "postmarket_rank_experiments"
    rows = _count(conn, table)
    missing_schema = sorted(_LOCKED_EXPERIMENT_COLUMNS - _table_columns(conn, table))
    observed: dict[str, object] = {
        "rows": rows,
        "valid_locked_experiments": 0,
        "invalid_experiments": {},
        "missing_schema": missing_schema,
    }
    if missing_schema:
        return observed, frozenset()

    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        experiments = conn.execute(
            "SELECT * FROM postmarket_rank_experiments ORDER BY experiment_id"
        ).fetchall()
    finally:
        conn.row_factory = original

    valid: set[str] = set()
    invalid: dict[str, list[str]] = {}
    for index, row in enumerate(experiments, 1):
        experiment_id = row["experiment_id"]
        identity = (
            experiment_id
            if isinstance(experiment_id, str) and experiment_id
            else f"row-{index}"
        )
        reasons: list[str] = []

        def parse_object(field: str, expected_keys: set[str]) -> dict[str, object]:
            value = _json_value(row[field], dict)
            if value is None or set(value) != expected_keys:
                reasons.append(f"{field} invalid")
                return {}
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if canonical != row[field]:
                reasons.append(f"{field} noncanonical")
            return value

        def parse_sessions(field: str) -> tuple[str, ...]:
            value = _json_value(row[field], list)
            if value is None or not value or any(not isinstance(item, str) for item in value):
                reasons.append(f"{field} invalid")
                return ()
            sessions = tuple(value)
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if canonical != row[field] or sessions != tuple(sorted(set(sessions))):
                reasons.append(f"{field} noncanonical")
            for session in sessions:
                try:
                    parsed = date.fromisoformat(session)
                except ValueError:
                    reasons.append(f"{field} contains invalid session")
                    continue
                if parsed.isoformat() != session or not EMPIRICAL_CALENDAR.is_session(parsed):
                    reasons.append(f"{field} contains non-XNYS session")
            return sessions

        dev = parse_sessions("development_sessions_json")
        holdout = parse_sessions("holdout_sessions_json")
        eligibility = parse_object(
            "eligibility_rule_json",
            {"move_pct", "min_cumulative_notional", "persistence_bars"},
        )
        selection = parse_object(
            "selection_rule_json",
            {"minimum_evidence_score", "maximum_ordinal_rank"},
        )
        policy = parse_object(
            "policy_json",
            {
                "min_precision",
                "min_recall",
                "min_definitive_labels",
                "min_positive_labels",
            },
        )

        if not isinstance(experiment_id, str) or not experiment_id.strip():
            reasons.append("experiment_id invalid")
        if row["empirical_version"] != EMPIRICAL_VERSION:
            reasons.append("empirical_version is not current")
        if row["status"] != "locked":
            reasons.append("status is not locked")
        if not isinstance(row["created_by"], str) or not row["created_by"].strip():
            reasons.append("created_by invalid")
        if row["rank_version"] != RANK_VERSION:
            reasons.append("rank_version is not current")
        if row["rank_contract_sha256"] != rank_contract_sha256():
            reasons.append("rank contract digest mismatch")
        if row["label_method"] not in LABEL_METHODS:
            reasons.append("label_method invalid")
        if dev and holdout:
            if set(dev) & set(holdout) or max(dev) >= min(holdout):
                reasons.append("development/holdout split invalid")
        if not (
            _finite_number(eligibility.get("move_pct"))
            and 0 < float(eligibility["move_pct"]) <= 100
            and _finite_number(eligibility.get("min_cumulative_notional"))
            and float(eligibility["min_cumulative_notional"]) > 0
            and isinstance(eligibility.get("persistence_bars"), int)
            and not isinstance(eligibility.get("persistence_bars"), bool)
            and int(eligibility["persistence_bars"]) >= 2
        ):
            reasons.append("eligibility rule values invalid")
        maximum_rank = selection.get("maximum_ordinal_rank")
        if not (
            _finite_number(selection.get("minimum_evidence_score"))
            and 0 <= float(selection["minimum_evidence_score"]) <= 100
            and (
                maximum_rank is None
                or (
                    isinstance(maximum_rank, int)
                    and not isinstance(maximum_rank, bool)
                    and maximum_rank > 0
                )
            )
        ):
            reasons.append("selection rule values invalid")
        if not (
            _finite_number(policy.get("min_precision"))
            and 0 < float(policy["min_precision"]) <= 1
            and _finite_number(policy.get("min_recall"))
            and TECHNICAL_MIN_RECALL <= float(policy["min_recall"]) <= 1
            and isinstance(policy.get("min_definitive_labels"), int)
            and not isinstance(policy.get("min_definitive_labels"), bool)
            and int(policy["min_definitive_labels"]) > 0
            and isinstance(policy.get("min_positive_labels"), int)
            and not isinstance(policy.get("min_positive_labels"), bool)
            and int(policy["min_positive_labels"]) > 0
        ):
            reasons.append("experiment policy values invalid")

        created: datetime | None = None
        try:
            created = datetime.fromisoformat(row["created_at_utc"])
            if created.tzinfo is None or created.utcoffset() is None:
                raise ValueError
            created = created.astimezone(timezone.utc)
        except (TypeError, ValueError):
            reasons.append("created_at_utc invalid")
        if created is not None and dev and holdout:
            final_development_close = datetime.combine(
                date.fromisoformat(max(dev)), time(20, 0), tzinfo=EMPIRICAL_ET
            ).astimezone(timezone.utc)
            first_holdout_open = EMPIRICAL_CALENDAR.session_open(
                date.fromisoformat(min(holdout))
            ).to_pydatetime()
            if created <= final_development_close or created >= first_holdout_open:
                reasons.append("experiment was not locked prospectively")

        if not reasons:
            payload = {
                "empirical_version": row["empirical_version"],
                "experiment_id": experiment_id.strip(),
                "created_at_utc": created.isoformat(),
                "created_by": row["created_by"].strip(),
                "rank_version": row["rank_version"],
                "rank_contract_sha256": row["rank_contract_sha256"],
                "label_method": row["label_method"],
                "development_sessions": dev,
                "holdout_sessions": holdout,
                "eligibility_rule": eligibility,
                "selection_rule": selection,
                "policy": policy,
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(canonical.encode()).hexdigest() != row["manifest_sha256"]:
                reasons.append("manifest digest mismatch")

        if reasons:
            invalid[str(identity)] = sorted(set(reasons))
        else:
            valid.add(str(experiment_id))

    observed["valid_locked_experiments"] = len(valid)
    observed["invalid_experiments"] = invalid
    return observed, frozenset(valid)


def _feature_pipeline_progress(
    conn: sqlite3.Connection,
) -> tuple[dict[str, object], bool]:
    """Prove one exact feature-complete context/lifecycle/rank chain.

    Mere table population is not evidence that the named feature families were
    computed, that lifecycle state came from a completed-bar observation, or
    that a rank decomposes the same candidate evidence.  This check therefore
    requires exact foreign-key identities and validates the stored JSON shapes.
    """
    required_columns = {
        "postmarket_candidate_context": _CONTEXT_FEATURE_COLUMNS,
        "postmarket_candidate_lifecycle": _LIFECYCLE_COLUMNS,
        "postmarket_candidate_lifecycle_observations": (
            _LIFECYCLE_OBSERVATION_COLUMNS
        ),
        "postmarket_candidate_ranks": _RANK_COLUMNS,
        "postmarket_rank_runs": _RANK_RUN_COLUMNS,
    }
    missing_schema = {
        table: sorted(columns - _table_columns(conn, table))
        for table, columns in required_columns.items()
        if columns - _table_columns(conn, table)
    }
    observed: dict[str, object] = {
        "context_rows": _count(conn, "postmarket_candidate_context"),
        "lifecycle_transition_rows": _count(
            conn, "postmarket_candidate_lifecycle"
        ),
        "lifecycle_observation_rows": _count(
            conn, "postmarket_candidate_lifecycle_observations"
        ),
        "rank_rows": _count(conn, "postmarket_candidate_ranks"),
        "rank_run_rows": _count(conn, "postmarket_rank_runs"),
        "coherent_complete_chains": 0,
        "missing_schema": missing_schema,
    }
    if missing_schema:
        return observed, False

    feature_status_counts: dict[str, dict[str, int]] = {}
    for field in (
        "status",
        "volatility_status",
        "market_relative_status",
        "sector_relative_status",
        "liquidity_status",
        "catalyst_status",
        "data_confidence_status",
    ):
        feature_status_counts[field] = {
            str(value or "<EMPTY>"): int(count)
            for value, count in conn.execute(
                f"""SELECT {field},COUNT(*)
                    FROM postmarket_candidate_context GROUP BY {field}"""
            ).fetchall()
        }
    observed["context_status_counts"] = feature_status_counts
    observed["rankable_rank_rows"] = _count(
        conn,
        "postmarket_candidate_ranks",
        "rankable=1 AND ordinal_rank IS NOT NULL",
    )
    observed["rankable_lifecycle_rows"] = _count(
        conn,
        "postmarket_candidate_lifecycle",
        "state IN ('CONFIRMED','STRENGTHENING','REQUALIFIED') "
        "AND actionability='QUALIFIED'",
    )

    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              ctx.*,
              tr.lifecycle_version AS transition_lifecycle_version,
              tr.state AS transition_state,
              tr.actionability AS transition_actionability,
              tr.evidence_bar_open_ts_utc AS transition_bar_utc,
              obs.lifecycle_version AS observation_lifecycle_version,
              obs.evidence_bar_open_ts_utc AS observation_bar_utc,
              ranks.rank_id,ranks.rankable,ranks.ordinal_rank,
              ranks.lifecycle_state AS rank_lifecycle_state,
              ranks.evidence_score,ranks.raw_component_score,
              ranks.penalty_total,ranks.evidence_coverage_pct,
              ranks.components_json AS rank_components_json,
              ranks.penalties_json AS rank_penalties_json,
              ranks.exclusion_reasons_json,ranks.explanation_json,
              runs.rank_version,runs.rank_contract_sha256,
              runs.status AS rank_run_status,
              runs.weights_json,runs.thresholds_json
            FROM postmarket_candidate_ranks AS ranks
            JOIN postmarket_candidate_context AS ctx
              ON ctx.context_id=ranks.context_id
             AND ctx.candidate_id=ranks.candidate_id
             AND ctx.session=ranks.session
             AND ctx.symbol=ranks.symbol
             AND ctx.direction=ranks.direction
            JOIN postmarket_candidate_lifecycle AS tr
              ON tr.transition_id=ranks.transition_id
             AND tr.candidate_id=ranks.candidate_id
             AND tr.session=ranks.session
             AND tr.symbol=ranks.symbol
             AND tr.direction=ranks.direction
            JOIN postmarket_candidate_lifecycle_observations AS obs
              ON obs.seq=ranks.observation_seq
             AND obs.candidate_id=ranks.candidate_id
             AND obs.session=ranks.session
             AND obs.symbol=ranks.symbol
            JOIN postmarket_rank_runs AS runs
              ON runs.rank_run_id=ranks.rank_run_id
            WHERE ranks.rankable=1
              AND ranks.ordinal_rank IS NOT NULL
              AND ranks.lifecycle_state IN ('CONFIRMED','STRENGTHENING','REQUALIFIED')
              AND tr.state=ranks.lifecycle_state
              AND tr.lifecycle_version=obs.lifecycle_version
              AND ctx.lifecycle_observation_seq=obs.seq
              AND ctx.lifecycle_evidence_bar_open_ts_utc=obs.evidence_bar_open_ts_utc
              AND tr.evidence_bar_open_ts_utc=obs.evidence_bar_open_ts_utc
              AND runs.status='complete'
            ORDER BY ranks.rank_id
            """
        ).fetchall()
    finally:
        conn.row_factory = original

    valid = 0
    for row in rows:
        context_feature_status = (
            isinstance(row["context_version"], int)
            and row["context_version"] > 0
            and row["status"] == "complete"
            and row["volatility_status"] == "AVAILABLE"
            and row["market_relative_status"] == "AVAILABLE"
            and row["sector_relative_status"] == "AVAILABLE"
            and row["liquidity_status"] == "AVAILABLE"
            and row["catalyst_status"] in {"VERIFIED", "NO_VERIFIED_CATALYST"}
            and row["data_confidence_status"] in {"HIGH", "MEDIUM"}
            and _finite_number(row["data_confidence_coverage_pct"])
            and 75 <= float(row["data_confidence_coverage_pct"]) <= 100
        )
        catalyst_sources = _json_value(row["catalyst_sources_json"], list)
        catalyst_details = _json_value(row["catalyst_details_json"], list)
        catalyst_coverage = _json_value(row["catalyst_coverage_json"], dict)
        confidence = _json_value(row["data_confidence_components_json"], dict)
        components = _json_value(row["rank_components_json"], dict)
        penalties = _json_value(row["rank_penalties_json"], dict)
        exclusions = _json_value(row["exclusion_reasons_json"], list)
        explanation = _json_value(row["explanation_json"], list)
        weights = _json_value(row["weights_json"], dict)
        thresholds = _json_value(row["thresholds_json"], dict)
        catalyst_coherent = (
            catalyst_sources is not None
            and catalyst_details is not None
            and (
                (
                    row["catalyst_status"] == "VERIFIED"
                    and bool(catalyst_sources)
                    and bool(catalyst_details)
                    and all(
                        isinstance(source, str) and source
                        for source in catalyst_sources
                    )
                    and all(isinstance(detail, dict) for detail in catalyst_details)
                )
                or (
                    row["catalyst_status"] == "NO_VERIFIED_CATALYST"
                    and catalyst_sources == []
                    and catalyst_details == []
                )
            )
        )
        confidence_coverage_matches = (
            confidence is not None
            and set(confidence) == _REQUIRED_CONFIDENCE_COMPONENTS
            and all(isinstance(value, bool) for value in confidence.values())
            and _finite_number(row["data_confidence_coverage_pct"])
            and abs(
                float(row["data_confidence_coverage_pct"])
                - round(
                    sum(confidence.values()) / len(confidence) * 100,
                    6,
                )
            )
            <= 0.000001
        )
        decomposition_matches = (
            components is not None
            and penalties is not None
            and all(_finite_number(value) for value in components.values())
            and all(_finite_number(value) for value in penalties.values())
            and all(
                _finite_number(row[field])
                for field in (
                    "evidence_score",
                    "raw_component_score",
                    "penalty_total",
                )
            )
            and abs(
                float(row["raw_component_score"])
                - round(sum(float(value) for value in components.values()), 6)
            )
            <= 0.000001
            and abs(
                float(row["penalty_total"])
                - round(sum(float(value) for value in penalties.values()), 6)
            )
            <= 0.000001
            and abs(
                float(row["evidence_score"])
                - round(
                    max(
                        0.0,
                        min(
                            100.0,
                            float(row["raw_component_score"])
                            + float(row["penalty_total"]),
                        ),
                    ),
                    6,
                )
            )
            <= 0.000001
        )
        structured_evidence = (
            catalyst_coherent
            and catalyst_coverage is not None
            and set(catalyst_coverage) == _CATALYST_COVERAGE_FAMILIES
            and all(
                isinstance(value, str) and value
                for value in catalyst_coverage.values()
            )
            and confidence_coverage_matches
            and components is not None
            and set(components) == _REQUIRED_RANK_COMPONENTS
            and penalties is not None
            and all(
                isinstance(key, str) and key and _finite_number(value)
                for key, value in penalties.items()
            )
            and decomposition_matches
            and weights == COMPONENT_WEIGHTS
            and thresholds == rank_thresholds()
            and exclusions == []
            and isinstance(explanation, list)
            and len(explanation) >= len(_REQUIRED_RANK_COMPONENTS)
            and all(isinstance(item, str) and item for item in explanation)
        )
        numeric_rank = (
            row["context_version"] == REQUIRED_CONTEXT_VERSION
            and row["rank_version"] == RANK_VERSION
            and isinstance(row["ordinal_rank"], int)
            and row["ordinal_rank"] > 0
            and all(
                _finite_number(row[field])
                for field in (
                    "evidence_score",
                    "raw_component_score",
                    "penalty_total",
                    "evidence_coverage_pct",
                )
            )
            and 0 <= float(row["evidence_score"]) <= 100
            and 0 < float(row["evidence_coverage_pct"]) <= 100
            and row["rank_contract_sha256"] == rank_contract_sha256()
            and row["transition_state"] in _RANKABLE_LIFECYCLE_STATES
            and row["transition_actionability"] == "QUALIFIED"
            and row["transition_lifecycle_version"] == LIFECYCLE_VERSION
            and row["observation_lifecycle_version"]
            == row["transition_lifecycle_version"]
        )
        if context_feature_status and structured_evidence and numeric_rank:
            valid += 1

    observed["coherent_complete_chains"] = valid
    return observed, valid > 0


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
    eligible_experiment_ids: frozenset[str],
) -> tuple[int, int, frozenset[str]]:
    """Return best latest-label count, its floor, and every passing experiment."""
    required_tables = {
        "postmarket_rank_experiments",
        "postmarket_independent_labels",
    }
    if (
        not eligible_experiment_ids
        or any(not _table_exists(conn, table) for table in required_tables)
    ):
        return 0, 0, frozenset()
    best_observed = best_required = 0
    passed_experiments: set[str] = set()
    for experiment_id, raw_sessions, raw_policy in conn.execute(
        """
        SELECT experiment_id,holdout_sessions_json,policy_json
        FROM postmarket_rank_experiments ORDER BY experiment_id
        """
    ).fetchall():
        if str(experiment_id) not in eligible_experiment_ids:
            continue
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
                and _provider_qualification_is_bound(report)
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

            experiment_progress, locked_experiment_ids = (
                _locked_experiment_progress(conn)
            )
            milestones.append(_milestone(
                "LOCKED_EMPIRICAL_EXPERIMENT",
                experiment_progress,
                {"valid_locked_experiments": 1},
                bool(locked_experiment_ids),
                "manifest-valid prospective experiment contract bound to the "
                "current rank contract",
            ))
            feature_progress, feature_pipeline_complete = (
                _feature_pipeline_progress(conn)
            )
            milestones.append(_milestone(
                "CONTEXT_LIFECYCLE_RANK_EVIDENCE",
                feature_progress,
                {
                    "coherent_complete_chains": 1,
                    "context_features": [
                        "volatility",
                        "market_relative_strength",
                        "sector_relative_strength",
                        "liquidity",
                        "catalyst",
                        "data_confidence",
                    ],
                    "lifecycle": (
                        "rankable transition linked to its completed-bar observation"
                    ),
                    "rank": "digest-bound finite decomposition of the same evidence",
                },
                feature_pipeline_complete,
                "exact candidate/context/transition/observation/rank identities plus "
                "validated feature and decomposition JSON",
            ))

            (
                holdout_labels,
                holdout_floor,
                label_ready_experiments,
            ) = _holdout_label_progress(conn, locked_experiment_ids)
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
        "CONTEXT_LIFECYCLE_RANK_EVIDENCE": (
            "Resolve unavailable required context features and prove one exact "
            "context/lifecycle/rank chain before counting campaign sessions."
        ),
        "BLINDED_HOLDOUT_LABELS": "Import independently produced, rank-blind holdout label manifests.",
        "EMPIRICAL_HOLDOUT_PASS": "Unblind once, then evaluate the frozen empirical holdout without retuning.",
        "CALIBRATION_HOLDOUT_PASS": "Fit on development only and pass the frozen calibration on holdout.",
        "INDEPENDENT_CUSTOMER_CASE_REVIEWS": "Run the isolated dry-run campaign and collect independent case reviews.",
        "CUSTOMER_DELIVERY_REVIEW_GATE": "Seal and independently reproduce the final customer-delivery review gate.",
        "EVIDENCE_LEDGER_VALIDATION": "Resolve malformed or conflicting evidence before interpreting progress.",
    }
    feature_milestone = next(
        (
            item
            for item in milestones
            if item.code == "CONTEXT_LIFECYCLE_RANK_EVIDENCE"
        ),
        None,
    )
    populated_but_incoherent = bool(
        feature_milestone is not None
        and feature_milestone.state != STATE_COMPLETE
        and isinstance(feature_milestone.observed, dict)
        and int(feature_milestone.observed.get("context_rows", 0)) > 0
        and int(feature_milestone.observed.get("coherent_complete_chains", 0)) == 0
    )
    if "EVIDENCE_LEDGER_VALIDATION" in blockers:
        priority_blocker = "EVIDENCE_LEDGER_VALIDATION"
    elif "DATABASE_INTEGRITY" in blockers:
        priority_blocker = "DATABASE_INTEGRITY"
    elif populated_but_incoherent:
        priority_blocker = "CONTEXT_LIFECYCLE_RANK_EVIDENCE"
    else:
        priority_blocker = blockers[0] if blockers else ""
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
