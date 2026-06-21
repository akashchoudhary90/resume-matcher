"""Build / refresh the skill taxonomy data file (resume_matcher/data/skills.json).

The matcher (resume_matcher/matching/taxonomy.py) is data-driven: it reads this JSON. This script
regenerates it from a large open skills catalog so you can scale from the bundled curated set to a
full vocabulary (~13k-34k skills) when you have access.

Sources
-------
1) Lightcast Open Skills API (~34k skills, free, O*NET-tagged) -- needs free API credentials:
     export LIGHTCAST_CLIENT_ID=...      LIGHTCAST_CLIENT_SECRET=...
     python scripts/build_skills.py --source lightcast --merge
   (Register at https://skills.emsidata.com / https://lightcast.io for Open Skills access.)

2) ESCO (~13.9k skills, fully open download, no API key):
     download the ESCO "skills_en.csv" from https://esco.ec.europa.eu/en/use-esco/download
     python scripts/build_skills.py --source esco --esco-csv path/to/skills_en.csv --merge

Flags
-----
  --merge        merge into the existing skills.json (keep curated entries) instead of overwriting.
  --out PATH     output path (default: resume_matcher/data/skills.json).
  --min-len N    drop skill names shorter than N chars (default 3; precision guard).

Format written: {canonical_id: {"name": str, "aliases": [str, ...]}}.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "resume_matcher" / "data" / "skills.json"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or ""


def _http_json(url: str, headers: dict, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fetch_lightcast() -> list[dict]:
    cid = os.environ.get("LIGHTCAST_CLIENT_ID")
    secret = os.environ.get("LIGHTCAST_CLIENT_SECRET")
    if not (cid and secret):
        sys.exit("Set LIGHTCAST_CLIENT_ID and LIGHTCAST_CLIENT_SECRET (free Open Skills creds).")
    body = urllib.parse.urlencode(
        {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "emsi_open"}
    ).encode()
    tok = _http_json(
        "https://auth.emsicloud.com/connect/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )["access_token"]
    out = _http_json(
        "https://emsiservices.com/skills/versions/latest/skills",
        {"Authorization": f"Bearer {tok}"},
    )
    # {data: [{id, name, type, ...}]}
    return [{"name": s.get("name", ""), "aliases": []} for s in out.get("data", []) if s.get("name")]


def parse_esco(csv_path: str) -> list[dict]:
    rows: list[dict] = []
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("preferredLabel") or "").strip()
            if not name:
                continue
            alt = (row.get("altLabels") or "").replace("\n", "|")
            aliases = [a.strip() for a in re.split(r"[|;]", alt) if a.strip()]
            rows.append({"name": name, "aliases": aliases})
    return rows


def to_taxonomy(rows: list[dict], min_len: int) -> dict:
    tax: dict[str, dict] = {}
    for r in rows:
        name = r["name"].strip()
        if len(name) < min_len:
            continue
        cid = slugify(name)
        if not cid:
            continue
        entry = tax.setdefault(cid, {"name": name, "aliases": []})
        for a in r.get("aliases", []):
            a = a.strip()
            if a and a.lower() != name.lower() and a not in entry["aliases"]:
                entry["aliases"].append(a)
    return tax


def main() -> None:
    ap = argparse.ArgumentParser(description="Build resume_matcher/data/skills.json")
    ap.add_argument("--source", choices=["lightcast", "esco"], required=True)
    ap.add_argument("--esco-csv", help="path to ESCO skills_en.csv (for --source esco)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--merge", action="store_true", help="merge into existing skills.json")
    ap.add_argument("--min-len", type=int, default=3)
    args = ap.parse_args()

    if args.source == "lightcast":
        rows = fetch_lightcast()
    else:
        if not args.esco_csv:
            sys.exit("--esco-csv is required for --source esco")
        rows = parse_esco(args.esco_csv)

    tax = to_taxonomy(rows, args.min_len)
    out = Path(args.out)
    if args.merge and out.exists():
        existing = json.loads(out.read_text(encoding="utf-8"))
        # curated entries win on name; union aliases
        for cid, spec in existing.items():
            e = tax.setdefault(cid, {"name": spec["name"], "aliases": []})
            e["name"] = spec.get("name", e["name"])
            e["aliases"] = sorted(set(e.get("aliases", []) + spec.get("aliases", [])))
    out.write_text(json.dumps(tax, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"Wrote {len(tax)} skills to {out} (source={args.source}, merge={args.merge}).")


if __name__ == "__main__":
    main()
