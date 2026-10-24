"""Chat and task launches must remain legible, distinct and keyboard reachable."""
import pytest

from tests.helpers.static_app import LIGHT_THEME, ODYSSEUS_THEME, open_agents, probe, assert_no_sideways_scroll

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width', [1440, 700, 390])
@pytest.mark.parametrize('style,theme', [
    ('agamemnon', LIGHT_THEME), ('agamemnon', ODYSSEUS_THEME),
    ('classic', LIGHT_THEME), ('classic', ODYSSEUS_THEME),
])
def test_launch_actions_show_their_difference_without_clipping(open_app, width, style, theme):
    page = open_app(width, style=style, theme=theme)
    page.route('**/api/agents/overview*', lambda route: route.fulfill(json={
        **route.fetch().json(), 'profiles': [{'name': 'Reader', 'description': 'Reads files'}]}))
    open_agents(page)
    for action in ('new-conversation', 'launch'):
        selector = f'#agents-dashboard .ag-start-actions [data-ag="{action}"]'
        box = probe(page, selector)
        assert box['hit'] and box['height'] >= 44, box
        assert page.locator(selector).evaluate('(el) => el.scrollHeight <= el.clientHeight + 1')
        page.locator(selector).focus()
        assert page.locator(selector).evaluate('(el) => document.activeElement === el')
    assert_no_sideways_scroll(page, '.ag-head')
    page.locator('[data-ag="new-conversation"]').press('Enter')
    page.wait_for_selector('#ag-newconv')
    assert page.locator('[data-ag="new-conversation"]').get_attribute('aria-expanded') == 'true'
    selector = '.ag-newconv-list [data-new-chat]'
    assert probe(page, selector)['hit']
    parent = page.locator('.ag-newconv-list').bounding_box()
    child = page.locator(selector).bounding_box()
    assert child['x'] + child['width'] <= parent['x'] + parent['width'] + 1
