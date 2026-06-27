#!/usr/bin/env python3
"""Standalone independent verifier for a Resume Matcher Defense File.

Re-checks a Defense File WITHOUT trusting (or contacting) the issuing server: the hash chain, per-record
reconciliation (each score re-derives from its recorded breakdown), and — for Ed25519 — the signatures.
Pass the issuer's published public key (obtained out-of-band, e.g. from /api/defense-file/pubkey or the
spec) with --issuer-key to also AUTHENTICATE the signer.

  python scripts/verify_defense_file.py defense-file.json
  python scripts/verify_defense_file.py defense-file.json --issuer-key <hex>

Exit code 0 == verified, 1 == failed. The verification algorithm is the open spec in
docs/DEFENSE_FILE_SPEC.md, so a third party can reimplement this with no dependency on us.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make this runnable directly (`python scripts/verify_defense_file.py …`) from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resume_matcher.audit.defense_file import verify_defense_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a Resume Matcher Defense File.")
    ap.add_argument("file", help="path to a defense-file.json")
    ap.add_argument("--issuer-key", default=None,
                    help="expected issuer Ed25519 public key (hex), obtained out-of-band, to authenticate the signer")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        doc = json.load(fh)

    verdict = verify_defense_file(doc, expected_public_key=args.issuer_key)
    print(json.dumps(verdict, indent=2))
    if verdict.get("issuer_verified") is None and verdict.get("sig_alg") == "ed25519":
        print("\nNote: pass --issuer-key <hex> to authenticate the signer (chain + reconciliation already "
              "checked).", file=sys.stderr)
    return 0 if verdict.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
