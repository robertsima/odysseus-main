"""Page style, colourways, the brand crest and background effects
(static/js/theme.js and the stylesheets), checked over the work surfaces.

The page style (Agamemnon or classic) and the colourway are separate choices:
switching one must keep the other, across a reload.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import (
    LIGHT_THEME, ODYSSEUS_THEME, PARENT_ID, assert_usable, contrast, expect, open_agents, open_workbench, probe,
    select_agent, set_select, settle, wait_ready,
)

pytestmark = pytest.mark.browser

COMMANDS = ["rail-email", "rail-agents", "rail-workbench", "rail-new-session", "rail-search-btn"]
CANVASES = "document.querySelectorAll('canvas[id$=\"-canvas\"]').length"


def style_of(page) -> str:
    return page.evaluate("document.documentElement.dataset.style")


def stored_theme(page) -> str:
    return page.evaluate("JSON.parse(localStorage.getItem('odysseus-theme')).name")


def background(page, selector: str) -> str:
    return page.evaluate("(s) => getComputedStyle(document.querySelector(s)).backgroundColor", selector)


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_page_style_and_colourway_switch_independently(open_app, static_app, width):
    page = open_app(width, chat=False, height=850)
    assert style_of(page) == "agamemnon"
    expect(page.locator(".ag-brand-name")).to_have_text("AGAMEMNON")
    if width < 768:
        page.click("#hamburger-btn")
        settle(page, "#sidebar")
    page.click("#sidebar-command-toggle")
    menu = page.evaluate("[...document.querySelectorAll('#sidebar-command-menu button')]"
                         ".map(b => ({target: b.dataset.commandTarget, named: b.textContent.trim().length > 0}))")
    assert [item["target"] for item in menu] == COMMANDS and all(item["named"] for item in menu), menu
    assert page.evaluate("document.activeElement.dataset.commandTarget") == "rail-email"
    page.click('#sidebar-command-menu [data-command-target="rail-agents"]')
    page.wait_for_function("getComputedStyle(document.getElementById('agents-dashboard')).display !== 'none'")
    page.evaluate("document.getElementById('close-agents-dashboard')?.click()")

    page.evaluate("document.getElementById('rail-theme').click()")
    page.wait_for_function("document.querySelector('#theme-popup').getBoundingClientRect().bottom <= innerHeight + 2")
    page.click("#theme-style-toggle")
    assert style_of(page) == "classic"
    page.click('#themeGrid [data-theme="ocean"]')
    assert style_of(page) == "classic"
    assert stored_theme(page) == "ocean"
    page.click("#theme-style-toggle")
    assert style_of(page) == "agamemnon"
    assert stored_theme(page) == "ocean"

    page.goto(static_app.url)
    wait_ready(page, chat=False)
    assert style_of(page) == "agamemnon"
    assert stored_theme(page) == "ocean"
    page.evaluate("document.getElementById('rail-workbench').click()")
    page.wait_for_function("getComputedStyle(document.getElementById('workbench-modal')).display !== 'none'")
    assert not page.errors, page.errors


@pytest.mark.parametrize("theme", [None, LIGHT_THEME, ODYSSEUS_THEME])
def test_sidebar_crest_takes_the_brand_colour_and_customization_keeps_navigation(open_app, theme):
    page = open_app(1440, theme=theme)
    style = style_of(page)
    crest = page.evaluate("(() => { const el = document.querySelector('.sidebar-brand-icon'), cs = getComputedStyle(el);"
                          " return {tag: el.tagName, mask: cs.webkitMaskImage || cs.maskImage, bg: cs.backgroundColor,"
                          " hidden: el.getAttribute('aria-hidden')}; })()")
    # A masked element, not an <img>: an image keeps the SVG's own gold
    # whatever the palette, the mask is painted in the wordmark's colour.
    assert "agamemnon-trojan-helmet.svg" in crest["mask"], crest
    assert crest["tag"] != "IMG" and crest["hidden"] == "true", crest
    assert crest["bg"] == probe(page, ".sidebar-brand-title")["color"], crest
    if theme == LIGHT_THEME:
        assert crest["bg"] == "rgb(196, 125, 90)", crest  # the light colourway's accent
    box = probe(page, ".sidebar-brand-icon")
    assert box["width"] == 40 and box["height"] == 40 and box["visible"], box
    assert probe(page, ".sidebar")["width"] == 240
    hamburger = probe(page, "#hamburger-btn")
    assert box["left"] - hamburger["right"] >= 8, (hamburger, box)
    # Each style keeps its own sidebar entry point.
    assert probe(page, "#sidebar-command-toggle")["visible"] == (style == "agamemnon")
    assert probe(page, "#sidebar-new-chat-btn")["visible"] == (style == "classic")
    # The "Agamemnon crest" colour in Customize > Advanced reaches the mark.
    page.evaluate("document.documentElement.style.setProperty('--brand-color', 'rgb(1, 2, 3)')")
    assert background(page, ".sidebar-brand-icon") == "rgb(1, 2, 3)"
    assert probe(page, ".sidebar-brand-title")["color"] == "rgb(1, 2, 3)"

    page.evaluate("document.getElementById('theme-modal').classList.remove('hidden')")
    page.click('#theme-tabs [data-tab="theme-tab-customize"]')
    assert page.evaluate("(() => { const c = document.querySelector('#theme-modal .admin-card:has(#theme-font-select)');"
                         " return c.scrollWidth <= c.clientWidth + 1; })()")
    set_select(page, "#theme-bg-pattern-select", "dots")
    assert "bg-pattern-dots" in page.evaluate("document.body.className")
    assert "radial-gradient" in page.evaluate("getComputedStyle(document.body).backgroundImage")


@pytest.mark.parametrize("width", [1440, 390])
def test_rain_shows_through_the_chat_but_not_through_work_windows(open_app, width):
    page = open_app(width)
    set_select(page, "#theme-bg-pattern-select", "rain")
    page.wait_for_function("document.querySelector('#rain-canvas') && document.body.classList.contains('bg-pattern-rain')")
    assert page.evaluate("getComputedStyle(document.querySelector('#rain-canvas')).pointerEvents") == "none"
    assert background(page, ".chat-container").endswith("0.76)")
    open_workbench(page)
    assert background(page, ".workbench-modal-content").startswith("rgb(")
    assert (page.evaluate("+getComputedStyle(document.querySelector('#rain-canvas')).zIndex")
            < page.evaluate("+getComputedStyle(document.querySelector('#workbench-modal')).zIndex"))
    assert background(page, ".wb-activity") != "rgba(0, 0, 0, 0)"
    page.click("#close-workbench-modal")
    open_agents(page)
    assert background(page, ".agents-modal-content").startswith("rgb(")
    page.click("#close-agents-dashboard")
    set_select(page, "#theme-bg-pattern-select", "none")
    page.wait_for_function("!document.querySelector('#rain-canvas')")
    assert background(page, ".chat-container").startswith("rgb(")
    # Reduced motion stops the animation but keeps the pattern class.
    set_select(page, "#theme-bg-pattern-select", "rain")
    page.wait_for_function("!!document.querySelector('#rain-canvas')")
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function("!document.querySelector('#rain-canvas')")
    assert page.evaluate("document.body.classList.contains('bg-pattern-rain')")
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_function("!!document.querySelector('#rain-canvas')")


@pytest.mark.parametrize("theme", [
    ODYSSEUS_THEME,
    # The old CDP test "reloaded" by navigating to its own URL with a
    # fragment, which does not reload, so it never saw this.
    pytest.param(None, marks=pytest.mark.xfail(strict=True, reason=(
        "static/js/theme.js saves the effect only when a colourway is already stored, "
        "so on a fresh profile the choice is lost on reload"))),
], ids=["stored-colourway", "fresh-profile"])
def test_background_effect_survives_a_reload(open_app, theme):
    page = open_app(1440, theme=theme)
    set_select(page, "#theme-bg-pattern-select", "rain")
    page.wait_for_function("!!document.querySelector('#rain-canvas')")
    page.reload()
    wait_ready(page)
    assert page.evaluate("document.getElementById('theme-bg-pattern-select').value") == "rain"
    page.wait_for_function("!!document.querySelector('#rain-canvas')")


@pytest.mark.parametrize("theme", [None, ODYSSEUS_THEME, LIGHT_THEME])
def test_floating_panels_are_opaque_over_lattice_and_keep_the_saved_soldier(open_app, static_app, theme):
    static_app.state.appearance[PARENT_ID] = "scout"
    page = open_app(1440, theme=theme, style="classic" if theme else "agamemnon")
    set_select(page, "#theme-bg-pattern-select", "lattice")
    assert page.evaluate("document.body.classList.contains('bg-pattern-lattice')")
    assert page.evaluate("getComputedStyle(document.body).backgroundImage") != "none"
    assert page.evaluate("getComputedStyle(document.querySelector('.chat-container')).visibility") == "visible"
    open_agents(page)
    panel = probe(page, ".agents-modal-content")
    assert 0 < panel["left"] and panel["width"] < page.viewport_size["width"], panel
    assert background(page, ".agents-modal-content").startswith("rgb(")
    assert page.evaluate("getComputedStyle(document.querySelector('#agents-dashboard .ag-soldier-sprite')).display") == "block"
    assert page.evaluate("getComputedStyle(document.querySelector('#agents-dashboard .ag-seal-mark')).display") == "none"
    variant = f'.ag-card[data-sid="{PARENT_ID}"] .ag-bot'
    expect(page.locator(variant).first).to_have_attribute("data-soldier-variant", "scout")
    select_agent(page, PARENT_ID)
    expect(page.locator("#ag-detail .ag-bot").first).to_have_attribute("data-soldier-variant", "scout")
    page.click("#close-agents-dashboard")
    open_workbench(page, activity=False)
    wb = probe(page, ".workbench-modal-content")
    assert wb["width"] < page.viewport_size["width"] and wb["left"] > 0, wb
    assert background(page, ".workbench-modal-content").startswith("rgb(")
    assert page.evaluate("getComputedStyle(document.querySelector('.chat-container')).visibility") == "visible"


def test_lattice_stays_still_under_reduced_motion(open_app):
    page = open_app(1440)
    page.emulate_media(reduced_motion="reduce")
    set_select(page, "#theme-bg-pattern-select", "lattice")
    assert page.evaluate("getComputedStyle(document.body).backgroundImage") != "none"
    assert page.evaluate(CANVASES) == 0


def test_animated_effects_replace_each_other_and_classic_rain_stays_behind(open_app):
    page = open_app(700)
    for effect in ("synapse", "constellations", "perlin-flow", "petals", "sparkles", "embers"):
        set_select(page, "#theme-bg-pattern-select", effect)
        page.wait_for_function("(id) => !!document.getElementById(id)", arg=f"{effect}-canvas")
        assert page.evaluate(CANVASES) == 1, effect
    set_select(page, "#theme-bg-pattern-select", "none")
    assert page.evaluate(CANVASES) == 0
    classic = open_app(700, theme=ODYSSEUS_THEME)
    set_select(classic, "#theme-bg-pattern-select", "rain")
    classic.wait_for_function("!!document.querySelector('#rain-canvas')")
    assert style_of(classic) == "classic"
    assert classic.evaluate("getComputedStyle(document.querySelector('#rain-canvas')).zIndex") == "0"


def test_agamemnon_text_is_legible(open_app):
    page = open_app(1440)

    def legible(selectors):
        for selector in selectors:
            assert contrast(page, selector) >= 4.5, selector
            assert probe(page, selector)["fontSize"] >= 12, selector

    legible([".ag-page-heading p", ".ag-page-heading strong", "#message"])
    open_agents(page)
    legible([".ag-row-latest", ".ag-row-meta-inline", ".ag-window-title>span", ".ag-appearance p"])
    page.click("#close-agents-dashboard")
    open_workbench(page)
    legible([".ag-run-summary p", ".ag-run-detail dd", ".ag-run-detail p", ".ag-run-console-output"])
    assert_usable(page, "#close-workbench-modal")
