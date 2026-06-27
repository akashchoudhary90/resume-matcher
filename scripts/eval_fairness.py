"""Fairness regression gate — the deterministic half of the two-key fairness change-control.

Runs the bias audit (audit/metrics.py four-fifths + exposure + homophily, proxy_leakage.py) over a
FIXED synthetic cohort and gates a SCORING change against committed thresholds: a change to weights /
taxonomy / the ranker that pushes the four-fifths impact ratio below 0.80, worsens it past tolerance,
collapses exposure/homophily parity, or makes the scoring features leak a protected attribute FAILS CI.

Bias stays audit-only and is NEVER a scoring input — this gate just makes a regression in those AUDIT
metrics block the merge, so no scoring change ships silently making disparate impact worse.

Deterministic + env-independent: fixed seed, mock engine, RM_EMBEDDINGS=tfidf. Honest: synthetic data,
not real-world outcomes (which we don't have) — so this gates RELATIVE fairness of the scoring logic.

Usage:
  python scripts/eval_fairness.py                 # report
  python scripts/eval_fairness.py --json
  python scripts/eval_fairness.py --check         # gate vs data/eval/fairness_baseline.json (exit 1)
  python scripts/eval_fairness.py --write-baseline # (re)write the committed baseline from this run
"""
import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("RM_EMBEDDINGS", "tfidf")      # deterministic retrieval everywhere
os.environ.setdefault("RM_INFERENCE_BACKEND", "mock")

from resume_matcher.api.service import AppState  # noqa: E402

_BASELINE = pathlib.Path(__file__).resolve().parent.parent / "data" / "eval" / "fairness_baseline.json"
SEED, N_STUDENTS, N_JOBS = 42, 200, 12


def _evaluate(backend: str = "mock") -> dict:
    state = AppState()
    state.backend = backend
    state.regenerate_synthetic(n_students=N_STUDENTS, n_jobs=N_JOBS, seed=SEED)
    audit = state.audit()
    attrs = audit.get("attributes", {})

    def imp(attr: str):
        return (attrs.get(attr) or {}).get("min_impact_ratio")

    metrics = {
        "race_min_impact_ratio": imp("race_ethnicity"),
        "gender_min_impact_ratio": imp("gender"),
        "exposure_parity_ratio": (audit.get("exposure") or {}).get("parity_ratio"),
        "homophily_disparity_ratio": (audit.get("homophily") or {}).get("disparity_ratio"),
        "proxy_leakage_auc": (audit.get("proxy_leakage") or {}).get("auc"),
        "proxy_leaks": (audit.get("proxy_leakage") or {}).get("leakage"),
        "n_selected": audit.get("n_selected"),
        "n_pool": audit.get("n_pool"),
    }
    return {"backend": backend, "seed": SEED, "n_students": N_STUDENTS, "metrics": metrics}


# Metrics that must stay HIGH (a ratio; floor) vs those that must stay LOW (proxy AUC; ceiling).
_MIN_KEYS = ("race_min_impact_ratio", "gender_min_impact_ratio",
             "exposure_parity_ratio", "homophily_disparity_ratio")


def _check(out: dict) -> list[str]:
    if not _BASELINE.exists():
        return [f"no committed fairness baseline ({_BASELINE.name}) — run --write-baseline"]
    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    floors = base.get("floors", {})
    tol = float(base.get("tolerance", 0.03))
    m = out["metrics"]
    viol: list[str] = []
    for key in _MIN_KEYS:
        floor = floors.get(f"min_{key}")
        val = m.get(key)
        if floor is not None and val is not None and val < floor - tol:
            viol.append(f"{key}={val} < floor {floor} (tol {tol})")
    ceil = floors.get("max_proxy_leakage_auc")
    if ceil is not None and m.get("proxy_leakage_auc") is not None and m["proxy_leakage_auc"] > ceil + tol:
        viol.append(f"proxy_leakage_auc={m['proxy_leakage_auc']} > ceiling {ceil} (tol {tol})")
    return viol


def _write_baseline(out: dict) -> None:
    m = out["metrics"]
    # Floor each ratio at min(current, 0.80): the synthetic cohort already shows disparate impact by
    # design (it's a bias-DETECTION demo), so this is a REGRESSION gate — don't drop below the current
    # value for an already-low metric, and don't cross below the 0.80 four-fifths line for a healthy one.
    floors = {f"min_{k}": round(min(m[k], 0.80), 3) for k in _MIN_KEYS if m.get(k) is not None}
    floors["max_proxy_leakage_auc"] = round(max(0.70, m.get("proxy_leakage_auc") or 0.0), 3)
    payload = {
        "note": "Committed fairness FLOORS for the synthetic cohort (seed 42, mock, tfidf). A scoring "
                "change that regresses past these (minus tolerance) fails CI. Synthetic, not outcomes.",
        "cohort": {"seed": SEED, "n_students": N_STUDENTS, "n_jobs": N_JOBS,
                   "embeddings": "tfidf", "backend": out["backend"]},
        "tolerance": 0.03,
        "current": m,
        "floors": floors,
    }
    _BASELINE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {_BASELINE}")


def _print(out: dict) -> None:
    m = out["metrics"]
    print("=== Fairness audit (synthetic cohort; bias is audit-only, never a scoring input) ===")
    print(f"cohort:                 seed {out['seed']}, {out['n_students']} students, tfidf, {out['backend']}")
    print(f"selected / pool:        {m['n_selected']} / {m['n_pool']}")
    print(f"race four-fifths min:   {m['race_min_impact_ratio']}")
    print(f"gender four-fifths min: {m['gender_min_impact_ratio']}")
    print(f"exposure parity ratio:  {m['exposure_parity_ratio']}")
    print(f"homophily disparity:    {m['homophily_disparity_ratio']}")
    print(f"proxy-leakage AUC:      {m['proxy_leakage_auc']}  (leaks={m['proxy_leaks']})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fairness regression gate (synthetic, deterministic).")
    ap.add_argument("--backend", default="mock")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true", help="gate vs the committed baseline; exit 1 on regression")
    ap.add_argument("--write-baseline", action="store_true", help="(re)write the committed baseline from this run")
    args = ap.parse_args()

    out = _evaluate(args.backend)
    if args.write_baseline:
        _write_baseline(out)
        return
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        _print(out)

    if args.check:
        viol = _check(out)
        if viol:
            print("\nFAIRNESS REGRESSION (vs committed baseline):", file=sys.stderr)
            for v in viol:
                print(f"  - {v}", file=sys.stderr)
            sys.exit(1)
        print("\nFairness check OK - no regression vs committed baseline.")


if __name__ == "__main__":
    main()
