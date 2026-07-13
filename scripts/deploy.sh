#!/usr/bin/env bash
#
# deploy.sh — one-command deploy driver for the Resume Matcher demo (rmdemo) stack.
#
# The live stack auto-deploys from GitHub: a systemd timer on the VPS runs
# deploy/cohost/auto-deploy.sh every ~3 min, which pulls origin/main, gates on CI, rebuilds ONLY the
# `-p rmdemo` compose project, and rolls back automatically if the container's HEALTHCHECK fails.
# So the canonical "deploy" is simply: land green code on origin/main. This script does that safely
# and then watches the deploy land.
#
# TWO MODES
#   (default)     From your dev machine: lint + tests -> push origin/main -> [optional SSH trigger]
#                 -> poll the public /api/health until the new build is healthy.
#   --remote      Full SSH-orchestrated deploy TO the VPS, live, without waiting for the timer:
#                 pull origin/main, rebuild the rmdemo stack, wait for HEALTHCHECK, and auto-roll-back
#                 to the previous commit if it fails. Needs RMDEMO_SSH. This is the "deploy now" path.
#   --stack-up    On the VPS (or any Docker host): build + `up -d` the rmdemo compose directly and
#                 wait for the container to report healthy. Use this for a hands-on deploy.
#
# SAFETY: every action is scoped to origin/main and the `-p rmdemo` project. It never touches the
# co-hosted trading stack, force-pushes, or rewrites history.
#
# USAGE
#   bash scripts/deploy.sh [options]
#   Options:
#     --skip-tests     skip the ruff + pytest quality gate (NOT recommended)
#     --allow-dirty    deploy the committed state even if the working tree has uncommitted changes
#     --trigger        after pushing, SSH to the VPS and run auto-deploy.sh now (needs RMDEMO_SSH)
#     --remote         full SSH deploy to the VPS now (pull+rebuild+health+rollback; needs RMDEMO_SSH)
#     --no-health      don't poll the health endpoint after deploying
#     --stack-up       build + up the rmdemo compose on THIS host instead of pushing (host deploy)
#     --yes, -y        don't prompt for confirmation
#     --help, -h       show this help
#
# ENVIRONMENT (defaults match deploy/cohost/*)
#   RMDEMO_HEALTH_URL   health probe URL       (default https://schulich.edufund.ca:8443/api/health)
#   RMDEMO_BRANCH       branch to deploy       (default main)
#   RMDEMO_SSH          user@host for --trigger (e.g. root@203.0.113.10)
#   RMDEMO_REPO_DIR     repo path on the VPS   (default /root/resume-matcher)
#   RMDEMO_LOG          autodeploy log on VPS  (default /var/log/rmdemo-autodeploy.log)
#   RMDEMO_HEALTH_INSECURE=1   pass curl -k (only if the cert isn't trusted locally)
#
set -euo pipefail

# ---- config -------------------------------------------------------------------------------------
BRANCH="${RMDEMO_BRANCH:-main}"
HEALTH_URL="${RMDEMO_HEALTH_URL:-https://schulich.edufund.ca:8443/api/health}"
REMOTE_REPO_DIR="${RMDEMO_REPO_DIR:-/root/resume-matcher}"
REMOTE_LOG="${RMDEMO_LOG:-/var/log/rmdemo-autodeploy.log}"
COMPOSE_FILE="deploy/cohost/docker-compose.cohost.yml"
ENV_FILE="deploy/cohost/.env"
PROJECT="rmdemo"
APP_CONTAINER="rmdemo-app"

SKIP_TESTS=0 ALLOW_DIRTY=0 DO_TRIGGER=0 CHECK_HEALTH=1 STACK_UP=0 ASSUME_YES=0 REMOTE_DEPLOY=0

# Windows git-bash curl (schannel) does an online cert-revocation check that often fails behind
# corporate/VPN networks even for a valid cert; disable it there only. Linux/mac curl is untouched.
CURL_EXTRA=""
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
        CURL_EXTRA="--ssl-no-revoke"
        # git-bash rewrites POSIX-looking args (e.g. the remote /root/... and /var/log/... paths we
        # pass through ssh) into Windows paths. Disable that mangling so remote paths survive intact.
        export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
        ;;
esac

# ---- pretty output ------------------------------------------------------------------------------
if [ -t 1 ]; then C_B='\033[1m'; C_G='\033[32m'; C_Y='\033[33m'; C_R='\033[31m'; C_D='\033[2m'; C_0='\033[0m'
else C_B=''; C_G=''; C_Y=''; C_R=''; C_D=''; C_0=''; fi
step() { printf "${C_B}==>${C_0} %s\n" "$*"; }
ok()   { printf "${C_G} ok${C_0} %s\n" "$*"; }
warn() { printf "${C_Y}  ! %s${C_0}\n" "$*"; }
die()  { printf "${C_R}error:${C_0} %s\n" "$*" >&2; exit 1; }

usage() { sed -n '/^# deploy\.sh/,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0; }

# ---- args ---------------------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-tests)  SKIP_TESTS=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --trigger)     DO_TRIGGER=1 ;;
        --remote)      REMOTE_DEPLOY=1 ;;
        --no-health)   CHECK_HEALTH=0 ;;
        --stack-up)    STACK_UP=1 ;;
        -y|--yes)      ASSUME_YES=1 ;;
        -h|--help)     usage ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

# Note: `cd "$(... )" || die` would NOT fire outside a repo — `cd ""` succeeds. Split the capture.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
[ -n "$REPO_ROOT" ] || die "not inside a git repository"
cd "$REPO_ROOT" || die "cannot cd to repo root: $REPO_ROOT"

# Fail fast on missing SSH target before doing any work (push/tests).
if [ "$REMOTE_DEPLOY" = 1 ] && [ -z "${RMDEMO_SSH:-}" ]; then
    die "--remote needs RMDEMO_SSH=user@host (e.g. RMDEMO_SSH=root@1.2.3.4)"
fi

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    printf "${C_B}%s${C_0} [y/N] " "$1"; read -r reply </dev/tty || reply=""
    case "$reply" in y|Y|yes|YES) return 0 ;; *) die "aborted" ;; esac
}

# ---- quality gate (shared) ----------------------------------------------------------------------
quality_gate() {
    if [ "$SKIP_TESTS" = 1 ]; then warn "skipping lint + tests (--skip-tests)"; return; fi
    step "Lint (ruff)"
    python -m ruff check . --output-format=concise || die "ruff found issues — fix them or --skip-tests"
    ok "ruff clean"
    step "Tests (pytest)"
    python -m pytest -q -p no:cacheprovider || die "tests failed — not deploying a red build"
    ok "tests passed"
}

# ---- health poll --------------------------------------------------------------------------------
poll_health() {
    local url="$1" tries="${2:-60}" delay="${3:-5}" insecure=""
    [ "${RMDEMO_HEALTH_INSECURE:-0}" = 1 ] && insecure="-k"
    step "Waiting for $url to report healthy (up to $((tries*delay))s)"
    local i body
    for i in $(seq 1 "$tries"); do
        if body=$(curl -fsS $CURL_EXTRA $insecure --max-time 10 "$url" 2>/dev/null) \
           && printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
            ok "healthy — $body"
            return 0
        fi
        printf "${C_D}  .. attempt %s/%s${C_0}\r" "$i" "$tries"
        sleep "$delay"
    done
    printf "\n"
    warn "health not confirmed within the window. Check the VPS: journalctl -u rmdemo-autodeploy or"
    warn "  docker logs $APP_CONTAINER   (the auto-deploy rolls back a bad build automatically)."
    return 1
}

# ---- mode: full SSH-orchestrated deploy to the VPS ----------------------------------------------
# Runs the whole deploy ON the VPS with LIVE output and rollback, without waiting for the timer.
# Everything is scoped to `-p rmdemo` / the rmdemo repo dir — the co-hosted trading stack is never
# touched. The remote script is fed over stdin; config is passed as leading env vars (paths only,
# no secrets) so there is no fragile interpolation inside the remote body.
remote_deploy() {
    [ -n "${RMDEMO_SSH:-}" ] || die "--remote needs RMDEMO_SSH=user@host (e.g. RMDEMO_SSH=root@1.2.3.4)"
    command -v ssh >/dev/null 2>&1 || die "ssh not found on this machine"
    step "Deploying to $RMDEMO_SSH  (repo=$REMOTE_REPO_DIR, project=$PROJECT, branch=$BRANCH)"
    confirm "Run a live pull+rebuild of the '$PROJECT' stack on $RMDEMO_SSH (auto-rollback on failure)?"
    ssh -o BatchMode=yes "$RMDEMO_SSH" \
        "REPO='$REMOTE_REPO_DIR' COMPOSE='$COMPOSE_FILE' ENVF='$ENV_FILE' PROJ='$PROJECT' \
         APP='$APP_CONTAINER' BRANCH='$BRANCH' TRIES='${RMDEMO_HEALTH_TRIES:-48}' bash -s" <<'REMOTE'
set -euo pipefail
cd "$REPO" || { echo "[vps] repo dir $REPO not found"; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "[vps] docker not found"; exit 2; }
# Serialize against the systemd auto-deploy timer (which flock -n's the SAME lock): a manual deploy
# and the timer must never git-pull/reset or run `up -d --build` concurrently in this checkout — two
# unbounded builds could spike CPU/RAM next to the co-hosted trading stack.
if command -v flock >/dev/null 2>&1 && exec 9>/var/lock/rmdemo-autodeploy.lock 2>/dev/null; then
    flock -w 300 9 || { echo "[vps] another rmdemo deploy is in progress — aborting"; exit 3; }
fi
if [ -n "$(git status --porcelain)" ]; then echo "[vps] working tree dirty — refusing (fix on the VPS first)"; exit 2; fi
prev=$(git rev-parse HEAD)
echo "[vps] current commit: ${prev:0:8}"
git fetch --quiet origin "$BRANCH"
git pull --ff-only origin "$BRANCH"
target=$(git rev-parse HEAD)
if [ "$prev" = "$target" ]; then echo "[vps] already at ${target:0:8} — redeploying anyway"; else echo "[vps] updated ${prev:0:8} -> ${target:0:8}"; fi
envargs=""; [ -f "$ENVF" ] && envargs="--env-file $ENVF"
deploy() { docker compose $envargs -f "$COMPOSE" -p "$PROJ" up -d --build; }
wait_healthy() {
    local s
    for _ in $(seq 1 "$TRIES"); do
        s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$APP" 2>/dev/null || echo missing)
        case "$s" in
            healthy)   echo "[vps] $APP healthy"; return 0 ;;
            unhealthy) echo "[vps] $APP UNHEALTHY"; return 1 ;;
            none)      echo "[vps] $APP has no HEALTHCHECK — assuming up"; return 0 ;;
        esac
        sleep 5
    done
    echo "[vps] timed out waiting for healthy"; return 1
}
echo "[vps] building + starting -p $PROJ ..."
# `deploy` inside the `if` condition so a FAILED BUILD (not just an unhealthy container) also falls
# through to rollback — a bare `deploy` under set -e would exit before the rollback path.
if deploy && wait_healthy; then
    echo "[vps] DEPLOY OK at ${target:0:8}"
    exit 0
fi
echo "[vps] deploy failed or unhealthy — rolling back to ${prev:0:8}"
git reset --hard "$prev"
if deploy && wait_healthy; then
    echo "[vps] rolled back to ${prev:0:8} (healthy) — investigate ${target:0:8}"
else
    echo "[vps] ROLLBACK ALSO FAILED — MANUAL INTERVENTION NEEDED on the VPS"
fi
exit 1
REMOTE
}

# ---- mode: direct host stack up -----------------------------------------------------------------
if [ "$STACK_UP" = 1 ]; then
    command -v docker >/dev/null 2>&1 || die "docker not found — --stack-up must run on a Docker host"
    # Only pass --env-file if it EXISTS: compose hard-fails on a missing --env-file, which would
    # defeat the documented fallback of exporting RM_ADMIN_PASSWORD / HOSTINGER_API_TOKEN in the shell.
    envf_arg=""
    if [ -f "$ENV_FILE" ]; then envf_arg="--env-file $ENV_FILE"
    else warn "$ENV_FILE not found — relying on exported RM_ADMIN_PASSWORD + HOSTINGER_API_TOKEN"; fi
    # Serialize with the auto-deploy timer (same lock) so a manual + timer build can't overlap.
    if command -v flock >/dev/null 2>&1 && exec 9>/var/lock/rmdemo-autodeploy.lock 2>/dev/null; then
        flock -w 300 9 || die "another rmdemo deploy is in progress — try again shortly"
    fi
    quality_gate
    step "Building + starting the '$PROJECT' stack on this host"
    confirm "Run: docker compose -p $PROJECT up -d --build  (rmdemo only; trading stack untouched)?"
    docker compose $envf_arg -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build
    step "Waiting for $APP_CONTAINER HEALTHCHECK"
    for _ in $(seq 1 48); do
        s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
              "$APP_CONTAINER" 2>/dev/null || echo missing)
        case "$s" in
            healthy)   ok "$APP_CONTAINER healthy"; exit 0 ;;
            unhealthy) die "$APP_CONTAINER UNHEALTHY — check: docker logs $APP_CONTAINER" ;;
            none)      warn "no HEALTHCHECK on $APP_CONTAINER — assuming up"; exit 0 ;;
        esac
        sleep 5
    done
    die "timed out waiting for $APP_CONTAINER to become healthy"
fi

# ---- mode: push deploy (default) ----------------------------------------------------------------
current_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$current_branch" = "$BRANCH" ] || die "on '$current_branch', not '$BRANCH'. Checkout $BRANCH first."

if [ -n "$(git status --porcelain)" ]; then
    if [ "$ALLOW_DIRTY" = 1 ]; then
        warn "working tree has uncommitted changes — deploying the COMMITTED state only"
    else
        git status --short
        die "uncommitted changes present. Commit them, or re-run with --allow-dirty to deploy HEAD as-is."
    fi
fi

git fetch --quiet origin "$BRANCH" || warn "could not fetch origin/$BRANCH"
ahead=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo "?")
behind=$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo "0")
[ "$behind" != 0 ] && die "local $BRANCH is behind origin by $behind commit(s) — pull/rebase first (no force-push here)."

if [ "$ahead" = 0 ]; then
    warn "origin/$BRANCH already matches HEAD — nothing new to push."
else
    step "About to deploy $ahead commit(s) to origin/$BRANCH:"
    git --no-pager log --oneline "origin/$BRANCH..HEAD" | sed 's/^/    /'
fi

quality_gate

if [ "$ahead" != 0 ]; then
    confirm "Push $BRANCH to origin? This triggers the VPS auto-deploy (CI-gated, auto-rollback)."
    step "Pushing origin/$BRANCH"
    git push origin "$BRANCH"
    ok "pushed $(git rev-parse --short HEAD)"
fi

deploy_rc=0
if [ "$REMOTE_DEPLOY" = 1 ]; then
    # Full deploy now: SSH pull+rebuild+health+rollback on the VPS (does not wait for the timer).
    remote_deploy || deploy_rc=$?
elif [ "$DO_TRIGGER" = 1 ]; then
    [ -n "${RMDEMO_SSH:-}" ] || die "--trigger needs RMDEMO_SSH=user@host"
    step "Triggering auto-deploy on $RMDEMO_SSH now (skipping the ~3 min timer)"
    # auto-deploy.sh self-redirects to its log; run it, then show the tail so you see the result.
    # `|| warn` so an SSH transport failure (255) can't abort the script under set -e before health.
    ssh "$RMDEMO_SSH" \
        "bash '$REMOTE_REPO_DIR/deploy/cohost/auto-deploy.sh' >/dev/null 2>&1 || true; \
         echo '--- tail $REMOTE_LOG ---'; tail -n 40 '$REMOTE_LOG' 2>/dev/null || echo '(no log yet)'" \
        || warn "could not reach $RMDEMO_SSH to trigger auto-deploy (the timer will still pick it up)"
else
    warn "not SSH-deploying; the VPS systemd timer will pick up the push within ~3 min."
    warn "  (pass --remote with RMDEMO_SSH=user@host to deploy immediately.)"
fi

# A failed/timed-out health probe should surface in the exit code, not be silently swallowed.
if [ "$CHECK_HEALTH" = 1 ] && ! poll_health "$HEALTH_URL"; then
    [ "$deploy_rc" = 0 ] && deploy_rc=1
fi

[ "$deploy_rc" = 0 ] || warn "deploy not confirmed healthy (see [vps]/health lines above)"
step "Done. Live at ${HEALTH_URL%/api/health}/"
exit "$deploy_rc"
