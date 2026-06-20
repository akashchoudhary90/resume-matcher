"""Generate the synthetic dataset under data/synthetic/. Run: python scripts/gen_synthetic.py"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from resume_matcher.ingestion.synthetic import generate_dataset  # noqa: E402


def main() -> None:
    out = pathlib.Path("data/synthetic")
    generate_dataset(out, n_students=60, n_jobs=12, seed=42)
    print(f"Wrote synthetic dataset to {out.resolve()}")
    print("  students.csv, jobs.csv, self_id.csv, resumes/*.txt")


if __name__ == "__main__":
    main()
