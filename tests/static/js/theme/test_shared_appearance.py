"""A palette switch must not erase a font choice or bypass shared controls."""
import json
import pytest

from tests.helpers.static_app import (
    assert_no_sideways_scroll, expect, open_agents, open_workbench,
    set_select, set_checkbox, settle, wait_ready,
)

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('style', ['agamemnon', 'classic'])
def test_newer_account_font_and_palette_apply_to_either_composition(static_app, new_page, style):
    page = new_page(1440)
    local = {'name': 'dark', 'colors': {'bg': '#222222', 'fg': '#eeeeee', 'panel': '#333333',
                                      'border': '#444444', 'red': '#ff5555'}, 'font': 'mono', 'updated_at': 1}
    account = {**local, 'name': 'paper', 'font': 'serif', 'colors': {**local['colors'], 'bg': '#f0efee'}}
    page.add_init_script(f"localStorage.setItem('odysseus-theme', {json.dumps(json.dumps(local))});"
                         f"localStorage.setItem('odysseus-page-style-v1', {json.dumps(json.dumps({'value': style, 'updated_at': 1}))});")
    page.route('**/api/prefs/theme', lambda route: route.fulfill(json={'key': 'theme', 'value': {'value': account, 'updated_at': 900}}))
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    page.wait_for_function("document.documentElement.style.getPropertyValue('--bg') === '#f0efee'")
    assert 'Georgia' in font(page, '#message')
    assert page.evaluate('document.documentElement.dataset.style') == style
    page.reload()
    wait_ready(page, chat=False)
    assert 'Georgia' in font(page, '#message')


@pytest.mark.parametrize('style', ['agamemnon', 'classic'])
def test_shared_font_resolver_paints_custom_font_before_modules(static_app, new_page, style):
    page = new_page(1440)
    saved = {'name': 'dark', 'font': "Reader's Choice", 'updated_at': 1}
    page.add_init_script(f"localStorage.setItem('odysseus-theme', {json.dumps(json.dumps(saved))});"
                         f"localStorage.setItem('odysseus-page-style-v1', {json.dumps(json.dumps({'value': style, 'updated_at': 1}))});")
    # Pause the module graph, not the shared parser-blocking resolver.
    page.route('**/static/js/theme.js', lambda route: route.abort())
    page.goto(static_app.url)
    assert page.evaluate('document.documentElement.dataset.style') == style
    assert "Reader's Choice" in font(page, 'body')


def font(page, selector):
    return page.locator(selector).first.evaluate("e => getComputedStyle(e).fontFamily")


def appearance(page, style):
    page.evaluate("document.getElementById('rail-theme').click()")
    page.wait_for_selector('#themeGrid [data-theme="ocean"]')
    set_checkbox(page, '#theme-style-toggle', style == 'agamemnon')


@pytest.mark.parametrize('style', ['agamemnon', 'classic'])
def test_stock_palette_keeps_explicit_font_through_switch_and_reload(open_app, style):
    page = open_app(1440, chat=False)
    appearance(page, style)
    set_select(page, '#theme-font-select', 'serif')
    page.click('#themeGrid [data-theme="ocean"]')
    expect(page.locator('#theme-font-select')).to_have_value('serif')
    assert 'Georgia' in font(page, '#message')
    set_checkbox(page, '#theme-style-toggle', style != 'agamemnon')
    assert 'Georgia' in font(page, '#message')
    page.reload()
    wait_ready(page, chat=False)
    assert 'Georgia' in font(page, '#message')
    assert page.evaluate('document.documentElement.dataset.style') == ('classic' if style == 'agamemnon' else 'agamemnon')


@pytest.mark.parametrize('style', ['agamemnon', 'classic'])
def test_default_ui_face_is_consistent_across_shared_windows(open_app, style):
    page = open_app(1440, chat=False)
    appearance(page, style)
    body_font = font(page, 'body')
    assert font(page, '#theme-modal .modal-content') == body_font
    assert font(page, '#theme-font-select') == body_font
    page.keyboard.press('Escape')
    open_workbench(page)
    assert font(page, '.workbench-modal-content') == body_font
    page.evaluate("window.workbenchModule.close()")
    open_agents(page)
    assert font(page, '.agents-modal-content') == body_font


@pytest.mark.parametrize('width', [1440, 700, 390])
def test_completed_turn_timer_and_shared_controls_survive_appearance_switch(open_app, width):
    page = open_app(width)
    # Render through the production timer boundary (no model request).
    page.evaluate("async () => { const {showTurnDuration} = await import('/static/js/roundTiming.js');"
                  " showTurnDuration(document.querySelector('.msg-ai'), 73.5); }")
    badge = page.locator('.agent-turn-duration').first
    expect(badge).to_have_text('Turn · 1m 14s')
    for style in ['classic', 'agamemnon']:
        page.evaluate("async style => { const {savePageStyle} = await import('/static/js/theme.js'); savePageStyle(style); }", style)
        settle(page)
        expect(badge).to_be_visible()
        expect(page.locator('#message').first).to_be_visible()
        assert_no_sideways_scroll(page, '#chat-history')
        title = page.locator('#current-meta')
        expect(title).to_be_visible()
        title.focus()
        title.press('Enter')
        expect(page.locator('#export-dropdown-menu')).to_be_visible()
        page.evaluate("document.getElementById('export-rename-btn').click()")
        expect(page.locator('.session-rename-input')).to_be_visible()
        page.locator('.session-rename-input').press('Escape')
