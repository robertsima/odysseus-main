"""The context strip as a browser draws it: above the transcript, with the
target selector when a document is open, and never wider than the screen.

Bug caught: the chat's settings line sat under the composer, where a person
reading the conversation never saw who answered or what the chat could touch.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import SESSION_ID, assert_no_sideways_scroll, assert_usable, probe


pytestmark = pytest.mark.browser

SETTINGS = {
    "settings": {}, "approval_mode": "ask_risky", "approval_modes": ["auto", "ask_risky", "ask_all"], "shell_access": "sandbox",
    "context": {
        "who": {"kind": "agent", "name": "SDS Consulting Lead"}, "project": "Tall Tree Group", "document": None,
        "doc_target": "general",
        "access": {"summary": "File editing allowed · External actions require approval"},
    },
}


def _open(open_app, width, **kwargs):
    page = open_app(width, **kwargs)
    patches = []
    saved_settings = dict(SETTINGS["settings"])

    def settings(route):
        if route.request.method == "PATCH":
            patch = json.loads(route.request.post_data or "{}")
            patches.append(patch)
            saved_settings.update(patch)
        route.fulfill(json={**SETTINGS, "settings": saved_settings})

    page.route(f"**/api/session/{SESSION_ID}/settings", settings)
    page.evaluate("window.chatSettingsModule.refresh()")
    page.wait_for_selector("#chat-status-line .ctx-main", state="attached")
    page.wait_for_function("document.querySelector('#chat-status-line .ctx-project')")
    return page, patches


def _show_document(page):
    page.evaluate("""() => {
        window.documentModule.getCurrentDocId = () => 'doc-1';
        window.documentModule.getCurrentDocTitle = () => 'DA-02';
        window.chatSettingsModule.render();
    }""")


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("style", ["agamemnon", "classic"])
def test_the_strip_sits_above_the_transcript_and_fits(open_app, width, style):
    page, _ = _open(open_app, width, style=style)

    strip = assert_usable(page, "#chat-status-line")
    history = assert_usable(page, "#chat-history")
    assert strip["bottom"] <= history["top"] + 1
    assert page.evaluate("document.getElementById('chat-status-line').nextElementSibling.id") != "chat-status-line"
    assert page.inner_text("#chat-status-line .ctx-main").startswith("Tall Tree Group")
    assert probe(page, ".chat-input-bar + #chat-status-line", wait=False) is None
    assert strip["height"] <= (100 if width <= 390 else 76)  # two lines
    assert_no_sideways_scroll(page)


@pytest.mark.parametrize("width", [1440, 390])
def test_an_open_document_brings_the_selector_and_saves_the_pick(open_app, width):
    from playwright.sync_api import expect

    page, patches = _open(open_app, width)
    assert page.query_selector("#chat-status-line .ctx-seg") is None

    _show_document(page)

    assert page.query_selector("#chat-status-line .ctx-seg") is not None
    assert page.locator("#chat-status-line .ctx-doc").inner_text().endswith("DA-02")
    choices = page.get_by_role("radiogroup", name="Include the open document in messages")
    include = choices.get_by_role("radio", name="Include document", exact=True)
    exclude = choices.get_by_role("radio", name="Leave document out", exact=True)
    expect(choices.get_by_role("radio")).to_have_count(2)
    expect(include).to_have_attribute("aria-checked", "true")
    expect(exclude).to_have_attribute("aria-checked", "false")
    assert_no_sideways_scroll(page)
    right = page.evaluate("document.querySelector('#chat-status-line .ctx-seg').getBoundingClientRect().right")
    assert right <= width

    def is_settings_patch(response):
        return response.url.endswith(f"/api/session/{SESSION_ID}/settings") and response.request.method == "PATCH"

    with page.expect_response(is_settings_patch):
        exclude.click()
    expect(exclude).to_have_attribute("aria-checked", "true")
    expect(page.locator("#chat-status-line .ctx-doc")).to_have_count(0)

    assert patches == [{"doc_target": "general"}]

    # Arrow navigation must select, persist and keep focus after re-rendering.
    exclude.focus()
    with page.expect_response(is_settings_patch):
        exclude.press("ArrowLeft")
    expect(include).to_have_attribute("aria-checked", "true")
    expect(include).to_be_focused()
    expect(page.locator("#chat-status-line .ctx-doc")).to_have_count(1)

    assert patches == [{"doc_target": "general"}, {"doc_target": "document"}]


def test_the_composer_lost_the_indicators_that_moved(open_app):
    page, _ = _open(open_app, 1440)

    for node in ("chat-context-pill", "character-indicator-btn", "workspace-indicator-btn"):
        assert page.evaluate(f"!!document.getElementById('{node}').closest('#chat-status-line')"), node
        assert page.evaluate(f"!!document.getElementById('{node}').closest('.chat-input-bar')") is False, node

