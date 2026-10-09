"""A third tool must not reserve dock width twice or cover the editor/chat."""
import pytest


pytestmark = pytest.mark.browser


@pytest.fixture
def workspace_page(open_app):
    return open_app(1440, height=920)


def open_sections(page):
    page.evaluate("""async () => {
      const notes = await import('/static/js/notes.js?v=20260915vaulttree3');
      notes.openPanel();
      window.agentsDashboard.open();
    }""")
    page.wait_for_selector('#workspace-section-tabs')


def rectangles(page):
    return page.locator('.workspace-section:not(.workspace-parked)').evaluate_all("""els => els.map(el => {
      const r = el.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height};
    })""")


def assert_sections(page, count):
    rects = rectangles(page)
    assert len(rects) == count
    size = page.viewport_size
    for r in rects:
        assert r['w'] >= 320 and r['h'] >= 200
        assert r['x'] >= 0 and r['y'] >= 0
        assert r['x'] + r['w'] <= size['width'] + 1
        assert r['y'] + r['h'] <= size['height'] + 1
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            overlap = min(a['x'] + a['w'], b['x'] + b['w']) - max(a['x'], b['x'])
            vertical = min(a['y'] + a['h'], b['y'] + b['h']) - max(a['y'], b['y'])
            assert overlap < 1 or vertical < 1


def test_third_fourth_fifth_section_and_responsive_restore(workspace_page):
    page = workspace_page
    open_sections(page)
    assert_sections(page, 3)
    page.evaluate("""async () => {
      const d = await import('/static/js/document.js?v=20260928docedittarget1');
      d.default.openPanel();
    }""")
    page.wait_for_selector('.doc-editor-pane.workspace-section')
    assert_sections(page, 4)
    assert page.locator('.doc-divider').evaluate_all("els => els.every(el => getComputedStyle(el).display === 'none')")
    page.locator('#chat-history').evaluate("el => el.insertAdjacentHTML('beforeend', '<ol><li>Visible list marker</li></ol>')")
    assert page.locator('#chat-history ol').last.evaluate("el => parseFloat(getComputedStyle(el).paddingInlineStart) >= 30")
    page.evaluate("window.workbenchModule.open()")
    page.wait_for_function("document.querySelectorAll('#workspace-section-tabs button').length === 5")
    assert_sections(page, 4)
    page.set_viewport_size({'width': 720, 'height': 900})
    page.wait_for_timeout(100)
    assert_sections(page, 4)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_timeout(100)
    assert_sections(page, 1)
    page.get_by_role('button', name='Chat', exact=True).click()
    assert page.locator('#chat-container').is_visible()
    page.set_viewport_size({'width': 1440, 'height': 920})
    page.wait_for_timeout(100)
    assert_sections(page, 4)


def test_launch_model_default_catalog_and_custom(workspace_page):
    page = workspace_page
    page.route('**/api/models', lambda route: route.fulfill(json={'items': [
        {'endpoint_name': 'local', 'url': 'http://local/v1', 'models': ['qwen3']},
        {'endpoint_name': 'image', 'model_type': 'image', 'models': ['not-chat']},
    ]}))
    # The static settings model controls may already have requested the shared catalog.
    page.reload()
    page.wait_for_function('!!window.agentsDashboard')
    page.evaluate("window.agentsDashboard.open()")
    page.locator('[data-ag="launch"]').click()
    select = page.locator('#ag-model').locator('..').locator('select')
    select.select_option('qwen3@local')
    assert page.locator('#ag-model').input_value() == 'qwen3@local'
    select.select_option('__odysseus_custom_model__')
    page.locator('#ag-model').fill('private/custom@endpoint')
    assert page.locator('#ag-model').input_value() == 'private/custom@endpoint'
    select.select_option('')
    assert page.locator('#ag-model').input_value() == ''


def test_pointer_fullscreen_minimize_restore_and_observer_quiescence(workspace_page):
    page = workspace_page
    open_sections(page)
    page.evaluate("window.documentModule.openPanel()")
    page.wait_for_selector('.doc-editor-pane.workspace-section')
    assert page.locator('.workspace-section:not(.workspace-parked)').evaluate_all("""els => els.every(el => {
      const r = el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + 20));
    })""")
    page.evaluate("document.querySelector('.doc-editor-pane').classList.add('doc-fullscreen')")
    page.wait_for_function("document.querySelectorAll('.workspace-section:not(.workspace-parked)').length === 1")
    assert page.locator('.doc-editor-pane').bounding_box()['width'] > 1000
    page.evaluate("document.querySelector('.doc-editor-pane').classList.remove('doc-fullscreen')")
    page.wait_for_function("document.querySelectorAll('.workspace-section:not(.workspace-parked)').length === 4")
    page.evaluate("""async () => {
      const m = await import('/static/js/modalManager.js'); m.minimize('agents-dashboard');
    }""")
    page.wait_for_function("document.querySelectorAll('#workspace-section-tabs button').length === 3")
    page.evaluate("""async () => {
      const m = await import('/static/js/modalManager.js'); m.restore('agents-dashboard');
    }""")
    page.wait_for_function("document.querySelectorAll('#workspace-section-tabs button').length === 4")
    page.evaluate("""() => {
      window.workspaceMutations = 0;
      new MutationObserver(r => window.workspaceMutations += r.length).observe(document.body,
        {subtree:true, attributes:true, attributeFilter:['class'], childList:true});
    }""")
    page.wait_for_timeout(200)
    assert page.evaluate('window.workspaceMutations') < 30
    page.evaluate("window.documentModule.closePanel(); window.agentsDashboard.close()")
    page.wait_for_function("!document.body.classList.contains('workspace-split')")
    assert page.locator('#workspace-section-tabs').count() == 0


def test_fresh_mobile_tools_focus_new_section(open_app):
    page = open_app(390, height=844)
    open_sections(page)
    assert_sections(page, 1)
    assert page.locator('#agents-dashboard .modal-content').is_visible()
    page.get_by_role('button', name='Chat', exact=True).click()
    assert page.locator('#chat-container').is_visible()


@pytest.mark.parametrize('width,height', [(1440, 920), (720, 900)])
def test_running_progress_does_not_clip_composer(open_app, width, height):
    """Expanded running-worker/checklist content must yield to usable chat controls."""
    page = open_app(width, height=height)
    open_sections(page)
    page.evaluate('window.documentModule.openPanel()')
    page.wait_for_selector('.doc-editor-pane.workspace-section')
    page.wait_for_selector('.chat-progress-shelf .agent-strip-row')
    page.wait_for_timeout(150)
    assert page.locator('#chat-history').bounding_box()['height'] >= 60
    controls = page.locator('.chat-input-bar button:visible, .chat-input-bar textarea:visible')
    assert controls.count() >= 3
    assert controls.evaluate_all("""els => els.every(el => {
      const r = el.getBoundingClientRect(), pane = document.querySelector('#chat-container').getBoundingClientRect();
      return r.top >= pane.top && r.bottom <= pane.bottom && r.left >= pane.left && r.right <= pane.right
        && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    })""")
    page.locator('.chat-input-bar textarea:visible').fill('Composer remains usable')
    assert page.locator('.chat-input-bar textarea:visible').input_value() == 'Composer remains usable'
