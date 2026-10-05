"""Signing in through static/login.html against the real backend."""
from __future__ import annotations

import re

import pytest

from tests.helpers.live_app import ADMIN, PASSWORD
from tests.helpers.static_app import expect

pytestmark = [pytest.mark.browser, pytest.mark.xdist_group("live_app")]


def test_sign_in_opens_the_app_and_a_wrong_password_does_not(live_app, new_page):
    page = new_page(1440)
    # Signed out, the app sends the browser to the sign-in page.
    page.goto(live_app.url + "/")
    page.wait_for_url(re.compile(r"/login$"))
    error = page.locator("#error")

    page.fill("#username", ADMIN)
    page.fill("#password", "not-the-password")
    with page.expect_response(lambda r: r.url.endswith("/api/auth/login")) as attempt:
        page.click("#submitBtn")
    assert attempt.value.status == 401
    expect(error).not_to_have_text("")
    assert page.url.endswith("/login")

    page.fill("#password", PASSWORD)
    page.click("#submitBtn")
    page.wait_for_url(live_app.url + "/")
    # The page holds a second, hidden #message; the composer is the one with the role.
    expect(page.get_by_role("textbox", name="Message input")).to_be_editable()
    status = page.request.get(live_app.url + "/api/auth/status").json()
    assert status.get("authenticated") is True and status.get("username") == ADMIN, status
