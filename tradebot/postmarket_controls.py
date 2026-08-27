"""Offline operational-control exercises for the postmarket shadow observer.

The exercises use deterministic fixtures and temporary SQLite databases. They
cannot fetch market data, send alerts, place orders, restart services, edit
configuration, or touch production databases. A passing artifact proves the
named control behaved correctly at the recorded code revision; it never arms
customer delivery.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tradebot.detectors import Bar
from tradebot.postmarket import (
    OUTCOME_BELOW_MOVE,
    OUTCOME_CANDIDATE,
    OUTCOME_FETCH_ERROR,
    OUTCOME_MALFORMED_BAR,
    OUTCOME_NO_RTH_CLOSE,
    connect as connect_shadow,
    evaluate_earnings_reaction,
    fetch_error_evaluation,
    record_shadow_tick,
)
from tradebot.postmarket_health import evaluate_postmarket_health
from tradebot.postmarket_shadow import shadow_enabled


CONTROL_SCHEMA_VERSION = 1
CONTROL_KINDS = ("failure_injection", "kill_switch", "rollback_runbook")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
SHADOW_SOURCE_PATH = REPO_ROOT / "tradebot" / "postmarket_shadow.py"
SESSION = date(2026, 8, 26)
SESSION_CLOSE = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
FORBIDDEN_DELIVERY_IMPORTS = (
    "tradebot.alerts",
    "tradebot.broker",
    "tradebot.order",
    "tradebot.telegram_bot",
)


@dataclass(frozen=True)
class ControlCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class ControlEvidence:
    schema_version: int
    kind: str
    status: str
    revision: str
    completed_at_utc: str
    checks: tuple[ControlCheck, ...]


@dataclass(frozen=True)
class WrittenControl:
    kind: str
    path: str
    sha256: str
    revision: str
    completed_at_utc: str


def _revision(raw: str, context: str) -> str:
    value = raw.strip()
    if value == "unknown" or not REVISION_PATTERN.fullmatch(value):
        raise ValueError(f"{context} must be a 7-40 character lowercase Git SHA")
    return value


def _completed_at(raw: datetime | None) -> datetime:
    value = raw or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("completed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _artifact(kind: str, revision: str, completed_at: datetime, checks) -> ControlEvidence:
    if kind not in CONTROL_KINDS:
        raise ValueError(f"unsupported control kind {kind!r}")
    checks = tuple(checks)
    if not checks:
        raise ValueError("control evidence requires at least one check")
    return ControlEvidence(
        schema_version=CONTROL_SCHEMA_VERSION,
        kind=kind,
        status="passed" if all(check.passed for check in checks) else "failed",
        revision=_revision(revision, "revision"),
        completed_at_utc=_completed_at(completed_at).isoformat(),
        checks=checks,
    )


def _bar(
    symbol: str,
    ts: datetime,
    close: float,
    *,
    volume: int = 1_000_000,
    high: float | None = None,
) -> Bar:
    return Bar(
        symbol,
        ts,
        close,
        close if high is None else high,
        close,
        close,
        volume,
    )


def _evaluate(symbol: str, postmarket: list[Bar], *, rth: list[Bar] | None = None):
    rth = (
        [_bar(symbol, SESSION_CLOSE - timedelta(minutes=5), 100.0)]
        if rth is None
        else rth
    )
    return evaluate_earnings_reaction(
        symbol,
        SESSION,
        rth,
        postmarket,
        session_close=SESSION_CLOSE,
        now=SESSION_CLOSE + timedelta(minutes=10),
    )


def run_failure_injection(
    revision: str,
    *,
    completed_at: datetime | None = None,
) -> ControlEvidence:
    """Exercise loud provider/data failures and prove zero candidate leakage."""
    revision = _revision(revision, "revision")
    provider = fetch_error_evaluation("PROVIDER", SESSION, RuntimeError("vendor down"))
    missing = _evaluate(
        "MISSING",
        [
            _bar("MISSING", SESSION_CLOSE, 110.0),
            _bar("MISSING", SESSION_CLOSE + timedelta(minutes=5), 111.0),
        ],
        rth=[],
    )
    malformed = _evaluate(
        "MALFORMED",
        [
            _bar("MALFORMED", SESSION_CLOSE, 110.0),
            _bar(
                "MALFORMED",
                SESSION_CLOSE + timedelta(minutes=5),
                111.0,
                high=100.0,
            ),
        ],
    )
    persistence = _evaluate(
        "REVERSAL",
        [
            _bar("REVERSAL", SESSION_CLOSE, 110.0),
            _bar("REVERSAL", SESSION_CLOSE + timedelta(minutes=5), 101.0),
        ],
    )
    evaluations = (provider, missing, malformed, persistence)

    with tempfile.TemporaryDirectory(prefix="perch-postmarket-control-") as raw_dir:
        db_path = Path(raw_dir) / "shadow.db"
        conn = connect_shadow(db_path)
        try:
            tick_id, new_candidates = record_shadow_tick(
                conn,
                evaluations,
                session=SESSION,
                tick_utc=SESSION_CLOSE + timedelta(minutes=10),
                completed_utc=SESSION_CLOSE + timedelta(minutes=10, seconds=1),
                run_id="failure-injection",
                run_mode="control-exercise",
                code_version=revision,
                data_feed="sip",
                scheduled_symbols=len(evaluations),
                latency_ms=1_000,
            )
            tick = conn.execute(
                "SELECT scheduled_symbols,evaluated_symbols,invariant_ok,error_count,"
                "candidate_observations FROM postmarket_ticks WHERE tick_id=?",
                (tick_id,),
            ).fetchone()
            persisted_outcomes = dict(
                conn.execute(
                    "SELECT symbol,outcome FROM postmarket_observations WHERE tick_id=?",
                    (tick_id,),
                ).fetchall()
            )
            candidate_rows = conn.execute(
                "SELECT COUNT(*) FROM postmarket_candidates"
            ).fetchone()[0]
            integrity = [row[0] for row in conn.execute("PRAGMA quick_check")]
        finally:
            conn.close()

    expected_outcomes = {
        "PROVIDER": OUTCOME_FETCH_ERROR,
        "MISSING": OUTCOME_NO_RTH_CLOSE,
        "MALFORMED": OUTCOME_MALFORMED_BAR,
        "REVERSAL": OUTCOME_BELOW_MOVE,
    }
    checks = (
        ControlCheck(
            "provider_failure_is_loud",
            provider.outcome == OUTCOME_FETCH_ERROR,
            f"provider outcome={provider.outcome}; reason={provider.reason}",
        ),
        ControlCheck(
            "missing_bar_is_rejected",
            missing.outcome == OUTCOME_NO_RTH_CLOSE,
            f"missing baseline outcome={missing.outcome}",
        ),
        ControlCheck(
            "malformed_bar_is_rejected",
            malformed.outcome == OUTCOME_MALFORMED_BAR,
            f"malformed input outcome={malformed.outcome}",
        ),
        ControlCheck(
            "persistence_failure_is_rejected",
            persistence.outcome != OUTCOME_CANDIDATE,
            f"single spike then reversal outcome={persistence.outcome}",
        ),
        ControlCheck(
            "tick_conservation_holds",
            tick == (4, 4, 1, 1, 0),
            "scheduled/evaluated/invariant/errors/candidates=" + repr(tick),
        ),
        ControlCheck(
            "persisted_outcomes_are_attributable",
            persisted_outcomes == expected_outcomes,
            f"persisted outcomes={json.dumps(persisted_outcomes, sort_keys=True)}",
        ),
        ControlCheck(
            "no_candidate_is_fabricated",
            new_candidates == 0 and candidate_rows == 0,
            f"new_candidates={new_candidates}; candidate_rows={candidate_rows}",
        ),
        ControlCheck(
            "exercise_database_is_consistent",
            integrity == ["ok"],
            f"PRAGMA quick_check={integrity!r}",
        ),
    )
    return _artifact("failure_injection", revision, _completed_at(completed_at), checks)


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def run_kill_switch(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    shadow_source_path: Path = SHADOW_SOURCE_PATH,
) -> ControlEvidence:
    """Exercise the default-off parser and disabled health behavior."""
    revision = _revision(revision, "revision")
    false_values = ("0", "false", "NO", "off", "")
    true_values = ("1", "true", "YES", "on")
    false_results = tuple(shadow_enabled(value) for value in false_values)
    true_results = tuple(shadow_enabled(value) for value in true_values)
    try:
        shadow_enabled("maybe")
    except ValueError as exc:
        ambiguous_error = str(exc)
    else:
        ambiguous_error = ""

    health = evaluate_postmarket_health(
        Path("/intentionally/missing/heartbeat.json"),
        enabled=False,
        now=SESSION_CLOSE + timedelta(minutes=30),
    )
    compose_raw = compose_path.read_bytes()
    source_raw = shadow_source_path.read_bytes()
    compose = compose_raw.decode("utf-8")
    source = source_raw.decode("utf-8")
    imports = _imported_modules(source)
    delivery_imports = sorted(
        module
        for module in imports
        if any(module.startswith(prefix) for prefix in FORBIDDEN_DELIVERY_IMPORTS)
    )
    checks = (
        ControlCheck(
            "documented_false_values_disable",
            not any(false_results),
            f"values={false_values!r}; results={false_results!r}",
        ),
        ControlCheck(
            "documented_true_values_enable",
            all(true_results),
            f"values={true_values!r}; results={true_results!r}",
        ),
        ControlCheck(
            "ambiguous_configuration_fails_closed",
            bool(ambiguous_error),
            ambiguous_error or "ambiguous configuration was accepted",
        ),
        ControlCheck(
            "disabled_health_requires_no_heartbeat",
            health.healthy and not health.enabled and not health.window_active,
            f"health={asdict(health)!r}",
        ),
        ControlCheck(
            "compose_default_is_off",
            "POSTMARKET_SHADOW_ENABLED: ${POSTMARKET_SHADOW_ENABLED:-0}" in compose,
            f"compose_sha256={hashlib.sha256(compose_raw).hexdigest()}; "
            "default fallback expected 0",
        ),
        ControlCheck(
            "observer_has_no_delivery_import",
            not delivery_imports,
            f"observer_source_sha256={hashlib.sha256(source_raw).hexdigest()}; "
            f"forbidden delivery imports={delivery_imports!r}",
        ),
    )
    return _artifact("kill_switch", revision, _completed_at(completed_at), checks)


def _git_commit(repo_path: Path, revision: str) -> str:
    revision = _revision(revision, "Git revision")
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Git revision {revision} is not a commit in {repo_path}")
    return result.stdout.strip().lower()


def _git_is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        raise ValueError(f"Git ancestry check failed: {result.stderr.strip()}")
    return result.returncode == 0


def _git_head(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot resolve checked-out HEAD in {repo_path}")
    return result.stdout.strip().lower()


def _database_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        "quick_check": [row[0] for row in conn.execute("PRAGMA quick_check")],
        "ticks": conn.execute("SELECT COUNT(*) FROM postmarket_ticks").fetchone()[0],
        "observations": conn.execute(
            "SELECT COUNT(*) FROM postmarket_observations"
        ).fetchone()[0],
        "candidates": conn.execute(
            "SELECT COUNT(*) FROM postmarket_candidates"
        ).fetchone()[0],
        "triggers": tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        ),
    }


def _record_control_tick(conn: sqlite3.Connection, revision: str, index: int) -> None:
    evaluation = fetch_error_evaluation(f"CONTROL{index}", SESSION, RuntimeError("test"))
    instant = SESSION_CLOSE + timedelta(minutes=10 + index)
    record_shadow_tick(
        conn,
        [evaluation],
        session=SESSION,
        tick_utc=instant,
        completed_utc=instant + timedelta(seconds=1),
        run_id=f"rollback-{index}",
        run_mode="control-exercise",
        code_version=revision,
        data_feed="sip",
        scheduled_symbols=1,
    )


def run_rollback_rehearsal(
    revision: str,
    rollback_revision: str,
    *,
    completed_at: datetime | None = None,
    repo_path: Path = REPO_ROOT,
) -> ControlEvidence:
    """Rehearse SQLite restore and verify the declared code rollback ancestor."""
    revision = _revision(revision, "revision")
    rollback_revision = _revision(rollback_revision, "rollback_revision")
    current_commit = _git_commit(repo_path, revision)
    rollback_commit = _git_commit(repo_path, rollback_revision)
    is_ancestor = _git_is_ancestor(repo_path, rollback_commit, current_commit)

    with tempfile.TemporaryDirectory(prefix="perch-postmarket-rollback-") as raw_dir:
        root = Path(raw_dir)
        source_path = root / "source.db"
        backup_path = root / "backup.db"
        restored_path = root / "restored.db"
        source = connect_shadow(source_path)
        try:
            _record_control_tick(source, revision, 0)
            source_snapshot = _database_snapshot(source)
            backup = sqlite3.connect(backup_path)
            try:
                source.backup(backup)
                backup_snapshot = _database_snapshot(backup)
            finally:
                backup.close()
            _record_control_tick(source, revision, 1)
            mutated_snapshot = _database_snapshot(source)
        finally:
            source.close()

        backup = sqlite3.connect(backup_path)
        restored = sqlite3.connect(restored_path)
        try:
            backup.backup(restored)
            restored_snapshot = _database_snapshot(restored)
        finally:
            restored.close()
            backup.close()

        backup_sha = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        restored_sha = hashlib.sha256(restored_path.read_bytes()).hexdigest()

    expected_triggers = {
        "postmarket_candidates_no_delete",
        "postmarket_candidates_no_update",
        "postmarket_observations_no_delete",
        "postmarket_observations_no_update",
        "postmarket_ticks_no_delete",
        "postmarket_ticks_no_update",
    }
    checks = (
        ControlCheck(
            "rollback_revision_exists",
            len(rollback_commit) == 40,
            f"declared={rollback_revision}; resolved={rollback_commit}",
        ),
        ControlCheck(
            "rollback_revision_is_ancestor",
            is_ancestor,
            f"rollback={rollback_commit}; current={current_commit}",
        ),
        ControlCheck(
            "sqlite_backup_is_consistent",
            backup_snapshot == source_snapshot
            and backup_snapshot["quick_check"] == ["ok"],
            f"source={source_snapshot!r}; backup={backup_snapshot!r}",
        ),
        ControlCheck(
            "exercise_proves_point_in_time_restore",
            mutated_snapshot["ticks"] == source_snapshot["ticks"] + 1
            and restored_snapshot == source_snapshot,
            f"before={source_snapshot!r}; mutated={mutated_snapshot!r}; "
            f"restored={restored_snapshot!r}",
        ),
        ControlCheck(
            "restored_append_only_guards_exist",
            set(restored_snapshot["triggers"]) == expected_triggers,
            f"restored triggers={restored_snapshot['triggers']!r}",
        ),
        ControlCheck(
            "restored_database_matches_backup_bytes",
            backup_sha == restored_sha,
            f"backup_sha256={backup_sha}; restored_sha256={restored_sha}",
        ),
    )
    return _artifact("rollback_runbook", revision, _completed_at(completed_at), checks)


def artifact_bytes(artifact: ControlEvidence) -> bytes:
    return (json.dumps(asdict(artifact), separators=(",", ":"), sort_keys=True) + "\n").encode()


def write_artifact(path: Path, artifact: ControlEvidence) -> WrittenControl:
    """Create an immutable artifact; never replace an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = artifact_bytes(artifact)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to replace existing control artifact {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return WrittenControl(
        kind=artifact.kind,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        revision=artifact.revision,
        completed_at_utc=artifact.completed_at_utc,
    )


def run_control_suite(
    revision: str,
    rollback_revision: str,
    output_dir: Path,
    *,
    completed_at: datetime | None = None,
    repo_path: Path = REPO_ROOT,
) -> tuple[WrittenControl, ...]:
    completed = _completed_at(completed_at)
    tested_commit = _git_commit(repo_path, revision)
    checked_out_commit = _git_head(repo_path)
    if tested_commit != checked_out_commit:
        raise ValueError(
            "revision does not match checked-out HEAD; refusing false attribution "
            f"(revision={tested_commit}, HEAD={checked_out_commit})"
        )
    exercises: tuple[Callable[[], ControlEvidence], ...] = (
        lambda: run_failure_injection(revision, completed_at=completed),
        lambda: run_kill_switch(revision, completed_at=completed),
        lambda: run_rollback_rehearsal(
            revision,
            rollback_revision,
            completed_at=completed,
            repo_path=repo_path,
        ),
    )
    artifacts = tuple(exercise() for exercise in exercises)
    if any(artifact.status != "passed" for artifact in artifacts):
        failed = [artifact.kind for artifact in artifacts if artifact.status != "passed"]
        raise RuntimeError(f"control exercise failed; no artifacts written: {failed}")
    if output_dir.exists():
        raise FileExistsError("refusing to replace an existing control evidence set")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.",
            suffix=".tmp",
            dir=output_dir.parent,
        )
    )
    try:
        staged = tuple(
            write_artifact(staging / f"{artifact.kind}.json", artifact)
            for artifact in artifacts
        )
        os.rename(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return tuple(
        WrittenControl(
            kind=item.kind,
            path=str(output_dir / Path(item.path).name),
            sha256=item.sha256,
            revision=item.revision,
            completed_at_utc=item.completed_at_utc,
        )
        for item in staged
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--rollback-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        written = run_control_suite(
            args.revision,
            args.rollback_revision,
            args.output_dir,
            repo_path=args.repo,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps([asdict(item) for item in written], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
