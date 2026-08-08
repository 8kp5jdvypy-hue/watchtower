#!/usr/bin/env bash
#
# Auto-restart watchdog for the live stack (Perch Phase 2: harden the
# live scanner). Meant to run on a short interval via the accompanying
# LaunchAgent (every 5 minutes) — NOT as a long-running loop itself, so
# the watchdog never becomes a second unsupervised process that itself
# needs watching.
#
# If the runner (scanner) is down during market hours, or the bot/worker
# are down at any time, this re-runs start.sh — which is already
# idempotent (see is_running()/start_one() there), so it only starts
# what's actually missing, never double-starts a healthy process.
#
# The runner is deliberately NOT restarted outside market hours — there
# is nothing for it to do, and it exits on its own at session close, so
# "down after hours" is its normal, correct state, not a fault.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
LOG_FILE="$DATA_DIR/watchdog.log"

cd "$REPO_ROOT" || exit 1
mkdir -p "$DATA_DIR"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG_FILE"; }

is_market_open() {
  python3 - <<'PY'
import sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
try:
    from tradebot.runner import CALENDAR, ET
    now = datetime.now(timezone.utc)
    session_date = now.astimezone(ET).date()
    if not CALENDAR.is_session(session_date):
        sys.exit(1)
    open_ts = CALENDAR.session_open(session_date).to_pydatetime()
    close_ts = CALENDAR.session_close(session_date).to_pydatetime()
    sys.exit(0 if open_ts <= now <= close_ts else 1)
except Exception:
    sys.exit(1)
PY
}

proc_down() {  # $1 = pidfile, $2 = match string -- returns 0 (true) if down
  local pid
  pid="$(cat "$1" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && ps -p "$pid" -o command= 2>/dev/null | grep -q -- "$2"; then
    return 1
  fi
  return 0
}

restart_needed=0
reasons=()

if proc_down "$DATA_DIR/bot.pid" "tradebot.telegram_bot.main"; then
  reasons+=("bot")
  restart_needed=1
fi
if proc_down "$DATA_DIR/worker.pid" "tradebot.telegram_bot.worker"; then
  reasons+=("worker")
  restart_needed=1
fi
if proc_down "$DATA_DIR/runner.pid" "tradebot.runner"; then
  if is_market_open; then
    reasons+=("runner (during market hours)")
    restart_needed=1
  fi
fi

if [[ "$restart_needed" == "1" ]]; then
  detail="watchdog restarting: ${reasons[*]}"
  log "$detail"
  bash "$SCRIPT_DIR/start.sh" >> "$LOG_FILE" 2>&1
  DETAIL="$detail" python3 - <<'PY'
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
from tradebot import incidents

now = datetime.now(timezone.utc)
detail = os.environ["DETAIL"]
# Immediate open+close: a short, already-resolved incident record (the
# restart already happened above) rather than a lingering "open" entry
# that would falsely keep status.sh/the status page in ATTENTION NEEDED
# after start.sh has already fixed it.
incidents.open_incident("watchdog_restart", detail, now)
incidents.close_incident("watchdog_restart", now)
PY
fi
