"""Review changes sheet in the real page: it fits the viewport at phone width.

Bug guarded: a long table row or an unbroken placeholder pushed the sheet
wider than the screen, so the Export button fell off the right edge.
"""
import json
import os

import pytest

from tests.helpers.static_app import (
    LIGHT_THEME, ODYSSEUS_THEME, assert_inside_parent, assert_no_sideways_scroll, expect, settle,
)

pytestmark = pytest.mark.browser

OLD = """# Services agreement

## 1. Scope

Design work for the portal.

## 2. Fees

| Item | Fee |
|---|---|
| Design | 1000 |
| Hosting, per month, billed in advance and excluding VAT unless stated | 40 |

## 3. Term

Twelve months from signature.
"""
LIVE = """# Services agreement

## 1. Scope

Design work for the portal, see [[annex_with_a_very_long_name_that_cannot_wrap_anywhere_at_all_ever]].

## 2. Fees

| Item | Fee |
|---|---|
| Design | 1200 |
| Hosting, per month, billed in advance and excluding VAT unless stated | 40 |

## 3. Term

Twelve months from signature. Renewal: TBD
"""
VERSIONS = [
    {"id": "b", "version_number": 2, "content": LIVE, "summary": None, "source": "ai", "created_at": "2026-10-23T10:00:00"},
    {"id": "a", "version_number": 1, "content": OLD, "summary": None, "source": "user", "created_at": "2026-10-22T10:00:00"},
]
SHOTS = os.environ.get("ODYSSEUS_SHOT_DIR")


def open_review(open_app, width, theme, style):
    page = open_app(width, theme=theme, style=style, chat=False)
    page.route("**/api/document/agr/versions", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(VERSIONS)))
    page.evaluate("""(live) => {
        const m = window.documentModule;
        m.openPanel();
        m.handleDocUpdate({doc_id: 'agr', action: 'create', title: 'Agreement', language: 'markdown', content: live, version: 2});
    }""", LIVE)
    page.wait_for_selector("#doc-footer-export-btn", state="visible")
    # The mock page leaves the footer partly below the fold on a phone.
    page.evaluate("""() => document.getElementById('doc-footer-export-btn').dispatchEvent(
        new MouseEvent('click', {bubbles: true, cancelable: true, clientX: 200, clientY: 300}))""")
    first = page.locator("#doc-export-menu .doc-overflow-item").first
    expect(first).to_have_text("Review changes")
    first.evaluate("e => e.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: 200, clientY: 300}))")
    page.wait_for_selector("#doc-review-panel .dr-ph")
    return page


# Both page styles in both palettes; the stored theme is what flips the palette.
LOOKS = [("agamemnon", "dark", None), ("agamemnon", "light", LIGHT_THEME),
         ("classic", "dark", ODYSSEUS_THEME), ("classic", "light", LIGHT_THEME)]


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("style,mode,stored", LOOKS)
def test_review_sheet_fits_the_viewport(open_app, width, style, mode, stored):
    page = open_review(open_app, width, stored, style)
    if mode == "light":
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    page.locator("#doc-review-panel details.dr-section").first.evaluate("e => e.open = true")
    settle(page, "#doc-review-panel")
    assert_no_sideways_scroll(page, "#doc-review-panel .dr-body")
    assert_inside_parent(page, [(".dr-foot .dr-btn", "#doc-review-panel")])
    box = page.locator("#doc-review-panel").bounding_box()
    assert box["x"] >= -1 and box["x"] + box["width"] <= width + 1
    if width <= 700:
        assert box["width"] >= width - 1, "full width on a phone"
    for btn in page.locator("#doc-review-panel .dr-foot .dr-btn").all():
        b = btn.bounding_box()
        assert b["x"] + b["width"] <= width + 1
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/review-panel-{style}-{width}-{mode}.png")
