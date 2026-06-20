#!/usr/bin/env bash
# Auto-deploy the rmdemo (Resume Matcher demo) stack from GitHub.
#
# Run on a systemd timer (every ~3 min). It is a NO-OP unless origin/main moved, so it's cheap to run
# often. It pulls with the repo's read-only deploy key and rebuilds ONLY the `rmdemo` compose project
# — it never touches the trading stack. A flock guards against overlapping runs if a build runs long.
set -euo pipefail

REPO_DIR="${RMDEMO_REPO_DIR:-/root/resume-matcher}"
COMPOSE_FILE="deploy/cohost/docker-compose.cohost.yml"
ENV_FILE="deploy/cohost/.env"
PROJECT="rmdemo"
BRANCH="main"
LOG="${RMDEMO_LOG:-/var/log/rmdemo-autodeploy.log}"

exec >>"$LOG" 2>&1

# Single-flight: skip if a previous run is still building.
exec 9>/var/lock/rmdemo-autodeploy.lock
flock -n 9 || { echo "$(date -Is) another run in progress — skipping"; exit 0; }

echo "===== $(date -Is) autodeploy check ====="
cd "$REPO_DIR"

git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "up to date (${LOCAL:0:8}) — nothing to do"
    exit 0
fi

echo "change detected: ${LOCAL:0:8} -> ${REMOTE:0:8} — deploying"
git pull --ff-only origin "$BRANCH"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build
echo "deploy complete at $(date -Is) (now at ${REMOTE:0:8})"
