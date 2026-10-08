"""Session title activation must not consume keystrokes in its rename editor."""
import pytest

from tests.helpers.static_app import expect, set_checkbox

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('style', ['classic', 'agamemnon'])
def test_title_rename_accepts_spaces_and_enter_without_opening_actions(open_app, style):
    page = open_app(1440)
    page.evaluate("document.getElementById('rail-theme').click()")
    page.wait_for_selector('#theme-style-toggle', state='attached')
    set_checkbox(page, '#theme-style-toggle', style == 'agamemnon')
    page.keyboard.press('Escape')
    page.locator('#current-meta').focus()
    page.keyboard.press('Enter')
    page.click('#export-rename-btn')
    editor = page.locator('.session-rename-input')
    editor.press_sequentially('Quarterly planning notes')
    expect(editor).to_have_value('Quarterly planning notes')
    assert not page.locator('#export-dropdown-menu').evaluate("e => e.classList.contains('open')")
    with page.expect_request(lambda r: r.method == 'PATCH' and '/api/session/' in r.url) as rename:
        editor.press('Enter')
    assert 'Quarterly planning notes' in rename.value.post_data
    expect(editor).to_have_count(0)
    assert not page.locator('#export-dropdown-menu').evaluate("e => e.classList.contains('open')")
