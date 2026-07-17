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
#
# SAFETY (Phase 5): the rollback above restores CODE, not DATA — and /data/accounts.db is the real
# thing at risk. RM_ACCOUNTS_DB makes it BOTH the accounts store AND the platform DB
# (stores/db.py platform_db_path falls back to it), so every numbered migration runs against live
# users/tokens/saved projects the moment the app serves its first request — which is AFTER
# wait_healthy() returns and this script's rollback window has closed. A code rollback cannot undo a
# migration. So: snapshot the DB before every swap, and refuse the deploy if the snapshot fails.
#
# RESTORE (manual — a bad migration is not auto-detectable, so this is a human decision):
#   ls -t /var/backups/rmdemo/                       # newest first; names carry the target commit
#   docker compose -f deploy/cohost/docker-compose.cohost.yml -p rmdemo stop app
#   docker run --rm -v rmdemo_rmdemo_accounts:/data -v /var/backups/rmdemo:/backup \
#       rmdemo-app:latest sh -c 'rm -f /data/accounts.db-wal /data/accounts.db-shm && \
#                                cp /backup/<chosen>.db /data/accounts.db'
#   git reset --hard <last-good-commit> && <re-run this script's deploy()>
# Dropping the stale -wal/-shm matters: they belong to the REPLACED file and would corrupt the restore.
set -euo pipefail

REPO_DIR="${RMDEMO_REPO_DIR:-/root/resume-matcher}"
COMPOSE_FILE="deploy/cohost/docker-compose.cohost.yml"
ENV_FILE="deploy/cohost/.env"
PROJECT="rmdemo"
APP_CONTAINER="rmdemo-app"
APP_IMAGE="rmdemo-app:latest"
BRANCH="main"
LOG="${RMDEMO_LOG:-/var/log/rmdemo-autodeploy.log}"
HEALTH_TRIES="${RMDEMO_HEALTH_TRIES:-36}"   # x5s sleeps => up to 3 min for build + start-period
# Compose prefixes named volumes with the project: `rmdemo_accounts` -> `rmdemo_rmdemo_accounts`.
DB_VOLUME="${RMDEMO_DB_VOLUME:-${PROJECT}_rmdemo_accounts}"
BACKUP_DIR="${RMDEMO_BACKUP_DIR:-/var/backups/rmdemo}"
BACKUP_KEEP="${RMDEMO_BACKUP_KEEP:-10}"

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

# Snapshot /data/accounts.db before a swap. Uses SQLite's ONLINE BACKUP API (not cp): the app
# container is still serving and the DB is in WAL mode, so a file copy could capture a torn page or
# miss the WAL. .backup() is safe against concurrent writers and checkpoints the WAL into the copy.
# Returns non-zero on any failure — the caller REFUSES to deploy, which is the right failure mode:
# a demo left on last-good code loses nothing, an unbacked bad migration loses user data.
# Escape hatch: RMDEMO_SKIP_DB_BACKUP=1 (say, disk full at 3am and you accept the risk knowingly).
backup_db() {
    if [ "${RMDEMO_SKIP_DB_BACKUP:-0}" = "1" ]; then
        echo "  WARN: RMDEMO_SKIP_DB_BACKUP=1 — deploying with NO database snapshot"
        return 0
    fi
    if ! docker volume inspect "$DB_VOLUME" >/dev/null 2>&1; then
        echo "  no volume $DB_VOLUME yet (first deploy) — nothing to back up"
        return 0
    fi
    if ! docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then
        echo "  no $APP_IMAGE yet (first deploy) — nothing to back up"
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    local dest
    dest="accounts-$(date -u +%Y%m%dT%H%M%SZ)-${REMOTE:0:8}.db"
    # Volume mounted rw: read-only WAL access needs to create a -shm, which would fail.
    if ! docker run --rm \
            -v "${DB_VOLUME}:/data" \
            -v "${BACKUP_DIR}:/backup" \
            "$APP_IMAGE" \
            python -c '
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
if not os.path.exists(src):
    print("  no DB file yet — nothing to back up"); raise SystemExit(0)
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)          # online backup API — consistent under concurrent writes
d.close(); s.close()
n = os.path.getsize(dst)
if n == 0:
    print("  backup is 0 bytes — refusing"); raise SystemExit(1)
print(f"  snapshot ok: {n} bytes")
' /data/accounts.db "/backup/${dest}"; then
        echo "  DB BACKUP FAILED — refusing to deploy (set RMDEMO_SKIP_DB_BACKUP=1 to override)"
        rm -f "${BACKUP_DIR}/${dest}"
        return 1
    fi
    # Verify the snapshot is a readable SQLite file, not just non-empty.
    if ! docker run --rm -v "${BACKUP_DIR}:/backup" "$APP_IMAGE" \
            python -c '
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    print("  snapshot failed integrity_check"); raise SystemExit(1)
n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type=\"table\"").fetchone()[0]
print(f"  snapshot verified: {n} tables")
' "/backup/${dest}"; then
        echo "  DB BACKUP UNREADABLE — refusing to deploy"
        return 1
    fi
    echo "  backup: ${BACKUP_DIR}/${dest}"
    # Prune oldest, keep the newest $BACKUP_KEEP. `ls -t` is safe: our names have no spaces.
    # shellcheck disable=SC2012
    ls -t "${BACKUP_DIR}"/accounts-*.db 2>/dev/null | tail -n "+$((BACKUP_KEEP + 1))" | \
        while read -r old; do echo "  pruning $(basename "$old")"; rm -f "$old"; done
    return 0
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

# Snapshot BEFORE the swap: the new code's migrations run against this DB and no code rollback can
# undo them. A failed snapshot stops the deploy here, leaving the demo on last-good code.
if ! backup_db; then
    echo "staying on ${LOCAL:0:8} — no usable DB snapshot"
    exit 1
fi

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
