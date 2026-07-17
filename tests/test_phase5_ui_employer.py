"""Employer UI (api/static/employer.html) — Phase-5 slice S8.

The page is a static file, so these are structural tests, not browser tests: they pin the contract
between the page and the routes it calls (A18's harvest, A9's transition map) and the escaping
discipline every dynamic render owes. A structural test earns its keep here because both defects
this slice fixes were invisible ones — a rendered input nobody harvested (A18), and a transition map
the UI only implemented one arm of (A9). Neither showed up as an error; both just quietly lost work.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402

PAGE = Path(__file__).resolve().parents[1] / "resume_matcher" / "api" / "static" / "employer.html"


@pytest.fixture()
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    return TestClient(create_app())


def _collect_body(page: str) -> str:
    """The collect() harvest — everything from `function collect()` to its closing brace."""
    start = page.index("function collect()")
    return page[start:page.index("\n}", start)]


# ---- route smoke -----------------------------------------------------------------------------------
def test_employer_page_served_without_the_admin_gate(client):
    """/employer is in auth._PLATFORM_PREFIXES: employers hold accounts, not the admin password."""
    resp = client.get("/employer")
    assert resp.status_code == 200
    assert "Campus" in resp.text
    assert resp.text.lstrip().startswith("<!DOCTYPE html>")


def test_page_absent_when_platform_disabled(monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "0")
    assert TestClient(create_app()).get("/employer").status_code in (401, 404)


# ---- A18: the apply-email regression ---------------------------------------------------------------
def test_apply_email_input_is_harvested_by_collect(page):
    """A18: the input has been rendered since the review pane shipped; collect() never read it."""
    assert 'data-f="application_email"' in page          # the input still exists
    assert "application_email: get('application_email')" in _collect_body(page)


@pytest.mark.xfail(reason="A18 is only half-fixable inside S8: the UI now harvests "
                          "application_email, but `postings` has no such column and _EDITABLE "
                          "(stores/platform.py) drops it, so the value still lands nowhere. "
                          "Flips to XPASS when the column + _EDITABLE entry land.",
                   strict=False)
def test_submitted_posting_carries_application_email(client, tmp_path, monkeypatch):
    """The S8 key test from docs/PHASE5.md §7. It is written to the INTENDED end state and pinned
    xfail, so the day the column lands this reports XPASS instead of staying quietly missing."""
    monkeypatch.setenv("RM_JD_CORRECTIONS_PATH", str(tmp_path / "corrections.jsonl"))
    from resume_matcher.api.accounts import get_account_store

    token, _ = get_account_store().register("s8@acme.com", "password123",
                                            role="employer", org_name="Acme S8")
    client.cookies.set("rm_session", token)
    made = client.post("/api/postings", json={
        "fields": {"title": "Analyst", "description": "d", "application_method": "email",
                   "application_email": "apply@acme.com"}, "skills": []})
    assert made.status_code == 201
    posting = client.get(f"/api/postings/{made.json()['posting_id']}").json()
    assert posting["application_email"] == "apply@acme.com"


def test_every_rendered_field_input_is_harvested(page):
    """The A18 class of bug, generalized: an input the employer can type into but nothing collects
    is silent data loss. Every input the review pane renders must appear in collect()."""
    fields_block = page[page.index("const FIELDS = ["):page.index("function fieldValue")]
    # FIELDS drives `data-f="${key}"`; `pay`/`application` are composites that render the literal
    # data-f sub-inputs instead of a field of their own.
    rendered = set(re.findall(r"\['(\w+)',", fields_block)) - {"pay", "application"}
    rendered |= set(re.findall(r'data-f="([a-z_]+)"', page))
    harvested = set(re.findall(r"get\('([a-z_]+)'\)", _collect_body(page)))
    # `employer_name` is the org's own name — owned by the account, not the posting form.
    assert rendered - harvested == {"employer_name"}


# ---- A9: per-stage actions -------------------------------------------------------------------------
def test_stage_actions_mirror_the_store_transition_map(page):
    """A9: the UI offered only applied->shortlisted. The map here must match students.py exactly —
    including 'withdrawn' being absent, since B7 makes that the student's own move (the API 409s)."""
    from resume_matcher.stores.students import _APP_TRANSITIONS

    block = page[page.index("const APP_ACTIONS"):page.index("function appActions")]
    for status, allowed in _APP_TRANSITIONS.items():
        offered = set(re.findall(rf"{status}:\s*\[(.*?)\]\s*,\n", block, re.S))
        assert offered, f"{status} offers no actions"
        targets = set(re.findall(r"\['(\w+)'", offered.pop()))
        assert targets == allowed - {"withdrawn"}, status
    assert "withdrawn'," not in block
    assert "data-advance" not in page      # the lone Shortlist button is gone


# ---- surfaces this slice adds ----------------------------------------------------------------------
@pytest.mark.parametrize("needle", [
    "/api/postings/${pid}/applications",          # A10 applicants view
    "/api/notifications?unread=1",                # B4 bell badge
    "/api/messages/unread-count",                 # B4 message badge
    "/api/notifications/read",                    # B4 mark-all-read
    "/api/orgs/me/contacts",                      # C5 contacts CRUD
    "/contact`, {method:'PUT'",                   # C5 posting contact set
    "/checkin`, {method:'POST'",                  # C3 self check-in
    "{method:'PATCH', body: JSON.stringify(collect())}",   # B8 edit save
])
def test_surface_calls_its_route(page, needle):
    assert needle in page


def test_edit_button_only_on_editable_postings(page):
    """B8: the API only lets an employer edit draft/rejected — offering Edit on a live posting
    would be a button that always 409s."""
    block = page[page.index("async function loadMine()"):page.index("$('#mineTable').addEventListener")]
    edit_line = next(ln for ln in block.splitlines() if "data-edit" in ln)
    assert "p.status === 'draft' || p.status === 'rejected'" in block[:block.index(edit_line)]


# ---- escaping --------------------------------------------------------------------------------------
@pytest.mark.parametrize("expr", [
    "a.candidate_ref", "a.status", "a.email",          # A10 applicant rows
    "c.display_label", "c.role_title || '—'", "c.contact_id",   # C5 contacts
    "i.title", "i.body",                               # B4 notification feed
])
def test_dynamic_values_render_through_esc(page, expr):
    """Contacts are already redacted + HTML-escaped at write (P-F5) and notification titles are
    server-composed — they are escaped here anyway. A render that trusts the write path to have
    escaped is one migration away from being an XSS sink."""
    assert f"esc({expr})" in page


@pytest.mark.parametrize("section", [
    "async function openApplicants",     # A10
    "async function loadContacts",       # C5
    "$('#bellBtn').onclick",             # B4
    "function appActions",               # A9
])
def test_no_unescaped_interpolation_in_html_templates(page, section):
    """Every `${x}` inside an HTML-emitting template literal must be esc()'d or a safe scalar.
    Only markup-bearing literals are scanned — a `${pid}` in a fetch URL is a different context."""
    # Rule: an interpolation that reads a value off an object (`row.field`) is emitting data, so it
    # must route through esc(), a date format, or appActions() (which esc()s the id it embeds).
    # A ternary CONDITION reads data without emitting it, so only the branches are checked.
    emits_data = re.compile(r"\b[a-z]\w*\.\w+")
    sanitizer = re.compile(r"esc\(|new Date\(|appActions\(")
    body = page[page.index(section):page.index("\n}", page.index(section))]
    for literal in re.findall(r"`([^`]*)`", body):
        if "<" not in literal:
            continue                     # a fetch URL or plain string — a different context
        for expr in re.findall(r"\$\{([^{}]*)\}", literal):
            branches = expr.split("?", 1)[1] if "?" in expr else expr
            if emits_data.search(branches) and not sanitizer.search(branches):
                pytest.fail(f"unescaped interpolation in {section}: ${{{expr.strip()}}}")
