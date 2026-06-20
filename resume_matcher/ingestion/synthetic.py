"""Synthetic dataset generator — realistic-but-fake students, resumes, jobs, and voluntary self-ID.

Lets the whole team build and test the pipeline with ZERO real student PII (plan §A sequencing).
Deterministic given a seed. Writes the same file shapes the Handshake importer expects:

  <out>/students.csv         candidate_id,email,name,education_level,years_experience,resume_file
  <out>/resumes/<id>.txt     plain-text resume (skill names appear verbatim for evidence spans)
  <out>/jobs.csv             job_id,title,employer,description,required_skills,preferred_skills,min_education
  <out>/self_id.csv          candidate_id,race_ethnicity,gender   (for the bias-audit demo)
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

from faker import Faker

from ..matching.taxonomy import all_canonical_ids, canonical_name

_ARCHETYPES = {
    "Software Engineering": ["python", "java", "git", "rest_api", "docker", "sql", "linux"],
    "Data Science": ["python", "machine_learning", "pandas", "numpy", "sql", "data_analysis", "tensorflow"],
    "Web Development": ["javascript", "typescript", "react", "node_js", "html", "css", "rest_api"],
    "Cloud / DevOps": ["aws", "docker", "kubernetes", "linux", "git", "python", "azure"],
    "Business Analytics": ["excel", "sql", "tableau", "power_bi", "data_analysis", "communication"],
}
_SOFT = ["communication", "teamwork", "project_management", "agile"]
_EDU = ["bachelor", "bachelor", "bachelor", "master", "diploma"]
_RACE = ["Group A", "Group B", "Group C", "Group D"]
_GENDER = ["woman", "man", "nonbinary"]
_EMPLOYERS = ["NorthBank", "MapleSoft", "Cedar Analytics", "Lakeshore Cloud", "York Retail Co", "BlueJay AI"]


def _resume_text(faker: Faker, major: str, years: float, skills: list[str]) -> str:
    names = [canonical_name(s) for s in skills]
    bullets = []
    for i in range(0, len(skills), 2):
        pair = names[i : i + 2]
        bullets.append(f"- Built and shipped a project using {' and '.join(pair)}.")
    return (
        f"{faker.name()}\n{faker.email()}\n\n"
        f"SUMMARY\nMotivated {major} student with {years:.0f} years of experience building software "
        f"and analytical projects.\n\n"
        f"SKILLS\n{', '.join(names)}\n\n"
        f"EXPERIENCE\n" + "\n".join(bullets) + "\n\n"
        f"EDUCATION\nDegree in {major}, York University.\n"
    )


def generate_dataset(out_dir: str | Path, n_students: int = 60, n_jobs: int = 12, seed: int = 42) -> Path:
    out = Path(out_dir)
    (out / "resumes").mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    faker = Faker()
    Faker.seed(seed)

    archetypes = list(_ARCHETYPES)
    students, self_ids = [], []
    for i in range(n_students):
        cid = f"S{i:04d}"
        major = rng.choice(archetypes)
        core = _ARCHETYPES[major]
        skills = sorted(set(rng.sample(core, k=rng.randint(3, len(core))) + rng.sample(_SOFT, k=rng.randint(0, 2))))
        years = rng.choice([0, 0.5, 1, 1, 2, 2, 3])
        edu = rng.choice(_EDU)
        has_resume = rng.random() > 0.12  # ~12% of students have no public resume
        resume_file = f"{cid}.txt"
        if has_resume:
            (out / "resumes" / resume_file).write_text(
                _resume_text(faker, major, years, skills), encoding="utf-8"
            )
        students.append(
            {
                "candidate_id": cid,
                "email": f"{cid.lower()}@my.yorku.ca",
                "name": faker.name(),
                "education_level": edu,
                "years_experience": years,
                "resume_file": resume_file if has_resume else "",
            }
        )
        # Self-ID is assigned INDEPENDENTLY of skills — the tool must not (and does not) couple them.
        self_ids.append(
            {"candidate_id": cid, "race_ethnicity": rng.choice(_RACE), "gender": rng.choice(_GENDER)}
        )

    jobs = []
    for j in range(n_jobs):
        major = archetypes[j % len(archetypes)]
        core = _ARCHETYPES[major]
        required = rng.sample(core, k=min(4, len(core)))
        preferred = rng.sample([s for s in all_canonical_ids() if s not in required], k=3)
        jobs.append(
            {
                "job_id": f"J{j:03d}",
                "title": f"{major} Intern",
                "employer": rng.choice(_EMPLOYERS),
                "description": (
                    f"We are hiring a {major} intern. You will work with "
                    f"{', '.join(canonical_name(s) for s in required)}."
                ),
                "required_skills": ";".join(required),
                "preferred_skills": ";".join(preferred),
                "min_education": "bachelor" if rng.random() > 0.5 else "",
            }
        )

    _write_csv(out / "students.csv", students)
    _write_csv(out / "jobs.csv", jobs)
    _write_csv(out / "self_id.csv", self_ids)
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
