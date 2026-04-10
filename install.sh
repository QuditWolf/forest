#!/usr/bin/env bash
# install.sh — one-shot setup for TaskThink
# Run from the repo root: bash install.sh
# Re-running is safe — all steps are idempotent.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv"
BIN_LINK="$HOME/.local/bin/tasks"
COMPLETION_DIR="$HOME/.bash_completions"
BASHRC="$HOME/.bashrc"
DEFAULT_TASKS_ROOT="$HOME/org/taskthink"

# ── colours ───────────────────────────────────────────────────────────────────
bold=$(tput bold 2>/dev/null || true)
green=$(tput setaf 2 2>/dev/null || true)
yellow=$(tput setaf 3 2>/dev/null || true)
red=$(tput setaf 1 2>/dev/null || true)
reset=$(tput sgr0 2>/dev/null || true)

ok()   { echo "${green}${bold}  ok${reset}  $*"; }
info() { echo "${yellow}${bold}  ..${reset}  $*"; }
err()  { echo "${red}${bold} err${reset}  $*" >&2; }
hdr()  { echo; echo "${bold}$*${reset}"; echo "────────────────────────────────────────"; }

# ── helpers ───────────────────────────────────────────────────────────────────

# Append a block to bashrc only if it isn't already there (keyed by marker).
append_to_bashrc() {
  local marker="$1"
  local block="$2"
  if grep -qF "$marker" "$BASHRC" 2>/dev/null; then
    ok "bashrc: '$marker' already present — skipping"
  else
    printf '\n%s\n' "$block" >> "$BASHRC"
    ok "bashrc: added '$marker'"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
hdr "1. Check / install uv"
# ══════════════════════════════════════════════════════════════════════════════

if command -v uv &>/dev/null; then
  ok "uv already installed: $(uv --version)"
else
  info "installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed: $(uv --version)"
fi

# ══════════════════════════════════════════════════════════════════════════════
hdr "2. Python virtual environment"
# ══════════════════════════════════════════════════════════════════════════════

if [ -f "$VENV/bin/python" ]; then
  ok "venv already exists at $VENV"
else
  info "creating venv..."
  uv venv "$VENV"
  ok "venv created"
fi

info "installing/updating Python packages..."
uv pip install -e "$REPO" --quiet
ok "packages installed"

# ══════════════════════════════════════════════════════════════════════════════
hdr "3. Task data directories"
# ══════════════════════════════════════════════════════════════════════════════

TASKS_ROOT="${TASKS_ROOT:-$DEFAULT_TASKS_ROOT}"
mkdir -p "$TASKS_ROOT"
ok "tasks root: $TASKS_ROOT"

# ══════════════════════════════════════════════════════════════════════════════
hdr "4. CLI symlink"
# ══════════════════════════════════════════════════════════════════════════════

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/tasks" "$BIN_LINK"
ok "symlink: $BIN_LINK -> $VENV/bin/tasks"

# ══════════════════════════════════════════════════════════════════════════════
hdr "5. Bash completion"
# ══════════════════════════════════════════════════════════════════════════════

mkdir -p "$COMPLETION_DIR"
"$VENV/bin/tasks" --install-completion bash 2>/dev/null || true
ok "completion script installed"

# ══════════════════════════════════════════════════════════════════════════════
hdr "6. ~/.bashrc entries"
# ══════════════════════════════════════════════════════════════════════════════

# PATH
append_to_bashrc "tasks: PATH" "# tasks: PATH
export PATH=\"\$HOME/.local/bin:\$PATH\""

# TASKS_ROOT
append_to_bashrc "tasks: TASKS_ROOT" "# tasks: TASKS_ROOT
export TASKS_ROOT=\"$TASKS_ROOT\""

# bash completion
append_to_bashrc "tasks: completion" "# tasks: completion
if [ -f \"\$HOME/.bash_completions/tasks.sh\" ]; then
  source \"\$HOME/.bash_completions/tasks.sh\"
fi"

# ══════════════════════════════════════════════════════════════════════════════
hdr "7. Config file"
# ══════════════════════════════════════════════════════════════════════════════

CONFIG="$REPO/tasks.toml"
if [ -f "$CONFIG" ]; then
  ok "config already exists: $CONFIG"
else
  cat > "$CONFIG" <<EOF
# tasks.toml — runtime configuration
# Edit before running 'tasks serve' or './start.sh'

[server]
host = "0.0.0.0"   # bind address  e.g. "127.0.0.1" or "192.168.1.1"
port = 7000

[tasks]
root = "$TASKS_ROOT"
EOF
  ok "config written: $CONFIG"
fi

# ══════════════════════════════════════════════════════════════════════════════
hdr "8. Verify"
# ══════════════════════════════════════════════════════════════════════════════

export PATH="$HOME/.local/bin:$PATH"
export TASKS_ROOT

if "$BIN_LINK" --help &>/dev/null; then
  ok "'tasks' binary works"
else
  err "'tasks' binary failed — check $VENV"
  exit 1
fi

if [ -f "$REPO/static/index.html" ]; then
  ok "static/index.html present"
else
  err "static/index.html missing — web UI won't load"
fi

# ══════════════════════════════════════════════════════════════════════════════
echo
echo "${green}${bold}Installation complete.${reset}"
echo
echo "  Next steps:"
echo "    source ~/.bashrc          # apply PATH / completion in this shell"
echo "    \$EDITOR tasks.toml        # set your TASKS_ROOT and server host/port"
echo "    ./start.sh                # start web UI with hot-reload"
echo "    tasks serve               # start web UI (production)"
echo "    tasks add . 'My project' --folder   # create first page"
echo
echo "  Config:  $CONFIG"
echo "  Data:    $TASKS_ROOT"
echo
echo "  Open http://localhost:7000 in your browser after starting the server."
echo
