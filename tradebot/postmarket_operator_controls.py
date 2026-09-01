"""Offline control evidence for the owner-only postmarket notification bridge.

The exercises use temporary SQLite databases and checked-in source/config.
They never open production data, fetch a provider, contact Telegram, restart a
service, or place an order.  Passing evidence proves only the named controls at
the exact clean Git revision; it does not authorize customer delivery.
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

from tradebot.postmarket_context import ensure_context_schema
from tradebot.postmarket_discovery import connect as connect_discovery
from tradebot.postmarket_lifecycle import ensure_lifecycle_schema
from tradebot.postmarket_operator import operator_alert_id, run_operator_cycle
from tradebot.postmarket_operator_health import evaluate_operator_health
from tradebot.postmarket_operator_shadow import operator_alerts_enabled
from tradebot.postmarket_rank import ensure_rank_schema
from tradebot.telegram_bot.db import connect as connect_users


CONTROL_SCHEMA_VERSION = 1
CONTROL_KINDS = (
    "operator_failure_injection",
    "operator_kill_switch",
    "operator_owner_isolation",
)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
OPERATOR_PATH = REPO_ROOT / "tradebot" / "postmarket_operator.py"
SUPERVISOR_PATH = REPO_ROOT / "tradebot" / "postmarket_operator_shadow.py"
HEALTH_PATH = REPO_ROOT / "tradebot" / "postmarket_operator_health.py"
SESSION = date(2026, 8, 31)
NOW = datetime(2026, 8, 31, 20, 16, tzinfo=timezone.utc)
ADMIN_CHAT_ID = 9876


@dataclass(frozen=True)
class OperatorControlCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class OperatorControlEvidence:
    schema_version: int
    kind: str
    status: str
    revision: str
    completed_at_utc: str
    checks: tuple[OperatorControlCheck, ...]


@dataclass(frozen=True)
class WrittenOperatorControl:
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


def _artifact(
    kind: str,
    revision: str,
    completed_at: datetime,
    checks,
) -> OperatorControlEvidence:
    if kind not in CONTROL_KINDS:
        raise ValueError(f"unsupported operator control kind {kind!r}")
    checks = tuple(checks)
    if not checks:
        raise ValueError("operator control evidence requires checks")
    return OperatorControlEvidence(
        schema_version=CONTROL_SCHEMA_VERSION,
        kind=kind,
        status="passed" if all(check.passed for check in checks) else "failed",
        revision=_revision(revision, "revision"),
        completed_at_utc=_completed_at(completed_at).isoformat(),
        checks=checks,
    )


def _seed_candidate(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    first_detected_at: datetime,
    bar_open_at: datetime,
    run_id: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO postmarket_discovery_candidates
          (session,symbol,event_date,direction,discovery_version,first_detected_at,
           bar_open_ts_utc,rth_close,close,move_pct,cumulative_volume,
           cumulative_notional,sources_json,data_feed,market_data_provider,
           bar_timeframe,code_version,run_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION.isoformat(), symbol, SESSION.isoformat(), "up", 2,
            first_detected_at.isoformat(), bar_open_at.isoformat(), 10.0, 11.0,
            10.0, 20_000, 210_000.0, '["market_gainer"]', "sip", "alpaca",
            "5Min", "control", run_id,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def run_operator_failure_injection(
    revision: str,
    *,
    completed_at: datetime | None = None,
) -> OperatorControlEvidence:
    """Prove invalid ownership, stale/future data, and retries fail safely."""
    revision = _revision(revision, "revision")
    with tempfile.TemporaryDirectory(prefix="perch-operator-control-") as raw_dir:
        root = Path(raw_dir)
        shadow = connect_discovery(root / "shadow.db")
        ensure_lifecycle_schema(shadow)
        ensure_context_schema(shadow)
        ensure_rank_schema(shadow)
        users = connect_users(root / "users.db")
        users.execute(
            """
            INSERT INTO users (telegram_user_id,chat_id,created_at,is_admin)
            VALUES (?,?,?,1)
            """,
            (1234, ADMIN_CHAT_ID, NOW.isoformat()),
        )
        users.commit()
        valid_id = _seed_candidate(
            shadow,
            symbol="GPRO",
            first_detected_at=NOW - timedelta(seconds=60),
            bar_open_at=NOW - timedelta(minutes=6),
            run_id="valid",
        )
        stale_id = _seed_candidate(
            shadow,
            symbol="STALE",
            first_detected_at=NOW - timedelta(minutes=30),
            bar_open_at=NOW - timedelta(minutes=35),
            run_id="stale",
        )
        future_id = _seed_candidate(
            shadow,
            symbol="FUTURE",
            first_detected_at=NOW + timedelta(minutes=1),
            bar_open_at=NOW + timedelta(minutes=1),
            run_id="future",
        )
        try:
            try:
                run_operator_cycle(
                    shadow,
                    users,
                    session=SESSION,
                    chat_id=1111,
                    now=NOW,
                )
            except ValueError as exc:
                unauthorized_error = str(exc)
            else:
                unauthorized_error = ""
            rows_after_unauthorized = int(
                users.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            )
            first = run_operator_cycle(
                shadow,
                users,
                session=SESSION,
                chat_id=ADMIN_CHAT_ID,
                now=NOW,
            )
            second = run_operator_cycle(
                shadow,
                users,
                session=SESSION,
                chat_id=ADMIN_CHAT_ID,
                now=NOW,
            )
            rows = users.execute(
                "SELECT alert_id,chat_id,text,status FROM outbox ORDER BY created_at"
            ).fetchall()
            shadow_check = [row[0] for row in shadow.execute("PRAGMA quick_check")]
            users_check = [row[0] for row in users.execute("PRAGMA quick_check")]
        finally:
            shadow.close()
            users.close()

    ids = {operator_alert_id(stale_id), operator_alert_id(future_id)}
    checks = (
        OperatorControlCheck(
            "non_admin_destination_fails_before_outbox_write",
            "administrator" in unauthorized_error and rows_after_unauthorized == 0,
            f"error={unauthorized_error!r}; rows={rows_after_unauthorized}",
        ),
        OperatorControlCheck(
            "fresh_sound_candidate_enqueues_exactly_one_owner_row",
            first.alerts_enqueued == 1
            and len(rows) == 1
            and rows[0][0] == operator_alert_id(valid_id)
            and rows[0][1] == ADMIN_CHAT_ID
            and rows[0][3] == "pending",
            f"result={asdict(first)!r}; rows={rows!r}",
        ),
        OperatorControlCheck(
            "retry_is_idempotent",
            second.alerts_enqueued == 0
            and second.alerts_deduplicated == 1
            and len(rows) == 1,
            f"result={asdict(second)!r}; rows={len(rows)}",
        ),
        OperatorControlCheck(
            "stale_and_future_candidates_cannot_enter_outbox",
            first.stale_candidates == 1 and not ({row[0] for row in rows} & ids),
            f"stale={first.stale_candidates}; outbox_ids={[row[0] for row in rows]!r}",
        ),
        OperatorControlCheck(
            "message_discloses_shadow_provenance_and_no_order",
            len(rows) == 1
            and "Owner-only shadow intelligence" in rows[0][2]
            and "alpaca/sip" in rows[0][2]
            and "No order was placed" in rows[0][2],
            f"text={rows[0][2]!r}" if rows else "no row",
        ),
        OperatorControlCheck(
            "temporary_databases_remain_integral",
            shadow_check == ["ok"] and users_check == ["ok"],
            f"shadow={shadow_check!r}; users={users_check!r}",
        ),
    )
    return _artifact(
        "operator_failure_injection",
        revision,
        _completed_at(completed_at),
        checks,
    )


def _service_block(compose: str) -> str:
    marker = "  postmarket-operator:"
    if marker not in compose:
        return ""
    return compose.split(marker, 1)[1].split("\n  postmarket-customer-dry-run:", 1)[0]


def run_operator_kill_switch(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    supervisor_path: Path = SUPERVISOR_PATH,
    health_path: Path = HEALTH_PATH,
) -> OperatorControlEvidence:
    revision = _revision(revision, "revision")
    compose = compose_path.read_text(encoding="utf-8")
    service = _service_block(compose)
    supervisor = supervisor_path.read_text(encoding="utf-8")
    try:
        operator_alerts_enabled("ambiguous")
    except ValueError as exc:
        invalid_error = str(exc)
    else:
        invalid_error = ""
    disabled_health = evaluate_operator_health(
        Path("/definitely/missing/operator-heartbeat.json"),
        enabled=False,
        expected_revision=revision,
        now=NOW,
    )
    disabled_pos = supervisor.find("if not enabled:")
    connect_pos = supervisor.find("connect_shadow_readonly()")
    checks = (
        OperatorControlCheck(
            "switch_spellings_are_explicit_and_ambiguous_values_fail_closed",
            all(operator_alerts_enabled(value) for value in ("1", "true", "yes", "on"))
            and not any(
                operator_alerts_enabled(value)
                for value in ("0", "false", "no", "off", "")
            )
            and "must be one of" in invalid_error,
            f"invalid_error={invalid_error!r}",
        ),
        OperatorControlCheck(
            "compose_service_is_independently_default_off",
            bool(service)
            and "POSTMARKET_OPERATOR_ALERTS_ENABLED: "
            "${POSTMARKET_OPERATOR_ALERTS_ENABLED:-0}" in service
            and "POSTMARKET_OPERATOR_CHAT_ID: ${POSTMARKET_OPERATOR_CHAT_ID:-}" in service,
            f"service_present={bool(service)}",
        ),
        OperatorControlCheck(
            "disabled_mode_is_healthy_without_database_or_heartbeat",
            disabled_health.healthy,
            disabled_health.detail,
        ),
        OperatorControlCheck(
            "disabled_branch_precedes_database_connections",
            disabled_pos >= 0 and connect_pos > disabled_pos,
            f"disabled_position={disabled_pos}; connect_position={connect_pos}",
        ),
        OperatorControlCheck(
            "control_sources_are_digest_bound",
            all(path.is_file() for path in (compose_path, supervisor_path, health_path)),
            "; ".join(
                f"{path.name}={hashlib.sha256(path.read_bytes()).hexdigest()}"
                for path in (compose_path, supervisor_path, health_path)
            ),
        ),
    )
    return _artifact(
        "operator_kill_switch", revision, _completed_at(completed_at), checks
    )


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def run_operator_owner_isolation(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    operator_path: Path = OPERATOR_PATH,
    supervisor_path: Path = SUPERVISOR_PATH,
) -> OperatorControlEvidence:
    revision = _revision(revision, "revision")
    compose = compose_path.read_text(encoding="utf-8")
    service = _service_block(compose)
    operator_source = operator_path.read_text(encoding="utf-8")
    supervisor_source = supervisor_path.read_text(encoding="utf-8")
    imports = _imports(operator_path) | _imports(supervisor_path)
    forbidden = sorted(
        name
        for name in imports
        if name.startswith(("tradebot.alerts", "tradebot.broker", "tradebot.order"))
    )
    checks = (
        OperatorControlCheck(
            "destination_requires_exactly_one_admin_row",
            "SELECT COUNT(*) FROM users WHERE chat_id=? AND is_admin=1"
            in operator_source
            and "exactly one configured administrator" in operator_source,
            "administrator predicate and cardinality check are present",
        ),
        OperatorControlCheck(
            "outbox_enqueue_has_one_explicit_recipient_not_customer_fanout",
            "[(chat_id, render_operator_opportunity(candidate, now=now), None)]"
            in operator_source
            and "subscribers" not in operator_source
            and "watchlists" not in operator_source,
            "single configured chat tuple; no subscriber/watchlist query",
        ),
        OperatorControlCheck(
            "shadow_database_connection_is_read_only",
            "?mode=ro" in supervisor_source and "uri=True" in supervisor_source,
            "SQLite URI mode=ro is required by the supervisor",
        ),
        OperatorControlCheck(
            "service_has_only_worker_dependency",
            "\n      - worker" in service
            and "\n      - bot" not in service
            and "\n      - postmarket-discovery" not in service,
            "operator bridge depends on the outbox worker only",
        ),
        OperatorControlCheck(
            "bridge_has_no_broker_or_order_import",
            not forbidden,
            f"forbidden_imports={forbidden!r}",
        ),
        OperatorControlCheck(
            "message_is_owner_shadow_only_and_disclaims_execution",
            "Owner-only shadow intelligence" in operator_source
            and "No order was placed" in operator_source
            and "not advice" in operator_source,
            "owner-only, not-advice, and no-order disclosures are present",
        ),
    )
    return _artifact(
        "operator_owner_isolation", revision, _completed_at(completed_at), checks
    )


def _git_commit(repo: Path, revision: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{revision}^{{commit}}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_is_clean(repo: Path) -> bool:
    return not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def artifact_bytes(artifact: OperatorControlEvidence) -> bytes:
    return (json.dumps(asdict(artifact), separators=(",", ":"), sort_keys=True) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_artifact(
    path: Path,
    artifact: OperatorControlEvidence,
) -> WrittenOperatorControl:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = artifact_bytes(artifact)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError as exc:
        raise FileExistsError(
            f"refusing to replace existing operator control artifact {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o444)
        _fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return WrittenOperatorControl(
        kind=artifact.kind,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        revision=artifact.revision,
        completed_at_utc=artifact.completed_at_utc,
    )


def run_operator_control_suite(
    revision: str,
    output_dir: Path,
    *,
    completed_at: datetime | None = None,
) -> tuple[WrittenOperatorControl, ...]:
    completed = _completed_at(completed_at)
    tested_commit = _git_commit(REPO_ROOT, revision)
    checked_out_commit = _git_head(REPO_ROOT)
    if tested_commit != checked_out_commit:
        raise ValueError("revision does not match checked-out HEAD")
    if not _git_is_clean(REPO_ROOT):
        raise ValueError("checked-out Perch worktree is dirty")
    exercises: tuple[Callable[[], OperatorControlEvidence], ...] = (
        lambda: run_operator_failure_injection(revision, completed_at=completed),
        lambda: run_operator_kill_switch(revision, completed_at=completed),
        lambda: run_operator_owner_isolation(revision, completed_at=completed),
    )
    artifacts = tuple(exercise() for exercise in exercises)
    if any(artifact.status != "passed" for artifact in artifacts):
        failed = [artifact.kind for artifact in artifacts if artifact.status != "passed"]
        raise RuntimeError(f"operator control exercise failed; no artifacts written: {failed}")
    if output_dir.exists():
        raise FileExistsError("refusing to replace an existing operator control evidence set")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".tmp", dir=output_dir.parent)
    )
    try:
        staged = tuple(
            write_artifact(staging / f"{artifact.kind}.json", artifact)
            for artifact in artifacts
        )
        os.rename(staging, output_dir)
        _fsync_directory(output_dir.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return tuple(
        WrittenOperatorControl(
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
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        written = run_operator_control_suite(args.revision, args.output_dir)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps([asdict(item) for item in written], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
