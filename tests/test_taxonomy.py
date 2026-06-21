"""Data-driven skill taxonomy: broad coverage with precision (no over-matching plain prose)."""
from resume_matcher.matching.taxonomy import canonical_name, normalize_skills, skill_count


def test_vocabulary_is_large():
    assert skill_count() > 500  # data-driven set, far beyond the original ~70


def test_detects_multidomain_skills():
    jd = ("Python, SQL, Docker, Kubernetes, AWS, React, Next.js, Kafka, Terraform, PyTorch, "
          "Tableau, Figma, Salesforce, Excel, Swift, Kotlin")
    got = set(normalize_skills(jd))
    for s in ["python", "sql", "docker", "kubernetes", "aws", "react", "kafka", "terraform",
              "tableau", "figma", "salesforce", "excel", "swift", "kotlin"]:
        assert s in got, s


def test_no_false_positives_on_plain_prose():
    junk = "We dig into the next play, express our hive of ideas to teams, and drill the backlog."
    assert normalize_skills(junk) == []


def test_trailing_punctuation_and_node_disambiguation():
    assert "docker" in normalize_skills("We use Docker.")
    assert normalize_skills("Built with Node.js") == ["node_js"]


def test_core_skills_preserved_after_refactor():
    for s in ["python", "java", "javascript", "sql", "machine_learning", "aws", "git", "agile"]:
        assert s in normalize_skills(f"experience with {canonical_name(s)}"), s
