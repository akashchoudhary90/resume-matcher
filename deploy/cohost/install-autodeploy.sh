#!/usr/bin/env bash
# One-time installer for the rmdemo auto-deploy timer. Run as root on the VPS, from the repo:
#   sudo bash deploy/cohost/install-autodeploy.sh
#
# Installs the auto-deploy script + a systemd timer that pulls & redeploys the rmdemo stack whenever
# origin/main changes. Idempotent — rerun any time you change these files.
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "ERROR: run as root (sudo bash deploy/cohost/install-autodeploy.sh)"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"

# Sanity: the env file the auto-deploy uses must exist (RM_ADMIN_PASSWORD + HOSTINGER_API_TOKEN).
if [ ! -f "$HERE/.env" ]; then
    echo "ERROR: $HERE/.env not found."
    echo "Create it first:  cp '$HERE/.env.example' '$HERE/.env' 2>/dev/null; "
    echo "then put RM_ADMIN_PASSWORD and HOSTINGER_API_TOKEN in it (chmod 600)."
    exit 1
fi

install -m 0755 "$HERE/auto-deploy.sh"                    /usr/local/bin/rmdemo-autodeploy.sh
install -m 0644 "$HERE/systemd/rmdemo-autodeploy.service" /etc/systemd/system/
install -m 0644 "$HERE/systemd/rmdemo-autodeploy.timer"   /etc/systemd/system/
touch /var/log/rmdemo-autodeploy.log

systemctl daemon-reload
systemctl enable --now rmdemo-autodeploy.timer

echo "===== installed ====="
systemctl list-timers rmdemo-autodeploy.timer --no-pager || true
echo
echo "Run once now to confirm:  systemctl start rmdemo-autodeploy.service && tail -n 20 /var/log/rmdemo-autodeploy.log"
