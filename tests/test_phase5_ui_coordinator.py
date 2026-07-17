"""Coordinator dashboard (api/static/coordinator.html, docs/PHASE5.md §4, slice S9).

A static page has no runtime to assert against, so the tests here are a route smoke plus a
SOURCE-level escaping matrix over every value the page interpolates from a row.

The matrix is not ceremony. The repudiation queue (A1) renders `first`/`last`/`company` that arrived
from an unauthenticated public form — the store caps and redacts them at ingest, and this page is
where that text meets a signed-in coordinator's session. `esc()` is the second line of that defence
(SH-H1), so its absence has to fail a test rather than a review.

The escaping rule enforced below: every occurrence of a row-sourced expression must either sit
inside an `esc(` call opened within its own `${...}` interpolation, or be a control-flow guard
(`?`, `&&`, `||`, `===`) whose rendered branches are themselves checked by this same rule.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402

PAGE = Path(__file__).resolve().parents[1] / "resume_matcher" / "api" / "static" / "coordinator.html"


@pytest.fixture()
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    # an admin password IS set: the platform pages must still be reachable without the admin gate,
    # because coordinators authenticate with their own account cookie (auth._PLATFORM_PREFIXES)
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "not-the-coordinators-problem")
    return TestClient(create_app())


# ---- route smoke -----------------------------------------------------------------------------------
def test_coordinator_page_is_served_without_the_admin_gate(client):
    r = client.get("/coordinator", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Campus" in r.text


def test_coordinator_page_is_absent_when_the_platform_is_off(monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "0")
    assert TestClient(create_app()).get("/coordinator").status_code == 404


# ---- every S9 surface is actually wired -------------------------------------------------------------
_CARDS = ["healthCard", "alumniCard", "vouchCard", "repudCard", "ermCard", "codeCard",
          "rosterCard", "introOutcomes", "bellWrap"]


@pytest.mark.parametrize("card_id", _CARDS)
def test_new_cards_exist(page, card_id):
    assert f'id="{card_id}"' in page


_ENDPOINTS = [
    "/api/coordinator/under-networked",       # C1
    "/api/coordinator/mentors",               # C1 picker
    "/api/coordinator/mentorship-offers",     # C1 offer
    "/api/coordinator/mentorship-stats",      # C1 aggregates
    "/api/coordinator/mentor-match",          # C1 matcher
    "/api/coordinator/reports/intro-outcomes",  # C2
    "/api/coordinator/alumni",                # C4
    "/api/coordinator/orgs",                  # C5
    "/api/coordinator/vouches",               # B10
    "/api/coordinator/repudiations",          # A1
    "/api/notifications",                     # B4-lite
    "/checkin-code",                          # C3
    "/checkins",                              # C3 roster
]


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_endpoint_is_wired(page, endpoint):
    assert endpoint in page


# ---- the escaping matrix ----------------------------------------------------------------------------
_GUARDS = ("?", "&&", "||", "===", "!==")

# Every row-sourced expression this page renders. The first three are the public->admin stored-XSS
# surface (SH-H1); the rest are member/employer free text or DB enum values.
_DYNAMIC = [
    "r.first", "r.last", "r.company",          # A1 repudiation queue — PUBLIC input
    "v.subject_email", "v.voucher_email", "v.relationship", "v.verify_level",
    "v.evidence_redacted", "v.contested_note", "v.id",   # B10
    "s.email", "s.program", "m.email", "m.program",      # C1
    "c.email", "c.program",                              # C4
    "o.name", "o.link_status",                           # C5
    "a.email", "a.role", "a.org_name", "c.method",       # C3 roster
    "n.title", "n.body", "n.read_at",                    # B4-lite bell
]


def _escaped(page: str, needle: str) -> list[str]:
    """Occurrences of `needle` that are NEITHER inside an esc() opened in their own interpolation
    NOR a control-flow guard. Returns the offending snippets (empty = clean)."""
    bad = []
    for match in re.finditer(re.escape(needle) + r"\b", page):
        prefix = page[:match.start()]
        interp = prefix.rfind("${")
        if interp != -1 and "esc(" in prefix[interp:]:
            continue
        tail = page[match.end():match.end() + 6].strip()
        if any(tail.startswith(g) for g in _GUARDS):
            continue
        bad.append(page[max(0, match.start() - 40):match.end() + 40])
    return bad


@pytest.mark.parametrize("expr", _DYNAMIC)
def test_dynamic_values_are_escaped(page, expr):
    assert _escaped(page, expr) == []


def test_public_repudiation_fields_are_never_interpolated_raw(page):
    """SH-H1, said a second way: a future edit that drops esc() around the asserted name has to trip
    a test named after the finding, not just the generic matrix."""
    for field in ("first", "last", "company"):
        assert f"${{r.{field}}}" not in page
    assert "esc([r.first, r.last]" in page    # the asserted name is join()ed INSIDE esc()
    assert "esc(r.company" in page


def test_the_escaping_matrix_would_catch_an_unescaped_field(page):
    """The matrix is only worth having if it fails on a real regression — pin that it does."""
    sabotaged = page.replace("<td>${esc(r.company || '—')}</td>", "<td>${r.company}</td>")
    assert sabotaged != page, "the repudiation company cell moved — update this canary"
    assert _escaped(sabotaged, "r.company") != []


# ---- privacy invariants the page must uphold ---------------------------------------------------------
def test_no_grad_year_anywhere_on_the_page(page):
    """P-F2: grad_year is an age proxy that is never collected, so it can never be rendered."""
    assert "grad_year" not in page and "gradYear" not in page


def test_no_per_offer_mentorship_status_surface(page):
    """D8/P-F9: coordinators get MIN_CELL'd aggregates only. The mentor's inbox route has no business
    on this page — reading it would turn 'offer sent' into a decline oracle."""
    assert "/api/mentorship/offers" not in page
    assert "/api/coordinator/mentorship-stats" in page


def test_suppressed_cells_render_as_suppressed_not_zero(page):
    """A null cell is 'below the reporting floor', not 'zero' — the whole point of MIN_CELL."""
    assert "min_cell" in page
    assert "n&lt;" in page
