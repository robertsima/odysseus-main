"""First-run theme choices must persist even when no preset was saved before."""
import pytest
from tests.helpers.static_app import wait_ready

pytestmark = pytest.mark.browser


def test_first_font_and_background_choices_persist_and_respect_reduced_motion(open_app):
    page = open_app(1440, chat=False)
    page.emulate_media(reduced_motion='reduce')
    page.evaluate("document.getElementById('theme-modal').classList.remove('hidden')")
    page.click('#theme-tabs [data-tab="theme-tab-customize"]')
    page.locator('#theme-font-select').select_option('editorial')
    # By its visible label, so the label stays tied to the control for screen readers.
    page.get_by_label('Background effect', exact=True).select_option('aurora')
    saved = page.evaluate("JSON.parse(localStorage.getItem('odysseus-theme'))")
    assert saved['font'] == 'editorial' and saved['bgPattern'] == 'aurora'
    assert page.evaluate("getComputedStyle(document.body).animationName") == 'none'
    page.reload()
    wait_ready(page, chat=False)
    assert page.evaluate("document.body.classList.contains('bg-pattern-aurora')")
    assert 'Palatino' in page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--font-family')")
    for effect in ['grid', 'diagonal', 'rings', 'aurora']:
        page.evaluate("async p => (await import('/static/js/theme.js')).applyBgPattern(p)", effect)
        assert page.evaluate("getComputedStyle(document.body).backgroundImage") != 'none'
        page.evaluate("async () => (await import('/static/js/theme.js')).applyBgPattern('none')")
        assert not page.evaluate("p => document.body.classList.contains('bg-pattern-' + p)", effect)
