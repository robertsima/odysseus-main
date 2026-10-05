"""A Settings toggle (static/js/settings.js) saved to the real backend."""
from __future__ import annotations

import pytest

from tests.helpers.static_app import expect

pytestmark = [pytest.mark.browser, pytest.mark.xdist_group("live_app")]


def open_agent_settings(page):
    page.click("#user-bar-settings")
    page.click('[data-settings-tab="agents"]')
    return page.locator("#set-wbAutoOpen")


def test_settings_toggle_is_saved_and_survives_a_reload(live_app, live_page):
    page = live_page(1440)
    toggle = open_agent_settings(page)
    expect(toggle).to_be_checked()  # on by default
    try:
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/auth/settings")) as saved:
            # The checkbox is drawn as a switch; click what a user clicks.
            page.click("label.admin-switch:has(#set-wbAutoOpen)")
        assert saved.value.ok, saved.value.text()
        expect(toggle).not_to_be_checked()

        page.reload()
        toggle = open_agent_settings(page)
        expect(toggle).not_to_be_checked()
        with live_app.client() as client:
            assert client.get("/api/auth/settings").json()["workbench_auto_open"] is False
    finally:
        with live_app.client() as client:
            client.post("/api/auth/settings", json={"workbench_auto_open": True})
