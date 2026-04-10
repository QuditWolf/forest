#!/usr/bin/env bash
# start.sh — Start the TaskThink server with hot-reload (dev mode)
# Usage: ./start.sh [--config path/to/tasks.toml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# Parse --config flag
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|-c) export TASKS_CONFIG="$(realpath "$2")"; shift 2;;
    *) shift;;
  esac
done

if [ ! -f "$VENV/bin/python" ]; then
  echo "error: venv not found. run: bash install.sh"
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/static/index.html" ]; then
  echo "error: static/index.html missing"
  exit 1
fi

# Read host/port/root from tasks.toml (via the config module so logic is in one place)
read -r HOST PORT ROOT < <(
  "$VENV/bin/python" - <<'EOF'
from tasks.config import SERVER_HOST, SERVER_PORT, TASKS_ROOT
print(SERVER_HOST, SERVER_PORT, TASKS_ROOT)
EOF
)

mkdir -p "$ROOT"

[ -n "${TASKS_CONFIG:-}" ] && echo "  config : $TASKS_CONFIG"
echo "taskthink server"
echo "  host : $HOST"
echo "  port : $PORT"
echo "  root : $ROOT"
echo "  url  : http://$HOST:$PORT"
echo "  ctrl+c to stop"
echo ""

exec "$VENV/bin/uvicorn" tasks.api:app \
  --host "$HOST" \
  --port "$PORT" \
  --reload \
  --reload-dir "$SCRIPT_DIR/tasks"
