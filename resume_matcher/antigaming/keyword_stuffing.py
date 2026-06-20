"""Keyword-stuffing and verbatim job-description echo detection.

Pure statistical checks (no LLM, so they cannot themselves be injected). Flags resumes that spam a
term to game keyword matching, or that paste long verbatim runs of the job description back at the
screener. Advisory only.
"""
from __future__ import annotations

import re

from ..inference.schema import JobSpec

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z+#.]*")
_STOP = {
    "and", "the", "for", "with", "you", "your", "our", "are", "will", "have", "this", "that",
    "from", "their", "they", "able", "work", "team", "role", "job", "who", "all", "any", "can",
}


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def repetition_flag(text: str, ratio_threshold: float = 0.06, min_count: int = 6) -> str | None:
    toks = [t for t in _tokens(text) if t not in _STOP and len(t) > 2]
    if len(toks) < 40:
        return None
    counts: dict[str, int] = {}
    for t in toks:
        counts[t] = counts.get(t, 0) + 1
    term, cnt = max(counts.items(), key=lambda kv: kv[1])
    if cnt >= min_count and cnt / len(toks) >= ratio_threshold:
        return f"stuffing:repetition:{term}x{cnt}"
    return None


def jd_echo_flag(text: str, job: JobSpec, ngram: int = 8) -> str | None:
    """Flag if any contiguous `ngram`-word run from the job description appears verbatim in text."""
    jd = _tokens(job.description)
    if len(jd) < ngram:
        return None
    resume = " " + " ".join(_tokens(text)) + " "
    for i in range(len(jd) - ngram + 1):
        phrase = " ".join(jd[i : i + ngram])
        if f" {phrase} " in resume:
            return "stuffing:jd_echo"
    return None


def scan_keyword_stuffing(text: str, job: JobSpec) -> list[str]:
    flags = []
    if (f := repetition_flag(text)):
        flags.append(f)
    if (f := jd_echo_flag(text, job)):
        flags.append(f)
    return flags
