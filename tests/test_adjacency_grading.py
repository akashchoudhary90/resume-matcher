"""Adjacency credit + named-vs-demonstrated evidence grading (roadmap #3 / #3b).

Motivated by the measured 63-example opus baseline: adjacent-skills stratum scored 0.00 accuracy
(zero credit for PostgreSQL-for-MySQL etc.) and gamed-resumes 0.00 (a bare skills-dump scored
100.0). The rules under test: a demonstrated ADJACENT skill earns half credit (curated graph only —
the LLM proposes, code decides), and an evidence quote that is JUST the skill's name earns half
credit (named is not demonstrated). Every change stays inside the explainable, reconciling
breakdown; nothing auto-rejects.
"""
from resume_matcher.inference.schema import (
    CandidateProfile,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    SkillEvidence,
)
from resume_matcher.matching import ranker
from resume_matcher.matching.taxonomy import are_related, related_skills


def _cand(text):
    return CandidateProfile(candidate_id="C1", text=text, skills=[], years_experience=2.0)

def _job(required, must=None, preferred=None):
    return JobSpec(job_id="J1", title="T", employer="E", required_skills=required,
                   must_have_skills=must or [], preferred_skills=preferred or [])

def _ev(skill, status, span, adjacent_to=None):
    return SkillEvidence(skill_id=skill, skill_name=skill, status=status,
                         evidence_span=span, adjacent_to=adjacent_to)

def _extract(*evs):
    return MatchExtraction(candidate_id="C1", job_id="J1", skill_matches=list(evs))


# ---- taxonomy relations -------------------------------------------------------------------------

def test_relations_symmetric_and_curated():
    assert are_related("mysql", "postgresql") and are_related("postgresql", "mysql")
    assert are_related("aws", "gcp") and are_related("python", "matlab")
    assert not are_related("python", "photoshop")
    assert not are_related("python", "python")
    assert "postgresql" in related_skills("mysql")
    assert related_skills("nonexistent_skill") == ()


# ---- adjacency in the ranker --------------------------------------------------------------------

RESUME_PG = ("Built and maintained a PostgreSQL database for a student housing portal: designed the "
             "schema, wrote reporting queries with window functions, tuned slow joins.")

def test_valid_adjacency_earns_half_credit_with_explained_note():
    # NB: the quote must contain/evidence the ADJACENT skill (review hardening) — a span that never
    # names PostgreSQL can't be attributed to it.
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.partial,
                     "Built and maintained a PostgreSQL database", adjacent_to="postgresql")),
        _cand(RESUME_PG), _job(["mysql"]))
    assert res.fit_score == 50.0                                  # half of the 100-pt required bucket
    comp = res.explanation.components[0]
    assert comp.status == MatchStatus.partial and comp.verified
    assert "Adjacent skill demonstrated (PostgreSQL)" in comp.note
    assert any(f.startswith("adjacent_credit:mysql") for f in res.flags)
    # reconciliation invariant holds
    ex = res.explanation
    assert round(ex.subtotal * ex.education_factor * ex.experience_factor
                 * ex.must_have_factor * ex.integrity_factor, 1) == res.fit_score


def test_adjacency_never_earns_full_credit():
    # Model tries status=match with adjacent_to -> clamped to partial.
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.match,
                     "Built and maintained a PostgreSQL database", adjacent_to="postgresql")),
        _cand(RESUME_PG), _job(["mysql"]))
    assert res.fit_score == 50.0
    assert res.explanation.components[0].status == MatchStatus.partial


def test_invented_adjacency_is_refused():
    # photoshop is not related to mysql: claim discarded, zero credit, flagged.
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.partial,
                     "Built and maintained a PostgreSQL database", adjacent_to="photoshop")),
        _cand(RESUME_PG), _job(["mysql"]))
    assert res.fit_score == 0.0
    assert any(f.startswith("invalid_adjacency:mysql") for f in res.flags)
    assert res.discarded_matches and res.discarded_matches[0].skill_id == "mysql"


def test_adjacent_evidence_satisfies_must_have_gate():
    # A must-have covered by VALID adjacent evidence is not a missing deal-breaker (consistent with
    # plain partial evidence, which already lifts the gate). Humans rated exactly this pattern ok/65.
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.partial,
                     "Built and maintained a PostgreSQL database", adjacent_to="postgresql")),
        _cand(RESUME_PG), _job(["mysql"], must=["mysql"]))
    assert res.explanation.must_have_factor == 1.0
    assert not any(f.startswith("missing_must_have") for f in res.flags)


def test_adjacency_span_still_verified_verbatim():
    # The adjacency mechanism does not weaken anti-fabrication: an invented quote is still discarded.
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.partial,
                     "ten years of elite MySQL consulting", adjacent_to="postgresql")),
        _cand(RESUME_PG), _job(["mysql"]))
    assert res.fit_score == 0.0
    assert any(f.startswith("unverifiable_evidence:mysql") for f in res.flags)


# ---- named-vs-demonstrated (bare-mention clamp) -------------------------------------------------

DUMP = "Skills: Python, MySQL, Docker, Kubernetes, AWS, Excel, Communication, Leadership."

def test_bare_name_quote_is_downgraded_to_half_credit():
    res = ranker.score(
        _extract(_ev("python", MatchStatus.match, "Python")),
        _cand(DUMP), _job(["python"]))
    assert res.fit_score == 50.0                                   # not 100 for a naked list entry
    comp = res.explanation.components[0]
    assert comp.status == MatchStatus.partial
    assert "named" in comp.note.lower()
    assert any(f.startswith("bare_mention:python") for f in res.flags)


def test_list_fragment_quote_is_still_bare():
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.match, "Python, MySQL,")),
        _cand(DUMP), _job(["mysql"]))
    assert res.fit_score == 50.0
    assert any(f.startswith("bare_mention:mysql") for f in res.flags)


def test_demonstrating_quote_keeps_full_credit():
    text = "Tuned MySQL replication for the campus events app and cut page loads from 3s to 400ms."
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.match,
                     "Tuned MySQL replication for the campus events app")),
        _cand(text), _job(["mysql"]))
    assert res.fit_score == 100.0
    assert res.explanation.components[0].status == MatchStatus.match
    assert not any(f.startswith("bare_mention") for f in res.flags)


def test_skills_dump_no_longer_scores_perfect():
    # The gamed_01 shape: every job skill named in a list, nothing demonstrated. All bare mentions
    # -> every skill at half credit -> B-at-best territory, never a perfect score, and flagged.
    job = _job(["python", "mysql", "docker", "aws"])
    evs = [_ev(s, MatchStatus.match, s.capitalize() if s != "aws" else "AWS")
           for s in ["python", "mysql", "docker", "aws"]]
    res = ranker.score(_extract(*evs), _cand(DUMP), job)
    assert res.fit_score == 50.0
    assert sum(1 for f in res.flags if f.startswith("bare_mention:")) == 4
    assert "named" in res.explanation.summary.lower()


# ---- review regressions (adversarial findings, all reproduced then fixed) ------------------------

def test_trailing_period_does_not_defeat_bare_mention():
    # "Skills: Python, MySQL, Docker, AWS." — the sentence period glued to 'aws.' broke sequence
    # matching and the punctuation counted as residue; the plain dump scored 100 unflagged.
    text = "Skills: Python, MySQL, Docker, AWS."
    job = _job(["python", "mysql", "docker", "aws"])
    evs = [_ev(s, MatchStatus.match, text) for s in ["python", "mysql", "docker", "aws"]]
    res = ranker.score(_extract(*evs), _cand(text + " " + "x" * 250), job)
    assert res.fit_score == 50.0
    assert sum(1 for f in res.flags if f.startswith("bare_mention:")) == 4


def test_junk_decorated_dump_is_still_bare():
    # "Python ninja, MySQL guru, Docker wizard" — one junk word per name is decoration, not use.
    text = "Skills: Python ninja, MySQL guru, Docker wizard, AWS hero."
    job = _job(["python", "mysql", "docker", "aws"])
    evs = [_ev(s, MatchStatus.match, text) for s in ["python", "mysql", "docker", "aws"]]
    res = ranker.score(_extract(*evs), _cand(text + " " + "x" * 250), job)
    assert res.fit_score == 50.0
    assert sum(1 for f in res.flags if f.startswith("bare_mention:")) == 4


def test_demonstrated_list_of_tools_is_not_bare():
    # A real demonstration that happens to enumerate tools must keep full credit.
    text = "Built weekly executive dashboards in Tableau, Power BI, and Looker for three client teams."
    res = ranker.score(_extract(_ev("tableau", MatchStatus.match, text)), _cand(text),
                       _job(["tableau"]))
    assert res.fit_score == 100.0
    assert not any(f.startswith("bare_mention") for f in res.flags)


def test_gamed_fixtures_flag_through_the_real_pipeline():
    # End-to-end (mock adapter + evaluate): the eval set's gamed skills-dump fixtures must come out
    # flagged and below the A band — the exact shape that scored a clean 100 pre-fix.
    from resume_matcher.inference.adapter import get_adapter
    from resume_matcher.matching.benchmark import _candidate, _job_of, load_examples, resolve_dataset
    from resume_matcher.matching.evaluator import evaluate

    exs = {e["id"]: e for e in load_examples(resolve_dataset("coordinator"))}
    adapter = get_adapter("mock")
    for eid in ("gamed_01_it_support_skills_dump", "gamed_05_soc_cert_dump"):
        ex = exs[eid]
        res = evaluate(_candidate(ex["resume_text"]), _job_of(ex), adapter)
        assert res.fit_score < 80.0, f"{eid} scored {res.fit_score} (A band) through the pipeline"
        assert any(f.startswith("bare_mention:") for f in res.flags), f"{eid} not flagged"


def test_adjacency_quote_must_contain_the_adjacent_skill():
    # HIGH finding: any verbatim sentence + adjacent_to used to smuggle in credit and lift a
    # must-have gate with a false "demonstrated" note. The span must evidence the ADJACENT skill.
    text = "Worked as a barista for two years pulling espresso shots. " + "x" * 250
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.partial, "Worked as a barista", adjacent_to="postgresql")),
        _cand(text), _job(["mysql"], must=["mysql"]))
    assert res.fit_score == 0.0
    assert any(f.startswith("invalid_adjacency:mysql") for f in res.flags)
    assert res.explanation.must_have_factor < 1.0  # the gate is NOT lifted


def test_bare_adjacent_name_gets_honest_note_and_warn_flag():
    # A bare "Skills: PostgreSQL." adjacency still earns half credit (consistent with direct bare
    # mentions) but must be flagged and the note must NOT claim demonstration.
    text = "Skills: PostgreSQL. " + "x" * 250
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.partial, "Skills: PostgreSQL", adjacent_to="postgresql")),
        _cand(text), _job(["mysql"]))
    assert res.fit_score == 50.0
    comp = res.explanation.components[0]
    assert "NAMED" in comp.note and "demonstrated —" not in comp.note.split("(")[0]
    assert any(f.startswith("bare_mention:mysql") for f in res.flags)
    assert any(f.startswith("adjacent_credit:mysql") for f in res.flags)


def test_unicode_demonstrating_quote_is_not_bare():
    # CJK demonstration was ASCII-stripped to just "mysql" and mis-clamped.
    text = "用 MySQL 优化了订单查询性能，查询时间从三秒降到两百毫秒。" + "x" * 250
    res = ranker.score(
        _extract(_ev("mysql", MatchStatus.match, "用 MySQL 优化了订单查询性能")),
        _cand(text), _job(["mysql"]))
    assert res.fit_score == 100.0
    assert not any(f.startswith("bare_mention") for f in res.flags)


def test_stale_flags_do_not_survive_dedupe():
    # A bare duplicate beaten by a stronger full match must not leave a contradictory flag behind.
    text = "Tuned MySQL replication for the events app. Skills: MySQL. " + "x" * 250
    evs = [
        _ev("mysql", MatchStatus.match, "Skills: MySQL"),                      # bare
        _ev("mysql", MatchStatus.match, "Tuned MySQL replication for the events app"),  # real
    ]
    for order in (evs, list(reversed(evs))):
        res = ranker.score(_extract(*order), _cand(text), _job(["mysql"]))
        assert res.fit_score == 100.0
        assert not any(f.startswith("bare_mention") for f in res.flags)


def test_equal_partial_duplicates_are_order_independent():
    text = "Skills: PostgreSQL. Some SQLite tinkering. " + "x" * 250
    evs = [
        _ev("mysql", MatchStatus.partial, "Skills: PostgreSQL", adjacent_to="postgresql"),
        _ev("mysql", MatchStatus.partial, "Some SQLite tinkering", adjacent_to="sqlite"),
    ]
    a = ranker.score(_extract(*evs), _cand(text), _job(["mysql"]))
    b = ranker.score(_extract(*list(reversed(evs))), _cand(text), _job(["mysql"]))
    assert a.fit_score == b.fit_score
    assert a.explanation.components[0].note == b.explanation.components[0].note
    assert sorted(a.flags) == sorted(b.flags)


def test_relations_loader_tolerates_non_dict_json(tmp_path, monkeypatch):
    from resume_matcher.matching import taxonomy

    p = tmp_path / "rel.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(taxonomy, "_RELATIONS_PATH", p)
    assert taxonomy._load_relations() == {}


def test_must_have_only_jobspec_gets_adjacency_line_and_mock_proposal():
    # A raw JobSpec (no build_job_spec fold) listing a skill ONLY in must_have_skills must still be
    # promptable and assessable — the ranker scores it, so the proposal surface must cover it.
    from resume_matcher.inference.adapters.mock import MockAdapter
    from resume_matcher.inference.prompt import adjacency_lines

    job = JobSpec(job_id="J1", title="T", employer="E", required_skills=[],
                  must_have_skills=["mysql"], preferred_skills=[])
    assert "mysql" in adjacency_lines(job)
    ex = MockAdapter().extract(_cand(RESUME_PG), job)
    ev = next(e for e in ex.skill_matches if e.skill_id == "mysql")
    assert ev.adjacent_to == "postgresql"
    res = ranker.score(ex, _cand(RESUME_PG), job)
    assert res.fit_score == 50.0 and res.explanation.must_have_factor == 1.0


def test_mock_one_letter_surfaces_never_match():
    # "R" must not match inside R&D / R-squared (the boundary regex passes & and -).
    from resume_matcher.inference.adapters.mock import _find_span

    assert _find_span("Led R&D initiatives and computed R-squared fits.", "r") is None


# ---- mock adapter -------------------------------------------------------------------------------

def test_mock_proposes_adjacency_and_quotes_context():
    from resume_matcher.inference.adapters.mock import MockAdapter

    ex = MockAdapter().extract(_cand(RESUME_PG), _job(["mysql"]))
    ev = next(e for e in ex.skill_matches if e.skill_id == "mysql")
    assert ev.status == MatchStatus.partial and ev.adjacent_to == "postgresql"
    assert "PostgreSQL" in ev.evidence_span
    # and end-to-end through the ranker it earns the half credit
    res = ranker.score(ex, _cand(RESUME_PG), _job(["mysql"]))
    assert res.fit_score == 50.0


def test_mock_adjacency_respects_word_boundaries():
    # Regression: the one-letter skill "R" (related to python) must NOT match the letter r inside
    # "developer"/"years" — that produced phantom adjacency credit for candidates with no R at all.
    from resume_matcher.inference.adapters.mock import MockAdapter, _find_span

    text = "Java and SQL developer with Docker. 4 years experience."
    assert _find_span(text, "r") is None
    ex = MockAdapter().extract(_cand(text), _job(["python"]))
    assert not any(e.adjacent_to for e in ex.skill_matches)
    # One-letter surfaces are skipped entirely by the mock (taxonomy's own precision guard):
    # even a standalone "R" is not proposed — the real engine can still quote it, and the ranker's
    # _surface_present attributes it there.
    assert _find_span("Analysis in R and Python.", "r") is None


def test_mock_spans_carry_context_not_bare_names():
    from resume_matcher.inference.adapters.mock import MockAdapter

    text = "Tuned MySQL replication for the campus events app and cut page loads sharply."
    ex = MockAdapter().extract(_cand(text), _job(["mysql"]))
    ev = next(e for e in ex.skill_matches if e.skill_id == "mysql")
    assert len(ev.evidence_span) > len("MySQL") + 3                # windowed, not a naked name
    res = ranker.score(ex, _cand(text), _job(["mysql"]))
    assert res.fit_score == 100.0                                  # context quote -> full credit


# ---- prompt wiring ------------------------------------------------------------------------------

def test_prompt_lists_accepted_adjacencies_and_rules():
    from resume_matcher.inference.prompt import SYSTEM, build_messages

    msgs = build_messages(_cand("x"), _job(["mysql"], preferred=["tableau"]))
    user = msgs[1]["content"]
    assert "accepted adjacent skills" in user
    assert "postgresql" in user and "power_bi" in user
    assert "NAMED IS NOT DEMONSTRATED" in SYSTEM and "ADJACENT SKILLS" in SYSTEM


def test_prompt_omits_adjacency_section_when_no_relations():
    from resume_matcher.inference.prompt import build_messages

    msgs = build_messages(_cand("x"), _job(["communication"]))
    assert "accepted adjacent skills" not in msgs[1]["content"]


def test_schema_roundtrips_adjacent_to():
    import jsonschema

    from resume_matcher.inference.schema import match_extraction_schema

    ex = _extract(_ev("mysql", MatchStatus.partial, "PostgreSQL work", adjacent_to="postgresql"))
    jsonschema.validate(ex.model_dump(mode="json"), match_extraction_schema())


def test_pinned_schema_file_matches_live_contract():
    # The on-disk contract must never drift from the pydantic model (CI pin).
    import json

    from resume_matcher.inference.schema import SCHEMA_PATH, match_extraction_schema

    pinned = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert pinned == match_extraction_schema()
