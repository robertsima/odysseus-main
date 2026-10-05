"""The Workbench attached to the left or right of the chat (static/js/workbench.js).

The dock widths 350, 480 and 680 px sit in different container-query ranges
of the Workbench window and panels (430 and 660 px; 520, 610, 720 px), so each
is a layout of its own. Regressions these caught: the close button pushed off
screen, the tab bar overflowing a narrow dock, an unsaved editor draft lost on
resize, and the feed controls reading after the run timeline.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import (
    assert_inside_parent, assert_no_sideways_scroll, assert_usable, dock_workbench, editor_value, expect,
    open_workbench, probe, resize_workbench_dock, set_checkbox, settle, type_into_editor, visible_panels,
)

pytestmark = pytest.mark.browser

RUN_SECTIONS = ".ag-run-summary,.wb-toolbar,.wb-activity,.ag-run-timeline,.ag-run-console,.ag-run-detail"
RUN_ORDER = ["ag-run-summary", "wb-toolbar", "wb-activity", "ag-run-timeline", "ag-run-console", "ag-run-detail"]


def tab_bar_overflow(page) -> dict:
    return page.evaluate(
        "(() => { const bar = document.querySelector('#workbench-modal .wb-tabs');"
        " return {scroll: bar.scrollWidth, client: bar.clientWidth,"
        " tabs: [...bar.children].map(t => ({name: t.dataset.wbTab, scroll: t.scrollWidth, client: t.clientWidth}))}; })()")


@pytest.mark.parametrize("style", ["agamemnon", "classic"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_attached_workbench_resizes_and_stays_operable(open_app, side, style):
    page = open_app(1440, style=style)
    open_workbench(page)
    dock_workbench(page, side)
    for width in (680, 480, 350, 580):
        resize_workbench_dock(page, width, side)
        assert_usable(page, "#close-workbench-modal")
        assert_inside_parent(page, [("#close-workbench-modal", ".workbench-modal-content"),
                                    ("#wb-tab-prs", ".workbench-modal-body"),
                                    ("#wb-pause", "#wb-panel-activity")])
        assert_no_sideways_scroll(page, ".workbench-modal-content")
        assert_no_sideways_scroll(page, ".workbench-modal-body")
        tabs = tab_bar_overflow(page)
        assert tabs["scroll"] <= tabs["client"] + 2 and all(t["scroll"] <= t["client"] + 2 for t in tabs["tabs"]), tabs
        for tab, controls in [("activity", []),
                              ("changes", [".wb-repo-select", "#wb-base", ".wb-split", "#wb-diffpane"]),
                              ("commits", [".wb-repo-select", ".wb-split"]),
                              ("prs", [])]:
            assert_usable(page, f"#wb-tab-{tab}")
            page.click(f"#wb-tab-{tab}")
            assert visible_panels(page) == [tab]
            assert_no_sideways_scroll(page, ".workbench-modal-body")
            assert_inside_parent(page, [(c, f"#wb-{tab}") for c in controls])
            if tab == "changes":
                split = page.evaluate(
                    "(() => { const a = document.querySelector('#wb-changes .wb-list').getBoundingClientRect(),"
                    " b = document.querySelector('#wb-diffpane').getBoundingClientRect();"
                    " return {listBottom: a.bottom, diffTop: b.top}; })()")
                assert split["diffTop"] >= split["listBottom"] - 2, split
        page.click("#wb-tab-activity")
        assert_usable(page, "#wb-pause")
        if style == "agamemnon":
            order = page.evaluate(
                "['.ag-run-summary', '#wb-panel-activity .wb-toolbar', '#wb-activity', '.ag-run-timeline']"
                ".map(s => document.querySelector(s).getBoundingClientRect().top)")
            assert order == sorted(order), f"docked feed must precede secondary run details: {order}"
    assert page.evaluate("document.querySelector('.workbench-modal-content')._userDockWidth") == 580


@pytest.mark.parametrize("side", ["left", "right"])
def test_attached_editor_draft_survives_style_switch_and_resize(open_app, side):
    page = open_app(1440, style="classic")
    open_workbench(page, activity=False)
    dock_workbench(page, side)
    resize_workbench_dock(page, 350, side)
    page.click("#wb-tab-activity")
    assert_usable(page, "#wb-pause")
    page.click("#wb-tab-changes")
    page.wait_for_selector("#wb-diffpane .wb-diff")
    page.click('#wb-diffpane [data-wb-act="edit-file"]')
    page.wait_for_function("document.querySelector('#wb-editor-text')?.value.includes('migrate')")
    type_into_editor(page, "\n# unsaved dock draft")
    editor_fits = [("#wb-editor-text", "#wb-diffpane"), ('[data-wb-act="save-file"]', "#wb-diffpane")]
    for style in ("agamemnon", "classic", "agamemnon"):
        set_checkbox(page, "#theme-style-toggle", style == "agamemnon")
        page.wait_for_function("(style) => document.documentElement.dataset.style === style", arg=style)
        settle(page, ".workbench-modal-content")
        assert editor_value(page).endswith("# unsaved dock draft")
        assert page.evaluate(f"document.getElementById('workbench-modal').classList.contains('modal-{side}-docked')")
        assert_inside_parent(page, editor_fits)
        assert_usable(page, "#wb-tab-activity")
    for width in (680, 480, 350):
        resize_workbench_dock(page, width, side)
        assert editor_value(page).endswith("# unsaved dock draft")
        assert_inside_parent(page, editor_fits + [(".wb-repo-select", "#wb-changes")])


@pytest.mark.parametrize("side,width", [("right", 350), ("right", 480), ("right", 680), ("left", 350)])
def test_attached_workbench_reading_and_keyboard_order(open_app, side, width):
    page = open_app(1440)
    open_workbench(page)
    dock_workbench(page, side)
    resize_workbench_dock(page, width, side)
    # A real run's timeline can hold a source link. Tab from the feed
    # actions must reach it after the visible feed controls.
    page.evaluate("document.getElementById('ag-run-timeline-list').innerHTML ="
                  " '<li><a id=run-source-link href=#source>Source</a></li>'")
    dom = page.evaluate("(sel) => [...document.querySelector('#wb-panel-activity').querySelectorAll(sel)]"
                        ".map(e => e.className)", RUN_SECTIONS)
    assert dom == RUN_ORDER, dom
    positions = [probe(page, "." + name)["top"] for name in dom]
    assert positions == sorted(positions), positions
    page.focus("#wb-clear")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.closest('#wb-activity') !== null"), "feed must follow feed controls"
    for _ in range(20):
        if page.evaluate("document.activeElement.id") == "run-source-link":
            break
        page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "run-source-link", page.evaluate("document.activeElement.outerHTML")
    compact_filters = probe(page, ".wb-filter-options summary")["visible"]
    if compact_filters:
        assert_usable(page, ".wb-filter-options summary")
        page.click(".wb-filter-options summary")
    for selector in ("#wb-scope", "#wb-filter"):
        assert_usable(page, selector)
    for selector in ("#wb-pause", "#wb-clear"):
        target = probe(page, selector)
        assert target["height"] >= 40 and target["width"] >= 44, (selector, target)
    if width == 350:
        assert abs(probe(page, "#wb-pause")["top"] - probe(page, "#wb-clear")["top"]) <= 2
    if compact_filters:
        run_actions = page.evaluate("[...document.querySelectorAll('#wb-activity .wb-run-actions button')]"
                                    ".map(e => ({width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height}))")
        assert run_actions and all(a["width"] >= 44 and a["height"] >= 44 for a in run_actions), run_actions
    assert_no_sideways_scroll(page, ".workbench-modal-content")


def test_workbench_docks_beside_the_chat_and_keeps_both_usable(open_app):
    page = open_app(1440)
    page.wait_for_function("!document.getElementById('agent-strip').hidden")
    open_workbench(page)
    dock_workbench(page, "right")
    content = probe(page, ".workbench-modal-content")
    assert content["left"] > 600 and content["right"] <= 1441, content
    for selector in (".ag-run-summary", ".ag-run-timeline", ".ag-run-console", ".ag-run-detail"):
        box = probe(page, selector)
        assert box["visible"] and box["right"] <= 1440, (selector, box)
    assert probe(page, ".ag-run-detail")["top"] >= probe(page, ".ag-run-console")["bottom"]
    # The chat makes room, keeps its expanded progress shelf bounded and
    # stays usable beside the dock.
    for selector in ("#chat-history", ".chat-input-bar"):
        assert probe(page, selector)["right"] <= content["left"] + 1, selector
    assert probe(page, ".chat-progress-shelf")["height"] <= 205
    assert probe(page, "#chat-history")["height"] >= 260
    assert_usable(page, "#message")
    assert_no_sideways_scroll(page)
    page.click('#workbench-modal [data-wb-tab="commits"]')
    assert visible_panels(page) == ["commits"]
    assert_usable(page, "#close-workbench-modal")
    page.click("#close-workbench-modal")
    page.wait_for_function("document.getElementById('workbench-modal').classList.contains('hidden')")
    assert not page.evaluate("document.body.classList.contains('right-dock-active')")


def test_chat_strip_beside_a_half_width_docked_workbench(open_app):
    # 1024 px: docking takes half the screen and the sidebar hides.
    page = open_app(1024)
    open_workbench(page)
    dock_workbench(page, "right")
    prs = probe(page, "#wb-tab-prs")
    assert prs["visible"] and prs["right"] <= 1024
    page.wait_for_function("document.getElementById('sidebar').classList.contains('hidden')")
    settle(page, ".chat-container")
    strip, chat = probe(page, ".agent-strip"), probe(page, ".chat-container")
    assert strip["width"] <= chat["width"] and strip["height"] <= 155
    assert chat["width"] >= 520
    assert probe(page, "#chat-history")["height"] >= 200
    assert probe(page, ".chat-progress-shelf")["height"] <= 205
    # The strip's own Workbench shortcut hides while the Workbench is docked.
    expect(page.locator(".agent-strip-head [data-strip-act=workbench]")).to_have_css("display", "none")
    assert_usable(page, '.agent-strip-row [data-strip-act="stop"]')
    scroll = probe(page, "#scroll-bottom-btn", wait=False)
    if scroll["visible"]:
        assert scroll["bottom"] <= probe(page, "#chat-history")["bottom"], scroll
    assert_usable(page, ".agent-strip-toggle")
    assert_usable(page, "#message")
    assert_no_sideways_scroll(page)
    page.click(".agent-strip-toggle")
    page.wait_for_function("document.querySelector('.agent-strip-toggle').getAttribute('aria-expanded') === 'false'")
    settle(page, ".chat-input-bar")
    assert probe(page, ".agent-strip-head")["height"] < 55
    assert_usable(page, "#message")
    assert probe(page, "#chat-history")["height"] >= 250


@pytest.mark.parametrize("width", [700, 390])
def test_narrow_progress_and_workbench_do_not_cover_controls(open_app, width):
    page = open_app(width)
    page.wait_for_function("!document.getElementById('agent-strip').hidden")
    if width == 700:
        open_workbench(page)
        # At 700 px the Workbench takes the workspace as a page; docking is
        # disabled rather than squeezing the chat.
        assert not probe(page, "#wb-dock-right")["visible"]
        page.click("#close-workbench-modal")
        page.wait_for_function("document.getElementById('workbench-modal').classList.contains('hidden')")
        settle(page, ".chat-input-bar")
    assert_usable(page, '.agent-strip-row [data-strip-act="stop"]')
    assert_usable(page, "#message")
    assert probe(page, ".chat-progress-shelf")["height"] <= 210
    assert probe(page, "#chat-history")["height"] >= 175
    assert_no_sideways_scroll(page)
