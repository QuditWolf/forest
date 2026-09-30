#!/usr/bin/env bash
# Update forest in production without touching your data.
#
#   deploy/update.sh              snapshot data, pull latest code, rebuild, restart, health check
#   deploy/update.sh --rollback   go back to the image that ran before the last update
#
# Data (VAULTS_DIR, STATE_DIR) lives on the host and is never modified by this script;
# it is only copied into a snapshot first. The previous image is kept as forest:previous.
set -euo pipefail
# Run from a temp copy: `git pull` below may replace this very file while bash is reading it.
if [ -z "${FOREST_UPDATE_REPO:-}" ]; then
  export FOREST_UPDATE_REPO="$(cd "$(dirname "$0")/.." && pwd)"
  tmp=$(mktemp /tmp/forest-update.XXXXXX.sh); cp "$0" "$tmp"
  exec bash "$tmp" "$@"
fi
trap 'rm -f "$0"' EXIT
cd "$FOREST_UPDATE_REPO"

envval() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/^"//; s/"$//'; }
VAULTS_DIR=$(envval VAULTS_DIR); STATE_DIR=$(envval STATE_DIR)
BIND_IP=$(envval BIND_IP); PORT=$(envval PORT)
BACKUP_DIR=${BACKUP_DIR:-$(dirname "$VAULTS_DIR")/backups}
HEALTH="http://${BIND_IP:-127.0.0.1}:${PORT:-7700}/healthz"

healthy() {
  for _ in $(seq 1 30); do
    curl -fsS "$HEALTH" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

if [ "${1:-}" = "--rollback" ]; then
  docker image inspect forest:previous >/dev/null || { echo "no forest:previous image"; exit 1; }
  docker tag forest:previous forest:latest
  docker compose up -d --no-build
  healthy && echo "rolled back, healthy" || { echo "rollback started but not healthy - check: docker compose logs forest"; exit 1; }
  exit 0
fi

echo "== 1/5 snapshot data -> $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
tar czf "$BACKUP_DIR/forest-$stamp.tar.gz" -C "$(dirname "$VAULTS_DIR")" "$(basename "$VAULTS_DIR")" "$(basename "$STATE_DIR")"
ls -1t "$BACKUP_DIR"/forest-*.tar.gz | tail -n +11 | xargs -r rm --   # keep the last 10

echo "== 2/5 keep current image as forest:previous"
docker image inspect forest:latest >/dev/null 2>&1 && docker tag forest:latest forest:previous || true

echo "== 3/5 pull code"
git pull --ff-only

echo "== 4/5 rebuild + restart"
docker compose build
docker compose up -d

echo "== 5/5 health check ($HEALTH)"
if healthy; then
  echo "updated, healthy ($(git log -1 --format='%h %s'))"
  echo "== cleanup: old images and build cache (keeps forest:latest and forest:previous)"
  docker compose up -d --remove-orphans >/dev/null
  docker image prune -f >/dev/null                          # dangling layers from rebuilds
  docker builder prune -f --filter until=168h >/dev/null    # build cache older than a week
  docker system df | sed -n '1,4p'
  ls -1t "$BACKUP_DIR"/forest-*.tar.gz | head -3
else
  echo "NOT healthy - rolling back to previous image"
  docker compose logs --tail 50 forest || true
  docker image inspect forest:previous >/dev/null 2>&1 && { docker tag forest:previous forest:latest; docker compose up -d --no-build; }
  exit 1
fi
