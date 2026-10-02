"""Cross-style navigation and persisted appearance in the real static app."""
import json
import pytest
from tests.helpers.agamemnon_browser import Chromium, StaticAppServer, chromium_path

pytestmark = pytest.mark.skipif(not chromium_path(), reason='needs Chromium')

@pytest.mark.parametrize('width', [1440, 700, 390])
def test_command_and_style_independent_of_colorway(width):
    with StaticAppServer() as server:
        browser = Chromium(chromium_path())
        try:
            page = browser.page(width, 850)
            page.goto(server.url, settle=1)
            assert page.eval("document.documentElement.dataset.style") == 'agamemnon'
            assert page.eval("document.querySelector('.ag-brand-name').textContent") == 'AGAMEMNON'
            if width < 768:
                page.click('#hamburger-btn')
            page.click('#sidebar-command-toggle')
            assert page.eval("[...document.querySelectorAll('#sidebar-command-menu button')].map(x => x.textContent)") == ['Email', 'Phalanx', 'Workbench', 'New chat', 'Search']
            assert page.eval("document.activeElement.textContent") == 'Email'
            page.click('#sidebar-command-menu [data-command-target="rail-agents"]')
            page.wait_for("document.getElementById('agents-dashboard')?.classList.contains('visible') || getComputedStyle(document.getElementById('agents-dashboard')).display !== 'none'")
            page.eval("document.getElementById('close-agents-dashboard')?.click()")
            page.eval("document.getElementById('rail-theme').click()")
            page.wait_for("document.querySelector('#theme-popup').getBoundingClientRect().bottom <= innerHeight + 2")
            page.click('#theme-style-toggle')
            assert page.eval("document.documentElement.dataset.style") == 'classic'
            page.click('#themeGrid [data-theme="ocean"]')
            assert page.eval("document.documentElement.dataset.style") == 'classic'
            assert page.eval("JSON.parse(localStorage.getItem('odysseus-theme')).name") == 'ocean'
            page.click('#theme-style-toggle')
            assert page.eval("document.documentElement.dataset.style") == 'agamemnon'
            assert page.eval("JSON.parse(localStorage.getItem('odysseus-theme')).name") == 'ocean'
            page.goto(server.url, settle=1)
            assert page.eval("document.documentElement.dataset.style") == 'agamemnon'
            assert page.eval("JSON.parse(localStorage.getItem('odysseus-theme')).name") == 'ocean'
            page.eval("document.getElementById('rail-workbench').click()")
            page.wait_for("getComputedStyle(document.getElementById('workbench-modal')).display !== 'none'")
            assert not page.errors, page.errors
            page.close()
        finally:
            browser.close()


def test_legacy_palette_preserved_and_rail_order_persists():
    with StaticAppServer() as server:
        browser = Chromium(chromium_path())
        try:
            page = browser.page(1440, 900)
            theme = {'name': 'light', 'colors': {'bg': '#f0ebe3', 'fg': '#5a5248', 'panel': '#faf6f0', 'border': '#d4cdc2', 'red': '#c47d5a'}}
            page.before_load('localStorage.setItem("odysseus-theme", ' + json.dumps(json.dumps(theme)) + ');')
            page.goto(server.url, settle=1)
            assert page.eval("document.documentElement.dataset.style") == 'classic'
            assert page.eval("document.querySelector('.ag-brand-name').textContent") == 'AGAMEMNON'
            assert page.eval("document.querySelector('.brand-crest-icon').getBoundingClientRect().width > 0")
            page.eval("import('/static/js/navOrder.js').then(m => {m.writeNavOrder(['command','email','calendar']);m.applyNavOrder()})")
            page.wait_for("document.querySelector('#icon-rail .icon-rail-btn')?.id === 'rail-command'")
            page.eval("document.getElementById('rail-command').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown',altKey:true,bubbles:true}))")
            page.wait_for("document.querySelector('#icon-rail .icon-rail-btn')?.id !== 'rail-command'")
            assert page.eval("JSON.parse(localStorage.getItem('odysseus-nav-order-v1')).value[0]") != 'command'
            page.goto(server.url, settle=1)
            assert page.eval("document.querySelector('#icon-rail .icon-rail-btn').id") != 'rail-command'
            assert page.eval("document.documentElement.dataset.style") == 'classic'
            page.close()
        finally:
            browser.close()


def test_command_search_new_chat_email_and_keyboard_close():
    with StaticAppServer() as server:
        browser = Chromium(chromium_path())
        try:
            page = browser.page(1440, 900)
            page.goto(server.url, settle=1)
            page.click('#sidebar-command-toggle')
            page.eval("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape',bubbles:true}))")
            assert page.eval("document.getElementById('sidebar-command-toggle').getAttribute('aria-expanded')") == 'false'
            page.eval("window.searchClicked = false; document.getElementById('rail-search-btn').addEventListener('click', () => window.searchClicked = true, {once:true})")
            page.click('#sidebar-command-toggle')
            page.click('#sidebar-command-menu [data-command-target="rail-search-btn"]')
            assert page.eval('window.searchClicked')
            page.goto(server.url, settle=1)
            page.click('#sidebar-command-toggle')
            page.click('#sidebar-command-menu [data-command-target="rail-email"]')
            page.wait_for("document.querySelector('#email-lib-modal')?.offsetWidth > 0")
            page.goto(server.url, settle=1)
            page.click('#sidebar-command-toggle')
            page.click('#sidebar-command-menu [data-command-target="rail-new-session"]')
            page.wait_for("document.querySelector('#chat-container')?.classList.contains('welcome-active')")
            page.close()
        finally:
            browser.close()
