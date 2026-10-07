"""A docked or phone-sized skill editor must not clip Save or trap the text area."""
import json
import pytest
from tests.helpers.static_app import assert_no_sideways_scroll, assert_usable, wait_ready

pytestmark = pytest.mark.browser


def open_editor(page):
    def skills_api(route):
        path = route.request.url.split('/api/skills', 1)[1]
        if path == '':
            data = {'skills': [{'name': 'write-guide', 'status': 'draft', 'description': 'A writing guide'}]}
        elif path == '/write-guide/markdown':
            data = {'markdown': '# Writing guide\n\nKeep the reader in mind.\n' * 12, 'version': 'v1'}
        elif path == '/catalog':
            data = {'skills': []}
        else:
            data = {'status': 'none'}
        route.fulfill(content_type='application/json', body=json.dumps(data))
    page.route('**/api/skills**', skills_api)
    wait_ready(page)
    page.evaluate("document.getElementById('tool-memory-btn').click()")
    page.locator('[data-memory-tab="skills"]').click()
    page.locator('.skill-card-toggle').first.click()
    page.locator('.skill-md-pre').wait_for(state='visible')
    page.locator('.skill-card-preview').get_by_role('button', name='Edit', exact=True).click()
    page.locator('.skill-md-editor').wait_for(state='visible')


@pytest.mark.parametrize('width', [1440, 700, 390])
def test_editor_controls_fit_and_text_can_scale_and_resize(open_app, width):
    page = open_app(width)
    open_editor(page)
    if width == 1440:
        # A half-width tool window on a wide viewport must also wrap.
        page.locator('.memory-modal-content').evaluate("e => e.style.width = '620px'")
    ta = page.locator('.skill-md-editor')
    ta.fill('Draft one')
    ta.fill('Draft two')
    assert_usable(page, '.skill-md-undo')
    page.locator('.skill-md-undo').click()
    assert ta.input_value() == 'Draft one'
    page.get_by_label('Editor text size').select_option('18')
    assert ta.evaluate("e => getComputedStyle(e).fontSize") == '18px'
    assert ta.evaluate("e => getComputedStyle(e).resize") == 'vertical'
    assert ta.bounding_box()['height'] >= 160
    # Actions now follow the text instead of floating over it.
    page.locator('.skill-md-save').scroll_into_view_if_needed()
    assert_usable(page, '.skill-md-save')
    assert_no_sideways_scroll(page, '.memory-modal-content')


@pytest.mark.parametrize('width', [1440, 700, 390])
def test_editor_footer_never_covers_resized_text(open_app, width):
    """The action row must scroll after, not over, a tall editing surface."""
    page = open_app(width)
    open_editor(page)
    if width == 700:
        page.locator('.memory-modal-content').evaluate("e => e.style.width = '620px'")
    ta = page.locator('.skill-md-editor')
    ta.fill('\n'.join(f'Line {i}: keep this draft readable.' for i in range(80)))
    ta.evaluate("e => e.style.height = '900px'")
    preview = page.locator('.skill-card-preview')
    footer = preview.locator('.doclib-card-expanded-actions')
    for fraction in [0, 0.5, 1]:
        preview.evaluate('(e, f) => e.scrollTop = (e.scrollHeight - e.clientHeight) * f', fraction)
        geometry = preview.evaluate("""e => {
            const text = e.querySelector('.skill-md-editor').getBoundingClientRect();
            const actions = e.querySelector('.doclib-card-expanded-actions').getBoundingClientRect();
            return {textBottom: text.bottom, actionsTop: actions.top};
        }""")
        assert geometry['actionsTop'] >= geometry['textBottom'], 'Footer covers skill text'
    footer.scroll_into_view_if_needed()
    assert_usable(page, '.skill-md-save')
    ta.focus()
    page.keyboard.press('Control+End')
    page.keyboard.type('\nFinal visible draft line')
    assert ta.input_value().endswith('Final visible draft line')
