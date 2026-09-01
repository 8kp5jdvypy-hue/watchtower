"""Leakage-resistant empirical evaluation for the postmarket evidence rank.

This module is offline, append-only, and delivery-incapable.  It stores a
locked experiment contract, independently supplied labels, an explicit
holdout-unblinding event, and reproducible baseline-versus-rank reports.  It
does not fetch data, tune a threshold, send an alert, or place an order.
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
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import exchange_calendars as ecals


EMPIRICAL_VERSION = 1
TECHNICAL_MIN_RECALL = 0.95
CALENDAR = ecals.get_calendar("XNYS")
ET = ZoneInfo("America/New_York")
CLASSIFICATIONS = {"eligible", "ineligible", "ambiguous"}
LABEL_METHODS = {"blind_bar_review", "multi_provider_reconciliation"}
SPLITS = {"development", "holdout"}
EMPIRICAL_ARTIFACT_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


EMPIRICAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmarket_rank_experiments (
    experiment_id TEXT PRIMARY KEY,
    empirical_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    created_by TEXT NOT NULL,
    rank_version INTEGER NOT NULL,
    rank_contract_sha256 TEXT NOT NULL,
    label_method TEXT NOT NULL,
    development_sessions_json TEXT NOT NULL,
    holdout_sessions_json TEXT NOT NULL,
    eligibility_rule_json TEXT NOT NULL,
    selection_rule_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    CHECK (status='locked')
);
CREATE TABLE IF NOT EXISTS postmarket_independent_labels (
    label_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    revision INTEGER NOT NULL,
    classification TEXT NOT NULL,
    direction TEXT,
    eligible_at_utc TEXT,
    labeler TEXT NOT NULL,
    label_method TEXT NOT NULL,
    blinded_to_rank INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    rationale TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    artifact_providers_json TEXT NOT NULL,
    artifact_feeds_json TEXT NOT NULL,
    artifact_acquired_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    UNIQUE(experiment_id,session,symbol,revision),
    CHECK (classification IN ('eligible','ineligible','ambiguous')),
    CHECK (direction IN ('up','down') OR direction IS NULL),
    CHECK (blinded_to_rank=1)
);
CREATE INDEX IF NOT EXISTS idx_postmarket_independent_labels_lookup
    ON postmarket_independent_labels(experiment_id,session,symbol,revision);
CREATE TABLE IF NOT EXISTS postmarket_holdout_unblinds (
    unblind_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL UNIQUE,
    unblinded_at_utc TEXT NOT NULL,
    unblinded_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    label_inventory_sha256 TEXT NOT NULL,
    holdout_labels INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS postmarket_rank_empirical_runs (
    empirical_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    split TEXT NOT NULL,
    evaluated_at_utc TEXT NOT NULL,
    code_version TEXT,
    input_digest_sha256 TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    UNIQUE(experiment_id,split,input_digest_sha256),
    CHECK (split IN ('development','holdout'))
);

CREATE TRIGGER IF NOT EXISTS postmarket_rank_experiments_no_update
BEFORE UPDATE ON postmarket_rank_experiments BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_experiments is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_experiments_no_delete
BEFORE DELETE ON postmarket_rank_experiments BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_experiments is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_independent_labels_no_update
BEFORE UPDATE ON postmarket_independent_labels BEGIN
    SELECT RAISE(ABORT, 'postmarket_independent_labels is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_independent_labels_no_delete
BEFORE DELETE ON postmarket_independent_labels BEGIN
    SELECT RAISE(ABORT, 'postmarket_independent_labels is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_holdout_labels_frozen_after_unblind
BEFORE INSERT ON postmarket_independent_labels
WHEN EXISTS (
    SELECT 1
    FROM postmarket_holdout_unblinds u
    JOIN postmarket_rank_experiments e ON e.experiment_id=u.experiment_id
    JOIN json_each(e.holdout_sessions_json) s ON s.value=NEW.session
    WHERE u.experiment_id=NEW.experiment_id
)
BEGIN
    SELECT RAISE(ABORT, 'holdout labels are frozen after unblinding');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_holdout_unblinds_no_update
BEFORE UPDATE ON postmarket_holdout_unblinds BEGIN
    SELECT RAISE(ABORT, 'postmarket_holdout_unblinds is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_holdout_unblinds_no_delete
BEFORE DELETE ON postmarket_holdout_unblinds BEGIN
    SELECT RAISE(ABORT, 'postmarket_holdout_unblinds is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_empirical_runs_no_update
BEFORE UPDATE ON postmarket_rank_empirical_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_empirical_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS postmarket_rank_empirical_runs_no_delete
BEFORE DELETE ON postmarket_rank_empirical_runs BEGIN
    SELECT RAISE(ABORT, 'postmarket_rank_empirical_runs is append-only');
END;
"""


@dataclass(frozen=True)
class ExperimentPolicy:
    min_precision: float
    min_recall: float
    min_definitive_labels: int
    min_positive_labels: int


@dataclass(frozen=True)
class EligibilityRule:
    move_pct: float
    min_cumulative_notional: float
    persistence_bars: int


@dataclass(frozen=True)
class SelectionRule:
    minimum_evidence_score: float
    maximum_ordinal_rank: int | None


@dataclass(frozen=True)
class ClassificationMetrics:
    definitive_labels: int
    ambiguous_labels: int
    positive_labels: int
    selected: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    direction_mismatches: int
    precision: float | None
    recall: float | None


@dataclass(frozen=True)
class SessionMetrics:
    session: str
    baseline: ClassificationMetrics
    candidate_rank: ClassificationMetrics
    discovered_symbols: int
    rankable_symbols: int
    duplicate_candidate_rows: int


@dataclass(frozen=True)
class EmpiricalReport:
    empirical_version: int
    experiment_id: str
    split: str
    rank_version: int
    rank_contract_sha256: str
    sessions: tuple[str, ...]
    eligibility_rule: EligibilityRule
    selection_rule: SelectionRule
    policy: ExperimentPolicy
    baseline: ClassificationMetrics
    candidate_rank: ClassificationMetrics
    session_metrics: tuple[SessionMetrics, ...]
    precision_delta: float | None
    recall_delta: float | None
    passed_locked_policy: bool
    blocking_reasons: tuple[str, ...]
    holdout_unblinded: bool
    input_digest_sha256: str


@dataclass(frozen=True)
class WrittenEmpiricalArtifact:
    path: str
    sha256: str
    created: bool
    experiment_id: str
    split: str
    input_digest_sha256: str
    report_sha256: str
    experiment_manifest_sha256: str


def ensure_empirical_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(EMPIRICAL_SCHEMA)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(postmarket_rank_experiments)")
    }
    if "eligibility_rule_json" not in columns:
        count = conn.execute("SELECT COUNT(*) FROM postmarket_rank_experiments").fetchone()[0]
        if count:
            raise RuntimeError(
                "legacy rank experiments lack a locked eligibility rule; "
                "preserve the database and create a new empirical store"
            )
        conn.execute(
            "ALTER TABLE postmarket_rank_experiments ADD COLUMN eligibility_rule_json TEXT"
        )
    if "rank_contract_sha256" not in columns:
        # Existing experiments cannot be retroactively attributed to a rank
        # contract without rewriting locked evidence.  Leave them NULL and
        # require a new experiment for empirical qualification.
        conn.execute(
            "ALTER TABLE postmarket_rank_experiments "
            "ADD COLUMN rank_contract_sha256 TEXT"
        )


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _revision(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not REVISION_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be an attributable 7-40 character Git SHA")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sessions(values: Iterable[date], name: str) -> tuple[str, ...]:
    ordered = tuple(sorted(set(values)))
    if not ordered:
        raise ValueError(f"{name} must not be empty")
    if any(not CALENDAR.is_session(value) for value in ordered):
        raise ValueError(f"{name} must contain only XNYS sessions")
    return tuple(value.isoformat() for value in ordered)


def create_locked_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    created_at: datetime,
    created_by: str,
    rank_version: int,
    rank_contract_sha256: str,
    label_method: str,
    development_sessions: Iterable[date],
    holdout_sessions: Iterable[date],
    eligibility_rule: EligibilityRule,
    selection_rule: SelectionRule,
    policy: ExperimentPolicy,
) -> str:
    """Lock all tuning and acceptance choices before holdout results exist."""
    ensure_empirical_schema(conn)
    if not experiment_id.strip() or not created_by.strip():
        raise ValueError("experiment_id and created_by must be non-empty")
    if rank_version <= 0:
        raise ValueError("rank_version must be positive")
    contract_digest = rank_contract_sha256.strip().lower()
    if not SHA256_PATTERN.fullmatch(contract_digest):
        raise ValueError("rank_contract_sha256 must be a lowercase SHA-256 digest")
    if label_method not in LABEL_METHODS:
        raise ValueError(f"label_method must be one of {sorted(LABEL_METHODS)}")
    dev = _sessions(development_sessions, "development_sessions")
    holdout = _sessions(holdout_sessions, "holdout_sessions")
    if set(dev) & set(holdout):
        raise ValueError("development and holdout sessions must be disjoint")
    if max(dev) >= min(holdout):
        raise ValueError("all development sessions must precede every holdout session")
    if (
        not isinstance(eligibility_rule.move_pct, (int, float))
        or isinstance(eligibility_rule.move_pct, bool)
        or not math.isfinite(eligibility_rule.move_pct)
        or not 0 < eligibility_rule.move_pct <= 100
    ):
        raise ValueError("eligibility move_pct must be in (0,100]")
    if (
        not isinstance(eligibility_rule.min_cumulative_notional, (int, float))
        or isinstance(eligibility_rule.min_cumulative_notional, bool)
        or not math.isfinite(eligibility_rule.min_cumulative_notional)
        or eligibility_rule.min_cumulative_notional <= 0
    ):
        raise ValueError("eligibility min_cumulative_notional must be positive")
    if (
        not isinstance(eligibility_rule.persistence_bars, int)
        or isinstance(eligibility_rule.persistence_bars, bool)
        or eligibility_rule.persistence_bars < 2
    ):
        raise ValueError("eligibility persistence_bars must be at least 2")
    if not 0 <= selection_rule.minimum_evidence_score <= 100:
        raise ValueError("minimum_evidence_score must be between 0 and 100")
    if selection_rule.maximum_ordinal_rank is not None and selection_rule.maximum_ordinal_rank <= 0:
        raise ValueError("maximum_ordinal_rank must be positive when supplied")
    if not 0 < policy.min_precision <= 1:
        raise ValueError("min_precision must be in (0,1]")
    if not TECHNICAL_MIN_RECALL <= policy.min_recall <= 1:
        raise ValueError(f"min_recall must be at least {TECHNICAL_MIN_RECALL}")
    if policy.min_definitive_labels <= 0 or policy.min_positive_labels <= 0:
        raise ValueError("label sample floors must be positive")
    created = _utc(created_at, "created_at")
    final_development_close = datetime.combine(
        date.fromisoformat(max(dev)), time(20, 0), tzinfo=ET
    ).astimezone(timezone.utc)
    first_holdout_open = CALENDAR.session_open(
        date.fromisoformat(min(holdout))
    ).to_pydatetime()
    if created <= final_development_close:
        raise ValueError("experiment must be locked after development sessions complete")
    if created >= first_holdout_open:
        raise ValueError("experiment must be locked before the first holdout session opens")
    payload = {
        "empirical_version": EMPIRICAL_VERSION,
        "experiment_id": experiment_id.strip(),
        "created_at_utc": created.isoformat(),
        "created_by": created_by.strip(),
        "rank_version": rank_version,
        "rank_contract_sha256": contract_digest,
        "label_method": label_method,
        "development_sessions": dev,
        "holdout_sessions": holdout,
        "eligibility_rule": asdict(eligibility_rule),
        "selection_rule": asdict(selection_rule),
        "policy": asdict(policy),
    }
    manifest_digest = _digest(payload)
    existing = conn.execute(
        "SELECT manifest_sha256 FROM postmarket_rank_experiments WHERE experiment_id=?",
        (experiment_id.strip(),),
    ).fetchone()
    if existing:
        if existing[0] != manifest_digest:
            raise ValueError("experiment_id is already locked to a different manifest")
        return manifest_digest
    with conn:
        conn.execute(
            """
            INSERT INTO postmarket_rank_experiments
                (experiment_id,empirical_version,status,created_at_utc,created_by,
                 rank_version,label_method,development_sessions_json,
                 rank_contract_sha256,
                 holdout_sessions_json,eligibility_rule_json,selection_rule_json,
                 policy_json,manifest_sha256)
            VALUES (?,?, 'locked',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload["experiment_id"], EMPIRICAL_VERSION, payload["created_at_utc"],
                payload["created_by"], rank_version, label_method, _canonical(dev),
                contract_digest,
                _canonical(holdout), _canonical(asdict(eligibility_rule)),
                _canonical(asdict(selection_rule)),
                _canonical(asdict(policy)), manifest_digest,
            ),
        )
    return manifest_digest


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


def locked_eligibility_rule(
    conn: sqlite3.Connection, experiment_id: str,
) -> EligibilityRule:
    """Return the immutable independent-label definition for an experiment."""
    ensure_empirical_schema(conn)
    experiment = _experiment(conn, experiment_id)
    return EligibilityRule(**json.loads(experiment["eligibility_rule_json"]))


def record_independent_label(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    session: date,
    symbol: str,
    classification: str,
    direction: str | None,
    eligible_at: datetime | None,
    labeler: str,
    reason_code: str,
    rationale: str,
    artifact_sha256: str,
    artifact_providers: Iterable[str],
    artifact_feeds: Iterable[str],
    artifact_acquired_at: datetime,
    recorded_at: datetime,
    _ensure_schema: bool = True,
    _manage_transaction: bool = True,
) -> int:
    """Append a label supplied without reading candidate or rank tables."""
    if _ensure_schema:
        ensure_empirical_schema(conn)
    experiment = _experiment(conn, experiment_id)
    allowed = set(json.loads(experiment["development_sessions_json"])) | set(
        json.loads(experiment["holdout_sessions_json"])
    )
    if session.isoformat() not in allowed:
        raise ValueError("label session is outside the locked experiment")
    holdout_sessions = set(json.loads(experiment["holdout_sessions_json"]))
    if session.isoformat() in holdout_sessions and conn.execute(
        "SELECT 1 FROM postmarket_holdout_unblinds WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone() is not None:
        raise ValueError("holdout labels are frozen after unblinding")
    canonical_symbol = symbol.strip().upper()
    if not canonical_symbol or canonical_symbol != symbol.strip():
        raise ValueError("symbol must be canonical uppercase")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"classification must be one of {sorted(CLASSIFICATIONS)}")
    if classification == "eligible":
        if direction not in {"up", "down"} or eligible_at is None:
            raise ValueError("eligible labels require direction and eligible_at")
        eligible_utc = _utc(eligible_at, "eligible_at")
        if eligible_utc.astimezone(ET).date() != session:
            raise ValueError("eligible_at must fall in the labeled session")
        rth_close = CALENDAR.session_close(session).to_pydatetime().astimezone(timezone.utc)
        session_end = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
        if not rth_close <= eligible_utc <= session_end:
            raise ValueError("eligible_at must fall in the postmarket window")
        eligible_text = eligible_utc.isoformat()
    else:
        if direction is not None or eligible_at is not None:
            raise ValueError("ineligible/ambiguous labels cannot declare direction or eligible_at")
        eligible_text = None
    if not all(value.strip() for value in (labeler, reason_code, rationale)):
        raise ValueError("labeler, reason_code, and rationale must be non-empty")
    if len(artifact_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in artifact_sha256):
        raise ValueError("artifact_sha256 must be a 64-character hexadecimal digest")
    providers = tuple(sorted({value.strip().lower() for value in artifact_providers if value.strip()}))
    feeds = tuple(sorted({value.strip().lower() for value in artifact_feeds if value.strip()}))
    if not providers or not feeds:
        raise ValueError("artifact providers and feeds must be non-empty")
    if experiment["label_method"] == "multi_provider_reconciliation" and len(providers) < 2:
        raise ValueError("multi_provider_reconciliation requires at least two providers")
    acquired = _utc(artifact_acquired_at, "artifact_acquired_at")
    recorded = _utc(recorded_at, "recorded_at")
    session_end = datetime.combine(session, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    if acquired < session_end:
        raise ValueError("independent evidence must be acquired after the session window")
    if recorded < acquired:
        raise ValueError("a label cannot be recorded before its evidence was acquired")
    if eligible_at is not None and recorded < _utc(eligible_at, "eligible_at"):
        raise ValueError("a label cannot be recorded before its eligibility instant")
    revision = conn.execute(
        """SELECT COALESCE(MAX(revision),0)+1 FROM postmarket_independent_labels
           WHERE experiment_id=? AND session=? AND symbol=?""",
        (experiment_id, session.isoformat(), canonical_symbol),
    ).fetchone()[0]
    def insert() -> sqlite3.Cursor:
        return conn.execute(
            """
            INSERT INTO postmarket_independent_labels
                (experiment_id,session,symbol,revision,classification,direction,
                 eligible_at_utc,labeler,label_method,blinded_to_rank,reason_code,
                 rationale,artifact_sha256,artifact_providers_json,
                 artifact_feeds_json,artifact_acquired_at_utc,recorded_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)
            """,
            (
                experiment_id, session.isoformat(), canonical_symbol, revision,
                classification, direction, eligible_text, labeler,
                experiment["label_method"], reason_code, rationale,
                artifact_sha256.lower(), _canonical(providers), _canonical(feeds),
                acquired.isoformat(), recorded.isoformat(),
            ),
        )
    if _manage_transaction:
        with conn:
            cursor = insert()
    else:
        cursor = insert()
    return int(cursor.lastrowid)


def _latest_labels(conn: sqlite3.Connection, experiment_id: str, sessions: tuple[str, ...]):
    if not sessions:
        return []
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


def holdout_label_inventory(
    conn: sqlite3.Connection, experiment_id: str,
) -> tuple[str, int, datetime]:
    """Preview the exact label inventory a one-way unblind would freeze."""
    ensure_empirical_schema(conn)
    experiment = _experiment(conn, experiment_id)
    sessions = tuple(json.loads(experiment["holdout_sessions_json"]))
    labels = _latest_labels(conn, experiment_id, sessions)
    if not labels:
        raise ValueError("holdout cannot be unblinded before independent labels exist")
    inventory = [
        {key: row[key] for key in (
            "session", "symbol", "revision", "classification", "direction",
            "eligible_at_utc", "artifact_sha256", "artifact_providers_json",
            "artifact_feeds_json", "artifact_acquired_at_utc"
        )}
        for row in labels
    ]
    latest_label_time = max(datetime.fromisoformat(row["recorded_at_utc"]) for row in labels)
    return _digest(inventory), len(labels), latest_label_time


def unblind_holdout(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    unblinded_at: datetime,
    unblinded_by: str,
    reason: str,
    expected_inventory_sha256: str,
) -> str:
    """Irreversibly record that holdout rank results may now be evaluated."""
    ensure_empirical_schema(conn)
    _experiment(conn, experiment_id)
    if not unblinded_by.strip() or not reason.strip():
        raise ValueError("unblinded_by and reason must be non-empty")
    digest, label_count, latest_label_time = holdout_label_inventory(conn, experiment_id)
    if expected_inventory_sha256.lower() != digest:
        raise ValueError("holdout inventory digest did not match explicit confirmation")
    unblind_time = _utc(unblinded_at, "unblinded_at")
    if unblind_time < latest_label_time:
        raise ValueError("holdout cannot be unblinded before all labels were recorded")
    existing = conn.execute(
        "SELECT label_inventory_sha256 FROM postmarket_holdout_unblinds WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if existing:
        if existing[0] != digest:
            raise ValueError("holdout labels changed after unblinding")
        return digest
    with conn:
        conn.execute(
            """INSERT INTO postmarket_holdout_unblinds
               (experiment_id,unblinded_at_utc,unblinded_by,reason,
                label_inventory_sha256,holdout_labels) VALUES (?,?,?,?,?,?)""",
            (
                experiment_id, unblind_time.isoformat(),
                unblinded_by.strip(), reason.strip(), digest, label_count,
            ),
        )
    return digest


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _classify(labels, selected: dict[tuple[str, str], str]) -> ClassificationMetrics:
    tp = fp = tn = fn = ambiguous = positives = mismatches = 0
    for row in labels:
        key = (row["session"], row["symbol"])
        direction = selected.get(key)
        if row["classification"] == "ambiguous":
            ambiguous += 1
            continue
        if row["classification"] == "eligible":
            positives += 1
            if direction is None:
                fn += 1
            elif direction == row["direction"]:
                tp += 1
            else:
                fp += 1
                mismatches += 1
        elif direction is None:
            tn += 1
        else:
            fp += 1
    definitive = len(labels) - ambiguous
    label_keys = {(row["session"], row["symbol"]) for row in labels}
    return ClassificationMetrics(
        definitive, ambiguous, positives, len(label_keys & selected.keys()), tp, fp, tn, fn,
        mismatches, _ratio(tp, tp + fp), _ratio(tp, tp + fn),
    )


def _selected_symbols(
    conn: sqlite3.Connection,
    sessions: tuple[str, ...],
    rank_version: int,
    rank_contract_sha256: str,
    rule: SelectionRule,
) -> tuple[
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
    dict[str, tuple[int, int]],
    tuple[dict[str, object], ...],
]:
    placeholders = ",".join("?" for _ in sessions)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        candidates = conn.execute(
            f"""SELECT candidate_id,session,symbol,direction,first_detected_at
                FROM postmarket_discovery_candidates WHERE session IN ({placeholders})
                ORDER BY first_detected_at,candidate_id""", sessions,
        ).fetchall()
        ranks = conn.execute(
            f"""
            SELECT r.*,x.as_of_utc,x.recorded_at_utc,x.input_digest_sha256,
                   x.rank_contract_sha256
            FROM postmarket_candidate_ranks r
            JOIN postmarket_rank_runs x ON x.rank_run_id=r.rank_run_id
            WHERE r.session IN ({placeholders}) AND x.rank_version=?
              AND x.rank_contract_sha256=? AND r.rankable=1
            ORDER BY x.as_of_utc,r.rank_id
            """, (*sessions, rank_version, rank_contract_sha256),
        ).fetchall()
    finally:
        conn.row_factory = previous
    baseline: dict[tuple[str, str], str] = {}
    candidate_ids: dict[int, tuple[str, str, str]] = {}
    counts: dict[str, list[int]] = {session: [0, 0] for session in sessions}
    for row in candidates:
        key = (row["session"], row["symbol"])
        counts[row["session"]][0] += 1
        baseline.setdefault(key, row["direction"])
        candidate_ids[int(row["candidate_id"])] = (*key, row["direction"])
    chosen: dict[tuple[str, str], str] = {}
    rankable_keys: set[tuple[str, str]] = set()
    seen_candidates: set[int] = set()
    first_rankable_evidence: list[dict[str, object]] = []
    for row in ranks:
        candidate_id = int(row["candidate_id"])
        if candidate_id in seen_candidates or candidate_id not in candidate_ids:
            continue
        seen_candidates.add(candidate_id)
        session, symbol, direction = candidate_ids[candidate_id]
        key = (session, symbol)
        rankable_keys.add(key)
        first_rankable_evidence.append({
            "candidate_id": candidate_id,
            "session": session,
            "symbol": symbol,
            "direction": direction,
            "rank_run_id": int(row["rank_run_id"]),
            "rank_id": int(row["rank_id"]),
            "rank_contract_sha256": row["rank_contract_sha256"],
            "rank_input_digest_sha256": row["input_digest_sha256"],
            "as_of_utc": row["as_of_utc"],
            "recorded_at_utc": row["recorded_at_utc"],
            "ordinal_rank": (
                None if row["ordinal_rank"] is None else int(row["ordinal_rank"])
            ),
            "evidence_score": float(row["evidence_score"]),
        })
        if float(row["evidence_score"]) < rule.minimum_evidence_score:
            continue
        if rule.maximum_ordinal_rank is not None and (
            row["ordinal_rank"] is None or int(row["ordinal_rank"]) > rule.maximum_ordinal_rank
        ):
            continue
        chosen.setdefault(key, direction)
    for session, symbol in rankable_keys:
        counts[session][1] += 1
    stats = {
        session: (len({key for key in baseline if key[0] == session}), values[1])
        for session, values in counts.items()
    }
    return baseline, chosen, stats, tuple(first_rankable_evidence)


def evaluate_rank_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    split: str,
    evaluated_at: datetime,
    code_version: str | None,
) -> EmpiricalReport:
    ensure_empirical_schema(conn)
    revision = _revision(code_version, "code_version")
    evaluated = _utc(evaluated_at, "evaluated_at")
    if split not in SPLITS:
        raise ValueError(f"split must be one of {sorted(SPLITS)}")
    experiment = _experiment(conn, experiment_id)
    contract_digest = experiment["rank_contract_sha256"]
    if not isinstance(contract_digest, str) or not SHA256_PATTERN.fullmatch(
        contract_digest
    ):
        raise ValueError(
            "legacy experiment is missing an attributable rank contract digest"
        )
    unblind = conn.execute(
        "SELECT unblinded_at_utc,label_inventory_sha256,holdout_labels "
        "FROM postmarket_holdout_unblinds WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if split == "holdout":
        if unblind is None:
            raise ValueError("holdout is sealed; record an explicit unblind event first")
        unblinded_at = _utc(datetime.fromisoformat(unblind[0]), "unblinded_at_utc")
        if evaluated < unblinded_at:
            raise ValueError("evaluation cannot predate holdout unblinding")
    sessions = tuple(json.loads(experiment[f"{split}_sessions_json"]))
    labels = _latest_labels(conn, experiment_id, sessions)
    if labels:
        latest_label_time = max(
            _utc(datetime.fromisoformat(row["recorded_at_utc"]), "recorded_at_utc")
            for row in labels
        )
        if evaluated < latest_label_time:
            raise ValueError("evaluation cannot predate its latest label evidence")
    if split == "holdout":
        inventory_digest, inventory_count, _ = holdout_label_inventory(
            conn, experiment_id
        )
        if inventory_digest != unblind[1] or inventory_count != int(unblind[2]):
            raise ValueError("holdout label inventory changed after unblinding")
    placeholders = ",".join("?" for _ in sessions)
    rank_times = conn.execute(
        f"""
        SELECT MAX(x.as_of_utc),MAX(x.recorded_at_utc)
        FROM postmarket_rank_runs x
        JOIN postmarket_candidate_ranks r ON r.rank_run_id=x.rank_run_id
        WHERE r.session IN ({placeholders}) AND x.rank_version=?
          AND x.rank_contract_sha256=?
        """,
        (*sessions, int(experiment["rank_version"]), contract_digest),
    ).fetchone()
    for value, name in zip(rank_times, ("rank as_of_utc", "rank recorded_at_utc")):
        if value is not None and evaluated < _utc(datetime.fromisoformat(value), name):
            raise ValueError("evaluation cannot predate its latest rank evidence")
    eligibility_rule = EligibilityRule(**json.loads(experiment["eligibility_rule_json"]))
    rule = SelectionRule(**json.loads(experiment["selection_rule_json"]))
    policy = ExperimentPolicy(**json.loads(experiment["policy_json"]))
    baseline_selected, rank_selected, stats, first_rankable_evidence = _selected_symbols(
        conn, sessions, int(experiment["rank_version"]), contract_digest, rule
    )
    baseline = _classify(labels, baseline_selected)
    ranked = _classify(labels, rank_selected)
    per_session = []
    for session in sessions:
        session_labels = [row for row in labels if row["session"] == session]
        base_slice = {key: value for key, value in baseline_selected.items() if key[0] == session}
        rank_slice = {key: value for key, value in rank_selected.items() if key[0] == session}
        candidate_rows = conn.execute(
            "SELECT COUNT(*) FROM postmarket_discovery_candidates WHERE session=?", (session,)
        ).fetchone()[0]
        per_session.append(SessionMetrics(
            session, _classify(session_labels, base_slice), _classify(session_labels, rank_slice),
            stats[session][0], stats[session][1], max(0, candidate_rows - stats[session][0]),
        ))
    inventory = {
        "manifest_sha256": experiment["manifest_sha256"],
        "rank_contract_sha256": contract_digest,
        "split": split,
        "labels": [{key: row[key] for key in row.keys()} for row in labels],
        "baseline": sorted((*key, value) for key, value in baseline_selected.items()),
        "ranked": sorted((*key, value) for key, value in rank_selected.items()),
        "first_rankable_evidence": first_rankable_evidence,
    }
    input_digest = _digest(inventory)
    blockers = []
    mismatched_contract_runs = int(conn.execute(
        f"""
        SELECT COUNT(*) FROM postmarket_rank_runs
        WHERE session IN ({placeholders}) AND rank_version=?
          AND COALESCE(rank_contract_sha256,'')<>?
        """,
        (*sessions, int(experiment["rank_version"]), contract_digest),
    ).fetchone()[0])
    if mismatched_contract_runs:
        blockers.append("RANK_CONTRACT_MISMATCH_PRESENT")
    if ranked.definitive_labels < policy.min_definitive_labels:
        blockers.append("MIN_DEFINITIVE_LABELS_NOT_MET")
    if ranked.positive_labels < policy.min_positive_labels:
        blockers.append("MIN_POSITIVE_LABELS_NOT_MET")
    if ranked.ambiguous_labels:
        blockers.append("AMBIGUOUS_LABELS_PRESENT")
    if ranked.direction_mismatches:
        blockers.append("DIRECTION_MISMATCHES_PRESENT")
    if ranked.precision is None or ranked.precision < policy.min_precision:
        blockers.append("PRECISION_FLOOR_NOT_MET")
    if ranked.recall is None or ranked.recall < policy.min_recall:
        blockers.append("RECALL_FLOOR_NOT_MET")
    report = EmpiricalReport(
        EMPIRICAL_VERSION, experiment_id, split, int(experiment["rank_version"]),
        contract_digest, sessions,
        eligibility_rule, rule, policy, baseline, ranked, tuple(per_session),
        (ranked.precision - baseline.precision if ranked.precision is not None and baseline.precision is not None else None),
        (ranked.recall - baseline.recall if ranked.recall is not None and baseline.recall is not None else None),
        not blockers, tuple(blockers), split == "holdout", input_digest,
    )
    # The report itself is immutable and idempotent for the exact evidence inventory.
    raw = _canonical(asdict(report))
    report_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    existing = conn.execute(
        """
        SELECT code_version,report_json,report_sha256
        FROM postmarket_rank_empirical_runs
        WHERE experiment_id=? AND split=? AND input_digest_sha256=?
        """,
        (experiment_id, split, input_digest),
    ).fetchone()
    if existing is not None:
        if existing[0] != revision or existing[1] != raw or existing[2] != report_sha256:
            raise ValueError(
                "empirical input inventory was already evaluated with different attribution"
            )
        return report
    with conn:
        conn.execute(
            """
            INSERT INTO postmarket_rank_empirical_runs
                (experiment_id,split,evaluated_at_utc,code_version,input_digest_sha256,
                 report_json,report_sha256) VALUES (?,?,?,?,?,?,?)
            """,
            (
                experiment_id, split, evaluated.isoformat(),
                revision, input_digest, raw, report_sha256,
            ),
        )
    return report


def export_empirical_report(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    split: str,
    input_digest_sha256: str,
    output_dir: Path | str,
) -> WrittenEmpiricalArtifact:
    """Publish one exact persisted run as an immutable, digest-bound artifact."""
    ensure_empirical_schema(conn)
    if split not in SPLITS:
        raise ValueError(f"split must be one of {sorted(SPLITS)}")
    digest = input_digest_sha256.strip().lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("input_digest_sha256 must be a lowercase SHA-256 digest")
    row = conn.execute(
        """
        SELECT r.empirical_run_id,r.experiment_id,r.split,r.evaluated_at_utc,
               r.code_version,r.input_digest_sha256,r.report_json,r.report_sha256,
               e.manifest_sha256
        FROM postmarket_rank_empirical_runs r
        JOIN postmarket_rank_experiments e ON e.experiment_id=r.experiment_id
        WHERE r.experiment_id=? AND r.split=? AND r.input_digest_sha256=?
        """,
        (experiment_id, split, digest),
    ).fetchone()
    if row is None:
        raise ValueError("empirical run does not exist for the exact input digest")
    code_version = _revision(row[4], "empirical run code_version")
    report_raw = row[6]
    if not isinstance(report_raw, str):
        raise ValueError("empirical run report_json must be text")
    report_digest = hashlib.sha256(report_raw.encode()).hexdigest()
    if report_digest != row[7]:
        raise ValueError("empirical run report digest does not match stored JSON")
    try:
        report = json.loads(report_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("empirical run report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise ValueError("empirical run report root must be an object")
    if _canonical(report) != report_raw:
        raise ValueError("empirical run report is not canonical JSON")
    if (
        report.get("experiment_id") != experiment_id
        or report.get("split") != split
        or report.get("input_digest_sha256") != digest
    ):
        raise ValueError("empirical run report identity does not match stored metadata")
    if split == "holdout" and report.get("holdout_unblinded") is not True:
        raise ValueError("holdout empirical report is not explicitly unblinded")
    experiment_manifest_sha256 = row[8]
    if (
        not isinstance(experiment_manifest_sha256, str)
        or not SHA256_PATTERN.fullmatch(experiment_manifest_sha256)
    ):
        raise ValueError("experiment manifest digest is invalid")
    if not isinstance(row[3], str):
        raise ValueError("empirical run evaluated_at_utc must be text")
    try:
        parsed_evaluated_at = datetime.fromisoformat(row[3])
    except ValueError as exc:
        raise ValueError("empirical run evaluated_at_utc is invalid") from exc
    evaluated_at = _utc(
        parsed_evaluated_at, "empirical run evaluated_at_utc"
    ).isoformat()
    payload = {
        "schema_version": EMPIRICAL_ARTIFACT_VERSION,
        "artifact_type": "postmarket_rank_empirical",
        "empirical_run_id": int(row[0]),
        "experiment_id": experiment_id,
        "split": split,
        "evaluated_at_utc": evaluated_at,
        "code_version": code_version,
        "input_digest_sha256": digest,
        "report_sha256": report_digest,
        "experiment_manifest_sha256": experiment_manifest_sha256,
        "report": report,
    }
    raw = (_canonical(payload) + "\n").encode()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    experiment_key = hashlib.sha256(experiment_id.encode()).hexdigest()[:12]
    destination = directory / (
        f"postmarket_rank_empirical_{experiment_key}_{split}_{digest[:16]}_v"
        f"{EMPIRICAL_ARTIFACT_VERSION}.json"
    )
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != raw:
            raise ValueError("existing empirical artifact does not match exact run")
        return WrittenEmpiricalArtifact(
            str(destination), artifact_sha256, False, experiment_id, split,
            digest, report_digest, experiment_manifest_sha256,
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != raw:
                raise ValueError("concurrent empirical artifact does not match exact run")
            created = False
        else:
            created = True
        temporary.unlink()
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return WrittenEmpiricalArtifact(
        str(destination), artifact_sha256, created, experiment_id, split,
        digest, report_digest, experiment_manifest_sha256,
    )
