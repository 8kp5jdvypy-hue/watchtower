#!/usr/bin/env bash
#
# Stop the live stack gracefully. Sends SIGTERM (not SIGKILL) so the
# worker finishes its current send batch and the bot/runner unwind
# cleanly; the caffeinate guardian exits on its own once the scanner is
# gone, but we tidy it up explicitly too.
#
# Usage:
#   scripts/stop.sh          # graceful SIGTERM to all
#   scripts/stop.sh --force  # SIGKILL anything still alive after a grace wait
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
  FORCE="1"
fi

stop_one() {  # $1 = label, $2 = pidfile
  local label="$1" pidfile="$2" pid
  [[ -f "$pidfile" ]] || { echo "  $label: no pidfile, skipping"; return 0; }
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -z "$pid" ]] || ! ps -p "$pid" >/dev/null 2>&1; then
    echo "  $label: not running (stale pidfile removed)"
    rm -f "$pidfile"
    return 0
  fi
  echo "  $label: SIGTERM -> pid $pid"
  kill -TERM "$pid" 2>/dev/null || true
}

echo "Stopping Kestrel live stack"
# Scanner first so caffeinate's -w releases; then bot, then worker.
stop_one "runner"     "$DATA_DIR/runner.pid"
stop_one "bot"        "$DATA_DIR/bot.pid"
stop_one "worker"     "$DATA_DIR/worker.pid"
stop_one "caffeinate" "$DATA_DIR/caffeinate.pid"

# Grace period, then verify. Poll instead of a flat sleep so a clean
# shutdown returns fast.
echo "Waiting for graceful shutdown..."
for _ in $(seq 1 20); do
  alive=0
  for pf in runner.pid bot.pid worker.pid caffeinate.pid; do
    p="$(cat "$DATA_DIR/$pf" 2>/dev/null || true)"
    [[ -n "$p" ]] && ps -p "$p" >/dev/null 2>&1 && alive=1
  done
  [[ "$alive" == "0" ]] && break
  sleep 0.5
done

for pf in runner.pid bot.pid worker.pid caffeinate.pid; do
  p="$(cat "$DATA_DIR/$pf" 2>/dev/null || true)"
  if [[ -n "$p" ]] && ps -p "$p" >/dev/null 2>&1; then
    if [[ -n "$FORCE" ]]; then
      echo "  ${pf%.pid}: still alive, SIGKILL -> pid $p"
      kill -KILL "$p" 2>/dev/null || true
      rm -f "$DATA_DIR/$pf"
    else
      echo "  ${pf%.pid}: STILL RUNNING (pid $p) — rerun with --force to SIGKILL"
    fi
  else
    rm -f "$DATA_DIR/$pf"
  fi
done

echo "Done."
