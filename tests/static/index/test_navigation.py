"""Navigation in the page shell (static/index.html, static/js/sidebar-layout.js,
static/js/navOrder.js, static/js/navRail.js): the sidebar on phones, the
collapsed rail's group icons and flyouts, the saved layout, and the product
names the Agamemnon identity puts on them. The Command menu this file used to
cover is gone; its destinations are in the groups and the fixed rail actions.
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


def open_tool(page, row: str) -> None:
    """Click a tool's sidebar row. The row is in the page whether or not the
    sidebar is showing, so the click is sent to it directly."""
    page.evaluate("(id) => document.getElementById(id).click()", row)


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_phalanx_and_the_workbench_open_from_their_rows_under_their_names(open_app, width):
    page = open_app(width)
    # The Agamemnon identity names the chat Scribe and the agents Phalanx.
    expect(page.locator(".ag-page-heading h1")).to_have_text("Scribe")
    assert page.title() == "Scribe — Agamemnon"
    assert page.evaluate("document.getElementById('message').placeholder") == "Message Scribe…"
    assert page.evaluate("document.querySelector('link[rel=icon]').getAttribute('href')") == HELMET
    assert probe(page, "#ag-open-theme", wait=False) is None
    assert page.evaluate("getComputedStyle(document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')).display") != "none"
    assert probe(page, "#sidebar-command-menu", wait=False) is None
    assert probe(page, "#sidebar-command-toggle", wait=False) is None
    assert probe(page, "#rail-command", wait=False) is None
    open_tool(page, "tool-agents-btn")
    page.wait_for_function("!document.getElementById('agents-dashboard').hidden")
    assert probe(page, "#agents-dashboard .ag-card")["visible"]
    expect(page.locator(".ag-phalanx-title")).to_have_text("Phalanx")
    expect(page.locator("#ag-window-summary")).to_contain_text("Command center")
    page.click("#close-agents-dashboard")
    page.wait_for_function("document.getElementById('agents-dashboard').hidden")

    open_tool(page, "tool-workbench-btn")
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


def test_the_old_command_destinations_are_reachable_from_the_rail(open_app, static_app):
    page = open_app(1440, chat=False)
    page.evaluate("document.getElementById('hamburger-btn').click()")
    page.wait_for_function("document.documentElement.classList.contains('ody-sidebar-mini')")
    # Search and New chat are fixed actions at the top of the mini rail.
    page.evaluate("window.searchClicked = false; document.getElementById('rail-search-btn')"
                  ".addEventListener('click', () => { window.searchClicked = true; }, {once: true})")
    page.click("#rail-search-btn")
    page.wait_for_function("window.searchClicked === true")
    page.keyboard.press("Escape")
    # Email is a tool in the Work group's flyout.
    page.click("#navgrp-work")
    page.click('#nav-flyout [data-nav-item="email"]')
    page.wait_for_function("document.querySelector('#email-lib-modal')?.offsetWidth > 0")
    assert page.evaluate("document.getElementById('nav-flyout').hidden")
    # Phalanx and the Workbench are in the Agents group's flyout.
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    # The mini mode is remembered across the reload.
    page.wait_for_function("document.documentElement.classList.contains('ody-sidebar-mini')")
    page.click("#navgrp-agents")
    labels = page.evaluate("[...document.querySelectorAll('#nav-flyout .nav-flyout-item')].map(n => n.dataset.navItem)")
    assert labels == ["agents", "workbench"]


def test_legacy_palette_keeps_classic_style_and_the_saved_layout_persists(open_app, static_app):
    page = open_app(1440, theme=LIGHT_THEME, chat=False)
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    assert page.evaluate("document.querySelector('.brand-crest-icon').getBoundingClientRect().width > 0")
    page.evaluate("async () => { const m = await import('/static/js/navLayout.js');"
                  " m.updateLayout((layout) => m.moveGroupBy(layout, 'more', -1)); }")
    order = "[...document.querySelectorAll('#sidebar .nav-group')].map(g => g.dataset.navGroup)"
    page.wait_for_function(f"{order}.join() === 'work,more,agents'")
    assert page.evaluate("[...document.querySelectorAll('#rail-nav-groups [data-nav-group-btn]')].map(b => b.dataset.navGroupBtn).join()") == "work,more,agents"
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-nav-layout-v2')).value.groups[1].id") == "more"
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    assert page.evaluate(f"{order}.join()") == "work,more,agents"
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-theme')).name") == json.loads(LIGHT_THEME)["name"]
