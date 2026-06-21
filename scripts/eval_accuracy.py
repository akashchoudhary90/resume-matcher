"""Run the accuracy measurement harness and print a report.

Usage:
  python scripts/eval_accuracy.py [path/to/labeled_examples.json]

Scores each labeled (job, resume, human-rating) example with the same deterministic pipeline the app
uses and reports how well it agrees with human judgment. Edit the ranker weights
(resume_matcher/matching/ranker.py) and re-run to tune; replace ratings with real outcomes to calibrate.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from resume_matcher.matching.benchmark import load_examples, run_benchmark  # noqa: E402


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    out = run_benchmark(load_examples(path))
    m = out["metrics"]
    print("=== Accuracy benchmark ===")
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


if __name__ == "__main__":
    main()
