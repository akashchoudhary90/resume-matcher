<#
.SYNOPSIS
  One-command deploy driver for the Resume Matcher demo (rmdemo) stack - PowerShell twin of
  scripts/deploy.sh, for driving deploys natively from Windows PowerShell.

.DESCRIPTION
  The live stack auto-deploys from GitHub: a systemd timer on the VPS runs deploy/cohost/auto-deploy.sh
  every ~3 min, which pulls origin/main, gates on CI, rebuilds ONLY the `-p rmdemo` compose project, and
  rolls back if the container HEALTHCHECK fails. So "deploy" = land green code on origin/main.

  MODES
    (default)   Lint + tests -> push origin/main -> poll /api/health until the new build is healthy.
    -Remote     Full SSH-orchestrated deploy TO the VPS now (no waiting for the timer): pull origin/main,
                rebuild the rmdemo stack, wait for HEALTHCHECK, auto-roll-back on failure. Needs -Ssh.
    -Trigger    SSH to the VPS and run auto-deploy.sh now (skips the timer). Needs -Ssh.
    -StackUp    Run `docker compose -p rmdemo up -d --build` against THIS machine's Docker host.

  Every action is scoped to origin/main and the -p rmdemo project. It never touches the co-hosted
  trading stack, force-pushes, or rewrites history.

.EXAMPLE
  .\scripts\deploy.ps1
  Lint + test, push main, wait until the public health endpoint is healthy.

.EXAMPLE
  $env:RMDEMO_SSH = 'root@1.2.3.4'; .\scripts\deploy.ps1 -Remote
  Push, then SSH-deploy to the VPS immediately with rollback.

.NOTES
  Config via params or env: RMDEMO_SSH, RMDEMO_HEALTH_URL, RMDEMO_BRANCH, RMDEMO_REPO_DIR.
#>
[CmdletBinding()]
param(
    [switch]$Remote,
    [switch]$Trigger,
    [switch]$StackUp,
    [switch]$SkipTests,
    [switch]$AllowDirty,
    [switch]$NoHealth,
    [switch]$Yes,
    [string]$Ssh,
    [string]$HealthUrl,
    [string]$Branch,
    [string]$RepoDir
)

$ErrorActionPreference = 'Stop'

# ---- config (param > env > default) -------------------------------------------------------------
if (-not $Ssh)       { $Ssh       = $env:RMDEMO_SSH }
if (-not $HealthUrl) { $HealthUrl = if ($env:RMDEMO_HEALTH_URL) { $env:RMDEMO_HEALTH_URL } else { 'https://schulich.edufund.ca:8443/api/health' } }
if (-not $Branch)    { $Branch    = if ($env:RMDEMO_BRANCH)     { $env:RMDEMO_BRANCH }     else { 'main' } }
if (-not $RepoDir)   { $RepoDir   = if ($env:RMDEMO_REPO_DIR)   { $env:RMDEMO_REPO_DIR }   else { '/root/resume-matcher' } }

$Project     = 'rmdemo'
$AppName     = 'rmdemo-app'
$ComposeFile = 'deploy/cohost/docker-compose.cohost.yml'
$EnvFile     = 'deploy/cohost/.env'
$RemoteLog   = if ($env:RMDEMO_LOG) { $env:RMDEMO_LOG } else { '/var/log/rmdemo-autodeploy.log' }
$Tries       = if ($env:RMDEMO_HEALTH_TRIES) { [int]$env:RMDEMO_HEALTH_TRIES } else { 48 }

# ---- pretty output ------------------------------------------------------------------------------
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host " ok $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

function Confirm-Action($msg) {
    if ($Yes) { return }
    $ans = Read-Host "$msg [y/N]"
    if ($ans -notmatch '^(y|Y|yes|YES)$') { Die 'aborted' }
}

# ---- quality gate -------------------------------------------------------------------------------
function Invoke-QualityGate {
    if ($SkipTests) { Warn 'skipping lint + tests (-SkipTests)'; return }
    Step 'Lint (ruff)'
    python -m ruff check . --output-format=concise
    if ($LASTEXITCODE -ne 0) { Die 'ruff found issues - fix them or pass -SkipTests' }
    Ok 'ruff clean'
    Step 'Tests (pytest)'
    python -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { Die 'tests failed - not deploying a red build' }
    Ok 'tests passed'
}

# ---- health poll --------------------------------------------------------------------------------
function Wait-Health($url, $tries, $delay = 5) {
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
    Step "Waiting for $url to report healthy (up to $($tries * $delay)s)"
    for ($i = 1; $i -le $tries; $i++) {
        try {
            $r = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
            if ($r.status -eq 'ok') {
                Ok ("healthy - " + ($r | ConvertTo-Json -Compress))
                return $true
            }
        } catch { }
        Write-Host ("  .. attempt {0}/{1}`r" -f $i, $tries) -NoNewline
        Start-Sleep -Seconds $delay
    }
    Write-Host ''
    Warn 'health not confirmed within the window - check the VPS auto-deploy log / docker logs.'
    return $false
}

# ---- the bash body that runs ON the VPS (identical to deploy.sh's reviewed remote body) ----------
# Single-quoted here-string: PowerShell does NOT expand it; $REPO/$COMPOSE/... are remote bash vars,
# supplied via the `VAR=... bash -s` prefix below. Normalized to LF before piping (CRLF breaks bash).
$RemoteBody = @'
set -euo pipefail
cd "$REPO" || { echo "[vps] repo dir $REPO not found"; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "[vps] docker not found"; exit 2; }
if command -v flock >/dev/null 2>&1 && exec 9>/var/lock/rmdemo-autodeploy.lock 2>/dev/null; then
    flock -w 300 9 || { echo "[vps] another rmdemo deploy is in progress - aborting"; exit 3; }
fi
if [ -n "$(git status --porcelain)" ]; then echo "[vps] working tree dirty - refusing (fix on the VPS first)"; exit 2; fi
prev=$(git rev-parse HEAD)
echo "[vps] current commit: ${prev:0:8}"
git fetch --quiet origin "$BRANCH"
git pull --ff-only origin "$BRANCH"
target=$(git rev-parse HEAD)
if [ "$prev" = "$target" ]; then echo "[vps] already at ${target:0:8} - redeploying anyway"; else echo "[vps] updated ${prev:0:8} -> ${target:0:8}"; fi
envargs=""; [ -f "$ENVF" ] && envargs="--env-file $ENVF"
deploy() { docker compose $envargs -f "$COMPOSE" -p "$PROJ" up -d --build; }
wait_healthy() {
    local s
    for _ in $(seq 1 "$TRIES"); do
        s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$APP" 2>/dev/null || echo missing)
        case "$s" in
            healthy)   echo "[vps] $APP healthy"; return 0 ;;
            unhealthy) echo "[vps] $APP UNHEALTHY"; return 1 ;;
            none)      echo "[vps] $APP has no HEALTHCHECK - assuming up"; return 0 ;;
        esac
        sleep 5
    done
    echo "[vps] timed out waiting for healthy"; return 1
}
echo "[vps] building + starting -p $PROJ ..."
if deploy && wait_healthy; then
    echo "[vps] DEPLOY OK at ${target:0:8}"
    exit 0
fi
echo "[vps] deploy failed or unhealthy - rolling back to ${prev:0:8}"
git reset --hard "$prev"
if deploy && wait_healthy; then
    echo "[vps] rolled back to ${prev:0:8} (healthy) - investigate ${target:0:8}"
else
    echo "[vps] ROLLBACK ALSO FAILED - MANUAL INTERVENTION NEEDED on the VPS"
fi
exit 1
'@

function Invoke-RemoteDeploy {
    if (-not $Ssh) { Die '-Remote needs -Ssh <user@host> or $env:RMDEMO_SSH' }
    if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { Die 'ssh not found on this machine' }
    Step "Deploying to $Ssh  (repo=$RepoDir, project=$Project, branch=$Branch)"
    Confirm-Action "Run a live pull+rebuild of the '$Project' stack on $Ssh (auto-rollback on failure)?"
    $body = $RemoteBody -replace "`r`n", "`n"      # ensure LF for the remote bash
    $prefix = "REPO='$RepoDir' COMPOSE='$ComposeFile' ENVF='$EnvFile' PROJ='$Project' APP='$AppName' BRANCH='$Branch' TRIES='$Tries' bash -s"
    $body | ssh -o BatchMode=yes $Ssh $prefix
    return $LASTEXITCODE
}

# =================================================================================================
# Locate the repo root and cd into it.
$root = git rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0 -or -not $root) { Die 'not inside a git repository' }
Set-Location $root

if ($Remote -and -not $Ssh) { Die '-Remote needs -Ssh <user@host> or $env:RMDEMO_SSH' }

# ---- mode: direct host stack up -----------------------------------------------------------------
if ($StackUp) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die 'docker not found - -StackUp must run on a Docker host' }
    $envfArgs = @()
    if (Test-Path $EnvFile) { $envfArgs = @('--env-file', $EnvFile) }
    else { Warn "$EnvFile not found - relying on exported RM_ADMIN_PASSWORD + HOSTINGER_API_TOKEN" }
    Invoke-QualityGate
    Step "Building + starting the '$Project' stack on this host"
    Confirm-Action "Run: docker compose -p $Project up -d --build  (rmdemo only; trading stack untouched)?"
    docker compose @envfArgs -f $ComposeFile -p $Project up -d --build
    if ($LASTEXITCODE -ne 0) { Die 'docker compose up failed' }
    Step "Waiting for $AppName HEALTHCHECK"
    for ($i = 1; $i -le $Tries; $i++) {
        $s = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $AppName 2>$null
        if ($s -eq 'healthy') { Ok "$AppName healthy"; exit 0 }
        if ($s -eq 'unhealthy') { Die "$AppName UNHEALTHY - check: docker logs $AppName" }
        if ($s -eq 'none') { Warn "no HEALTHCHECK on $AppName - assuming up"; exit 0 }
        Start-Sleep -Seconds 5
    }
    Die "timed out waiting for $AppName to become healthy"
}

# ---- mode: push deploy (default) ----------------------------------------------------------------
$currentBranch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($currentBranch -ne $Branch) { Die "on '$currentBranch', not '$Branch'. Checkout $Branch first." }

$dirty = git status --porcelain
if ($dirty) {
    if ($AllowDirty) { Warn 'working tree has uncommitted changes - deploying the COMMITTED state only' }
    else { git status --short; Die 'uncommitted changes present. Commit them, or pass -AllowDirty to deploy HEAD as-is.' }
}

git fetch --quiet origin $Branch
if ($LASTEXITCODE -ne 0) { Warn "could not fetch origin/$Branch" }
$behind = [int](git rev-list --count "HEAD..origin/$Branch")
if ($behind -ne 0) { Die "local $Branch is behind origin by $behind commit(s) - pull/rebase first (no force-push here)." }
$ahead = [int](git rev-list --count "origin/$Branch..HEAD")

if ($ahead -eq 0) {
    Warn "origin/$Branch already matches HEAD - nothing new to push."
} else {
    Step "About to deploy $ahead commit(s) to origin/${Branch}:"
    git --no-pager log --oneline "origin/$Branch..HEAD" | ForEach-Object { Write-Host "    $_" }
}

Invoke-QualityGate

if ($ahead -ne 0) {
    Confirm-Action "Push $Branch to origin? This triggers the VPS auto-deploy (CI-gated, auto-rollback)."
    Step "Pushing origin/$Branch"
    git push origin $Branch
    if ($LASTEXITCODE -ne 0) { Die 'git push failed' }
    Ok ("pushed " + (git rev-parse --short HEAD))
}

$deployRc = 0
if ($Remote) {
    $deployRc = Invoke-RemoteDeploy
}
elseif ($Trigger) {
    if (-not $Ssh) { Die '-Trigger needs -Ssh <user@host> or $env:RMDEMO_SSH' }
    Step "Triggering auto-deploy on $Ssh now (skipping the ~3 min timer)"
    $cmd = "bash '$RepoDir/deploy/cohost/auto-deploy.sh' >/dev/null 2>&1 || true; echo '--- tail $RemoteLog ---'; tail -n 40 '$RemoteLog' 2>/dev/null || echo '(no log yet)'"
    ssh $Ssh $cmd
    if ($LASTEXITCODE -ne 0) { Warn "could not reach $Ssh to trigger auto-deploy (the timer will still pick it up)" }
}
else {
    Warn "not SSH-deploying; the VPS systemd timer will pick up the push within ~3 min."
    Warn "  (pass -Remote with -Ssh user@host to deploy immediately.)"
}

if (-not $NoHealth) {
    if (-not (Wait-Health $HealthUrl $Tries)) {
        if ($deployRc -eq 0) { $deployRc = 1 }
    }
}

if ($deployRc -ne 0) { Warn 'deploy not confirmed healthy (see [vps]/health lines above)' }
$base = $HealthUrl -replace '/api/health$', ''
Step "Done. Live at $base/"
exit $deployRc
