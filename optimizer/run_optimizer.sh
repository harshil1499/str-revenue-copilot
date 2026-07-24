#!/usr/bin/env bash
# Wrapper that the OS scheduler invokes daily.
#
# Two jobs it does beyond just running the Python:
#   1. Loads credentials from a local, git-ignored env file (not the shell profile — that
#      coupling is unreliable under a headless scheduler).
#   2. Waits for the network before doing anything. The scheduler fires deferred jobs at
#      machine-wake, often before Wi-Fi/DNS is up; without this guard the run would fail its
#      API calls silently and could re-render stale data as if fresh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/../private/optimizer.log"
PYTHON="${PYTHON:-/usr/bin/python3}"
ENV_FILE="$SCRIPT_DIR/../private/.env"     # git-ignored; see .env.example for shape

ts() { date "+%Y-%m-%d %H:%M:%S"; }

# Poll the API host until reachable (~60s max), so we never run on a half-woken machine.
wait_for_network() {
  local i
  for i in $(seq 1 12); do
    if curl -s -o /dev/null -m 5 https://api.pricelabs.co; then
      return 0
    fi
    echo "[$(ts)] network not ready (attempt $i/12), waiting 5s..."
    sleep 5
  done
  return 1
}

{
  echo "==================== $(ts) starting optimizer run ===================="

  if [[ ! -f "$ENV_FILE" ]]; then
    echo "[$(ts)] ERROR: missing $ENV_FILE (copy .env.example → .env and fill it in)"
    exit 1
  fi

  set -a; source "$ENV_FILE"; set +a

  if [[ -z "${PRICELABS_API_KEY:-}" ]]; then
    echo "[$(ts)] ERROR: missing required credentials in $ENV_FILE"
    exit 1
  fi

  if ! wait_for_network; then
    echo "[$(ts)] ERROR: network unreachable after 60s — skipping run (do not overwrite good data)"
    exit 3
  fi

  "$PYTHON" "$SCRIPT_DIR/weekday_price_optimizer.py" 2>&1

  echo "==================== $(ts) finished ===================="
} >> "$LOG" 2>&1
