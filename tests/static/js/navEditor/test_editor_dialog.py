"""The Edit navigation dialog and the group header in a real browser
(static/js/navEditor.js, static/js/navOrder.js, static/nav-groups.css): the
dialog is drawn above Settings and fits the viewport, a drag in the sidebar
moves a tool into an empty group without launching it, and an arrangement made
there survives a reload.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import LIGHT_THEME, assert_no_sideways_scroll, assert_usable, expect, settle, wait_ready

pytestmark = pytest.mark.browser

LAYOUT_KEY = "odysseus-nav-layout-v2"


def open_sidebar(page) -> None:
    if page.evaluate("document.getElementById('sidebar').getBoundingClientRect().right <= 0"
                     " || document.getElementById('sidebar').classList.contains('hidden')"):
        page.evaluate("window._odyOpenSidebar('left')")
    settle(page, "#sidebar")


def layout(page) -> dict:
    return json.loads(page.evaluate(f"localStorage.getItem('{LAYOUT_KEY}')"))["value"]


@pytest.mark.parametrize("theme", [None, LIGHT_THEME])
@pytest.mark.parametrize("style", ["agamemnon", "classic"])
@pytest.mark.parametrize("width", [1440, 390])
def test_the_dialog_fits_the_viewport_and_its_controls_are_reachable(open_app, width, style, theme):
    page = open_app(width, theme=theme, style=style, chat=False, height=800)
    open_sidebar(page)
    page.click("#work-section .nav-group-menu-btn")
    page.click('#nav-group-menu [role="menuitem"]:has-text("Edit navigation")')
    box = assert_usable(page, ".nav-editor")
    assert box["left"] >= 0 and box["right"] <= width and box["bottom"] <= 800, box
    for selector in (".nav-editor-name", '[data-act="item-down"][data-item="today"]', '[data-act="item-move"][data-item="today"]',
                     '[data-act="new-group"]', '[data-act="reset"]', ".nav-editor-foot .primary"):
        page.locator(selector).first.scroll_into_view_if_needed()
        assert_usable(page, selector)
    assert_no_sideways_scroll(page, ".nav-editor-body")
    page.keyboard.press("Escape")
    expect(page.locator(".nav-editor")).to_have_count(0)


def test_edit_navigation_opens_above_settings_and_changes_survive_a_reload(open_app, static_app):
    page = open_app(1440, chat=False)
    page.evaluate("document.getElementById('user-bar-settings').click()")
    page.wait_for_selector("#edit-navigation-btn", state="attached")
    page.evaluate("document.querySelector('[data-settings-tab=\"appearance\"], [data-tab=\"appearance\"]')?.click()")
    page.evaluate("document.getElementById('edit-navigation-btn').click()")
    assert_usable(page, ".nav-editor .nav-editor-foot .primary")
    page.click('[data-act="new-group"]')
    page.fill(".nav-editor-name:focus", "Clients")
    page.keyboard.press("Tab")
    added = layout(page)["groups"][-1]
    assert added["label"] == "Clients" and added["items"] == []
    page.select_option('[data-act="item-move"][data-item="notes"]', added["id"])
    page.click(".nav-editor-foot .primary")
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    open_sidebar(page)
    assert page.evaluate("[...document.querySelectorAll('#sidebar .nav-group')].map(g => g.querySelector('.nav-group-title').textContent)") == ["Work", "Agents", "More", "Clients"]
    assert page.evaluate("[...document.querySelectorAll('#sidebar .nav-group:last-of-type > .list-item')].map(n => n.id)") == ["tool-notes-btn"]


def test_dragging_a_tool_into_an_empty_group_moves_it_and_does_not_open_it(open_app):
    page = open_app(1440, chat=False)
    page.evaluate("async () => { const m = await import('/static/js/navLayout.js');"
                  " m.updateLayout((layout) => m.createGroup(layout, {label: 'Clients', id: 'clients'}).layout); }")
    open_sidebar(page)
    page.wait_for_selector('.nav-group[data-nav-group="clients"] .nav-group-empty', state="visible")
    page.drag_and_drop("#tool-notes-btn", '.nav-group[data-nav-group="clients"] .nav-group-empty')
    page.wait_for_timeout(450)
    assert page.evaluate("[...document.querySelectorAll('.nav-group[data-nav-group=\"clients\"] > .list-item')].map(n => n.id)") == ["tool-notes-btn"]
    assert [g["items"] for g in layout(page)["groups"] if g["id"] == "clients"] == [["notes"]]
    assert page.evaluate("[...document.querySelectorAll('.modal')].filter(m => getComputedStyle(m).display !== 'none' && !m.classList.contains('hidden')).length") == 0


def test_the_group_header_shows_its_menu_and_a_folded_group_still_shows_its_label(open_app):
    page = open_app(1440, chat=False)
    open_sidebar(page)
    for group in ("work", "agents", "more"):
        assert_usable(page, f"#{group}-section .nav-group-toggle")
        assert_usable(page, f"#{group}-section .nav-group-menu-btn")
    assert page.evaluate("document.querySelector('#more-section .nav-group-toggle').getAttribute('aria-expanded')") == "false"
