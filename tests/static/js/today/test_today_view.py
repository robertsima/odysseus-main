"""The Today view (static/js/today.js) in the real app shell: the rail button
opens it, the server's order and reasons are drawn, and a button reaches
``POST /api/today/action`` and the list redraws from the next answer.

The canned API has no Today routes, so each test serves ``/api/today`` itself.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import expect, settle

pytestmark = pytest.mark.browser

TODAY = [
    {"key": "todoist:t1", "title": "Pay rent", "why": "Overdue since Tue", "source": "todoist",
     "due": "2026-10-06", "state": None, "blocker": None,
     "actions": ["start", "defer", "done", "open"], "open_url": "https://app.todoist.com/app/task/t1"},
    {"key": "outcome:o1", "title": "Ship the importer", "why": "Blocked until you act", "source": "outcome",
     "due": None, "state": "blocked",
     "blocker": {"code": "WORKSPACE_REQUIRED", "summary": "The worker has no workspace.", "recovery": "user",
                 "user_action": "Pick a repository folder and start again."},
     "actions": ["resume", "defer", "wait", "done"], "open_url": "/?session=c1"},
]
NEXT = [
    {"key": "note:n1", "title": "Draft the newsletter", "why": "Due today", "source": "note",
     "due": None, "state": None, "blocker": None, "actions": ["defer", "done"], "open_url": "/notes?id=n1"},
]


class TodayApi:
    """Serves ``/api/today``; records the actions posted and drops each acted-on item."""

    def __init__(self, page):
        self.today = [dict(i) for i in TODAY]
        self.next = [dict(i) for i in NEXT]
        self.actions = []
        page.route("**/api/today", self._list)
        page.route("**/api/today/action", self._action)

    def _list(self, route):
        route.fulfill(json={"today": self.today, "next": self.next, "degraded": [], "generated_at": "2026-10-10T10:00:00Z"})

    def _action(self, route):
        body = json.loads(route.request.post_data or "{}")
        self.actions.append(body)
        self.today = [i for i in self.today if i["key"] != body["key"]]
        route.fulfill(json={"ok": True})


def open_today(page):
    # The rail is hidden in the default Agamemnon layout; the button is still wired.
    page.evaluate("document.getElementById('rail-today').click()")
    expect(page.locator("#today-modal")).to_be_visible()
    expect(page.locator("#today-modal .today-item")).to_have_count(3)
    settle(page, ".today-modal-content")


def test_rail_button_opens_ranked_items_with_reasons_and_the_blocker_step(open_app):
    """Catches a rail button, markup or stylesheet change that leaves Today unreachable or unreadable."""
    page = open_app(1440, chat=False)
    TodayApi(page)
    open_today(page)

    titles = page.locator("#today-modal .today-title").all_inner_texts()
    assert titles == ["Pay rent", "Ship the importer", "Draft the newsletter"]
    expect(page.locator('.today-item[data-key="todoist:t1"] .today-why')).to_have_text("Overdue since Tue")
    expect(page.locator('.today-item[data-key="outcome:o1"] .today-blocker')).to_contain_text(
        "Pick a repository folder and start again.")
    # The capture line sits above both sections, with its placeholder.
    expect(page.locator("#today-capture-input")).to_have_attribute("placeholder", "Capture a task, appointment or follow-up")
    assert page.errors == []


def test_snooze_posts_the_item_key_and_the_list_redraws_without_it(open_app):
    """Catches an action button that posts nothing, or a list that keeps showing a snoozed item."""
    page = open_app(1440, chat=False)
    api = TodayApi(page)
    open_today(page)

    page.click('.today-item[data-key="todoist:t1"] [data-today-action="defer"]')

    expect(page.locator('.today-item[data-key="todoist:t1"]')).to_have_count(0)
    expect(page.locator("#today-modal .today-status")).to_contain_text("Snoozed")
    assert api.actions == [{"key": "todoist:t1", "action": "defer"}]


def test_phone_width_keeps_the_view_inside_the_screen(open_app):
    """Catches a Today layout that scrolls sideways or hides its buttons on a phone."""
    page = open_app(390, chat=False)
    TodayApi(page)
    page.evaluate("window.todayModule.open()")
    expect(page.locator("#today-modal .today-item")).to_have_count(3)
    settle(page, ".today-modal-content")

    widths = page.evaluate("[document.querySelector('#today-body').scrollWidth, document.querySelector('#today-body').clientWidth]")
    assert widths[0] <= widths[1], f"Today scrolls sideways: {widths}"
    box = page.locator('.today-item[data-key="todoist:t1"] [data-today-action="done"]').bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
