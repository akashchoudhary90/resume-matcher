"""Skill taxonomy normalization.

Raw skill strings (from resumes and job descriptions) are mapped to canonical skill IDs so synonyms
collapse and keyword-stuffing over surface forms gains nothing.

The vocabulary is DATA-DRIVEN: it loads from ``resume_matcher/data/skills.json``
(``{canonical_id: {"name": str, "aliases": [str, ...]}}``). To scale to a large catalog (e.g.
Lightcast Open Skills, ~34k skills) just regenerate that file — see ``scripts/build_skills.py`` — the
rest of the system only depends on the ``normalize_skills`` / ``canonical_name`` API.

Matching is a single compiled-regex pass over the text (handles thousands of surface forms in one
scan). A precision guard drops ambiguous surfaces (bare single letters, common English words, most
2-char tokens) so a big vocabulary doesn't start matching ordinary prose.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "skills.json"

# 2-char abbreviations that are safe (unambiguous tech terms); other 2-char/1-char tokens are dropped.
_SHORT_ALLOW = {"js", "ts", "ml", "dl", "ai", "ui", "ux", "qa", "bi", "hr", "ci", "cd", "db", "ar", "vr", "etl", "api"}
# Surfaces that must never match, even if a dataset lists them as a skill name/alias — they are common
# English words (or names) whose everyday sense dominates in resumes/job posts. The underlying skill is
# usually still detected via a qualified alias (e.g. "next" is dropped but "next.js" still matches).
_STOPWORDS = {
    # generic
    "go", "it", "data", "design", "cloud", "web", "team", "teams", "code", "test", "build", "lead",
    "research", "analysis", "management", "development", "engineering", "science", "support", "the",
    "processing", "factor", "scheme", "lift", "play", "express", "next", "drill", "patient", "patients",
    # tech names that are common English words (skill kept via qualified aliases where possible)
    "less", "lit", "dig", "storm", "beam", "pig", "hive", "camel", "impala", "ant", "grunt", "gulp",
    "bun", "crystal", "arc", "racket", "sage", "comet", "meteor", "ember", "backbone", "remix",
    "astro", "prism", "ping", "elm", "nim", "pike", "io", "hugo", "spark",
}

# Minimal fallback so the package imports even if the data file is missing.
_FALLBACK = {
    "python": {"name": "Python", "aliases": []},
    "java": {"name": "Java", "aliases": []},
    "javascript": {"name": "JavaScript", "aliases": ["js"]},
    "sql": {"name": "SQL", "aliases": []},
    "excel": {"name": "Excel", "aliases": []},
    "communication": {"name": "Communication", "aliases": []},
}


def _load_raw() -> dict:
    try:
        data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data else _FALLBACK
    except Exception:  # noqa: BLE001 - missing/corrupt data file must not break import
        return _FALLBACK


_RAW = _load_raw()
_CANONICAL: dict[str, str] = {
    cid: (spec.get("name") or cid.replace("_", " ").title()) for cid, spec in _RAW.items()
}


def _acceptable_surface(form: str) -> bool:
    """Reject surfaces that would over-match ordinary text. Forms with +, #, or . (c++, c#, .net,
    node.js) are always kept; purely-alphabetic forms must clear length/stopword guards."""
    if not form or len(form) <= 1:
        return False
    if form in _STOPWORDS:
        return False
    has_special = bool(re.search(r"[+#.]", form))
    if has_special:
        return True
    if form.isalpha() and len(form) == 2 and form not in _SHORT_ALLOW:
        return False
    return True


def _build_surface_map() -> dict[str, str]:
    """surface form (lowercase) -> canonical_id. Canonical name/id forms take priority over aliases."""
    index: dict[str, str] = {}

    def add(surface: str, cid: str) -> None:
        s = (surface or "").strip().lower()
        if _acceptable_surface(s):
            index.setdefault(s, cid)

    # Pass 1: canonical names + id-derived forms (highest priority).
    for cid, name in _CANONICAL.items():
        add(name, cid)
        add(cid.replace("_", " "), cid)
        add(cid, cid)
    # Pass 2: aliases (only if the surface isn't already claimed).
    for cid, spec in _RAW.items():
        for alias in spec.get("aliases", []) or []:
            add(alias, cid)
    return index


_SURFACE_TO_CID = _build_surface_map()
# Longest surfaces first so multi-word skills win over their substrings ("machine learning" > "learning").
_SORTED_SURFACES = sorted(_SURFACE_TO_CID, key=len, reverse=True)
_PATTERN = (
    re.compile(
        r"(?<![\w+#.])(?:" + "|".join(re.escape(s) for s in _SORTED_SURFACES) + r")(?![\w+#])(?!\.\w)"
    )
    if _SORTED_SURFACES
    else None
)


def canonical_name(skill_id: str) -> str:
    return _CANONICAL.get(skill_id, skill_id.replace("_", " ").title())


def all_canonical_ids() -> list[str]:
    return list(_CANONICAL)


def skill_count() -> int:
    return len(_CANONICAL)


def normalize_skills(text: str) -> list[str]:
    """Return the sorted set of canonical skill IDs found in `text` (single regex pass)."""
    if not text or _PATTERN is None:
        return []
    found: set[str] = set()
    for m in _PATTERN.finditer(text.lower()):
        cid = _SURFACE_TO_CID.get(m.group())
        if cid:
            found.add(cid)
    return sorted(found)
