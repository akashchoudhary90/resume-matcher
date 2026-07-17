"""Operator data-subject-request tool: erase an ACCOUNT, or repudiate a NON-MEMBER identity.

    python scripts/dsr_erase.py --email who@x.com --dry-run          # preview the cascade
    python scripts/dsr_erase.py --email who@x.com                    # asks for `ERASE who@x.com`
    python scripts/dsr_erase.py --user-id 42 --yes --json            # scripted DSR + receipt

    python scripts/dsr_erase.py --repudiate --school 1 --email ex@x.com
    python scripts/dsr_erase.py --repudiate --school 1 --first Ada --last Byron --company Acme

Erasure shares ONE implementation with the self-serve `DELETE /api/account` route
(stores/erasure.erase_account) — there is no second cascade to drift out of sync. Both planes must
report success for exit 0, so a partial cascade is visible to the operator instead of silent.

The printed receipt for the DSR file carries a HASH of the address, never the address: the paperwork
proving we erased someone must not itself re-store them. Writes to the platform DB
(RM_PLATFORM_DB / RM_ACCOUNTS_DB / data/platform.db) and the audit DB (RM_AUDIT_DB).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resume_matcher.stores.db import connect, migrate, platform_db_path  # noqa: E402
from resume_matcher.stores.erasure import ErasureError, erase_account, user_id_hash  # noqa: E402
from resume_matcher.stores.graph import GraphError  # noqa: E402


def _resolve(email: str | None, user_id: int | None) -> tuple[int, str]:
    path = platform_db_path()
    migrate(path)
    with closing(connect(path)) as conn:
        if user_id is not None:
            row = conn.execute("SELECT id, email FROM users WHERE id=?", (user_id,)).fetchone()
        else:
            row = conn.execute("SELECT id, email FROM users WHERE email=?",
                               ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise ErasureError(f"no account matching {user_id or email!r}")
    return row["id"], row["email"]


def _do_erase(args: argparse.Namespace) -> int:
    uid, email = _resolve(args.email, args.user_id)
    preview = erase_account(uid, dry_run=True)
    if not args.json:
        print(f"user_id={uid} email={email}\nwould delete/anonymize:")
        for table, n in sorted(preview["tables"].items()):
            if n:
                print(f"  {table:<24} {n}")
    if args.dry_run:
        if args.json:
            print(json.dumps(preview, indent=2))
        return 0

    if not args.yes:
        # Typing the address back is the guard against erasing the wrong row from a shell history.
        if input(f"\nType `ERASE {email}` to proceed: ").strip() != f"ERASE {email}":
            print("aborted", file=sys.stderr)
            return 1

    result = erase_account(uid)
    receipt = {"user_id_hash": user_id_hash(email), "erased_at": time.time(),
               "tables": result["tables"], "tombstoned": result["tombstoned"],
               "audit_plane_deleted": result["audit_plane_deleted"]}
    print(json.dumps(receipt, indent=2))
    # Exit 0 only when both planes report success. The platform plane is proven by the tombstone
    # (it commits in the same transaction as the users row); the audit plane is satisfied by
    # erase_account having run phase 1 to completion — a raised ErasureError never reaches here.
    return 0 if (result["tombstoned"] and result["tables"].get("users")) else 1


def _do_repudiate(args: argparse.Namespace) -> int:
    """Non-member path — the SAME store methods the public API calls, including the privacy-F3
    rule that a name assertion never touches an active member's data."""
    from resume_matcher.stores.graph import NetworkStore

    store = NetworkStore()
    if args.email:
        out = store.repudiate_execute_email(args.school, email=args.email)
    else:
        if not (args.first or args.last):
            print("error: --repudiate needs --email, or --first/--last (+ optional --company)",
                  file=sys.stderr)
            return 2
        out = store.repudiate_execute_name(args.school, first=args.first or "",
                                           last=args.last or "", company=args.company or "")
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", help="account address (erasure) or asserted address (repudiation)")
    ap.add_argument("--user-id", type=int, help="account id, when the address is ambiguous/unknown")
    ap.add_argument("--dry-run", action="store_true", help="print the counts, change nothing")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation (scripted DSRs)")
    ap.add_argument("--json", action="store_true", help="machine-readable output only")
    ap.add_argument("--repudiate", action="store_true",
                    help="NON-MEMBER path: suppress an asserted identity instead of erasing an account")
    ap.add_argument("--school", type=int, default=1, help="school_id for --repudiate")
    ap.add_argument("--first", help="--repudiate name path")
    ap.add_argument("--last", help="--repudiate name path")
    ap.add_argument("--company", help="--repudiate name path")
    args = ap.parse_args()

    try:
        if args.repudiate:
            return _do_repudiate(args)
        if not (args.email or args.user_id):
            ap.error("--email or --user-id is required")
        return _do_erase(args)
    except (ErasureError, GraphError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"database error (nothing was committed for the failing phase): {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
