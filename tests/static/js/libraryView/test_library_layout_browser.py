"""The Library's All view in a real browser (static/js/libraryView.js,
static/library.css), with /api/library/items stubbed at the network.

A card layout can pass every string check and still overflow the modal on a
phone, push a long title past the card, or leave the History disclosure inert
once the CSS is applied; only a browser shows that.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import assert_no_sideways_scroll, expect

pytestmark = pytest.mark.browser

LONG = "Tall Tree — Initial Server Setup and Hosting Agreement, Schedule A Pricing"
FOLDER = "Projects/Tall Tree Group/Agreements"


def _file(name, type_, **extra):
    return {"id": f"file:{FOLDER}/{name}", "kind": "file", "title": name, "type": type_,
            "path": f"{FOLDER}/{name}", "updated_at": "2026-10-20T10:00:00Z", **extra}


ITEMS = {
    "items": [
        {
            "id": "group:a", "kind": "group", "title": LONG, "type": "markdown", "role": "master",
            "project": "Tall Tree Group", "breadcrumb": ["Projects", "Tall Tree Group", "Agreements"],
            "path": f"{FOLDER}/Setup.md", "updated_at": "2026-10-22T08:00:00Z", "locked": False,
            "actions": ["open", "edit", "download", "history"],
            "group": {
                "master": _file("Setup.md", "markdown"), "current_export": _file("Setup.pdf", "pdf"),
                "signed": [],
                "history": [_file("Backups/Setup v2.md", "markdown", title="Setup v2"),
                            _file("Backups/Setup v1.md", "markdown", title="Setup v1")],
                "supporting": [_file("Receipts/setup.sha256", "other", title="setup.sha256")],
                "warnings": ["word_newer_than_master"],
            },
        },
        {
            "id": "group:b", "kind": "group", "title": "Tall Tree MSA", "type": "pdf", "role": "signed",
            "project": "Tall Tree Group", "breadcrumb": ["Projects", "Tall Tree Group", "Agreements"],
            "path": f"{FOLDER}/MSA signed.pdf", "updated_at": "2026-10-19T08:00:00Z", "locked": True,
            "actions": ["open", "download"],
            "group": {"master": None, "current_export": None, "signed": [_file("MSA signed.pdf", "pdf")],
                      "history": [], "supporting": [], "warnings": []},
        },
        {"id": "note:n1", "kind": "note", "title": "Call notes", "type": "note", "breadcrumb": [],
         "updated_at": "2026-10-21T09:00:00Z", "locked": False, "actions": ["open"]},
    ],
    "total": 3, "projects": ["Tall Tree Group"], "vault_shared": True,
}


def _open_library(page):
    page.route("**/api/library/items**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(ITEMS)))
    page.evaluate("document.getElementById('tool-library-btn').click()")
    page.wait_for_selector("#doclib-modal .lib-card")


@pytest.mark.parametrize("width", [1440, 390])
def test_cards_sit_inside_the_modal_without_sideways_scroll(open_app, width):
    page = open_app(width)
    _open_library(page)
    box = page.evaluate("""() => {
      const m = document.querySelector('#doclib-modal .modal-content').getBoundingClientRect();
      const cards = [...document.querySelectorAll('#doclib-modal .lib-card')].map(c => c.getBoundingClientRect());
      const chips = document.getElementById('doclib-filters');
      return {m: [m.left, m.right], cards: cards.map(r => [r.left, r.right, r.width]),
              chipScroll: [chips.scrollWidth, chips.clientWidth]};
    }""")
    assert len(box["cards"]) == 3
    for left, right, w in box["cards"]:
        assert left >= box["m"][0] - 1 and right <= box["m"][1] + 1, box
        assert w > 150, box
    assert_no_sideways_scroll(page, "#doclib-all-list")
    if width == 390:
        # The chips scroll inside their own row; the page does not.
        assert_no_sideways_scroll(page, None)


def test_history_disclosure_toggles(open_app):
    page = open_app(1440)
    _open_library(page)
    toggle = page.locator("#doclib-modal .lib-history-toggle").first
    panel = page.locator("#doclib-modal .lib-history").first
    expect(panel).to_be_hidden()
    toggle.click()
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Setup v2")
    toggle.click()
    expect(panel).to_be_hidden()


def test_signed_copy_has_no_edit_and_notice_is_one_line(open_app):
    page = open_app(1440)
    _open_library(page)
    signed = page.locator("#doclib-modal .lib-card", has_text="Tall Tree MSA")
    expect(signed).to_contain_text("Signed copy")
    expect(signed.get_by_role("button", name="Edit")).to_have_count(0)
    first = page.locator("#doclib-modal .lib-card").first
    expect(first.locator(".lib-notice")).to_have_text(
        "The Word copy is newer than the master. Open both to compare.")


def test_project_filter_chip_filters_and_clears(open_app):
    page = open_app(1440)
    seen = []

    def stub(route):
        seen.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(ITEMS))

    page.route("**/api/library/items**", stub)
    page.route("**/api/library/projects**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"projects": [
            {"name": "Tall Tree Group", "path": "Projects/Tall Tree Group",
             "counts": {"documents": 2, "files": 9, "notes": 1}, "updated_at": "2026-10-22T08:00:00Z"}]})))
    page.evaluate("document.getElementById('tool-library-btn').click()")
    page.wait_for_selector("#doclib-modal .lib-card")
    page.click("[data-lib-filter=projects]")
    page.click(".lib-project-card")
    chip = page.locator(".lib-project-chip")
    expect(chip).to_contain_text("Project: Tall Tree Group")
    assert any("project=Tall+Tree+Group" in u for u in seen), seen
    chip.locator(".lib-chip-x").click()
    expect(chip).to_have_count(0)
