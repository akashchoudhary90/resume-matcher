"""Phase-5 student + public UI (docs/PHASE5.md §4, slice S7).

Two things are worth testing about a static page, and neither is "does the JS run":

  * **Route smoke** — /repudiate is PUBLIC (B3). The page exists for someone with no account, so
    it must clear the admin gate (auth._PLATFORM_PREFIXES) and the account gate alike. A redirect
    or a 401 here is the whole feature failing.
  * **The escaping matrix** — every dynamic render on these pages goes through esc(). The pages are
    the last hop for employer free text (posting titles), peer free text (vouch evidence, class
    labels) and, on the coordinator page, third-party text typed by an anonymous stranger. The
    check below reads the source and fails on an unescaped interpolation of an API field, which is
    the only way to test this without a browser.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402

_STATIC = Path(__file__).resolve().parents[1] / "resume_matcher" / "api" / "static"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    return TestClient(create_app())


# ---- route smokes ----------------------------------------------------------------------------------
def test_repudiate_page_is_public_and_never_redirects_to_a_sign_in(client):
    """B3: a non-member data subject has no account and no admin password. If this page ever sits
    behind either gate, the removal right it exists to serve is unreachable."""
    r = client.get("/repudiate", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "/api/graph/repudiate" in body and "/api/graph/repudiate/confirm" in body
    # no nav chrome back into the signed-in app, and nothing that smells like a login wall
    assert "/login" not in body and "/api/account/login" not in body


def test_repudiate_page_promises_neither_deletion_nor_membership(client):
    """§8: both paths answer 202 with an identical shape whether or not anything matched. Copy that
    said "we found you" would hand back the membership oracle the queue exists to remove — and copy
    that said "deleted" would be a lie, since the submit only ever enqueues."""
    body = client.get("/repudiate").text
    assert "If that address is one we can act on" in body   # neutral: no hit/miss distinction
    assert "Nothing has been removed yet" in body           # the submit is not the deletion
    assert "queued" not in body.lower() or "review" in body.lower()


def test_repudiate_page_is_served_only_when_the_platform_is_on(monkeypatch):
    """The backing routes only exist under RM_PLATFORM_ENABLED, so the page must not outlive them."""
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "0")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "")
    assert TestClient(create_app()).get("/repudiate").status_code in (401, 404)


def test_student_page_serves(client):
    r = client.get("/student", follow_redirects=False)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


# ---- the escaping matrix ---------------------------------------------------------------------------
# Fields that MUST be escaped wherever a template literal renders them: every one is free text
# authored by somebody other than the viewer — an employer (posting title, org name), a peer (vouch
# evidence, class label, intro note), or the server quoting one of them back.
#
# A blanket "every ${...} is esc()'d" rule was tried and rejected: it cannot tell a render from a
# fetch URL or a ternary's condition, so it needs an allowlist long enough to rubber-stamp the very
# regression it exists to catch. This sweep is narrower and sharper — it names the dangerous fields
# and fails if one of them appears in an interpolation with no esc() in sight.
_MUST_ESCAPE = (
    "title", "org_name", "body", "email", "student_email", "subject_email", "voucher_email",
    "voucher_role", "label_display", "rationale", "topics", "program", "relationship",
    "evidence_redacted", "note_redacted", "email_masked", "claim_role", "requester_email",
    "relationship_hint", "location", "kind", "term", "filename",
)


def _renderable_source(page: str) -> str:
    """The page's source minus every `.textContent` STATEMENT (split on ';', not on newlines — the
    resume-meta assignment spans two lines and a line filter would let its interpolation through).

    textContent assignments are escaped by construction: the browser never parses them as markup,
    so they are the one place a bare `${me.email}` is fine. Everything else that interpolates is on
    its way to innerHTML and answers to the rule."""
    source = (_STATIC / page).read_text(encoding="utf-8")
    return ";".join(stmt for stmt in source.split(";") if ".textContent" not in stmt)


def _interpolations(source: str) -> list[str]:
    """Every ${...} in the source, brace-balanced (so a nested ${} rides along with its parent)."""
    out, i = [], 0
    while (i := source.find("${", i)) != -1:
        depth, j = 1, i + 2
        while j < len(source) and depth:
            depth += (source[j] == "{") - (source[j] == "}")
            j += 1
        out.append(source[i + 2:j - 1])
        i = j
    return out


# The two accepted escapers. escStored() is esc() for text the STORE already HTML-escaped at write
# (phase5._safe_text) — it decodes once before escaping so a club called "R&D" doesn't render as
# "R&amp;D". Both end in an escape; nothing else counts.
_ESCAPER = re.compile(r"\besc\(|\bescStored\(")


@pytest.mark.parametrize("field", _MUST_ESCAPE)
def test_free_text_fields_are_never_interpolated_without_esc(field):
    """`${v.evidence_redacted}` is the realistic regression — a reviewer adding a card reaches for
    the bare property far sooner than they defeat esc() on purpose. Any interpolation mentioning a
    listed field must run it through an escaper."""
    unsafe = [
        expr for page in ("student.html", "repudiate.html")
        for expr in _interpolations(_renderable_source(page))
        if re.search(r"\.%s\b" % re.escape(field), expr) and not _ESCAPER.search(expr)
    ]
    assert unsafe == [], f"{field} interpolated with no escaper: {unsafe}"


def test_the_store_escaped_wrapper_still_escapes():
    """escStored() decodes before escaping, so it is the one place a mistake would be invisible:
    if it ever returned t.value raw, the affiliation label would go to innerHTML unescaped and the
    decode step would have *created* the XSS it exists to avoid."""
    source = (_STATIC / "student.html").read_text(encoding="utf-8")
    body = source[source.index("const escStored"):source.index("const CONSENT_LABELS")]
    assert "return esc(t.value)" in body            # escapes on the way out
    assert "textarea" in body                       # RCDATA: the decode itself can't execute


def test_alumni_claim_sends_no_body_at_all():
    """P-F2/SC-C2: the claim route takes NO body — not a grad year, not a user id, not a status.
    The UI must not grow one: a grad year is an age proxy, and a client-supplied status or subject
    is the privilege-escalation the route hard-codes its way out of."""
    source = (_STATIC / "student.html").read_text(encoding="utf-8")
    call = re.search(r"api\('/api/alumni/claim',\s*\{([^}]*)\}", source)
    assert call is not None, "the alumni claim call moved — re-check what it now sends"
    assert "body" not in call.group(1)


def test_no_grad_year_input_lives_in_the_alumni_or_mentor_surfaces():
    """The pre-existing student_profiles.grad_year field (and its profile input) survives Phase 5 —
    S1 deliberately kept it. What must not exist is a grad-year anywhere in the C4 surfaces."""
    source = (_STATIC / "student.html").read_text(encoding="utf-8")
    for card in ("mentorCard", "alumniBtn"):
        assert card in source
    mentor = source[source.index('id="mentorCard"'):source.index('id="affilCard"')]
    assert "grad" not in mentor.lower()
    # exactly one grad-year input on the page, and it belongs to the pre-existing profile form
    assert source.count('id="gradYear"') == 1
    assert 'id="gradYear"' in source[source.index('id="profileCard"'):source.index('id="alumniMsg"')]


def test_student_page_links_the_public_repudiation_page():
    """B3 pointer (§4): the network card is where a member's contacts get uploaded, so it is where
    a non-member reader needs the way out."""
    source = (_STATIC / "student.html").read_text(encoding="utf-8")
    assert 'href="/repudiate"' in source


def test_mentorship_decline_ui_promises_no_notification():
    """D8/P-F9: the decline is silent to the student and invisible to coordinators. A confirm dialog
    saying "we'll let them know" would be the UI lying about the boundary the stores hold."""
    source = (_STATIC / "student.html").read_text(encoding="utf-8")
    assert "Nobody is told either way." in source
