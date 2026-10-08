"""Settings opened before auth finishes must refresh admin visibility in place."""
import pytest

from tests.helpers.static_app import expect, wait_ready

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('is_admin', [True, False])
def test_open_settings_updates_when_delayed_auth_resolves(static_app, new_page, is_admin):
    page = new_page(1440)
    pending = []
    status = {'authenticated': True, 'username': 'tester', 'is_admin': is_admin}
    released = False

    def auth_response(route):
        if released:
            route.fulfill(json=status)
        else:
            pending.append(route)

    page.route('**/api/auth/status', auth_response)
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    page.click('#user-bar-settings')
    agents = page.locator('[data-settings-tab="agents"]')
    expect(agents).to_be_hidden()
    assert pending
    released = True
    for route in pending:
        route.fulfill(json=status)
    page.wait_for_function('(admin) => window._isAdmin === admin', arg=is_admin)
    if is_admin:
        expect(agents).to_be_visible()
        agents.click()
        expect(page.locator('#set-wbAutoOpen')).to_be_attached()
    else:
        expect(agents).to_be_hidden()
