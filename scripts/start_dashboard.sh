#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Create it with: uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
  exit 1
fi

set -a
[[ -f .env ]] && source .env
set +a

export PATH="/Applications/VMware Fusion.app/Contents/Library:$PATH"
if [[ -z "${OSWORLD_ROOT:-}" ]]; then
  if [[ -d "$ROOT/../OSWorld" ]]; then
    export OSWORLD_ROOT="$(cd "$ROOT/../OSWorld" && pwd)"
  elif [[ -d "$ROOT/../../OSWorld" ]]; then
    export OSWORLD_ROOT="$(cd "$ROOT/../../OSWorld" && pwd)"
  fi
fi

exec .venv/bin/python scripts/macu_dashboard.py --host 127.0.0.1 --port "${MACU_DASHBOARD_PORT:-8002}" --open
