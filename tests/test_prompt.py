"""Prompt construction: untrusted-resume fencing + demonstrated-skill guidance (audit #9)."""
from resume_matcher.inference.prompt import RESUME_FENCE, SYSTEM, build_messages
from resume_matcher.inference.schema import CandidateProfile, JobSpec


def test_resume_is_fenced_as_untrusted():
    msgs = build_messages(
        CandidateProfile(candidate_id="C", text="Built REST APIs."),
        JobSpec(job_id="J", title="t", employer="e", required_skills=["python"]),
    )
    user = msgs[-1]["content"]
    assert user.count(RESUME_FENCE) == 2  # resume body fenced top and bottom
    assert "Built REST APIs." in user


def test_system_instructs_demonstrated_not_named():
    # The model must credit demonstrated-not-named skills, quoting the demonstrating phrase verbatim.
    assert "DEMONSTRATED" in SYSTEM
    assert "verbatim" in SYSTEM.lower()
