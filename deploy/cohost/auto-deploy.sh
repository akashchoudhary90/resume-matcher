#!/usr/bin/env bash
# Auto-deploy the rmdemo (Resume Matcher demo) stack from GitHub.
#
# Run on a systemd timer (every ~3 min). It is a NO-OP unless origin/main moved, so it's cheap to run
# often. It pulls with the repo's read-only deploy key and rebuilds ONLY the `rmdemo` compose project
# — it never touches the trading stack. A flock guards against overlapping runs if a build runs long.
#
# SAFETY (audit #12): captures the pre-pull commit, gates on the new commit's CI (best-effort), then
# after `up -d` waits for the app container's Docker HEALTHCHECK to report `healthy`. If it does not,
# it ROLLS BACK to the last-good commit and redeploys, so a broken push never leaves a dead demo. All
# operations are scoped to `-p rmdemo` / the `rmdemo-app` container — the trading stack is untouched.
set -euo pipefail

REPO_DIR="${RMDEMO_REPO_DIR:-/root/resume-matcher}"
COMPOSE_FILE="deploy/cohost/docker-compose.cohost.yml"
ENV_FILE="deploy/cohost/.env"
PROJECT="rmdemo"
APP_CONTAINER="rmdemo-app"
BRANCH="main"
LOG="${RMDEMO_LOG:-/var/log/rmdemo-autodeploy.log}"
HEALTH_TRIES="${RMDEMO_HEALTH_TRIES:-36}"   # x5s sleeps => up to 3 min for build + start-period

exec >>"$LOG" 2>&1

# Single-flight: skip if a previous run is still building.
exec 9>/var/lock/rmdemo-autodeploy.lock
flock -n 9 || { echo "$(date -Is) another run in progress — skipping"; exit 0; }

echo "===== $(date -Is) autodeploy check ====="
cd "$REPO_DIR"

# Never pull/reset over an uncommitted hotfix: `git reset --hard` on the rollback path would destroy
# it. Check TRACKED changes only (git diff --quiet HEAD) so a stray ignored/untracked file — e.g.
# deploy/cohost/.env — can't false-refuse and silently freeze the unattended timer.
if ! git diff --quiet HEAD 2>/dev/null; then
    echo "working tree has uncommitted tracked changes — refusing autodeploy (manual fix needed)"
    exit 1
fi

git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "up to date (${LOCAL:0:8}) — nothing to do"
    exit 0
fi

deploy() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build
}

# Wait until the app container's Docker HEALTHCHECK reports healthy (or fail fast on unhealthy/timeout).
wait_healthy() {
    local status
    for _ in $(seq 1 "$HEALTH_TRIES"); do
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$APP_CONTAINER" 2>/dev/null || echo missing)
        case "$status" in
            healthy)   return 0 ;;
            unhealthy) echo "  $APP_CONTAINER reported UNHEALTHY"; return 1 ;;
            none)      echo "  WARN: $APP_CONTAINER has no HEALTHCHECK — assuming up"; return 0 ;;
        esac
        sleep 5
    done
    echo "  timed out waiting for healthy (last status: ${status:-unknown})"
    return 1
}

# Best-effort CI gate: refuse to deploy ONLY if GitHub definitively reports the target commit's checks
# failed. Any uncertainty (no gh, no auth, pending) proceeds — the healthcheck+rollback is the real net.
ci_blocks_deploy() {
    command -v gh >/dev/null 2>&1 || { echo "  gh not available — skipping CI gate"; return 1; }
    local conclusions
    conclusions=$(gh api "repos/{owner}/{repo}/commits/$REMOTE/check-runs" \
        --jq '[.check_runs[].conclusion] | join(",")' 2>/dev/null || echo "")
    case "$conclusions" in
        *failure*|*cancelled*|*timed_out*)
            echo "  CI for ${REMOTE:0:8} is NOT green ($conclusions) — refusing to deploy"; return 0 ;;
        *) return 1 ;;
    esac
}

echo "change detected: ${LOCAL:0:8} -> ${REMOTE:0:8}"
if ci_blocks_deploy; then
    echo "staying on ${LOCAL:0:8} until CI is green"
    exit 0
fi

PREV="$LOCAL"   # last-good commit to restore if the new one fails its healthcheck
echo "deploying ${REMOTE:0:8} (rollback target: ${PREV:0:8})"
git pull --ff-only origin "$BRANCH"
deploy

if wait_healthy; then
    echo "deploy healthy at $(date -Is) (now at ${REMOTE:0:8})"
    exit 0
fi

echo "HEALTHCHECK FAILED for ${REMOTE:0:8} — rolling back to ${PREV:0:8}"
git reset --hard "$PREV"
deploy
if wait_healthy; then
    echo "rolled back to last-good ${PREV:0:8} — healthy at $(date -Is)"
else
    echo "ROLLBACK ALSO UNHEALTHY — manual intervention needed (repo at ${PREV:0:8})"
fi
exit 1
