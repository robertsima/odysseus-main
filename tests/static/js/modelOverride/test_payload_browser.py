"""Custom and inherited task/assistant overrides must reach their save APIs."""
import pytest

pytestmark = pytest.mark.browser
CUSTOM = '__odysseus_custom_model__'


def test_stt_local_endpoint_custom_default_and_locked_controls(open_app):
    """Legacy inline hiding must not hide the enhanced endpoint voice picker."""
    page = open_app(1440, height=920)
    payloads = []
    page.route('**/api/stt/providers', lambda r: r.fulfill(json={'providers': [
        {'id': 'local', 'name': 'Local', 'privacy': 'local'},
        {'id': 'endpoint:voice', 'name': 'Voice', 'privacy': 'hosted'}]}))
    def preferences(route):
        if route.request.method == 'PUT':
            payloads.append(route.request.post_data_json)
        route.fulfill(json={'enabled': True, 'provider': 'local', 'model': 'base'})
    page.route('**/api/stt/preferences', preferences)
    page.route('**/api/models', lambda r: r.fulfill(json={'items': [
        {'endpoint_name': 'voice', 'url': 'http://voice/v1', 'model_type': 'stt', 'models': ['whisper-1']}]}))
    page.reload()
    page.wait_for_function('!!window.agentsDashboard')
    page.evaluate("async () => { const m = await import('/static/js/settings.js'); m.default.open('models'); }")
    provider = page.locator('#set-sttProviderSelect')
    page.wait_for_function("document.querySelector('#set-sttModelSelect').closest('.model-override-control') !== null")
    local = page.locator('#set-sttModelSelect')
    local.select_option('small')
    page.wait_for_function("document.querySelector('#set-sttSettingsMsg').textContent === 'Saved'")
    assert payloads[-1]['model'] == 'small'
    provider.select_option('endpoint:voice')
    endpoint = page.locator('#set-sttModelInput').locator('..').locator('select')
    assert endpoint.is_visible()
    endpoint.select_option(CUSTOM)
    page.locator('#set-sttModelInput').fill('private/voice')
    page.locator('#set-sttLangInput').click()
    page.wait_for_timeout(150)
    assert payloads[-1]['provider'] == 'endpoint:voice'
    assert payloads[-1]['model'] == 'private/voice'
    endpoint.select_option('whisper-1')
    page.wait_for_timeout(150)
    assert payloads[-1]['model'] == 'whisper-1'
    endpoint.select_option('')
    page.wait_for_timeout(150)
    assert payloads[-1]['model'] == 'base'
    page.locator('#set-sttModelInput').evaluate('el => el.disabled = true')
    page.wait_for_timeout(50)
    assert endpoint.is_disabled()
    assert page.locator('#set-sttModelInput').is_disabled()
    provider.select_option('local')
    assert local.is_visible()


def test_task_model_custom_catalog_default_payloads(open_app):
    page = open_app(1440, height=920)
    page.route('**/api/models', lambda r: r.fulfill(json={'items': [
        {'endpoint_name': 'local', 'url': 'http://local/v1', 'models': ['qwen']}
    ]}))
    payloads = []
    def tasks(route):
        if route.request.method == 'POST':
            payloads.append(route.request.post_data_json)
            route.fulfill(json={'id': 'test-task', 'ok': True})
        else:
            route.fulfill(json={'tasks': []})
    page.route('**/api/tasks', tasks)
    for choice, text, model, endpoint in [
        (CUSTOM, 'private/custom', 'private/custom', ''),
        ('http://local/v1::qwen', None, 'qwen', 'http://local/v1'),
        ('', None, '', ''),
    ]:
        page.evaluate('window.tasksModule.openTasks()')
        page.locator('.tasks-tab[data-tab="new"]').click()
        page.locator('#tasks-modal [data-idx="0"]').click()
        select = page.locator('#task-form-model')
        page.wait_for_function("Array.from(document.querySelector('#task-form-model').options).some(o => o.value === 'http://local/v1::qwen')")
        select.select_option(choice)
        if text:
            select.locator('..').locator('input').fill(text)
        page.locator('#task-form-name').fill('Payload check')
        page.locator('#task-form-prompt').fill('Check payload')
        page.locator('#task-form-save').click()
        page.wait_for_timeout(150)
        assert payloads[-1]['model'] == model
        assert payloads[-1]['endpoint_url'] == endpoint


def test_assistant_model_custom_and_default_payloads(open_app):
    page = open_app(1440, height=920)
    payloads = []
    def settings(route):
        if route.request.method != 'GET':
            payloads.append(route.request.post_data_json)
        route.fulfill(json={'crew': {}, 'check_ins': []})
    page.route('**/api/assistant/settings', settings)
    page.route('**/api/model-endpoints', lambda r: r.fulfill(json=[]))
    for choice, text, expected in [(CUSTOM, 'private/assistant', 'private/assistant'), ('', None, None)]:
        page.evaluate("async () => { const m = await import('/static/js/assistant.js'); m.openAssistantSettings(); }")
        select = page.locator('#assistant-model')
        select.select_option(choice)
        if text:
            select.locator('..').locator('input').fill(text)
        page.locator('#assistant-settings-save').click()
        page.wait_for_timeout(150)
        assert payloads[-1]['model'] == expected
        assert payloads[-1]['endpoint_url'] is None
