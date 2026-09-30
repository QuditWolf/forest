#!/usr/bin/env bash
# start.sh - run the forest server locally with hot reload (dev / offline use)
# Usage: ./start.sh [--data DIR]   (default data dir: ~/org/forest-data or FOREST_DATA)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$DIR/.venv/bin/python" ] || { echo "run: bash install.sh"; exit 1; }
[ "${1:-}" = "--data" ] && export FOREST_DATA="$(realpath "$2")"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m forest.cli serve --reload
