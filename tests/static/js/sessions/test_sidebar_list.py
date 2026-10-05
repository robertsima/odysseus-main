"""The chat list in the sidebar (static/js/sessions.js) and the rail's delete
button (static/app.js): duplicate rows collapse, and a deleted chat is removed
on the server for good and from every local copy of the list.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import SESSION_ID, expect, wait_ready

pytestmark = pytest.mark.browser

SECOND = "sess-two"
BASE = {"id": SESSION_ID, "name": "Orders migration", "model": "anthropic/claude-sonnet-5",
        "updated_at": "2026-10-01T10:00:00Z", "message_count": 4}


def listed(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('#sidebar .session-item')].map(e => e.dataset.sessionId)")


def open_sidebar(new_page, static_app, sessions, seed_order=None):
    page = new_page(1440)
    if seed_order is not None:
        page.add_init_script("if (!localStorage.getItem('session-order')) "
                             f"localStorage.setItem('session-order', {json.dumps(json.dumps(seed_order))});")
    page.route("**/api/sessions", lambda route: route.fulfill(json=sessions))
    page.goto(static_app.url + f"/#{SESSION_ID}")
    wait_ready(page)
    page.click("#chats-section-title")
    expect(page.locator("#session-list")).to_be_visible()
    return page


def test_the_same_chat_returned_twice_is_listed_once(new_page, static_app):
    page = open_sidebar(new_page, static_app, [BASE, dict(BASE, id=SECOND, name="Second chat"), dict(BASE)])
    expect(page.locator("#sidebar .session-item")).to_have_count(2)
    assert sorted(listed(page)) == sorted([SESSION_ID, SECOND])


def record_session_requests(page, static_app):
    requests = []
    page.on("request", lambda r: requests.append((r.method, r.url.replace(static_app.url, "")))
            if "/api/session/" in r.url and r.method != "GET" else None)
    return requests


def test_deleting_a_chat_from_the_list_removes_it_for_good_and_from_the_saved_order(new_page, static_app):
    sessions = [BASE, dict(BASE, id=SECOND, name="Second chat")]
    page = open_sidebar(new_page, static_app, sessions, seed_order=[SESSION_ID, SECOND])
    page.route(f"**/api/session/{SECOND}", lambda route: route.fulfill(json={"ok": True}))
    item = page.locator(f'#sidebar .session-item[data-session-id="{SECOND}"]')
    item.hover()
    item.locator(".session-menu-btn").click()
    page.locator(".dropdown:visible .dropdown-item-compact", has_text="Delete").click()
    # The row leaves the list before the request goes out, so wait for the request.
    with page.expect_request(lambda r: r.method == "DELETE" and r.url.endswith(f"/api/session/{SECOND}")):
        page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator(f'#sidebar .session-item[data-session-id="{SECOND}"]')).to_have_count(0)
    assert page.evaluate("JSON.parse(localStorage.getItem('session-order'))") == [SESSION_ID]


def test_the_rails_delete_button_deletes_the_open_chat_instead_of_archiving_it(new_page, static_app):
    sessions = [BASE, dict(BASE, id=SECOND, name="Second chat")]
    page = open_sidebar(new_page, static_app, sessions)
    requests = record_session_requests(page, static_app)

    def delete(route):
        if route.request.method == "DELETE":
            sessions[:] = [s for s in sessions if s["id"] != SESSION_ID]
        route.fulfill(json={"ok": True})

    page.route(f"**/api/session/{SESSION_ID}", delete)
    page.evaluate("document.getElementById('rail-delete-session').click()")
    with page.expect_request(lambda r: r.method == "DELETE" and r.url.endswith(f"/api/session/{SESSION_ID}")):
        page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator("#sidebar .session-item")).to_have_count(1)
    assert listed(page) == [SECOND]
    assert not [path for _, path in requests if path.endswith("/archive")], requests
