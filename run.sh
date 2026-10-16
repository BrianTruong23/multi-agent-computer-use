#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PORT="${MACU_DASHBOARD_PORT:-8002}"
PID_FILE="$ROOT/runs/dashboard/server.pid"

# Stop only an existing MACU dashboard on this port.
existing_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [[ -n "$existing_pid" ]]; then
  existing_command="$(ps -p "$existing_pid" -o command= 2>/dev/null || true)"
  if [[ "$existing_command" != *"macu_dashboard.py"* ]]; then
    echo "Port $PORT is occupied by another process: $existing_command" >&2
    exit 1
  fi
  # Ask the dashboard to terminate its active MACU process group before the
  # server exits. Otherwise run_macu becomes orphaned and keeps the VM claimed.
  job_running="$(curl -fsS "http://127.0.0.1:$PORT/api/status" 2>/dev/null | python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("job",{}).get("running") else "no")' 2>/dev/null || echo no)"
  if [[ "$job_running" == "yes" ]]; then
    echo "Stopping active MACU run before dashboard restart..."
    curl -fsS -X POST "http://127.0.0.1:$PORT/api/stop" >/dev/null 2>&1 || true
    for _ in {1..30}; do
      job_running="$(curl -fsS "http://127.0.0.1:$PORT/api/status" 2>/dev/null | python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("job",{}).get("running") else "no")' 2>/dev/null || echo no)"
      [[ "$job_running" != "yes" ]] && break
      sleep 0.5
    done
  fi
  echo "Stopping existing MACU dashboard (PID $existing_pid)..."
  kill "$existing_pid" 2>/dev/null || true
  for _ in {1..30}; do
    kill -0 "$existing_pid" 2>/dev/null || break
    sleep 0.2
  done
fi

mkdir -p "$ROOT/runs/dashboard"
echo "$$" > "$PID_FILE"

# A separate watchdog survives dashboard crashes (including SIGKILL). When the
# server PID disappears it terminates MACU workers, powers off pool VMs, frees
# the registry, and clears interrupted task claims.
set -a
[[ -f "$ROOT/.env" ]] && source "$ROOT/.env"
set +a
if [[ -z "${OSWORLD_ROOT:-}" ]]; then
  if [[ -d "$ROOT/../OSWorld" ]]; then
    export OSWORLD_ROOT="$(cd "$ROOT/../OSWorld" && pwd)"
  elif [[ -d "$ROOT/../../OSWorld" ]]; then
    export OSWORLD_ROOT="$(cd "$ROOT/../../OSWorld" && pwd)"
  fi
fi
"$ROOT/.venv/bin/python" -c \
  "from pathlib import Path; from utils.dashboard_cleanup import cleanup_dashboard_runtime; cleanup_dashboard_runtime(Path(r'$ROOT'), Path(r'${OSWORLD_ROOT:-$ROOT/../OSWorld}'), Path(r'$ROOT/runs/dashboard'))"
nohup "$ROOT/.venv/bin/python" "$ROOT/utils/dashboard_cleanup.py" \
  --watch-pid "$$" \
  --project-root "$ROOT" \
  --osworld-root "${OSWORLD_ROOT:-$ROOT/../OSWorld}" \
  --result-root "$ROOT/runs/dashboard" \
  --port "$PORT" \
  > "$ROOT/runs/dashboard/cleanup-watchdog.log" 2>&1 &
echo "$!" > "$ROOT/runs/dashboard/cleanup-watchdog.pid"

# Prevent OSWorld's open-file setup request from hanging for 30 minutes.
export MACU_OPEN_FILE_TIMEOUT_SECONDS="${MACU_OPEN_FILE_TIMEOUT_SECONDS:-30}"
export MACU_DASHBOARD_PORT="$PORT"

echo "Starting MACU dashboard at http://127.0.0.1:$PORT"
exec "$ROOT/scripts/start_dashboard.sh"
