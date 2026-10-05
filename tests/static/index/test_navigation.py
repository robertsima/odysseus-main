"""Navigation in the page shell (static/index.html, static/js/sidebar-layout.js,
static/js/navOrder.js): the Command menu, the sidebar on phones, the rail
order, and the product names the Agamemnon identity puts on them.

Command menu items are identified by ``data-command-target``, not by their
wording: an earlier version pinned the "New chat" label and broke when it was
reworded with no change in behavior.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import (
    LIGHT_THEME, ODYSSEUS_THEME, assert_no_sideways_scroll, assert_usable, expect, open_workbench, probe, settle,
    visible_panels, wait_ready,
)

pytestmark = pytest.mark.browser

HELMET = "/static/branding/agamemnon-trojan-helmet.svg"
COMMANDS = ["rail-email", "rail-agents", "rail-workbench", "rail-new-session", "rail-search-btn"]


def show_command_toggle(page) -> None:
    """On narrow screens the sidebar starts closed; open it."""
    shown = ("(() => { const r = document.getElementById('sidebar-command-toggle').getBoundingClientRect();"
             " return r.width > 0 && r.left >= 0; })()")
    if not page.evaluate(shown):
        page.evaluate("window._odyOpenSidebar('left')")
        page.wait_for_function(shown)
    settle(page, "#sidebar-command-toggle")


def run_command(page, target: str) -> None:
    page.click("#sidebar-command-toggle")
    page.click(f'#sidebar-command-menu [data-command-target="{target}"]')


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_command_menu_opens_the_phalanx_and_the_workbench_under_their_names(open_app, width):
    page = open_app(width)
    # The Agamemnon identity names the chat Scribe and the agents Phalanx.
    expect(page.locator(".ag-page-heading h1")).to_have_text("Scribe")
    assert page.title() == "Scribe — Agamemnon"
    assert page.evaluate("document.getElementById('message').placeholder") == "Message Scribe…"
    assert page.evaluate("document.querySelector('link[rel=icon]').getAttribute('href')") == HELMET
    assert probe(page, "#ag-open-theme", wait=False) is None
    assert page.evaluate("getComputedStyle(document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')).display") != "none"
    assert page.evaluate("document.getElementById('sidebar-command-menu').hidden") is True

    show_command_toggle(page)
    assert_usable(page, "#sidebar-command-toggle")
    run_command(page, "rail-agents")
    page.wait_for_function("!document.getElementById('agents-dashboard').hidden")
    assert probe(page, "#agents-dashboard .ag-card")["visible"]
    expect(page.locator(".ag-phalanx-title")).to_have_text("Phalanx")
    expect(page.locator("#ag-window-summary")).to_contain_text("Command center")
    page.click("#close-agents-dashboard")
    page.wait_for_function("document.getElementById('agents-dashboard').hidden")

    show_command_toggle(page)
    assert_usable(page, "#sidebar-command-toggle")
    run_command(page, "rail-workbench")
    page.wait_for_function("!document.getElementById('workbench-modal').classList.contains('hidden')")
    assert visible_panels(page) == ["changes"]
    page.click("#wb-tab-activity")
    assert probe(page, ".ag-run-summary")["visible"]


@pytest.mark.parametrize("style", ["classic", "agamemnon"])
@pytest.mark.parametrize("width", [1440, 700, 390])
def test_phalanx_nav_item_matches_workbench_type(open_app, style, width):
    page = open_app(width, style=style)
    assert page.evaluate("(() => { const a = document.querySelector('#tool-agents-btn .ag-nav-label');"
                         " const b = document.querySelector('#tool-agents-btn .ody-nav-label');"
                         " const target = getComputedStyle(document.querySelector('#tool-workbench-btn .grow'));"
                         " const label = getComputedStyle(getComputedStyle(a).display === 'none' ? b : a);"
                         " return label.fontSize === target.fontSize && label.fontWeight === target.fontWeight; })()")


def test_classic_style_keeps_navigation_names_and_plain_activity(open_app):
    page = open_app(1440, theme=ODYSSEUS_THEME)
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    assert probe(page, "#tool-agents-btn")["visible"]
    expect(page.locator(".ag-nav-label")).to_have_text("Phalanx")
    assert page.evaluate("getComputedStyle(document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')).display") != "none"
    assert page.evaluate("document.querySelector('link[rel=icon]').getAttribute('href')") == HELMET
    assert page.evaluate("document.getElementById('message').placeholder") == "Message Scribe…"
    open_workbench(page, activity=False)
    assert visible_panels(page) == ["changes"]
    assert not probe(page, ".ag-run-summary")["visible"]
    # The classic Activity tab is the plain feed, without the run panels.
    page.click("#wb-tab-activity")
    for selector in (".ag-run-summary", ".ag-run-timeline", ".ag-run-console"):
        assert not probe(page, selector)["visible"], selector
    assert_usable(page, "#wb-pause")
    assert_no_sideways_scroll(page, ".workbench-modal-content")


@pytest.mark.parametrize("theme", [None, LIGHT_THEME])
@pytest.mark.parametrize("width", [700, 320])
def test_open_phone_sidebar_keeps_the_wordmark_clear_of_the_hamburger(open_app, width, theme):
    page = open_app(width, theme=theme)
    if not page.evaluate("(() => { const r = document.getElementById('sidebar').getBoundingClientRect();"
                         " return r.right > 0 && r.left < innerWidth; })()"):
        page.click("#hamburger-btn")
    settle(page, "#sidebar")
    sidebar, hamburger = probe(page, ".sidebar"), probe(page, "#hamburger-btn")
    crest, title = probe(page, ".sidebar-brand-icon"), probe(page, ".sidebar-brand-title")
    assert crest["visible"] and title["visible"], (crest, title)
    assert sidebar["left"] <= crest["left"] and title["right"] <= sidebar["right"], (sidebar, title)
    for box in (crest, title):
        overlaps = (box["left"] < hamburger["right"] and box["right"] > hamburger["left"]
                    and box["top"] < hamburger["bottom"] and box["bottom"] > hamburger["top"])
        assert not overlaps, (box, hamburger)


def test_command_menu_closes_on_escape_and_runs_search_email_and_new_chat(open_app, static_app):
    page = open_app(1440, chat=False)
    toggle = page.locator("#sidebar-command-toggle")
    page.click("#sidebar-command-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    page.keyboard.press("Escape")
    expect(toggle).to_have_attribute("aria-expanded", "false")

    page.evaluate("window.searchClicked = false; document.getElementById('rail-search-btn')"
                  ".addEventListener('click', () => { window.searchClicked = true; }, {once: true})")
    run_command(page, "rail-search-btn")
    page.wait_for_function("window.searchClicked === true")

    page.goto(static_app.url)
    wait_ready(page, chat=False)
    run_command(page, "rail-email")
    page.wait_for_function("document.querySelector('#email-lib-modal')?.offsetWidth > 0")

    page.goto(static_app.url)
    wait_ready(page, chat=False)
    run_command(page, "rail-new-session")
    page.wait_for_function("document.querySelector('#chat-container')?.classList.contains('welcome-active')")


def test_legacy_palette_keeps_classic_style_and_rail_order_persists(open_app, static_app):
    page = open_app(1440, theme=LIGHT_THEME, chat=False)
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    assert page.evaluate("document.querySelector('.brand-crest-icon').getBoundingClientRect().width > 0")
    page.evaluate("async () => { const m = await import('/static/js/navOrder.js');"
                  " m.writeNavOrder(['command', 'email', 'calendar']); m.applyNavOrder(); }")
    page.wait_for_function("document.querySelector('#icon-rail .icon-rail-btn')?.id === 'rail-command'")
    # Alt+ArrowDown moves a rail button down one place. The rail is hidden
    # in this layout, so the key event goes to the button directly.
    page.evaluate("document.getElementById('rail-command').dispatchEvent("
                  "new KeyboardEvent('keydown', {key: 'ArrowDown', altKey: true, bubbles: true}))")
    page.wait_for_function("document.querySelector('#icon-rail .icon-rail-btn')?.id !== 'rail-command'")
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-nav-order-v1')).value[0]") != "command"
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    assert page.evaluate("document.querySelector('#icon-rail .icon-rail-btn').id") != "rail-command"
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-theme')).name") == json.loads(LIGHT_THEME)["name"]
