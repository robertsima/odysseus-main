"""The root theme identity and first paint (static/js/theme.js and the inline
bootstrap in static/index.html).

``html[data-theme]`` names the identity that layout rules key on. It has to
follow every way a colourway is committed, and a saved palette, font or text
size has to be there at first paint, before any module runs.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import LIGHT_THEME, ODYSSEUS_THEME, expect, set_select, settle, wait_ready

pytestmark = pytest.mark.browser

HELMET = "agamemnon-trojan-helmet.svg"


def identity(page) -> str:
    return page.evaluate("document.documentElement.dataset.theme")


def stored(page) -> dict | None:
    return page.evaluate("JSON.parse(localStorage.getItem('odysseus-theme') || 'null')")


def root_var(page, name: str) -> str:
    return page.evaluate("(n) => document.documentElement.style.getPropertyValue(n).trim()", name)


def favicons(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('link[rel*=\"icon\"]')].map(l => l.href.split('/').pop())")


def open_theme_popup(page) -> None:
    page.evaluate("document.getElementById('rail-theme').click()")
    page.wait_for_selector('#themeGrid [data-theme="ocean"]')


def test_a_fresh_visit_paints_the_default_identity_and_palette(open_app):
    page = open_app(1440, chat=False)
    assert identity(page) == "dark"
    assert stored(page) is None
    for token in ("--bg", "--fg", "--panel", "--border", "--red", "--brand-color"):
        assert root_var(page, token), f"{token} is not set on the root"
    assert root_var(page, "--bg") == "#111417"


@pytest.mark.parametrize("theme, name, bg", [(ODYSSEUS_THEME, "odysseus", "#211f1c"), (LIGHT_THEME, "light", "#f0ebe3")])
def test_a_saved_palette_wins_at_first_paint_and_is_left_as_stored(open_app, theme, name, bg):
    page = open_app(1440, chat=False, theme=theme)
    assert identity(page) == name
    assert root_var(page, "--bg") == bg
    saved = stored(page)
    assert saved["name"] == name and saved["colors"]["bg"] == bg


def test_identity_follows_a_swatch_a_colour_edit_a_reload_and_the_reset_button(open_app, static_app):
    page = open_app(1440, chat=False)
    open_theme_popup(page)
    page.click('#themeGrid [data-theme="ocean"]')
    assert identity(page) == "ocean"
    # Editing a colour while a stock theme is active saves into the transient
    # "custom" slot. That is a storage choice, not a switch of identity.
    page.evaluate("(() => { const c = document.getElementById('clr-bg'); c.value = '#223344';"
                  " c.dispatchEvent(new Event('input', {bubbles: true})); })()")
    page.wait_for_function("JSON.parse(localStorage.getItem('odysseus-theme')).name === 'custom'")
    assert identity(page) == "ocean"
    assert stored(page)["colors"]["bg"] == "#223344"
    # A reload keeps the identity the layout was last given.
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    assert stored(page)["name"] == "custom"
    open_theme_popup(page)
    page.click('.admin-tab[data-tab="theme-tab-customize"]')
    page.click("#theme-reset-btn")
    assert identity(page) == "dark"
    assert stored(page) is None


def test_the_favicon_stays_the_product_helmet_whatever_the_colourway(open_app):
    page = open_app(1440, chat=False)
    assert favicons(page) == [HELMET, HELMET]
    open_theme_popup(page)
    page.click('#themeGrid [data-theme="ocean"]')
    settle(page)
    assert favicons(page) == [HELMET, HELMET]


def test_the_font_choice_survives_a_colour_edit(open_app):
    page = open_app(1440, chat=False)
    open_theme_popup(page)
    page.click('#themeGrid [data-theme="ocean"]')
    mono = root_var(page, "--font-family")
    set_select(page, "#theme-font-select", "serif")
    page.wait_for_function("JSON.parse(localStorage.getItem('odysseus-theme') || '{}').font === 'serif'")
    serif = root_var(page, "--font-family")
    assert serif != mono
    page.evaluate("(() => { const c = document.getElementById('clr-bg'); c.value = '#223344';"
                  " c.dispatchEvent(new Event('input', {bubbles: true})); })()")
    page.wait_for_function("JSON.parse(localStorage.getItem('odysseus-theme')).name === 'custom'")
    assert root_var(page, "--font-family") == serif
    assert stored(page)["font"] == "serif"
    expect(page.locator("#theme-font-select")).to_have_value("serif")


def test_a_saved_text_size_is_applied_before_the_first_paint(open_app):
    page = open_app(1440, chat=False)
    assert page.evaluate("getComputedStyle(document.documentElement).zoom") == "1"
    page.evaluate("localStorage.setItem('odysseus-ui-scale', '125')")
    page.add_init_script("document.addEventListener('DOMContentLoaded', () => {"
                         " window.__scaleAtParse = document.documentElement.className; });")
    page.reload()
    wait_ready(page, chat=False)
    assert "ui-scale-125" in page.evaluate("window.__scaleAtParse")
    assert page.evaluate("getComputedStyle(document.documentElement).zoom") == "1.25"
