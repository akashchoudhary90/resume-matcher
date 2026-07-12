"""Seed or promote a platform user from the CLI (coordinators/admins can't self-register).

    python scripts/create_user.py coord@york.ca --password S3cret123 --role coordinator
    python scripts/create_user.py hr@acme.com --password S3cret123 --role employer --org "Acme"
    python scripts/create_user.py someone@x.com --promote coordinator      # change an existing role

Writes to the platform DB (RM_PLATFORM_DB / RM_ACCOUNTS_DB / data/platform.db).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resume_matcher.api.accounts import ROLES, AccountError, AccountStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("email")
    ap.add_argument("--password", help="required when creating a new user")
    ap.add_argument("--role", choices=ROLES, default="coordinator")
    ap.add_argument("--org", help="employer org name (created/joined with a pending school link)")
    ap.add_argument("--school-id", type=int, default=1, help="school the account belongs to")
    ap.add_argument("--promote", choices=ROLES, metavar="ROLE",
                    help="change an EXISTING user's role instead of creating one")
    args = ap.parse_args()

    store = AccountStore()
    if args.promote:
        with closing(sqlite3.connect(store.path)) as conn:
            cur = conn.execute("UPDATE users SET role=? WHERE email=?",
                               (args.promote, args.email.strip().lower()))
            conn.commit()
        if not cur.rowcount:
            print(f"no user with email {args.email!r}", file=sys.stderr)
            return 1
        print(f"{args.email} -> role={args.promote}")
        return 0

    if not args.password:
        ap.error("--password is required when creating a user")
    try:
        store.create_user(args.email, args.password, role=args.role, org_name=args.org,
                          school_id=args.school_id)
    except AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {args.email} role={args.role}" + (f" org={args.org}" if args.org else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
