"""Fresh-account panels, keyboard activation and cross-panel hint lifetime."""
import pytest
from tests.helpers.static_app import expect

pytestmark = [pytest.mark.browser, pytest.mark.xdist_group('live_app')]


@pytest.mark.parametrize('width', [1440, 390])
def test_major_panels_use_default_font_and_keyboard_controls(live_app, live_page, width):
    page = live_page(width)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for selector, modal in [('#tool-memory-btn', '#memory-modal'),
                            ('#tool-workbench-btn', '#workbench-modal'),
                            ('#tool-agents-btn', '#agents-dashboard')]:
        if page.locator('#sidebar').evaluate("el => el.classList.contains('hidden')"):
            page.locator('#hamburger-btn').click()
        row = page.locator(selector)
        row.focus()
        row.press('Enter')
        expect(page.locator(modal)).to_be_visible()
        page.wait_for_timeout(500)  # panel's deliberate autofocus completes
        assert page.locator(modal).evaluate("el => getComputedStyle(el.querySelector('.modal-content')).fontFamily").startswith('"Space Grotesk"')
        if modal == '#memory-modal':
            skills = page.locator('[data-memory-tab="skills"]')
            skills.focus()
            skills.press('Enter')
            expect(page.locator('[data-memory-panel="skills"]')).to_be_visible()
        control = page.locator(modal + ' input:visible, ' + modal + ' button:visible').first
        control.focus()
        expect(control).to_be_focused()
        page.keyboard.press('Escape')
        expect(page.locator(modal)).to_be_hidden()
    page.locator('#user-bar-settings').evaluate('el => el.click()')
    expect(page.locator('#settings-modal')).to_be_visible()
    page.keyboard.press('Escape')
    assert not errors, errors


def test_onboarding_hint_is_removed_when_its_panel_closes(live_app, live_page):
    page = live_page(1440)
    page.evaluate("localStorage.removeItem('tour-hint-seen'); window.__hintDraws=[]; document.getElementById('tool-calendar-btn').click()")
    page.wait_for_selector('.tour-hint', timeout=5000)
    page.locator('#calendar-modal .close-btn').first.click()
    expect(page.locator('.tour-hint')).to_have_count(0, timeout=1500)
    page.locator('#tool-tasks-btn').evaluate('el => el.click()')
    page.wait_for_timeout(700)
    expect(page.locator('.tour-hint')).to_have_count(0)


@pytest.mark.parametrize('width', [1440, 390])
def test_attachment_previews_can_be_added_and_removed(live_app, live_page, width):
    page = live_page(width)
    page.locator('#file-input').set_input_files({'name': 'release-check.txt', 'mimeType': 'text/plain', 'buffer': b'Isolated attachment contract'})
    chip = page.locator('#attach-strip .thumb')
    expect(chip).to_have_count(1)
    expect(chip).to_contain_text('release-check.txt')
    chip.locator('button').click()
    expect(chip).to_have_count(0)
