"""The chat page (static/index.html and its stylesheets) as a browser draws it:
transcript, composer, progress shelf and heading.

These caught a composer collapsed to 28 px under the transcript and code
blocks squeezed by wide message gutters at 320 px.
"""
from __future__ import annotations

import re

import pytest

from tests.helpers.static_app import (
    ODYSSEUS_THEME, assert_no_sideways_scroll, assert_usable, expect, open_workbench, probe, settle, visible_panels,
)

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_transcript_and_composer_fill_the_screen_and_take_input(open_app, width):
    page = open_app(width)
    height = page.viewport_size["height"]
    history = assert_usable(page, "#chat-history")
    bar = assert_usable(page, ".chat-input-bar")
    message = assert_usable(page, "#message")
    heading = probe(page, ".ag-page-heading")
    # The composer sits under the transcript, wholly on screen, as wide as it.
    assert bar["top"] >= history["bottom"] - 1
    assert bar["bottom"] <= height + 1
    assert bar["width"] >= history["width"] - 2
    assert message["width"] >= bar["width"] * 0.6
    # The transcript is the part that grows, and it scrolls on its own.
    assert history["height"] >= (220 if width <= 390 else 330)
    assert history["overflowY"] == "auto" and history["scrollHeight"] > history["clientHeight"]
    assert heading["visible"] and heading["bottom"] <= history["top"]
    assert probe(page, ".ag-context-rail", wait=False) is None
    assert history["width"] >= (width - history["left"]) * 0.88
    assert_no_sideways_scroll(page)

    page.click("#message")
    assert page.evaluate("document.activeElement.id") == "message"
    page.keyboard.type("Check the rollback plan")
    # The page builds a second #message element; the composer is the first.
    assert page.evaluate("document.getElementById('message').value") == "Check the rollback plan"
    scrolled = page.evaluate("(() => { const h = document.getElementById('chat-history'); h.scrollTop = 0;"
                             " const top = h.scrollTop; h.scrollTop = h.scrollHeight; return [top, h.scrollTop]; })()")
    assert scrolled[0] == 0 and scrolled[1] > 200, scrolled


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_expanded_checklist_and_agents_share_a_bounded_shelf(open_app, width):
    page = open_app(width)
    page.wait_for_function("!document.getElementById('task-checklist').hidden"
                           " && !document.getElementById('agent-strip').hidden")
    expect(page.locator(".tc-toggle")).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".agent-strip-toggle")).to_have_attribute("aria-expanded", "true")
    settle(page, ".chat-progress-shelf")
    height = page.viewport_size["height"]
    shelf, history, bar = probe(page, ".chat-progress-shelf"), probe(page, "#chat-history"), probe(page, ".chat-input-bar")
    assert shelf["height"] <= (250 if width <= 1024 else 205), shelf
    assert history["height"] >= (180 if width <= 390 else 260), history
    assert_usable(page, "#message")
    assert bar["top"] >= shelf["bottom"] - 1 and bar["bottom"] <= height + 1
    # Side by side on a wide screen, stacked below that.
    checklist, agents = probe(page, "#task-checklist"), probe(page, "#agent-strip")
    assert (abs(checklist["top"] - agents["top"]) < 2) == (width >= 1440)
    for selector in (".tc-list", ".agent-strip-rows"):
        box = probe(page, selector)
        assert box["height"] <= (89 if width <= 1024 else 77) and box["overflowY"] == "auto", (selector, box)
    assert_no_sideways_scroll(page)
    page.click(".tc-toggle")
    expect(page.locator(".tc-toggle")).to_have_attribute("aria-expanded", "false")
    page.click(".agent-strip-toggle")
    expect(page.locator(".agent-strip-toggle")).to_have_attribute("aria-expanded", "false")


def test_heading_shows_the_selected_chat_model_and_response_state(open_app):
    page = open_app(1440)
    expect(page.locator("#ag-session-label")).to_have_text("Orders migration")
    expect(page.locator("#model-picker-label")).to_have_text("claude-sonnet-5")
    status = page.locator("#ag-chat-status")
    expect(status).to_have_text("")
    page.evaluate("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', {detail: {active: true}}))")
    expect(status).to_have_text(re.compile(r"\S"))
    page.evaluate("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', {detail: {active: false}}))")
    expect(status).to_have_text("")


def test_send_button_submits_the_typed_message(open_app):
    # The canned API cannot answer; tests/static/index/test_live_flows.py
    # covers the reply. This checks the visible control reaches the real
    # form handler with the typed text.
    page = open_app(1440)
    page.click("#message")
    page.keyboard.type("Review the rollout risks")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/chat_stream")) as sent:
        page.click(".send-btn")
    assert "Review the rollout risks" in (sent.value.post_data or "")


@pytest.mark.parametrize("width", [1440, 320])
def test_messages_and_code_blocks_stay_readable(open_app, width):
    page = open_app(width)
    if width == 1440:
        command = assert_usable(page, "#sidebar-command-toggle")
        assert command["height"] >= 44 and command["fontSize"] >= 14, command
    # Message cards fill the transcript instead of leaving wide gutters that
    # squeeze code blocks on a phone.
    history, message = probe(page, "#chat-history"), probe(page, "#chat-history .msg-ai")
    assert message["width"] >= history["width"] * 0.85
    assert_no_sideways_scroll(page, "#chat-history")
    # The copy/edit/run buttons sit above the code, top or bottom placement.
    for bottom in (False, True):
        code = page.evaluate("""(bottom) => { const p = document.querySelector('#chat-history pre:has(> .copy-code)');
            const actions = [...p.querySelectorAll('.copy-code,.edit-code,.run-code')];
            if (bottom) actions.forEach(b => b.classList.add('bottom'));
            return {padding: parseFloat(getComputedStyle(p).paddingTop),
                    buttonBottom: Math.max(...actions.map(b => b.getBoundingClientRect().bottom)),
                    codeTop: p.querySelector('code').getBoundingClientRect().top}; }""", bottom)
        assert code["padding"] >= 44 and code["codeTop"] >= code["buttonBottom"], code


@pytest.mark.parametrize("width", [1440, 390])
def test_odysseus_theme_keeps_its_own_layout(open_app, width):
    page = open_app(width, theme=ODYSSEUS_THEME)
    assert page.evaluate("document.documentElement.dataset.theme") == "odysseus"
    for selector in (".ag-page-heading", ".ag-context-rail"):
        assert not (probe(page, selector, wait=False) or {}).get("visible"), selector
    bar = assert_usable(page, ".chat-input-bar")
    assert_usable(page, "#message")
    assert bar["width"] <= 802  # the base composer keeps its 800 px measure
    open_workbench(page, activity=False)
    assert not (probe(page, ".ag-run-control", wait=False) or {}).get("visible")
    assert visible_panels(page) == ["changes"]
    page.click('#workbench-modal [data-wb-tab="commits"]')
    assert visible_panels(page) == ["commits"]
    page.click('#workbench-modal [data-wb-tab="changes"]')
    assert visible_panels(page) == ["changes"]
    assert_no_sideways_scroll(page)
