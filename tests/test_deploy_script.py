"""Black-box tests for the exact-revision VPS deployment wrapper."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
REVISION = "a" * 40
OTHER_REVISION = "b" * 40
PREVIOUS_REVISION = "c" * 40
APP_SERVICES = (
    "worker",
    "bot",
    "runner",
    "postmarket",
    "postmarket-discovery",
    "postmarket-external-context",
    "postmarket-customer-dry-run",
    "api",
)
DATABASES = (
    "journal.db",
    "users.db",
    "evaluations.db",
    "postmarket_shadow.db",
    "universe.db",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def deploy_env(tmp_path):
    root = tmp_path / "repo"
    scripts = root / "scripts"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    command_log = tmp_path / "commands.log"
    systemd_target = tmp_path / "systemd-target"
    for directory in (scripts, fake_bin, state, root / "systemd", root / "data", systemd_target):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2(DEPLOY_SCRIPT, scripts / "deploy.sh")
    (scripts / "deploy.sh").chmod(0o755)
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / ".env").write_text("TEST_ONLY=1\n")
    for unit in ("perch.service", "perch-backup.service", "perch-backup.timer"):
        shutil.copy2(REPO_ROOT / "systemd" / unit, root / "systemd" / unit)
    for database in DATABASES:
        (root / "data" / database).write_bytes(b"test database placeholder")

    _write_executable(
        fake_bin / "git",
        r'''#!/usr/bin/env bash
set -euo pipefail
echo "GIT_SHA=${GIT_SHA:-}|git $*" >> "$FAKE_COMMAND_LOG"
case "$1 ${2:-}" in
  "status --porcelain")
    [[ "${FAKE_DIRTY:-0}" == "1" ]] && echo " M operator-change"
    ;;
  "fetch origin")
    ;;
  "rev-parse --verify")
    value="${3:-}"
    if [[ "$value" == "origin/main^{commit}" ]]; then
      echo "$FAKE_REMOTE_REVISION"
    elif [[ "$value" == "HEAD^{commit}" ]]; then
      if [[ -f "$FAKE_STATE/checked-out" ]]; then
        echo "$FAKE_EXPECTED_REVISION"
      else
        echo "$FAKE_PREVIOUS_REVISION"
      fi
    elif [[ "$value" == "${FAKE_EXPECTED_REVISION}^{commit}" ]]; then
      [[ "${FAKE_UNRESOLVED:-0}" == "1" ]] && exit 1
      echo "$FAKE_EXPECTED_REVISION"
    else
      exit 1
    fi
    ;;
  "checkout --detach")
    touch "$FAKE_STATE/checked-out"
    ;;
  "merge-base --is-ancestor")
    [[ "${FAKE_ANCESTOR:-1}" == "1" ]]
    ;;
  *)
    echo "unexpected git command: $*" >&2
    exit 90
    ;;
esac
''',
    )
    _write_executable(
        fake_bin / "systemctl",
        r'''#!/usr/bin/env bash
set -euo pipefail
echo "GIT_SHA=${GIT_SHA:-}|systemctl $*" >> "$FAKE_COMMAND_LOG"
if [[ "$1" == "start" && "${FAKE_SYSTEMCTL_START_FAIL:-0}" == "1" ]]; then
  exit 1
elif [[ "$1" == "show" && "$*" == *"--property=Result"* ]]; then
  echo "${FAKE_BACKUP_RESULT:-success}"
elif [[ "$1" == "show" && "$*" == *"--property=ExecMainStatus"* ]]; then
  echo "${FAKE_BACKUP_STATUS:-0}"
fi
''',
    )
    _write_executable(
        fake_bin / "docker",
        r'''#!/usr/bin/env bash
set -euo pipefail
echo "GIT_SHA=${GIT_SHA:-}|docker $*" >> "$FAKE_COMMAND_LOG"
if [[ "$1" == "compose" && "$2" == "up" ]]; then
  [[ "${FAKE_DOCKER_UP_FAIL:-0}" == "0" ]]
elif [[ "$1" == "compose" && "$2" == "exec" ]]; then
  service="$4"
  if [[ "${FAKE_MISMATCH_SERVICE:-}" == "$service" ]]; then
    echo "fffffff"
  else
    echo "${FAKE_EXPECTED_REVISION:0:7}"
  fi
fi
''',
    )
    _write_executable(
        fake_bin / "sqlite3",
        r'''#!/usr/bin/env bash
set -euo pipefail
echo "GIT_SHA=${GIT_SHA:-}|sqlite3 $*" >> "$FAKE_COMMAND_LOG"
if [[ "$*" == *"${FAKE_BAD_DATABASE:-__none__}"* ]]; then
  echo "database disk image is malformed"
else
  echo "ok"
fi
''',
    )
    _write_executable(
        fake_bin / "curl",
        r'''#!/usr/bin/env bash
set -euo pipefail
echo "GIT_SHA=${GIT_SHA:-}|curl $*" >> "$FAKE_COMMAND_LOG"
echo "${FAKE_HEALTH_RESPONSE:-{\"ok\":true}}"
''',
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_COMMAND_LOG": str(command_log),
            "FAKE_STATE": str(state),
            "FAKE_EXPECTED_REVISION": REVISION,
            "FAKE_REMOTE_REVISION": REVISION,
            "FAKE_PREVIOUS_REVISION": PREVIOUS_REVISION,
            "PERCH_SYSTEMD_DIR": str(systemd_target),
        }
    )
    return {
        "root": root,
        "script": scripts / "deploy.sh",
        "env": env,
        "log": command_log,
        "systemd_target": systemd_target,
    }


def _run(deploy_env, *args, **overrides):
    env = deploy_env["env"].copy()
    env.update({key: str(value) for key, value in overrides.items()})
    return subprocess.run(
        [str(deploy_env["script"]), *args],
        cwd=deploy_env["root"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _log(deploy_env) -> str:
    path = deploy_env["log"]
    return path.read_text() if path.exists() else ""


def test_happy_path_binds_revision_backs_up_waits_and_verifies_every_surface(deploy_env):
    result = _run(deploy_env, REVISION)

    assert result.returncode == 0, result.stderr
    assert "predeploy_backup=success" in result.stdout
    assert "postdeploy_backup=success" in result.stdout
    assert f"DEPLOYMENT_VERIFIED mode=deploy previous={PREVIOUS_REVISION} revision={REVISION}" in result.stdout
    for service in APP_SERVICES:
        assert f"service_revision[{service}]=aaaaaaa" in result.stdout
    for database in DATABASES:
        assert f"sqlite_quick_check[data/{database}]=ok" in result.stdout

    log = _log(deploy_env)
    assert log.count("systemctl start perch-backup.service") == 2
    assert f"git checkout --detach {REVISION}" in log
    assert "GIT_SHA=aaaaaaa|docker compose up -d --build --wait --wait-timeout 300" in log
    for service in APP_SERVICES:
        assert f"docker compose exec -T {service} python3 -c" in log
    installed = (deploy_env["systemd_target"] / "perch.service").read_text()
    assert "ExecStart=/usr/bin/docker compose up -d\n" in installed
    assert "ExecStart=/usr/bin/docker compose up -d --build" not in installed


def test_requires_a_full_sha_before_running_any_external_command(deploy_env):
    result = _run(deploy_env, "abc1234")

    assert result.returncode == 2
    assert "full lowercase 40-character Git SHA" in result.stderr
    assert _log(deploy_env) == ""


def test_dirty_checkout_fails_before_fetch_backup_or_deploy(deploy_env):
    result = _run(deploy_env, REVISION, FAKE_DIRTY=1)

    assert result.returncode == 1
    assert "checkout is dirty" in result.stderr
    log = _log(deploy_env)
    assert "git status --porcelain" in log
    assert "git fetch" not in log
    assert "systemctl start" not in log
    assert "docker compose up" not in log


def test_normal_deploy_refuses_a_revision_other_than_current_origin_main(deploy_env):
    result = _run(deploy_env, REVISION, FAKE_REMOTE_REVISION=OTHER_REVISION)

    assert result.returncode == 1
    assert "requested revision is not current origin/main" in result.stderr
    assert "systemctl start" not in _log(deploy_env)


def test_predeploy_backup_failure_stops_before_checkout_or_compose(deploy_env):
    result = _run(
        deploy_env,
        REVISION,
        FAKE_BACKUP_RESULT="failed",
        FAKE_BACKUP_STATUS=1,
    )

    assert result.returncode == 1
    assert "predeploy backup failed" in result.stderr
    log = _log(deploy_env)
    assert "git checkout" not in log
    assert "docker compose up" not in log


def test_backup_service_start_failure_is_attributed_before_checkout(deploy_env):
    result = _run(
        deploy_env,
        REVISION,
        FAKE_SYSTEMCTL_START_FAIL=1,
        FAKE_BACKUP_RESULT="failed",
        FAKE_BACKUP_STATUS=1,
    )

    assert result.returncode == 1
    assert "predeploy backup could not start or complete: result=failed status=1" in result.stderr
    log = _log(deploy_env)
    assert "git checkout" not in log
    assert "docker compose up" not in log


def test_running_revision_mismatch_fails_before_postdeploy_backup(deploy_env):
    result = _run(deploy_env, REVISION, FAKE_MISMATCH_SERVICE="runner")

    assert result.returncode == 1
    assert "service revision mismatch: service=runner" in result.stderr
    assert _log(deploy_env).count("systemctl start perch-backup.service") == 1


def test_compose_start_failure_stops_before_revision_and_data_checks(deploy_env):
    result = _run(deploy_env, REVISION, FAKE_DOCKER_UP_FAIL=1)

    assert result.returncode == 1
    log = _log(deploy_env)
    assert "docker compose up -d --build --wait --wait-timeout 300" in log
    assert "docker compose exec" not in log
    assert "sqlite3 " not in log
    assert log.count("systemctl start perch-backup.service") == 1


def test_sqlite_failure_prevents_public_health_and_postdeploy_backup(deploy_env):
    result = _run(deploy_env, REVISION, FAKE_BAD_DATABASE="journal.db")

    assert result.returncode == 1
    assert "SQLite quick_check failed" in result.stderr
    log = _log(deploy_env)
    assert "curl " not in log
    assert log.count("systemctl start perch-backup.service") == 1


def test_missing_required_database_prevents_public_health_and_postdeploy_backup(deploy_env):
    (deploy_env["root"] / "data" / "universe.db").unlink()

    result = _run(deploy_env, REVISION)

    assert result.returncode == 1
    assert "required production database is missing: data/universe.db" in result.stderr
    log = _log(deploy_env)
    assert "curl " not in log
    assert log.count("systemctl start perch-backup.service") == 1


def test_unhealthy_public_response_prevents_postdeploy_backup(deploy_env):
    result = _run(deploy_env, REVISION, FAKE_HEALTH_RESPONSE='{"ok":false}')

    assert result.returncode == 1
    assert 'public health response was not healthy: {"ok":false}' in result.stderr
    assert _log(deploy_env).count("systemctl start perch-backup.service") == 1


def test_rollback_requires_the_revision_to_be_an_origin_main_ancestor(deploy_env):
    result = _run(
        deploy_env,
        "--rollback",
        REVISION,
        FAKE_REMOTE_REVISION=OTHER_REVISION,
        FAKE_ANCESTOR=0,
    )

    assert result.returncode == 1
    assert "rollback revision is not an ancestor" in result.stderr
    assert "systemctl start" not in _log(deploy_env)


def test_verified_ancestor_rollback_uses_the_same_complete_gate(deploy_env):
    result = _run(
        deploy_env,
        "--rollback",
        REVISION,
        FAKE_REMOTE_REVISION=OTHER_REVISION,
        FAKE_ANCESTOR=1,
    )

    assert result.returncode == 0, result.stderr
    assert f"DEPLOYMENT_VERIFIED mode=rollback previous={PREVIOUS_REVISION} revision={REVISION}" in result.stdout
