"""The Library's chat tidy action (static/js/documentLibrary.js)."""
from __future__ import annotations

import pytest

from tests.helpers.static_app import expect

pytestmark = pytest.mark.browser


def test_tidy_posts_to_the_registered_route_once_and_reenables(open_app, static_app):
    page = open_app(1440)
    page.click("#chats-library-btn")
    tidy = page.locator("#doclib-chats-tidy-btn")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/chats/tidy")):
        tidy.click()
    expect(tidy).to_be_enabled()
    assert static_app.state.chat_tidy_requests == 1
    assert static_app.state.posted("/api/chats/tidy") == [{}]
