"""Skill taxonomy normalization.

Raw skill strings (from resumes and job descriptions) are mapped to canonical skill IDs so synonyms
collapse and keyword-stuffing over surface forms gains nothing. This ships a compact built-in
taxonomy; in production, swap `load_taxonomy()` for Lightcast Open Skills (free, O*NET-tagged) to get
34k+ skills and real transferable-skill mappings — the rest of the system only depends on the
`normalize_skills` / `canonical_name` API, not on this dictionary.
"""
from __future__ import annotations

import re

# canonical_id -> display name
_CANONICAL: dict[str, str] = {
    "python": "Python",
    "java": "Java",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "cpp": "C++",
    "csharp": "C#",
    "sql": "SQL",
    "html": "HTML",
    "css": "CSS",
    "react": "React",
    "node_js": "Node.js",
    "django": "Django",
    "flask": "Flask",
    "machine_learning": "Machine Learning",
    "deep_learning": "Deep Learning",
    "nlp": "NLP",
    "data_analysis": "Data Analysis",
    "pandas": "pandas",
    "numpy": "NumPy",
    "tensorflow": "TensorFlow",
    "pytorch": "PyTorch",
    "aws": "AWS",
    "azure": "Azure",
    "gcp": "GCP",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "git": "Git",
    "linux": "Linux",
    "excel": "Excel",
    "tableau": "Tableau",
    "power_bi": "Power BI",
    "communication": "Communication",
    "teamwork": "Teamwork",
    "project_management": "Project Management",
    "agile": "Agile",
    "rest_api": "REST APIs",
    "mongodb": "MongoDB",
    "postgresql": "PostgreSQL",
    "spark": "Apache Spark",
    "hadoop": "Hadoop",
    "r_lang": "R",
    # --- common additions for better coverage of real postings (unambiguous surface forms only) ---
    "kotlin": "Kotlin",
    "scala": "Scala",
    "php": "PHP",
    "rust": "Rust",
    "golang": "Golang",  # NOT "Go" — that would make bare "go" match ordinary English text
    "ruby": "Ruby",
    "rails": "Ruby on Rails",
    "dotnet": ".NET",
    "fastapi": "FastAPI",
    "spring_boot": "Spring Boot",
    "angular": "Angular",
    "vue": "Vue.js",
    "svelte": "Svelte",
    "nextjs": "Next.js",
    "redux": "Redux",
    "graphql": "GraphQL",
    "terraform": "Terraform",
    "ansible": "Ansible",
    "jenkins": "Jenkins",
    "ci_cd": "CI/CD",
    "kafka": "Apache Kafka",
    "redis": "Redis",
    "elasticsearch": "Elasticsearch",
    "rabbitmq": "RabbitMQ",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "airflow": "Apache Airflow",
    "bigquery": "BigQuery",
    "redshift": "Amazon Redshift",
    "mysql": "MySQL",
    "selenium": "Selenium",
    "pytest": "pytest",
    "jira": "Jira",
    "confluence": "Confluence",
    "figma": "Figma",
    "salesforce": "Salesforce",
    "powerpoint": "PowerPoint",
    "matplotlib": "Matplotlib",
}

# surface form (lowercase) -> canonical_id. Includes the canonical names themselves + synonyms.
_SYNONYMS: dict[str, str] = {
    "js": "javascript",
    "ecmascript": "javascript",
    "ts": "typescript",
    "c++": "cpp",
    "cplusplus": "cpp",
    "c#": "csharp",
    "c sharp": "csharp",
    "node": "node_js",
    "node.js": "node_js",
    "nodejs": "node_js",
    "ml": "machine_learning",
    "dl": "deep_learning",
    "natural language processing": "nlp",
    "postgres": "postgresql",
    "power bi": "power_bi",
    "powerbi": "power_bi",
    "rest": "rest_api",
    "restful": "rest_api",
    "rest apis": "rest_api",
    "amazon web services": "aws",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "r programming": "r_lang",
    "scikit-learn": "machine_learning",
    "sklearn": "machine_learning",
    # synonyms for the expanded skill set
    "go lang": "golang",
    "ruby on rails": "rails",
    "asp.net": "dotnet",
    ".net core": "dotnet",
    ".net": "dotnet",
    "vuejs": "vue",
    "vue.js": "vue",
    "next.js": "nextjs",
    "ci/cd": "ci_cd",
    "cicd": "ci_cd",
    "continuous integration": "ci_cd",
    "apache kafka": "kafka",
    "apache airflow": "airflow",
    "elastic search": "elasticsearch",
    "big query": "bigquery",
    "amazon redshift": "redshift",
    "spring boot": "spring_boot",
    "springboot": "spring_boot",
    "power point": "powerpoint",
}


def _build_surface_index() -> list[tuple[str, str]]:
    index: dict[str, str] = {}
    for cid, name in _CANONICAL.items():
        index[name.lower()] = cid
        index[cid.replace("_", " ")] = cid
        index[cid] = cid
    index.update(_SYNONYMS)
    # Longest surface forms first so "machine learning" matches before "learning".
    return sorted(index.items(), key=lambda kv: len(kv[0]), reverse=True)


_SURFACE_INDEX = _build_surface_index()


def canonical_name(skill_id: str) -> str:
    return _CANONICAL.get(skill_id, skill_id.replace("_", " ").title())


def all_canonical_ids() -> list[str]:
    return list(_CANONICAL)


def normalize_skills(text: str) -> list[str]:
    """Return the sorted set of canonical skill IDs found in `text`."""
    if not text:
        return []
    low = " " + text.lower() + " "
    found: set[str] = set()
    for surface, cid in _SURFACE_INDEX:
        # word-ish boundaries; allow +, #, . INSIDE surface forms (c++, node.js) but treat a
        # trailing "." as a boundary unless it's followed by a word char — so "Docker." at the end
        # of a sentence still matches, while "node" inside "node.js" does not match on its own.
        pattern = r"(?<![\w+#.])" + re.escape(surface) + r"(?![\w+#])(?!\.\w)"
        if re.search(pattern, low):
            found.add(cid)
    return sorted(found)
