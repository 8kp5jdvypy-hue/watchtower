#!/usr/bin/env bash
# Exact-revision production deployment for the Perch VPS.
#
# Normal release:
#   scripts/deploy.sh <40-character-origin-main-sha>
#
# Explicit rollback to a reviewed ancestor of origin/main:
#   scripts/deploy.sh --rollback <40-character-ancestor-sha>
#
# The wrapper owns the GIT_SHA build argument, verified backups, detached
# checkout, Compose wait, per-service revision proof, SQLite checks, public
# health check, and systemd unit installation. Do not reconstruct those steps
# manually.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MODE="deploy"
if [[ "${1:-}" == "--rollback" ]]; then
  MODE="rollback"
  shift
fi

EXPECTED_REVISION="${1:-}"
if [[ -z "$EXPECTED_REVISION" || $# -ne 1 ]]; then
  echo "Usage: scripts/deploy.sh [--rollback] <40-character-git-sha>" >&2
  exit 2
fi
if [[ ! "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: revision must be a full lowercase 40-character Git SHA." >&2
  exit 2
fi

for command_name in git docker systemctl install sqlite3 curl grep; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: required command is unavailable: $command_name" >&2
    exit 1
  fi
done

if [[ ! -f docker-compose.yml || ! -f .env ]]; then
  echo "ERROR: run from a deployed checkout containing docker-compose.yml and .env." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: checkout is dirty; refusing to overwrite operator or uncommitted files." >&2
  exit 1
fi

echo "Fetching authoritative origin/main..."
git fetch origin main

if ! RESOLVED_REVISION="$(git rev-parse --verify "${EXPECTED_REVISION}^{commit}" 2>/dev/null)"; then
  echo "ERROR: requested revision does not resolve to a commit: $EXPECTED_REVISION" >&2
  exit 1
fi
REMOTE_MAIN="$(git rev-parse --verify 'origin/main^{commit}')"

if [[ "$MODE" == "deploy" ]]; then
  if [[ "$RESOLVED_REVISION" != "$REMOTE_MAIN" ]]; then
    echo "ERROR: requested revision is not current origin/main." >&2
    echo "expected=$RESOLVED_REVISION origin_main=$REMOTE_MAIN" >&2
    exit 1
  fi
elif ! git merge-base --is-ancestor "$RESOLVED_REVISION" "$REMOTE_MAIN"; then
  echo "ERROR: rollback revision is not an ancestor of current origin/main." >&2
  exit 1
fi

PREVIOUS_REVISION="$(git rev-parse --verify 'HEAD^{commit}')"

run_backup() {
  local phase="$1" result status
  echo "Running verified $phase backup..."
  if ! systemctl start perch-backup.service; then
    result="$(systemctl show perch-backup.service --property=Result --value 2>/dev/null || echo unknown)"
    status="$(systemctl show perch-backup.service --property=ExecMainStatus --value 2>/dev/null || echo unknown)"
    echo "ERROR: $phase backup could not start or complete: result=$result status=$status" >&2
    exit 1
  fi
  result="$(systemctl show perch-backup.service --property=Result --value)"
  status="$(systemctl show perch-backup.service --property=ExecMainStatus --value)"
  if [[ "$result" != "success" || "$status" != "0" ]]; then
    echo "ERROR: $phase backup failed: result=$result status=$status" >&2
    exit 1
  fi
  echo "${phase}_backup=success"
}

run_backup predeploy

echo "Checking out exact revision $RESOLVED_REVISION..."
git checkout --detach "$RESOLVED_REVISION"
ACTUAL_REVISION="$(git rev-parse --verify 'HEAD^{commit}')"
if [[ "$ACTUAL_REVISION" != "$RESOLVED_REVISION" ]]; then
  echo "ERROR: checkout mismatch: expected=$RESOLVED_REVISION actual=$ACTUAL_REVISION" >&2
  exit 1
fi

SYSTEMD_DIR="${PERCH_SYSTEMD_DIR:-/etc/systemd/system}"
install -m 0644 systemd/perch.service "$SYSTEMD_DIR/perch.service"
install -m 0644 systemd/perch-backup.service "$SYSTEMD_DIR/perch-backup.service"
install -m 0644 systemd/perch-backup.timer "$SYSTEMD_DIR/perch-backup.timer"
install -m 0644 systemd/perch-screening-archive.service \
  "$SYSTEMD_DIR/perch-screening-archive.service"
install -m 0644 systemd/perch-screening-archive.timer \
  "$SYSTEMD_DIR/perch-screening-archive.timer"
systemctl daemon-reload
systemctl enable --now perch-screening-archive.timer

SHORT_REVISION="${RESOLVED_REVISION:0:7}"
echo "Building and starting revision $SHORT_REVISION..."
GIT_SHA="$SHORT_REVISION" docker compose up -d --build --wait --wait-timeout 300

APP_SERVICES=(
  worker
  bot
  runner
  postmarket
  postmarket-discovery
  postmarket-external-context
  postmarket-customer-dry-run
  api
)
for service in "${APP_SERVICES[@]}"; do
  running_revision="$(
    docker compose exec -T "$service" python3 -c \
      "from tradebot.journal import code_version; print(code_version())"
  )"
  running_revision="${running_revision//$'\r'/}"
  running_revision="${running_revision//$'\n'/}"
  if [[ "$running_revision" != "$SHORT_REVISION" ]]; then
    echo "ERROR: service revision mismatch: service=$service expected=$SHORT_REVISION actual=$running_revision" >&2
    exit 1
  fi
  echo "service_revision[$service]=$running_revision"
done

REQUIRED_DATABASES=(
  data/journal.db
  data/users.db
  data/evaluations.db
  data/postmarket_shadow.db
  data/universe.db
)
for database in "${REQUIRED_DATABASES[@]}"; do
  if [[ ! -f "$database" ]]; then
    echo "ERROR: required production database is missing: $database" >&2
    exit 1
  fi
  quick_check="$(sqlite3 -readonly "$database" 'PRAGMA quick_check;')"
  if [[ -z "$quick_check" ]] || printf '%s\n' "$quick_check" | grep -Evqx 'ok'; then
    echo "ERROR: SQLite quick_check failed: database=$database result=$quick_check" >&2
    exit 1
  fi
  echo "sqlite_quick_check[$database]=ok"
done

health_response="$(curl -fsS https://api.perchmarkets.com/healthz)"
if [[ "$health_response" != *'"ok":true'* ]]; then
  echo "ERROR: public health response was not healthy: $health_response" >&2
  exit 1
fi
echo "public_health=$health_response"

run_backup postdeploy

echo "DEPLOYMENT_VERIFIED mode=$MODE previous=$PREVIOUS_REVISION revision=$RESOLVED_REVISION"
