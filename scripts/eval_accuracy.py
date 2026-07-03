"""Run the accuracy measurement harness and print an HONEST report.

Usage:
  python scripts/eval_accuracy.py                 # PRIMARY realistic set (coordinator), mock backend
  python scripts/eval_accuracy.py --dataset labeled    # clear-cut seed (smoke/sanity ONLY)
  python scripts/eval_accuracy.py --dataset path/to.json
  python scripts/eval_accuracy.py --backend claude_cli --runs 3   # measure the REAL engine + spread
  python scripts/eval_accuracy.py --backend claude_cli --runs 3 --write-baseline  # commit real floors
  python scripts/eval_accuracy.py --all           # both named sets
  python scripts/eval_accuracy.py --json           # machine-readable
  python scripts/eval_accuracy.py --check          # gate vs data/eval/baseline_metrics.json (exit 1 on regression)

HONESTY NOTES (this is the whole point of #8):
  * The default dataset is the REALISTIC `coordinator` set, NOT the self-confirming seed. The mock
    keyword engine scores POORLY on it (label_accuracy ~0.25) - that is the true baseline and the
    reason the Claude engine exists.
  * The `labeled` seed scores ~1.0 with the mock, but that is a PIPELINE SMOKE TEST, not accuracy.
    Never quote it as "the tool is ~100% accurate".
  * The backend that produced the numbers is always printed. Mock != the deployed engine.
  * `--runs N` runs the SAME set N times and reports mean/stdev/min/max per metric. An LLM backend is
    non-deterministic, so one run is a point estimate; the spread tells you how much to trust it and
    how tight a regression floor can honestly be. The mock is deterministic (stdev 0).
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from resume_matcher.inference.adapter import InferenceError, get_adapter  # noqa: E402
from resume_matcher.matching.benchmark import (  # noqa: E402
    _AGG_METRICS,
    NAMED_DATASETS,
    PRIMARY_DATASET,
    load_examples,
    resolve_dataset,
    run_benchmark_repeated,
    stratum_breakdown,
)

_BASELINE = pathlib.Path(__file__).resolve().parent.parent / "data" / "eval" / "baseline_metrics.json"
_MOCK_CAVEAT = "keyword baseline - NOT the deployed Claude engine; poor on realistic resumes by design"


def _assert_backend_available(backend: str) -> None:
    """Fail CLEANLY (not with a traceback, and NEVER by silently falling back to mock — that would
    make the report a lie) when the requested engine can't actually run here."""
    if backend == "claude_cli":
        from resume_matcher.inference.adapters import claude_cli as cc
        if not cc.available():
            sys.exit(
                "Cannot benchmark backend 'claude_cli' here: it needs the `claude` CLI on PATH AND "
                "CLAUDE_CODE_OAUTH_TOKEN set (run `claude setup-token`). Run this on the box that "
                "has the subscription token (e.g. the VPS). Refusing to silently fall back to the "
                "mock — that would report mock numbers under the claude_cli label.")


def _progress_printer(dataset: str, runs: int):
    """A live one-line counter on stderr (kept off stdout so --json / the report stay clean)."""
    def cb(run_no: int, done: int, total: int) -> None:
        tail = f" (run {run_no}/{runs})" if runs > 1 else ""
        end = "\n" if done >= total and run_no >= runs else ""
        print(f"\r  scoring {dataset}: {done}/{total}{tail}   ", end=end, file=sys.stderr, flush=True)
    return cb


def _evaluate(dataset: str, backend: str, runs: int, show_progress: bool = False) -> dict:
    """Always routes through the repeated runner (runs>=1) so the report + baseline emission have a
    uniform shape: `metrics` holds the per-metric MEAN (== the single value when runs==1) and
    `aggregate` holds the full mean/stdev/min/max."""
    _assert_backend_available(backend)
    path = resolve_dataset(dataset)
    examples = load_examples(path)
    adapter = get_adapter(backend)
    prog = _progress_printer(dataset, runs) if show_progress else None
    rep = run_benchmark_repeated(examples, adapter, runs=runs, progress=prog)
    agg = rep["aggregate"]
    metrics = {"n": rep["n"], "n_labeled": rep["n_labeled"], "confusion": rep["confusion"]}
    for k in _AGG_METRICS:
        metrics[k] = agg[k]["mean"] if agg[k] else None
    return {"dataset": dataset, "dataset_path": str(path), "backend": backend, "runs": runs,
            "metrics": metrics, "aggregate": agg, "per_example_fit": rep["per_example_fit"],
            "rows": rep["last_rows"]}


def _fmt_metric(agg_entry: dict | None, runs: int) -> str:
    if agg_entry is None:
        return "n/a (undefined every run)"
    # A metric that computed to a non-finite value on some runs (e.g. spearman is nan when a run's
    # ranking collapses to a constant) was dropped from the aggregate — surface that, or a collapsed
    # engine would masquerade as rock-solid (stdev 0.0 over the one surviving run).
    warn = "" if agg_entry["n"] >= runs else f"  !! only {agg_entry['n']}/{runs} runs produced a value"
    if runs <= 1:
        return f"{agg_entry['mean']}{warn}"
    return (f"{agg_entry['mean']} ± {agg_entry['stdev']}  "
            f"[min {agg_entry['min']}, max {agg_entry['max']}]{warn}")


def _print_report(out: dict) -> None:
    agg, runs, backend = out["aggregate"], out["runs"], out["backend"]
    m = out["metrics"]
    caveat = f"  [{_MOCK_CAVEAT}]" if backend == "mock" else ""
    print("=== Accuracy benchmark ===")
    print(f"dataset:             {out['dataset']}  ({out['dataset_path']})")
    print(f"engine/backend:      {backend}{caveat}")
    print(f"runs:                {runs}" + ("  (non-deterministic engine - spread shown)"
                                            if runs > 1 else ""))
    print(f"examples:            {m['n']} (labeled: {m['n_labeled']})")
    print(f"label accuracy:      {_fmt_metric(agg['label_accuracy'], runs)}")
    print(f"within-one-bucket:   {_fmt_metric(agg['within_one_bucket'], runs)}")
    print(f"spearman (rank):     {_fmt_metric(agg['spearman'], runs)}")
    print(f"MAE vs human score:  {_fmt_metric(agg['mae'], runs)}")
    if runs > 1:
        wob = sorted(((v["stdev"], rid) for rid, v in out["per_example_fit"].items() if v),
                     reverse=True)[:5]
        if wob and wob[0][0] > 0:
            print("\nmost unstable examples (fit stdev across runs):")
            for sd, rid in wob:
                if sd > 0:
                    print(f"  {str(rid)[:28]:28} stdev {sd}")
    strata = stratum_breakdown(out["rows"])
    if len(strata) > 1:
        print("\nper-stratum (last run) — which failure mode needs work:")
        print(f"  {'stratum':12} {'n':>3} {'label_acc':>10} {'mae':>7}")
        for s in strata:
            print(f"  {s['stratum']:12} {s['n']:>3} {str(s['label_accuracy']):>10} {str(s['mae']):>7}")
    print("\nper-example (last run):")
    print(f"  {'id':22} {'job':16} {'fit':>5} {'tool':6} {'human':6} {'hscore':>6}")
    for r in out["rows"]:
        print(f"  {str(r['id'])[:22]:22} {str(r['job'])[:16]:16} {r['tool_fit']:>5} "
              f"{r['tool_label']:6} {str(r['human_label']):6} {str(r['human_score']):>6}")
    print("\nconfusion (human -> tool):")
    for h, d in (m["confusion"] or {}).items():
        print(f"  {h:8} -> {d}")


def _suggest_baseline(out: dict) -> dict:
    """Turn measured aggregate metrics into a regression-floor block for datasets[dataset][backend].
    Floors sit BELOW the observed mean by one stdev plus slack, so normal run-to-run wobble doesn't
    trip the gate; the mae ceiling sits ABOVE by the same margin. Only floors for metrics that were
    actually measured are emitted."""
    agg = out["aggregate"]
    role = ("keyword-mock floor (NOT a quality claim; poor on realistic resumes by design)"
            if out["backend"] == "mock"
            else f"measured real-engine floor ({out['backend']}, {out['runs']} run(s))")
    entry: dict = {"role": role, "measured": {k: agg[k] for k in _AGG_METRICS}}

    def floor(a, slack):
        return None if a is None else round(a["mean"] - a["stdev"] - slack, 3)

    la, w1, sp = floor(agg["label_accuracy"], 0.05), floor(agg["within_one_bucket"], 0.05), \
        floor(agg["spearman"], 0.10)
    if la is not None:
        entry["min_label_accuracy"] = max(0.0, la)
    if w1 is not None:
        entry["min_within_one_bucket"] = max(0.0, w1)
    if sp is not None:
        entry["min_spearman"] = max(-1.0, sp)  # spearman is bounded [-1,1]; an unclamped <-1 floor
        #                                         would make the gate un-triggerable
    if agg["mae"] is not None:
        entry["max_mae"] = round(agg["mae"]["mean"] + agg["mae"]["stdev"] + 3.0, 1)
    return entry


def _write_baseline(out: dict) -> None:
    data = json.loads(_BASELINE.read_text(encoding="utf-8"))
    ds = data["datasets"].setdefault(out["dataset"], {})
    if "backend" in ds:  # migrate a legacy flat entry in place before nesting the new one
        legacy = {k: v for k, v in ds.items() if k != "backend"}
        ds = data["datasets"][out["dataset"]] = {ds["backend"]: legacy}
    ds[out["backend"]] = _suggest_baseline(out)
    _BASELINE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote baseline datasets['{out['dataset']}']['{out['backend']}'] to {_BASELINE.name} "
          f"- review the diff and commit it.", file=sys.stderr)


def _baseline_for(out: dict) -> dict | None:
    """The committed floors for this (dataset, backend), or None if none exist yet. Tolerates the
    legacy flat schema (a `backend` field directly on the dataset)."""
    ds = json.loads(_BASELINE.read_text(encoding="utf-8"))["datasets"].get(out["dataset"], {})
    if isinstance(ds, dict) and "backend" in ds:  # legacy flat form
        ds = {ds["backend"]: ds}
    return ds.get(out["backend"]) if isinstance(ds, dict) else None


def _check(out: dict) -> list[str] | None:
    """Compare metrics to the committed floors. Returns a (possibly empty) violation list, or None
    when there is NO baseline for this (dataset, backend) - the caller treats None as a SKIP, so
    checking a not-yet-baselined engine never fails the run.

    Gates the WORST run, not the mean: a floor is a hard minimum, so a non-deterministic engine that
    breaches it on any run is a regression even if the mean clears. For the deterministic mock,
    min==mean==max, so the CI gate is unchanged."""
    baseline = _baseline_for(out)
    if baseline is None:
        return None
    agg = out["aggregate"]
    viol: list[str] = []

    def lo(metric: str, key: str) -> None:
        floor = baseline.get(key)
        if floor is None:
            return
        a = agg.get(metric)
        worst = a["min"] if a else None  # worst (lowest) observed run
        if worst is None or worst < floor:
            viol.append(f"{metric}(worst run)={worst} < floor {floor}")

    lo("label_accuracy", "min_label_accuracy")
    lo("within_one_bucket", "min_within_one_bucket")
    lo("spearman", "min_spearman")
    ceil = baseline.get("max_mae")
    if ceil is not None:
        a = agg.get("mae")
        worst = a["max"] if a else None  # worst (highest) observed error
        if worst is None or worst > ceil:
            viol.append(f"mae(worst run)={worst} > ceiling {ceil}")
    return viol


def main() -> None:
    ap = argparse.ArgumentParser(description="Accuracy measurement harness (honest by default).")
    ap.add_argument("dataset_pos", nargs="?", help="dataset name/path (positional, optional)")
    ap.add_argument("--dataset", help="named set ('coordinator'|'labeled') or a path", default=None)
    ap.add_argument("--backend", default="mock", help="inference backend (default: mock)")
    ap.add_argument("--runs", type=int, default=1,
                    help="run the SAME set N times and report mean/stdev/min/max (default 1)")
    ap.add_argument("--all", action="store_true", help="run every named dataset")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--check", action="store_true",
                    help="gate against data/eval/baseline_metrics.json; exit 1 on regression")
    ap.add_argument("--emit-baseline", action="store_true",
                    help="print a paste-ready regression-floor block for this backend")
    ap.add_argument("--write-baseline", action="store_true",
                    help="merge the measured floors into baseline_metrics.json (review + commit)")
    args = ap.parse_args()

    runs = max(1, args.runs)
    dataset = args.dataset or args.dataset_pos or PRIMARY_DATASET
    datasets = list(NAMED_DATASETS) if args.all else [dataset]

    # _assert_backend_available fast-paths claude_cli; this catches every other not-runnable backend
    # (missing dep, server down, rejected key) so any engine fails CLEANLY with a non-zero exit and a
    # one-line message — never a raw traceback, and never a silent fall-back to mock (would lie).
    try:
        # Live progress on stderr for the human report; suppressed for --json (clean stdout).
        results = [_evaluate(ds, args.backend, runs, show_progress=not args.json) for ds in datasets]
    except InferenceError as exc:
        sys.exit(f"Cannot benchmark backend '{args.backend}': {exc}")
    failures: list[str] = []
    skips: list[str] = []
    for out in results:
        if args.check:
            viol = _check(out)
            if viol is None:
                out["check_status"] = "skipped"
                skips.append(f"{out['dataset']}/{out['backend']}")
            else:
                out["check_violations"] = viol
                failures += [f"{out['dataset']}: {v}" for v in viol]

    if args.json:
        payload = [{"dataset": o["dataset"], "backend": o["backend"], "runs": o["runs"],
                    "metrics": o["metrics"], "aggregate": o["aggregate"],
                    **({"check_violations": o.get("check_violations"),
                        "check_status": o.get("check_status")} if args.check else {})}
                   for o in results]
        print(json.dumps(payload if len(payload) > 1 else payload[0], indent=2))
    else:
        for out in results:
            _print_report(out)
            print()

    if args.emit_baseline or args.write_baseline:
        for out in results:
            block = {out["backend"]: _suggest_baseline(out)}
            print(f"# suggested baseline for datasets['{out['dataset']}']:")
            print(json.dumps(block, indent=2))
        if args.write_baseline:
            for out in results:
                _write_baseline(out)

    if args.check:
        for s in skips:
            print(f"NOTE: no committed baseline for {s} - check skipped (add one with "
                  f"--write-baseline).", file=sys.stderr)
        if failures:
            print("ACCURACY REGRESSION (vs committed baseline):", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            sys.exit(1)
        print("Accuracy check OK - no regression vs committed baseline.")


if __name__ == "__main__":
    main()
