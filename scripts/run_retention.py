"""Run the relationship-graph retention/erasure purge once. Run: python scripts/run_retention.py

Hard-deletes graph edges past their expires_at and terminal intro_requests past purge_after (and
sweeps expired requests first). The app also runs this on a schedule (RM_GRAPH_RETENTION_HOURS), but
this script lets an operator run it on demand or from cron. Honors RM_ACCOUNTS_DB / the platform DB
path env like the rest of the platform.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> None:
    from resume_matcher.stores.retention import run_retention

    out = run_retention()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
