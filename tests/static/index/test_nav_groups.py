"""The sidebar navigation groups (static/index.html, static/nav-groups.css,
static/js/init.js, static/js/navOrder.js): Work, Agents and More, the folded
More group that remembers it was opened, and Theme in the footer."""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import LIGHT_THEME, ODYSSEUS_THEME, assert_no_sideways_scroll, expect, settle, wait_ready

pytestmark = pytest.mark.browser

GROUPS = {
    "work-section": ["tool-today-btn", "tool-library-btn", "tool-notes-btn", "tool-tasks-btn", "tool-calendar-btn"],
    "agents-section": ["tool-agents-btn", "tool-workbench-btn"],
    "more-section": ["tool-memory-btn", "tool-lotus-btn", "tool-compare-btn", "tool-cookbook-btn",
                     "tool-research-btn", "tool-gallery-btn"],
}
PANELS = '[id$="-modal"], #notes-pane, #agents-dashboard, #research-overlay, #compare-model-overlay'
VISIBLE_PANELS = ("(el) => el.offsetWidth > 0 && el.offsetHeight > 0 && !el.classList.contains('hidden')"
                  " && !el.hidden && getComputedStyle(el).visibility !== 'hidden'")


def visible_panels_now(page) -> list[str]:
    return page.evaluate(f"(sel) => [...document.querySelectorAll(sel)].filter({VISIBLE_PANELS}).map(el => el.id)", PANELS)


def open_sidebar(page) -> None:
    if page.evaluate("document.getElementById('sidebar').getBoundingClientRect().right <= 0"
                     " || document.getElementById('sidebar').classList.contains('hidden')"):
        page.evaluate("window._odyOpenSidebar('left')")
    settle(page, "#sidebar")


def collapsed(page, group: str) -> bool:
    return page.evaluate("(id) => document.getElementById(id).classList.contains('collapsed')", group)


@pytest.mark.parametrize("width", [1440, 390])
def test_sidebar_shows_three_groups_with_more_folded(open_app, width):
    page = open_app(width, chat=False)
    open_sidebar(page)
    assert page.evaluate("document.getElementById('tools-section')") is None
    titles = page.evaluate("[...document.querySelectorAll('#sidebar .nav-group')]"
                           ".map(g => g.id + ':' + g.querySelector('.nav-group-title').textContent.trim())")
    assert titles == ["work-section:Work", "agents-section:Agents", "more-section:More"]
    for group, ids in GROUPS.items():
        assert page.evaluate("(g) => [...document.querySelectorAll('#' + g + ' > .list-item')].map(n => n.id)", group) == ids
    assert not collapsed(page, "work-section") and not collapsed(page, "agents-section")
    assert collapsed(page, "more-section")
    for tool in GROUPS["more-section"]:
        assert not page.evaluate("(id) => document.getElementById(id).offsetParent !== null", tool), f"{tool} shows while More is folded"
    assert page.evaluate("document.getElementById('tool-theme-btn').closest('#sidebar-user-bar') !== null")
    for selector in ("#sidebar-command-toggle", "#tool-archive-btn"):
        assert page.evaluate("(s) => document.querySelector(s)", selector) is None


@pytest.mark.parametrize("width", [1440, 390])
def test_more_remembers_it_was_opened(open_app, static_app, width):
    page = open_app(width, chat=False)
    open_sidebar(page)
    page.click("#more-section .nav-group-title")
    expect(page.locator("#tool-gallery-btn")).to_be_visible()
    assert json.loads(page.evaluate("localStorage.getItem('section-collapsed')"))["more-section"] is False
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    open_sidebar(page)
    assert not collapsed(page, "more-section")
    expect(page.locator("#tool-gallery-btn")).to_be_visible()
    # Folding it again is remembered the same way.
    page.click("#more-section .nav-group-title")
    page.wait_for_function("document.getElementById('more-section').classList.contains('collapsed')")
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    assert collapsed(page, "more-section")


def test_every_former_tool_opens_from_its_row(open_app):
    page = open_app(1440, chat=False)
    open_sidebar(page)
    page.click("#more-section .nav-group-title")
    for tool in [t for ids in GROUPS.values() for t in ids]:
        before = visible_panels_now(page)
        page.evaluate("(id) => document.getElementById(id).click()", tool)
        try:
            page.wait_for_function(
                f"([sel, before]) => [...document.querySelectorAll(sel)].filter({VISIBLE_PANELS})"
                ".some(el => !before.includes(el.id))", arg=[PANELS, before], timeout=8000)
        except Exception as error:  # say which row did nothing
            raise AssertionError(f"{tool} opened nothing") from error
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)


@pytest.mark.parametrize("width", [1440, 390])
def test_theme_button_in_the_footer_opens_the_theme_modal(open_app, width):
    page = open_app(width, chat=False)
    open_sidebar(page)
    button = page.locator("#sidebar-user-bar #tool-theme-btn")
    expect(button).to_be_visible()
    settings = page.locator("#user-bar-settings").bounding_box()
    theme = button.bounding_box()
    assert theme["x"] + theme["width"] <= settings["x"] + 1, (theme, settings)
    assert abs(theme["y"] - settings["y"]) < 4
    assert page.evaluate("document.getElementById('theme-modal').classList.contains('hidden')")
    button.click()
    expect(page.locator("#theme-modal")).to_be_visible()


@pytest.mark.parametrize("theme", [None, LIGHT_THEME, ODYSSEUS_THEME])
@pytest.mark.parametrize("width", [1440, 390])
def test_open_groups_do_not_overflow_the_sidebar(open_app, width, theme):
    page = open_app(width, theme=theme, chat=False)
    open_sidebar(page)
    page.click("#more-section .nav-group-title")
    page.wait_for_timeout(900)
    assert_no_sideways_scroll(page, ".sidebar-inner")
    assert_no_sideways_scroll(page)
    for group in GROUPS:
        box = page.evaluate("(id) => { const r = document.getElementById(id).getBoundingClientRect(),"
                            " s = document.getElementById('sidebar').getBoundingClientRect();"
                            " return {l: r.left - s.left, r: s.right - r.right}; }", group)
        assert box["l"] >= 0 and box["r"] >= 0, (group, box)


def test_a_group_with_every_item_hidden_hides_its_header(open_app):
    page = open_app(1440, chat=False)
    open_sidebar(page)
    page.evaluate("window.applyUIVis({'tool-agents': false, 'tool-workbench': false})")
    expect(page.locator("#agents-section")).to_be_hidden()
    expect(page.locator("#work-section")).to_be_visible()
    page.evaluate("window.applyUIVis({'tool-today': false})")
    expect(page.locator("#agents-section")).to_be_visible()
    expect(page.locator("#tool-today-btn")).to_be_hidden()
    expect(page.locator("#tool-library-btn")).to_be_visible()


def test_a_folded_more_shows_a_dot_while_a_child_is_running(open_app):
    page = open_app(1440, chat=False)
    open_sidebar(page)
    dot = page.locator("#more-section .nav-group-dot")
    expect(dot).to_be_hidden()
    page.evaluate("document.getElementById('cookbook-notif-dot').style.display = ''")
    expect(dot).to_be_visible()
    page.evaluate("document.getElementById('cookbook-notif-dot').style.display = 'none'")
    expect(dot).to_be_hidden()
    page.evaluate("document.getElementById('tool-gallery-btn').classList.add('rail-minimized')")
    expect(dot).to_be_visible()
    page.click("#more-section .nav-group-title")
    page.wait_for_timeout(500)
    expect(dot).to_be_hidden()
    expect(page.locator("#tool-gallery-btn")).to_be_visible()
