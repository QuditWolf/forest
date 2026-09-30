#!/usr/bin/env bash
# install.sh - install forest locally.
#   bash install.sh          dev install into ./.venv (server + CLI + TUI), links ~/.local/bin/forest
#   bash install.sh --tool   just the CLI/TUI client as a uv tool (laptop talking to your server)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
if [ "${1:-}" = "--tool" ]; then
  uv tool install --force "$REPO"
  echo "installed. next: forest login --url https://tools.example.com"
  exit 0
fi
[ -f "$REPO/.venv/bin/python" ] || uv venv "$REPO/.venv" --python 3.12
VIRTUAL_ENV="$REPO/.venv" uv pip install -e "$REPO" --quiet
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO/.venv/bin/forest" "$HOME/.local/bin/forest"
echo "installed: $(command -v forest || echo "$HOME/.local/bin/forest")"
echo "local server: ./start.sh   |  client: forest login --url URL --token TOKEN"
