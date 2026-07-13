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

SKIP_TESTS=0 ALLOW_DIRTY=0 DO_TRIGGER=0 CHECK_HEALTH=1 STACK_UP=0 ASSUME_YES=0

# Windows git-bash curl (schannel) does an online cert-revocation check that often fails behind
# corporate/VPN networks even for a valid cert; disable it there only. Linux/mac curl is untouched.
CURL_EXTRA=""
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) CURL_EXTRA="--ssl-no-revoke" ;; esac

# ---- pretty output ------------------------------------------------------------------------------
if [ -t 1 ]; then C_B='\033[1m'; C_G='\033[32m'; C_Y='\033[33m'; C_R='\033[31m'; C_D='\033[2m'; C_0='\033[0m'
else C_B=''; C_G=''; C_Y=''; C_R=''; C_D=''; C_0=''; fi
step() { printf "${C_B}==>${C_0} %s\n" "$*"; }
ok()   { printf "${C_G} ok${C_0} %s\n" "$*"; }
warn() { printf "${C_Y}  ! %s${C_0}\n" "$*"; }
die()  { printf "${C_R}error:${C_0} %s\n" "$*" >&2; exit 1; }

usage() { sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ---- args ---------------------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-tests)  SKIP_TESTS=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --trigger)     DO_TRIGGER=1 ;;
        --no-health)   CHECK_HEALTH=0 ;;
        --stack-up)    STACK_UP=1 ;;
        -y|--yes)      ASSUME_YES=1 ;;
        -h|--help)     usage ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

cd "$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside the git repo"

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

# ---- mode: direct host stack up -----------------------------------------------------------------
if [ "$STACK_UP" = 1 ]; then
    command -v docker >/dev/null 2>&1 || die "docker not found — --stack-up must run on a Docker host"
    [ -f "$ENV_FILE" ] || warn "$ENV_FILE not found — the compose needs RM_ADMIN_PASSWORD + HOSTINGER_API_TOKEN"
    quality_gate
    step "Building + starting the '$PROJECT' stack on this host"
    confirm "Run: docker compose -p $PROJECT up -d --build  (rmdemo only; trading stack untouched)?"
    docker compose ${ENV_FILE:+--env-file "$ENV_FILE"} -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build
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

if [ "$DO_TRIGGER" = 1 ]; then
    [ -n "${RMDEMO_SSH:-}" ] || die "--trigger needs RMDEMO_SSH=user@host"
    step "Triggering auto-deploy on $RMDEMO_SSH now (skipping the ~3 min timer)"
    # auto-deploy.sh self-redirects to its log; run it, then show the tail so you see the result.
    ssh "$RMDEMO_SSH" \
        "bash '$REMOTE_REPO_DIR/deploy/cohost/auto-deploy.sh' >/dev/null 2>&1 || true; \
         echo '--- tail $REMOTE_LOG ---'; tail -n 40 '$REMOTE_LOG' 2>/dev/null || echo '(no log yet)'"
else
    warn "not SSH-triggering; the VPS systemd timer will pick up the push within ~3 min."
    warn "  (pass --trigger with RMDEMO_SSH=user@host to deploy immediately.)"
fi

[ "$CHECK_HEALTH" = 1 ] && poll_health "$HEALTH_URL" || true

step "Done. Live at ${HEALTH_URL%/api/health}/"
