#!/usr/bin/env bash
#
# Start the full live stack — the three long-running processes plus a
# caffeinate guardian that keeps this Mac from sleeping while they run.
#
# The three processes share nothing but data/*.db on disk:
#   worker  — tradebot.telegram_bot.worker : outbox delivery (the ONLY
#             thing that calls Telegram's send API)
#   bot     — tradebot.telegram_bot.main   : command dispatcher (long-poll)
#   runner  — tradebot.runner --live       : the scanner (evaluates bars,
#             journals every cluster, queues alerts)
#
# Why caffeinate: a sleeping Mac freezes all three; the heartbeat stops
# and the scanner misses bar evaluations during market hours. The
# guardian holds an idle-sleep assertion for exactly as long as the
# scanner lives (-w <runner pid>), then exits on its own.
#
# Idempotent: a process already running is left alone, not doubled.
# Usage:
#   scripts/start.sh                 # start whatever isn't running
#   scripts/start.sh --sync-commands # also push commands.py to BotFather
#
set -euo pipefail

# --- locate repo root (this script lives in scripts/) --------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

DATA_DIR="$REPO_ROOT/data"
mkdir -p "$DATA_DIR"

SYNC_COMMANDS=""
if [[ "${1:-}" == "--sync-commands" ]]; then
  SYNC_COMMANDS="--sync-commands"
fi

# --- load secrets --------------------------------------------------- #
if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "ERROR: .env not found at $REPO_ROOT/.env — cannot start." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$REPO_ROOT/.env"
set +a

PY="${PYTHON:-python3}"

# --- helpers -------------------------------------------------------- #
# A process is "running" if its pidfile holds a live pid whose command
# line still matches the module we expect (guards against a recycled pid).
is_running() {  # $1 = pidfile, $2 = match string in `ps` command
  local pidfile="$1" needle="$2" pid
  [[ -f "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -q -- "$needle"
}

start_one() {  # $1 = label, $2 = pidfile, $3 = logfile, $4 = match, $5.. = command
  local label="$1" pidfile="$2" logfile="$3" needle="$4"
  shift 4
  if is_running "$pidfile" "$needle"; then
    echo "  $label already running (pid $(cat "$pidfile"))"
    return 0
  fi
  nohup "$@" >>"$logfile" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$pidfile"
  echo "  $label started (pid $pid) -> ${logfile#$REPO_ROOT/}"
}

echo "Starting Kestrel live stack in $REPO_ROOT"

# --- start the three processes -------------------------------------- #
# Order: worker and bot first (delivery + command intake), scanner last.
start_one "worker" "$DATA_DIR/worker.pid" "$DATA_DIR/outbox_worker.log" \
  "tradebot.telegram_bot.worker" \
  "$PY" -m tradebot.telegram_bot.worker

# `main` takes --sync-commands; without it, main still hard-fails at
# startup if commands.py has drifted from BotFather (by design).
if [[ -n "$SYNC_COMMANDS" ]]; then
  start_one "bot" "$DATA_DIR/bot.pid" "$DATA_DIR/telegram_bot.log" \
    "tradebot.telegram_bot.main" \
    "$PY" -m tradebot.telegram_bot.main --sync-commands
else
  start_one "bot" "$DATA_DIR/bot.pid" "$DATA_DIR/telegram_bot.log" \
    "tradebot.telegram_bot.main" \
    "$PY" -m tradebot.telegram_bot.main
fi

start_one "runner" "$DATA_DIR/runner.pid" "$DATA_DIR/runner_live.log" \
  "tradebot.runner" \
  "$PY" -m tradebot.runner --live --broad-scan

# --- caffeinate guardian tied to the scanner's lifetime ------------- #
RUNNER_PID="$(cat "$DATA_DIR/runner.pid" 2>/dev/null || true)"
if [[ -n "$RUNNER_PID" ]] && ps -p "$RUNNER_PID" >/dev/null 2>&1; then
  if is_running "$DATA_DIR/caffeinate.pid" "caffeinate"; then
    echo "  caffeinate already running (pid $(cat "$DATA_DIR/caffeinate.pid"))"
  else
    # -d display, -i idle system, -m disk idle, -s system, -u user-active;
    # -w waits on the scanner pid so the assertion drops when it exits.
    nohup caffeinate -dimsu -w "$RUNNER_PID" >>"$DATA_DIR/caffeinate.log" 2>&1 &
    CAF_PID=$!
    disown "$CAF_PID" 2>/dev/null || true
    echo "$CAF_PID" >"$DATA_DIR/caffeinate.pid"
    echo "  caffeinate started (pid $CAF_PID, guarding runner pid $RUNNER_PID)"
  fi
fi

# --- power-source warning: caffeinate can't beat a closed lid on batt #
if command -v pmset >/dev/null 2>&1; then
  if pmset -g batt 2>/dev/null | grep -qi "Battery Power"; then
    echo ""
    echo "WARNING: running on BATTERY. caffeinate blocks idle sleep, but a"
    echo "         CLOSED LID or low battery can still sleep the Mac and stall"
    echo "         the scanner. Plug into AC power and keep the lid open for a"
    echo "         reliable market-hours run."
  fi
fi

echo ""
echo "Done. Check health with: scripts/status.sh"
