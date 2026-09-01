"""Offline operational-control exercises for market-wide discovery.

The exercises use deterministic fixtures, temporary SQLite databases, and
checked-in source/configuration. They cannot fetch market data, send alerts,
place orders, restart services, edit configuration, or touch production data.
A passing artifact proves only the named shadow control at the recorded code
revision; it never authorizes customer delivery.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tradebot.detectors import Bar
from tradebot.marketdata import MarketScreenEntry, MarketWideScreen
from tradebot.postmarket_discovery import connect as connect_discovery
from tradebot.postmarket_discovery_health import evaluate_discovery_health
from tradebot.postmarket_discovery_shadow import discovery_enabled, run_discovery_tick


CONTROL_SCHEMA_VERSION = 1
CONTROL_KINDS = (
    "discovery_failure_injection",
    "discovery_kill_switch",
    "discovery_delivery_isolation",
)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
DISCOVERY_SOURCE_PATH = REPO_ROOT / "tradebot" / "postmarket_discovery_shadow.py"
DISCOVERY_STORE_SOURCE_PATH = REPO_ROOT / "tradebot" / "postmarket_discovery.py"
DISCOVERY_HEALTH_SOURCE_PATH = REPO_ROOT / "tradebot" / "postmarket_discovery_health.py"
RTH_MOMENTUM_SOURCE_PATH = REPO_ROOT / "tradebot" / "rth_momentum.py"
RTH_AUDIT_SOURCE_PATH = REPO_ROOT / "tradebot" / "rth_momentum_audit.py"
RTH_CENSUS_SOURCE_PATH = REPO_ROOT / "tradebot" / "rth_missed_mover_census.py"
RTH_REPLAY_SOURCE_PATH = REPO_ROOT / "tradebot" / "rth_momentum_replay.py"
SESSION_CLOSE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
NOW = SESSION_CLOSE + timedelta(minutes=10)
UPDATED = NOW - timedelta(minutes=1)
EXPECTED_ENDPOINTS = (
    "market_movers",
    "most_actives_volume",
    "most_actives_trades",
)
FORBIDDEN_DELIVERY_IMPORTS = (
    "tradebot.alerts",
    "tradebot.broker",
    "tradebot.order",
    "tradebot.outbox",
    "tradebot.telegram_bot",
)


@dataclass(frozen=True)
class DiscoveryControlCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class DiscoveryControlEvidence:
    schema_version: int
    kind: str
    status: str
    revision: str
    completed_at_utc: str
    checks: tuple[DiscoveryControlCheck, ...]


@dataclass(frozen=True)
class WrittenDiscoveryControl:
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
) -> DiscoveryControlEvidence:
    if kind not in CONTROL_KINDS:
        raise ValueError(f"unsupported discovery control kind {kind!r}")
    checks = tuple(checks)
    if not checks:
        raise ValueError("discovery control evidence requires at least one check")
    return DiscoveryControlEvidence(
        schema_version=CONTROL_SCHEMA_VERSION,
        kind=kind,
        status="passed" if all(check.passed for check in checks) else "failed",
        revision=_revision(revision, "revision"),
        completed_at_utc=_completed_at(completed_at).isoformat(),
        checks=checks,
    )


def _screen(*, updated: datetime = UPDATED) -> MarketWideScreen:
    return MarketWideScreen(
        entries=(
            MarketScreenEntry(
                symbol="BROKEN",
                source="market_loser",
                rank=1,
                source_updated_at=updated,
                move_pct=-12.0,
                price=8.80,
            ),
        ),
        requested_top_n=50,
        provider="alpaca",
        feed="sip",
        endpoints=EXPECTED_ENDPOINTS,
        source_updates=tuple((endpoint, updated) for endpoint in EXPECTED_ENDPOINTS),
    )


def _counts(conn) -> tuple[int, int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM postmarket_discovery_ticks").fetchone()[0],
        conn.execute(
            "SELECT COUNT(*) FROM postmarket_discovery_observations"
        ).fetchone()[0],
        conn.execute(
            "SELECT COUNT(*) FROM postmarket_discovery_candidates"
        ).fetchone()[0],
    )


def _qualifying_bars(symbol: str) -> list[Bar]:
    return [
        Bar(symbol, SESSION_CLOSE - timedelta(minutes=5), 100, 100, 100, 100, 1_000),
        Bar(symbol, SESSION_CLOSE, 109, 109, 109, 109, 10_000),
        Bar(
            symbol,
            SESSION_CLOSE + timedelta(minutes=5),
            110,
            110,
            110,
            110,
            10_000,
        ),
    ]


def run_discovery_failure_injection(
    revision: str,
    *,
    completed_at: datetime | None = None,
) -> DiscoveryControlEvidence:
    """Prove missing bars and invalid/failed screens cannot leak candidates."""
    revision = _revision(revision, "revision")
    with tempfile.TemporaryDirectory(prefix="perch-discovery-control-") as raw_dir:
        conn = connect_discovery(Path(raw_dir) / "discovery.db")
        try:
            result, selection, evaluations = run_discovery_tick(
                conn,
                active_universe={"BROKEN"},
                scheduled_earnings=set(),
                now=NOW,
                run_id="discovery-failure-injection",
                version=revision,
                data_feed="sip",
                screen_fetch=lambda top: _screen(),
                bars_fetch=lambda symbols, session: {},
            )
            tick = conn.execute(
                """
                SELECT universe_symbols,screen_rows,screen_unique_symbols,
                       excluded_symbols,discovered_symbols,not_returned_symbols,
                       fetched_symbols,evaluated_symbols,candidate_observations,
                       new_candidates,invariant_ok,error_count
                FROM postmarket_discovery_ticks WHERE tick_id=?
                """,
                (result.tick_id,),
            ).fetchone()
            observation = conn.execute(
                """
                SELECT symbol,outcome,reason,data_feed,market_data_provider
                FROM postmarket_discovery_observations WHERE tick_id=?
                """,
                (result.tick_id,),
            ).fetchone()
            after_missing = _counts(conn)

            stale_fetch_called = False
            stale = _screen(updated=NOW - timedelta(seconds=181))

            def stale_bars_fetch(symbols, session):
                nonlocal stale_fetch_called
                stale_fetch_called = True
                return {}

            try:
                run_discovery_tick(
                    conn,
                    active_universe={"BROKEN"},
                    scheduled_earnings=set(),
                    now=NOW,
                    run_id="discovery-stale-screen-injection",
                    version=revision,
                    data_feed="sip",
                    screen_fetch=lambda top: stale,
                    bars_fetch=stale_bars_fetch,
                )
            except ValueError as exc:
                stale_error = str(exc)
            else:
                stale_error = ""
            after_stale = _counts(conn)

            def failed_screen_fetch(top):
                raise RuntimeError("injected screener outage")

            try:
                run_discovery_tick(
                    conn,
                    active_universe={"BROKEN"},
                    scheduled_earnings=set(),
                    now=NOW,
                    run_id="discovery-provider-injection",
                    version=revision,
                    data_feed="sip",
                    screen_fetch=failed_screen_fetch,
                    bars_fetch=lambda symbols, session: {},
                )
            except RuntimeError as exc:
                provider_error = str(exc)
            else:
                provider_error = ""
            after_provider = _counts(conn)
            integrity = [row[0] for row in conn.execute("PRAGMA quick_check")]
        finally:
            conn.close()

        sweep_conn = connect_discovery(Path(raw_dir) / "sweep-discovery.db")
        sweep_now = NOW + timedelta(minutes=1)
        try:
            def failed_sweep_fetch(symbols, start, end):
                raise RuntimeError("injected full-universe sweep outage")

            sweep_result, _, sweep_evaluations = run_discovery_tick(
                sweep_conn,
                active_universe={"BROKEN", "SWEEP"},
                scheduled_earnings=set(),
                now=sweep_now,
                run_id="discovery-sweep-failure-injection",
                version=revision,
                data_feed="sip",
                screen_fetch=lambda top: _screen(
                    updated=sweep_now - timedelta(minutes=1)
                ),
                bars_fetch=lambda symbols, session: {
                    "BROKEN": _qualifying_bars("BROKEN")
                },
                sweep_bars_fetch=failed_sweep_fetch,
                sweep_cycle_ticks=2,
            )
            sweep_outcomes = {
                row.symbol: row.outcome for row in sweep_evaluations
            }
            sweep_candidates = sweep_conn.execute(
                "SELECT symbol FROM postmarket_discovery_candidates ORDER BY symbol"
            ).fetchall()
            sweep_integrity = [
                row[0] for row in sweep_conn.execute("PRAGMA quick_check")
            ]
        finally:
            sweep_conn.close()

    checks = (
        DiscoveryControlCheck(
            "missing_bulk_bar_is_conserved_as_fetch_error",
            result.error_count == 1
            and result.fetched_symbols == 0
            and result.evaluated_symbols == 1
            and len(selection.symbols) == len(evaluations) == 1
            and observation is not None
            and observation[0] == "BROKEN"
            and observation[1] == "FETCH_ERROR",
            f"result={asdict(result)!r}; observation={observation!r}",
        ),
        DiscoveryControlCheck(
            "missing_bulk_bar_preserves_tick_conservation",
            tick == (1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1, 1),
            "universe/screen/unique/excluded/discovered/not_returned/fetched/"
            f"evaluated/candidates/new/invariant/errors={tick!r}",
        ),
        DiscoveryControlCheck(
            "missing_bulk_bar_cannot_fabricate_candidate",
            result.candidate_observations == 0
            and result.new_candidates == 0
            and after_missing == (1, 1, 0),
            f"result_candidates={(result.candidate_observations, result.new_candidates)!r}; "
            f"database_counts={after_missing!r}",
        ),
        DiscoveryControlCheck(
            "stale_screen_fails_before_bar_fetch_and_persistence",
            "stale" in stale_error.lower()
            and not stale_fetch_called
            and after_stale == after_missing,
            f"error={stale_error!r}; bar_fetch_called={stale_fetch_called}; "
            f"counts_before={after_missing!r}; counts_after={after_stale!r}",
        ),
        DiscoveryControlCheck(
            "screener_outage_fails_before_persistence",
            provider_error == "injected screener outage"
            and after_provider == after_missing,
            f"error={provider_error!r}; counts_before={after_missing!r}; "
            f"counts_after={after_provider!r}",
        ),
        DiscoveryControlCheck(
            "full_universe_sweep_outage_is_explicit_and_conserved",
            sweep_result.discovered_symbols == sweep_result.evaluated_symbols == 2
            and sweep_result.fetched_symbols == 1
            and sweep_result.error_count == 1
            and sweep_outcomes == {"BROKEN": "CANDIDATE", "SWEEP": "FETCH_ERROR"},
            f"result={asdict(sweep_result)!r}; outcomes={sweep_outcomes!r}",
        ),
        DiscoveryControlCheck(
            "full_universe_sweep_outage_cannot_fabricate_or_suppress_candidate",
            sweep_result.candidate_observations == 1
            and sweep_result.new_candidates == 1
            and sweep_candidates == [("BROKEN",)],
            "candidate_counts="
            f"{(sweep_result.candidate_observations, sweep_result.new_candidates)!r}; "
            f"candidates={sweep_candidates!r}",
        ),
        DiscoveryControlCheck(
            "persisted_failure_retains_sip_alpaca_provenance",
            observation is not None and observation[3:] == ("sip", "alpaca"),
            f"observation={observation!r}",
        ),
        DiscoveryControlCheck(
            "exercise_database_is_consistent",
            integrity == ["ok"] and sweep_integrity == ["ok"],
            f"PRAGMA quick_check bounded={integrity!r}; sweep={sweep_integrity!r}",
        ),
    )
    return _artifact(
        "discovery_failure_injection",
        revision,
        _completed_at(completed_at),
        checks,
    )


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _compose_service_block(compose: str, service: str) -> str:
    lines = compose.splitlines(keepends=True)
    marker = f"  {service}:"
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip() == marker),
        None,
    )
    if start is None:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            end = index
            break
    return "".join(lines[start:end])


def run_discovery_kill_switch(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    discovery_source_path: Path = DISCOVERY_SOURCE_PATH,
    health_source_path: Path = DISCOVERY_HEALTH_SOURCE_PATH,
) -> DiscoveryControlEvidence:
    """Exercise the independent default-off parser and disabled supervisor."""
    revision = _revision(revision, "revision")
    false_values = ("0", "false", "NO", "off", "")
    true_values = ("1", "true", "YES", "on")
    false_results = tuple(discovery_enabled(value) for value in false_values)
    true_results = tuple(discovery_enabled(value) for value in true_values)
    try:
        discovery_enabled("maybe")
    except ValueError as exc:
        ambiguous_error = str(exc)
    else:
        ambiguous_error = ""

    health = evaluate_discovery_health(
        Path("/intentionally/missing/discovery-heartbeat.json"),
        enabled=False,
        expected_revision=revision,
        now=NOW,
    )
    compose_raw = compose_path.read_bytes()
    source_raw = discovery_source_path.read_bytes()
    health_raw = health_source_path.read_bytes()
    compose = compose_raw.decode("utf-8")
    source = source_raw.decode("utf-8")
    health_source = health_raw.decode("utf-8")
    service = _compose_service_block(compose, "postmarket-discovery")
    disabled_branch = source.find("if not enabled:")
    database_setup = source.find("connect_journal(JOURNAL_PATH)")
    checks = (
        DiscoveryControlCheck(
            "documented_false_values_disable",
            not any(false_results),
            f"values={false_values!r}; results={false_results!r}",
        ),
        DiscoveryControlCheck(
            "documented_true_values_enable",
            all(true_results),
            f"values={true_values!r}; results={true_results!r}",
        ),
        DiscoveryControlCheck(
            "ambiguous_configuration_fails_closed",
            bool(ambiguous_error),
            ambiguous_error or "ambiguous configuration was accepted",
        ),
        DiscoveryControlCheck(
            "disabled_health_requires_no_heartbeat",
            health.healthy
            and not health.enabled
            and health.heartbeat_age_seconds is None,
            f"health={asdict(health)!r}",
        ),
        DiscoveryControlCheck(
            "compose_service_is_independently_default_off",
            bool(service)
            and "POSTMARKET_DISCOVERY_ENABLED: ${POSTMARKET_DISCOVERY_ENABLED:-0}"
            in service
            and "POSTMARKET_SHADOW_ENABLED" not in service,
            f"compose_sha256={hashlib.sha256(compose_raw).hexdigest()}; "
            f"service_present={bool(service)}",
        ),
        DiscoveryControlCheck(
            "disabled_main_loop_does_not_initialize_databases",
            disabled_branch >= 0
            and database_setup >= 0
            and "market-wide postmarket discovery disabled by kill switch" in source
            and disabled_branch < database_setup,
            f"source_sha256={hashlib.sha256(source_raw).hexdigest()}",
        ),
        DiscoveryControlCheck(
            "health_probe_uses_same_kill_switch",
            "enabled = discovery_enabled()" in health_source
            and "enabled=enabled" in health_source,
            f"health_source_sha256={hashlib.sha256(health_raw).hexdigest()}",
        ),
    )
    return _artifact(
        "discovery_kill_switch",
        revision,
        _completed_at(completed_at),
        checks,
    )


def run_discovery_delivery_isolation(
    revision: str,
    *,
    completed_at: datetime | None = None,
    compose_path: Path = COMPOSE_PATH,
    discovery_source_path: Path = DISCOVERY_SOURCE_PATH,
    store_source_path: Path = DISCOVERY_STORE_SOURCE_PATH,
    rth_source_path: Path = RTH_MOMENTUM_SOURCE_PATH,
    rth_audit_source_path: Path = RTH_AUDIT_SOURCE_PATH,
    rth_census_source_path: Path = RTH_CENSUS_SOURCE_PATH,
    rth_replay_source_path: Path = RTH_REPLAY_SOURCE_PATH,
) -> DiscoveryControlEvidence:
    """Prove the discovery service has no alert, broker, or order dependency."""
    revision = _revision(revision, "revision")
    compose_raw = compose_path.read_bytes()
    source_raw = discovery_source_path.read_bytes()
    store_raw = store_source_path.read_bytes()
    rth_raw = rth_source_path.read_bytes()
    rth_audit_raw = rth_audit_source_path.read_bytes()
    rth_census_raw = rth_census_source_path.read_bytes()
    rth_replay_raw = rth_replay_source_path.read_bytes()
    compose = compose_raw.decode("utf-8")
    source = source_raw.decode("utf-8")
    store = store_raw.decode("utf-8")
    rth_source = rth_raw.decode("utf-8")
    rth_audit_source = rth_audit_raw.decode("utf-8")
    rth_census_source = rth_census_raw.decode("utf-8")
    rth_replay_source = rth_replay_raw.decode("utf-8")
    service = _compose_service_block(compose, "postmarket-discovery")
    imports = (
        _imported_modules(source)
        | _imported_modules(store)
        | _imported_modules(rth_source)
        | _imported_modules(rth_audit_source)
        | _imported_modules(rth_census_source)
        | _imported_modules(rth_replay_source)
    )
    delivery_imports = sorted(
        module
        for module in imports
        if any(module.startswith(prefix) for prefix in FORBIDDEN_DELIVERY_IMPORTS)
    )
    forbidden_tokens = sorted(
        token
        for token in ("send_alert(", "enqueue_alert(", "place_order(", "submit_order(")
        if token in source or token in store or token in rth_source
        or token in rth_audit_source
        or token in rth_census_source
        or token in rth_replay_source
    )
    checks = (
        DiscoveryControlCheck(
            "discovery_modules_have_no_delivery_or_order_import",
            not delivery_imports,
            f"forbidden imports={delivery_imports!r}; "
            f"observer_sha256={hashlib.sha256(source_raw).hexdigest()}; "
            f"store_sha256={hashlib.sha256(store_raw).hexdigest()}; "
            f"rth_sha256={hashlib.sha256(rth_raw).hexdigest()}; "
            f"rth_audit_sha256={hashlib.sha256(rth_audit_raw).hexdigest()}; "
            f"rth_census_sha256={hashlib.sha256(rth_census_raw).hexdigest()}; "
            f"rth_replay_sha256={hashlib.sha256(rth_replay_raw).hexdigest()}",
        ),
        DiscoveryControlCheck(
            "discovery_modules_have_no_delivery_or_order_callsite",
            not forbidden_tokens,
            f"forbidden call tokens={forbidden_tokens!r}",
        ),
        DiscoveryControlCheck(
            "compose_runs_only_the_shadow_observer",
            "command: python -m tradebot.postmarket_discovery_shadow" in service,
            f"service_block_sha256={hashlib.sha256(service.encode()).hexdigest()}",
        ),
        DiscoveryControlCheck(
            "compose_has_no_delivery_service_dependency",
            "depends_on:" not in service
            and "\n      - worker\n" not in service
            and "\n      - bot\n" not in service,
            f"service_present={bool(service)}; service_lines={len(service.splitlines())}",
        ),
        DiscoveryControlCheck(
            "discovery_storage_is_local_shadow_evidence",
            "connect as connect_discovery" in source
            and 'SHADOW_PATH = REPO_ROOT / "data" / "postmarket_shadow.db"' in source,
            f"observer_sha256={hashlib.sha256(source_raw).hexdigest()}",
        ),
    )
    return _artifact(
        "discovery_delivery_isolation",
        revision,
        _completed_at(completed_at),
        checks,
    )


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


def _git_is_clean(repo_path: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot inspect Git worktree state in {repo_path}")
    return not result.stdout.strip()


def artifact_bytes(artifact: DiscoveryControlEvidence) -> bytes:
    return (json.dumps(asdict(artifact), separators=(",", ":"), sort_keys=True) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_artifact(
    path: Path,
    artifact: DiscoveryControlEvidence,
) -> WrittenDiscoveryControl:
    """Create a read-only artifact at a new path; never replace a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = artifact_bytes(artifact)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError as exc:
        raise FileExistsError(
            f"refusing to replace existing discovery control artifact {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o444)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return WrittenDiscoveryControl(
        kind=artifact.kind,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        revision=artifact.revision,
        completed_at_utc=artifact.completed_at_utc,
    )


def run_discovery_control_suite(
    revision: str,
    output_dir: Path,
    *,
    completed_at: datetime | None = None,
) -> tuple[WrittenDiscoveryControl, ...]:
    """Run the exact discovery control inventory and publish it atomically."""
    completed = _completed_at(completed_at)
    tested_commit = _git_commit(REPO_ROOT, revision)
    checked_out_commit = _git_head(REPO_ROOT)
    if tested_commit != checked_out_commit:
        raise ValueError(
            "revision does not match checked-out HEAD; refusing false attribution "
            f"(revision={tested_commit}, HEAD={checked_out_commit})"
        )
    if not _git_is_clean(REPO_ROOT):
        raise ValueError(
            "checked-out Perch worktree is dirty; refusing evidence from "
            "uncommitted or untracked code"
        )
    exercises: tuple[Callable[[], DiscoveryControlEvidence], ...] = (
        lambda: run_discovery_failure_injection(revision, completed_at=completed),
        lambda: run_discovery_kill_switch(revision, completed_at=completed),
        lambda: run_discovery_delivery_isolation(revision, completed_at=completed),
    )
    artifacts = tuple(exercise() for exercise in exercises)
    if any(artifact.status != "passed" for artifact in artifacts):
        failed = [artifact.kind for artifact in artifacts if artifact.status != "passed"]
        raise RuntimeError(f"discovery control exercise failed; no artifacts written: {failed}")
    if output_dir.exists():
        raise FileExistsError("refusing to replace an existing discovery control evidence set")
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
        _fsync_directory(output_dir.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return tuple(
        WrittenDiscoveryControl(
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
        written = run_discovery_control_suite(
            args.revision,
            args.output_dir,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps([asdict(item) for item in written], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
