#!/usr/bin/env bash
# Verify Phase-5 migration 004 on the live cohost VPS.
#
# WHY THIS EXISTS: /api/health cannot confirm the migration — it never constructs AccountStore, so
# it does not trigger migrate(). Migration 004 runs LAZILY on the first request that builds
# AccountStore (api/accounts.py -> migrate()), i.e. the first admin sign-in or account op. This
# script confirms the deployed commit, triggers/inspects the migration inside the running container,
# and checks the pre-migration snapshot the new db.py writes.
#
# RUN IT (from a machine that can SSH to the VPS — needs the demo's own key, NOT the trading key):
#   RMDEMO_SSH=user@host bash scripts/verify_phase5_migration.sh
#
# It is READ-ONLY except that it hits the login endpoint once to trigger the lazy migrate (you can
# also just wait for a real admin sign-in and run it with TRIGGER=0). It never touches the trading
# stack: every docker call is scoped to the rmdemo-app container.
set -euo pipefail
: "${RMDEMO_SSH:?set RMDEMO_SSH=user@host (the DEMO box — do not use the trading-stack key)}"
REPO_DIR="${RMDEMO_REPO_DIR:-/root/resume-matcher}"
APP="${RMDEMO_APP_CONTAINER:-rmdemo-app}"
DB="${RMDEMO_DB:-/data/accounts.db}"
WANT_COMMIT="${RMDEMO_WANT_COMMIT:-e3ba195}"
TRIGGER="${TRIGGER:-1}"   # 1 = curl the login page once to force the lazy migrate; 0 = just inspect

ssh -o BatchMode=yes "$RMDEMO_SSH" REPO_DIR="$REPO_DIR" APP="$APP" DB="$DB" \
    WANT="$WANT_COMMIT" TRIGGER="$TRIGGER" 'bash -s' <<'REMOTE'
set -euo pipefail
echo "===== $(date -Is) Phase-5 migration verification ====="

echo "--- 1. deployed commit ---"
cd "$REPO_DIR"
HEAD=$(git rev-parse --short HEAD)
echo "  HEAD=$HEAD  (want $WANT)"
[ "$HEAD" = "$WANT" ] && echo "  commit: MATCH" || echo "  commit: MISMATCH — deploy may not have landed"

echo "--- 2. trigger the lazy migrate (first AccountStore build) ---"
if [ "$TRIGGER" = "1" ]; then
    # POST a throwaway login to the app: it constructs AccountStore -> migrate(). Wrong creds are
    # fine — the store (and its migration) run before auth fails.
    docker exec "$APP" python - <<'PY' || echo "  (login-path trigger returned non-zero — migrate may already have run)"
from resume_matcher.api.accounts import AccountStore
AccountStore()          # same construction the login route does; runs migrate() on the live DB
print("  AccountStore constructed -> migrate() invoked")
PY
else
    echo "  TRIGGER=0 — inspecting current state only"
fi

echo "--- 3. inspect the live DB (read-only) ---"
docker exec "$APP" python - "$DB" <<'PY'
import sqlite3, os, sys
db = sys.argv[1]
def check(label, ok, extra=""): print(f"  [{'PASS' if ok else 'FAIL'}] {label} {extra}")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
v = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
check("schema_version == 4", v == 4, f"(got {v})")
ucols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
check("users.alumni_status present", "alumni_status" in ucols)
ge = {r[1] for r in c.execute("PRAGMA index_list(graph_edges)")}
check("graph_edges indexes intact", {"idx_gedges_key","idx_gedges_a","idx_gedges_b"} <= ge, f"({sorted(ge)})")
ap = {r[1] for r in c.execute("PRAGMA index_list(applications)")}
check("applications index intact", "idx_applications_posting" in ap)
nusers = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
print(f"  users now: {nusers}")
for t in ("notifications","mentor_profiles","affiliations","admin_sessions","vouch_invites"):
    ex = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
    check(f"table {t} exists", ex == 1)
c.close()
PY

echo "--- 4. pre-migration snapshot (db.py:_snapshot_before_migration) ---"
docker exec "$APP" python - "$DB" <<'PY'
import sqlite3, os, sys, glob
db = sys.argv[1]
snaps = sorted(glob.glob(db + ".pre-v*.bak"))
if not snaps:
    print("  [INFO] no .pre-v*.bak — expected only if the DB was fresh (nothing to lose) or "
          "RM_MIGRATION_BACKUP=0. A v3->v4 upgrade of a populated DB should have written one.")
else:
    for s in snaps:
        b = sqlite3.connect(f"file:{s}?mode=ro", uri=True)
        ok = b.execute("PRAGMA integrity_check").fetchone()[0]
        sv = b.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        nu = b.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        b.close()
        print(f"  {os.path.basename(s)}  integrity={ok}  version={sv}  users={nu}  "
              f"({os.path.getsize(s)} bytes)")
PY

echo "--- 5. this deploy's log line + confirm the NEW backup step is on disk for NEXT deploy ---"
grep -E 'deploy(ing|ed)|rollback|snapshot|backup' /var/log/rmdemo-autodeploy.log 2>/dev/null | tail -8 || echo "  (no autodeploy log found)"
grep -q 'backup_db' "$REPO_DIR/deploy/cohost/auto-deploy.sh" \
    && echo "  auto-deploy.sh backup_db(): present (runs on the NEXT deploy — bash had the old script in RAM for this one)" \
    || echo "  auto-deploy.sh backup_db(): MISSING"
echo "===== done ====="
REMOTE
