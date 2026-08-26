#!/usr/bin/env bash
#
# One-glance health check for the live stack: are the three processes up,
# is the scanner's heartbeat fresh, is the kill switch tripped, are there
# open incidents, and is the Mac at risk of sleeping.
#
# Exit code is 0 only when all three processes are up AND (if the market
# is open) the heartbeat is fresh — so it doubles as a cron/monitor probe.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"

RC=0

proc_line() {  # $1 = label, $2 = pidfile, $3 = match string
  local label="$1" pidfile="$2" needle="$3" pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && ps -p "$pid" -o command= 2>/dev/null | grep -q -- "$needle"; then
    printf "  %-11s UP    (pid %s)\n" "$label" "$pid"
  else
    printf "  %-11s DOWN\n" "$label"
    RC=1
  fi
}

echo "== processes =="
proc_line "worker"     "$DATA_DIR/worker.pid"     "tradebot.telegram_bot.worker"
proc_line "bot"        "$DATA_DIR/bot.pid"        "tradebot.telegram_bot.main"
proc_line "runner"     "$DATA_DIR/runner.pid"     "tradebot.runner"
proc_line "caffeinate" "$DATA_DIR/caffeinate.pid" "caffeinate"

echo ""
echo "== heartbeat =="
if heartbeat_out="$(PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m tradebot.runner_health \
  --heartbeat "$DATA_DIR/heartbeat.json" --max-age 900 2>&1)"; then
  echo "  $heartbeat_out"
else
  echo "  WARNING: $heartbeat_out"
  RC=1
fi

echo ""
echo "== kill switch =="
if [[ -f "$DATA_DIR/HALT" ]]; then
  echo "  HALT present — alerting is suppressed. Remove data/HALT to resume."
else
  echo "  no HALT flag (alerting enabled)"
fi

echo ""
echo "== open incidents =="
if [[ -f "$DATA_DIR/incidents.jsonl" ]]; then
  # Python prints the human lines, then a final "COUNT=N" line we parse.
  incident_out="$(python3 - "$DATA_DIR/incidents.jsonl" <<'PY'
import json, sys
n = 0
try:
    with open(sys.argv[1]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("ended_at") is None:
                n += 1
                print(f"  OPEN: {row.get('kind')} — {row.get('detail')}")
except Exception as e:
    print(f"  (error reading incidents: {e})")
print(f"COUNT={n}")
PY
)"
  cnt="$(printf '%s\n' "$incident_out" | sed -n 's/^COUNT=//p')"
  lines="$(printf '%s\n' "$incident_out" | grep -v '^COUNT=' || true)"
  echo "${lines:-  none}"
  [[ "$cnt" != "0" ]] && RC=1
else
  echo "  no incidents.jsonl"
fi

echo ""
echo "== power =="
if command -v pmset >/dev/null 2>&1; then
  batt_line="$(pmset -g batt 2>/dev/null | grep -i "'.*Power'" || true)"
  echo "  ${batt_line:-unknown}"
  if pmset -g batt 2>/dev/null | grep -qi "Battery Power"; then
    echo "  NOTE: on battery — a closed lid can still sleep the Mac. Prefer AC + lid open."
  fi
else
  echo "  pmset unavailable"
fi

echo ""
if [[ "$RC" == "0" ]]; then
  echo "OVERALL: healthy"
else
  echo "OVERALL: ATTENTION NEEDED (see warnings above)"
fi
exit "$RC"
