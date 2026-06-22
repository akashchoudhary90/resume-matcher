"""Run the accuracy measurement harness and print an HONEST report.

Usage:
  python scripts/eval_accuracy.py                 # PRIMARY realistic set (coordinator), mock backend
  python scripts/eval_accuracy.py --dataset labeled    # clear-cut seed (smoke/sanity ONLY)
  python scripts/eval_accuracy.py --dataset path/to.json
  python scripts/eval_accuracy.py --backend claude_cli # measure the real engine (needs CLI + token)
  python scripts/eval_accuracy.py --all           # both named sets
  python scripts/eval_accuracy.py --json           # machine-readable
  python scripts/eval_accuracy.py --check          # gate vs data/eval/baseline_metrics.json (exit 1 on regression)

HONESTY NOTES (this is the whole point of #8):
  * The default dataset is the REALISTIC `coordinator` set, NOT the self-confirming seed. The mock
    keyword engine scores POORLY on it (label_accuracy ~0.25) — that is the true baseline and the
    reason the Claude engine exists.
  * The `labeled` seed scores ~1.0 with the mock, but that is a PIPELINE SMOKE TEST, not accuracy.
    Never quote it as "the tool is ~100% accurate".
  * The backend that produced the numbers is always printed. Mock != the deployed engine.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from resume_matcher.inference.adapter import get_adapter  # noqa: E402
from resume_matcher.matching.benchmark import (  # noqa: E402
    NAMED_DATASETS,
    PRIMARY_DATASET,
    load_examples,
    resolve_dataset,
    run_benchmark,
)

_BASELINE = pathlib.Path(__file__).resolve().parent.parent / "data" / "eval" / "baseline_metrics.json"
_MOCK_CAVEAT = "keyword baseline - NOT the deployed Claude engine; poor on realistic resumes by design"


def _evaluate(dataset: str, backend: str) -> dict:
    path = resolve_dataset(dataset)
    examples = load_examples(path)
    adapter = get_adapter(backend)
    out = run_benchmark(examples, adapter)
    out["dataset"] = dataset
    out["dataset_path"] = str(path)
    out["backend"] = backend
    return out


def _print_report(out: dict) -> None:
    m = out["metrics"]
    backend = out["backend"]
    caveat = f"  [{_MOCK_CAVEAT}]" if backend == "mock" else ""
    print("=== Accuracy benchmark ===")
    print(f"dataset:             {out['dataset']}  ({out['dataset_path']})")
    print(f"engine/backend:      {backend}{caveat}")
    print(f"examples:            {m['n']} (labeled: {m['n_labeled']})")
    print(f"label accuracy:      {m['label_accuracy']}")
    print(f"within-one-bucket:   {m['within_one_bucket']}")
    print(f"spearman (rank):     {m['spearman']}")
    print(f"MAE vs human score:  {m['mae']}")
    print("\nper-example:")
    print(f"  {'id':22} {'job':16} {'fit':>5} {'tool':6} {'human':6} {'hscore':>6}")
    for r in out["rows"]:
        print(f"  {str(r['id'])[:22]:22} {str(r['job'])[:16]:16} {r['tool_fit']:>5} "
              f"{r['tool_label']:6} {str(r['human_label']):6} {str(r['human_score']):>6}")
    print("\nconfusion (human -> tool):")
    for h, d in (m["confusion"] or {}).items():
        print(f"  {h:8} -> {d}")


def _check(out: dict) -> list[str]:
    """Compare metrics to the committed floors for this dataset; return a list of violations."""
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))["datasets"].get(out["dataset"])
    if not baseline:
        return [f"no committed baseline for dataset '{out['dataset']}' — add one to {_BASELINE.name}"]
    if out["backend"] != baseline.get("backend"):
        return [f"baseline is for backend '{baseline.get('backend')}', ran '{out['backend']}'"]
    m = out["metrics"]
    viol: list[str] = []

    def lo(metric: str, key: str) -> None:
        floor = baseline.get(key)
        val = m.get(metric)
        if floor is not None and (val is None or val < floor):
            viol.append(f"{metric}={val} < floor {floor}")

    lo("label_accuracy", "min_label_accuracy")
    lo("within_one_bucket", "min_within_one_bucket")
    lo("spearman", "min_spearman")
    ceil = baseline.get("max_mae")
    if ceil is not None and (m.get("mae") is None or m["mae"] > ceil):
        viol.append(f"mae={m.get('mae')} > ceiling {ceil}")
    return viol


def main() -> None:
    ap = argparse.ArgumentParser(description="Accuracy measurement harness (honest by default).")
    ap.add_argument("dataset_pos", nargs="?", help="dataset name/path (positional, optional)")
    ap.add_argument("--dataset", help="named set ('coordinator'|'labeled') or a path",
                    default=None)
    ap.add_argument("--backend", default="mock", help="inference backend (default: mock)")
    ap.add_argument("--all", action="store_true", help="run every named dataset")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--check", action="store_true",
                    help="gate against data/eval/baseline_metrics.json; exit 1 on regression")
    args = ap.parse_args()

    dataset = args.dataset or args.dataset_pos or PRIMARY_DATASET
    datasets = list(NAMED_DATASETS) if args.all else [dataset]

    results = [_evaluate(ds, args.backend) for ds in datasets]
    failures: list[str] = []
    for out in results:
        if args.check:
            viol = _check(out)
            out["check_violations"] = viol
            if viol:
                failures += [f"{out['dataset']}: {v}" for v in viol]

    if args.json:
        payload = [{"dataset": o["dataset"], "backend": o["backend"], "metrics": o["metrics"],
                    **({"check_violations": o["check_violations"]} if args.check else {})}
                   for o in results]
        print(json.dumps(payload if len(payload) > 1 else payload[0], indent=2))
    else:
        for out in results:
            _print_report(out)
            print()

    if args.check:
        if failures:
            print("ACCURACY REGRESSION (vs committed baseline):", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            sys.exit(1)
        print("Accuracy check OK - no regression vs committed baseline.")


if __name__ == "__main__":
    main()
