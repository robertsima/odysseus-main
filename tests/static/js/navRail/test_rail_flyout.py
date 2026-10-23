"""The collapsed rail in a real browser (static/js/navRail.js, static/nav-groups.css):
group icons, the anchored flyout, the phone sheet and the off mode, in both
page styles and both palettes. The logic is covered under Node in flyout.test.mjs;
these tests check what only the cascade shows: that the icons and the flyout are
drawn, inside the viewport, and reachable.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import LIGHT_THEME, assert_no_sideways_scroll, assert_usable, probe, settle, wait_ready

pytestmark = pytest.mark.browser

STYLES = ["agamemnon", "classic"]
PALETTES = [None, LIGHT_THEME]


def go_mini(page) -> None:
    page.evaluate("document.getElementById('hamburger-btn').click()")
    page.wait_for_function("document.documentElement.classList.contains('ody-sidebar-mini')")
    settle(page, "#icon-rail")


@pytest.mark.parametrize("theme", PALETTES)
@pytest.mark.parametrize("style", STYLES)
def test_mini_rail_shows_fixed_actions_and_one_icon_per_group_and_the_flyout_opens_beside_it(open_app, style, theme):
    page = open_app(1440, theme=theme, style=style, chat=False)
    go_mini(page)
    for selector in ("#rail-search-btn", "#rail-new-session", "#navgrp-work", "#navgrp-agents", "#navgrp-more",
                     "#rail-theme", "#rail-settings"):
        assert_usable(page, selector)
    # Fixed actions at the top, groups in the middle, Theme and Settings at the bottom.
    top = lambda selector: probe(page, selector)["top"]  # noqa: E731
    assert top("#rail-search-btn") < top("#navgrp-work") < top("#navgrp-agents") < top("#navgrp-more") < top("#rail-theme") < top("#rail-settings")
    # The rail's own per-tool buttons are not drawn, and neither is the Command button.
    assert not probe(page, "#rail-tasks")["visible"]
    assert page.evaluate("document.getElementById('rail-command')") is None
    assert page.evaluate("document.getElementById('navgrp-more').getAttribute('aria-label')") == "More"

    page.click("#navgrp-more")
    flyout = assert_usable(page, "#nav-flyout")
    rail = probe(page, "#icon-rail")
    assert flyout["left"] >= rail["right"], (flyout, rail)
    assert flyout["bottom"] <= 900
    names = page.evaluate("[...document.querySelectorAll('#nav-flyout .nav-flyout-item')].map(n => n.textContent.trim())")
    assert names == ["Brain", "Lotus", "Compare", "Cookbook", "Deep Research", "Gallery"]
    for item in ("brain", "gallery"):
        assert probe(page, f'#nav-flyout [data-nav-item="{"memory" if item == "brain" else item}"]')["visible"]
    # The sidebar stayed collapsed.
    assert page.evaluate("document.getElementById('sidebar').getBoundingClientRect().width") < 5

    page.keyboard.press("Escape")
    assert page.evaluate("document.getElementById('nav-flyout').hidden")
    assert page.evaluate("document.activeElement.id") == "navgrp-more"
    assert_usable(page, "#navgrp-more")


def test_choosing_a_tool_in_the_flyout_opens_it_and_closes_the_flyout(open_app):
    page = open_app(1440, chat=False)
    go_mini(page)
    page.click("#navgrp-work")
    page.click('#nav-flyout [data-nav-item="email"]')
    page.wait_for_function("document.querySelector('#email-lib-modal')?.offsetWidth > 0")
    assert page.evaluate("document.getElementById('nav-flyout').hidden")


def test_clicking_elsewhere_closes_the_flyout_and_only_one_is_open(open_app):
    page = open_app(1440, chat=False)
    go_mini(page)
    page.click("#navgrp-work")
    page.click("#navgrp-agents")
    assert page.evaluate("document.querySelectorAll('.nav-flyout:not([hidden])').length") == 1
    assert page.evaluate("document.querySelector('#nav-flyout .nav-flyout-title').textContent") == "Agents"
    page.mouse.click(900, 400)
    assert page.evaluate("document.getElementById('nav-flyout').hidden")


@pytest.mark.parametrize("style", STYLES)
def test_a_group_with_many_tools_scrolls_inside_a_short_window(open_app, style):
    page = open_app(1440, style=style, chat=False, height=320)
    page.evaluate("async () => { const m = await import('/static/js/navLayout.js');"
                  " m.updateLayout((layout) => m.moveItem(m.moveItem(m.moveItem(layout, 'tasks', 'more'), 'notes', 'more'), 'today', 'more')); }")
    go_mini(page)
    page.click("#navgrp-more")
    flyout = assert_usable(page, "#nav-flyout")
    assert flyout["top"] >= 0 and flyout["bottom"] <= 320, flyout
    sizes = page.evaluate("(() => { const l = document.querySelector('#nav-flyout .nav-flyout-list'); return [l.scrollHeight, l.clientHeight, getComputedStyle(l).overflowY]; })()")
    assert sizes[0] > sizes[1] and sizes[2] == "auto", sizes
    page.evaluate("document.querySelector('#nav-flyout .nav-flyout-list').scrollTop = 9999")
    assert_usable(page, '#nav-flyout [data-nav-item="gallery"]')


@pytest.mark.parametrize("theme", PALETTES)
@pytest.mark.parametrize("style", STYLES)
def test_on_a_phone_the_flyout_is_a_sheet_that_fits_the_viewport(open_app, style, theme):
    page = open_app(390, theme=theme, style=style, chat=False, height=800)
    # On a phone the rail is drawn only in the mobile-mini state (what restoring
    # the sidebar after a tool closes leaves behind), not in off mode.
    page.evaluate("document.documentElement.classList.remove('ody-sidebar-off', 'ody-mobile-startup-sidebar-hidden');"
                  " document.getElementById('icon-rail').classList.remove('rail-hidden');"
                  " document.getElementById('icon-rail').classList.add('mobile-mini')")
    settle(page, "#icon-rail")
    assert_usable(page, "#navgrp-work")
    page.click("#navgrp-more")
    box = assert_usable(page, "#nav-flyout")
    assert page.evaluate("document.getElementById('nav-flyout').classList.contains('nav-flyout-sheet')")
    assert box["left"] >= 0 and box["right"] <= 390 and box["bottom"] <= 800, box
    assert_usable(page, '#nav-flyout [data-nav-item="gallery"]')
    assert probe(page, '#nav-flyout [data-nav-item="gallery"]')["height"] >= 44
    assert_no_sideways_scroll(page)
    assert_usable(page, "#nav-flyout .nav-flyout-close")
    page.click("#nav-flyout .nav-flyout-close")
    assert page.evaluate("document.getElementById('nav-flyout').hidden")


@pytest.mark.parametrize("style", STYLES)
def test_off_mode_keeps_the_hamburger_as_the_opener_and_draws_no_rail(open_app, style):
    page = open_app(1440, style=style, chat=False)
    page.evaluate("localStorage.setItem('odysseus-sidebar-mode', 'off')")
    page.reload()
    wait_ready(page, chat=False)
    page.wait_for_function("document.documentElement.classList.contains('ody-sidebar-off')")
    assert not probe(page, "#icon-rail")["visible"]
    assert_usable(page, "#hamburger-btn")
    page.click("#hamburger-btn")
    page.wait_for_function("!document.documentElement.classList.contains('ody-sidebar-off')")
    assert probe(page, "#sidebar")["visible"] or probe(page, "#icon-rail")["visible"]


def test_an_activity_dot_shows_on_the_group_icon(open_app):
    page = open_app(1440, chat=False)
    go_mini(page)
    assert not probe(page, "#navgrp-work .nav-rail-dot")["visible"]
    page.evaluate("document.getElementById('assistant-notif-dot').style.display = 'inline-block'")
    page.wait_for_function("!document.querySelector('#navgrp-work .nav-rail-dot').hidden")
    assert probe(page, "#navgrp-work .nav-rail-dot")["visible"]
    assert page.evaluate("document.querySelector('#navgrp-more .nav-rail-dot').hidden")
