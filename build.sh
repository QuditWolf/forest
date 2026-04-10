#!/usr/bin/env bash
# build.sh — Package TaskThink into a portable distributable.
# Creates dist/taskthink-<version>.tar.gz that can be extracted anywhere and run.
set -euo pipefail

VERSION=$(python3 -c "
import tomllib
with open('pyproject.toml','rb') as f:
    print(tomllib.load(f)['project']['version'])
")

DIST_DIR="dist/taskthink-${VERSION}"

echo "Building TaskThink v${VERSION}..."

# Clean and create dist directory
rm -rf "${DIST_DIR}"
mkdir -p "${DIST_DIR}"

# Copy source
cp -r tasks/           "${DIST_DIR}/tasks/"
cp -r static/          "${DIST_DIR}/static/"
cp    pyproject.toml   "${DIST_DIR}/"
cp    config.sample.toml "${DIST_DIR}/"
[ -f tasks.toml ] && cp tasks.toml "${DIST_DIR}/" || true

# Generate install.sh inside the bundle
cat > "${DIST_DIR}/install.sh" << 'INSTALL'
#!/usr/bin/env bash
# Install TaskThink dependencies into a local .venv
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Ensure uv is available
if ! command -v uv &>/dev/null; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "Creating virtual environment..."
uv venv .venv
uv pip install -e .

echo ""
echo "Install complete. Run ./run.sh [--config tasks.toml] to start."
INSTALL
chmod +x "${DIST_DIR}/install.sh"

# Generate run.sh inside the bundle
cat > "${DIST_DIR}/run.sh" << 'RUN'
#!/usr/bin/env bash
# Start the TaskThink server.
# Usage:
#   ./run.sh                        — uses tasks.toml in this directory (if present)
#   ./run.sh --config /path/to.toml — use explicit config file
#   TASKS_CONFIG=/path/to.toml ./run.sh
#   TASKS_ROOT=/my/pages ./run.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Parse --config flag
CONFIG_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|-c) CONFIG_ARG="$2"; shift 2;;
    *) shift;;
  esac
done

if [ -n "$CONFIG_ARG" ]; then
  export TASKS_CONFIG="$(realpath "$CONFIG_ARG")"
elif [ -z "${TASKS_CONFIG:-}" ] && [ -f "$DIR/tasks.toml" ]; then
  export TASKS_CONFIG="$DIR/tasks.toml"
fi

# Read host/port from config if available, fall back to defaults
HOST="${TASKS_HOST:-0.0.0.0}"
PORT="${TASKS_PORT:-7000}"

# Quick parse of tasks.toml for host/port (avoids Python startup just for this)
if [ -n "${TASKS_CONFIG:-}" ] && [ -f "$TASKS_CONFIG" ]; then
  _HOST=$(python3 -c "
import tomllib,sys
with open('$TASKS_CONFIG','rb') as f: c=tomllib.load(f)
h=c.get('server',{}).get('host')
if h: print(h)
" 2>/dev/null || true)
  _PORT=$(python3 -c "
import tomllib,sys
with open('$TASKS_CONFIG','rb') as f: c=tomllib.load(f)
p=c.get('server',{}).get('port')
if p: print(p)
" 2>/dev/null || true)
  [ -n "$_HOST" ] && HOST="$_HOST"
  [ -n "$_PORT" ] && PORT="$_PORT"
fi

echo "Starting TaskThink on http://${HOST}:${PORT}"
[ -n "${TASKS_CONFIG:-}" ] && echo "  Config: $TASKS_CONFIG"

PYTHON=".venv/bin/python"
[ -f "$PYTHON" ] || { echo "Run ./install.sh first"; exit 1; }

exec "$PYTHON" -m uvicorn tasks.api:app --host "$HOST" --port "$PORT"
RUN
chmod +x "${DIST_DIR}/run.sh"

# Create tarball
mkdir -p dist
tar -czf "dist/taskthink-${VERSION}.tar.gz" -C dist "taskthink-${VERSION}"

echo ""
echo "Built: dist/taskthink-${VERSION}.tar.gz"
echo ""
echo "To deploy on another machine:"
echo "  1. Copy dist/taskthink-${VERSION}.tar.gz"
echo "  2. tar -xzf taskthink-${VERSION}.tar.gz"
echo "  3. cd taskthink-${VERSION}"
echo "  4. cp config.sample.toml tasks.toml && \$EDITOR tasks.toml"
echo "  5. ./install.sh"
echo "  6. ./run.sh"
