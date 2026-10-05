"""Reordering the navigation (static/js/navOrder.js): dragging a tool in the
sidebar, the core controls that never move, and the order surviving a reload.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import wait_ready

pytestmark = pytest.mark.browser

KEY = "odysseus-nav-order-v1"
CORE_RAIL = ["rail-delete-session", "rail-chats", "rail-documents", "rail-settings"]


def tool_order(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('#tools-section [id^=\"tool-\"][id$=\"-btn\"]')].map(b => b.id)")


def modals_open(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('.modal')].filter(m => getComputedStyle(m).display !== 'none'"
                         " && !m.classList.contains('hidden') && !m.hidden).map(m => m.id)")


def test_dragging_a_sidebar_tool_moves_it_without_opening_it_and_the_order_survives_a_reload(open_app, static_app):
    page = open_app(1440, chat=False)
    before = tool_order(page)
    assert before.index("tool-calendar-btn") < before.index("tool-notes-btn")
    opened = modals_open(page)
    page.drag_and_drop("#tool-calendar-btn", "#tool-notes-btn")
    page.wait_for_function("(id) => localStorage.getItem(id) !== null", arg=KEY)
    after = tool_order(page)
    assert after.index("tool-calendar-btn") > after.index("tool-notes-btn")
    assert sorted(after) == sorted(before), "a tool was lost or duplicated"
    # The browser may follow a drop with a click on the dragged button.
    page.wait_for_timeout(400)
    assert modals_open(page) == opened, "the drag also opened the tool"
    page.goto(static_app.url)
    wait_ready(page, chat=False)
    assert tool_order(page) == after


def test_core_controls_cannot_be_moved(open_app):
    page = open_app(1440, chat=False)
    draggable = page.evaluate("(ids) => ids.map(id => document.getElementById(id).draggable)", CORE_RAIL)
    assert draggable == [False] * len(CORE_RAIL)
    page.evaluate("(ids) => ids.forEach(id => document.getElementById(id).dispatchEvent("
                  "new KeyboardEvent('keydown', {key: 'ArrowDown', altKey: true, bubbles: true})))", CORE_RAIL)
    page.wait_for_timeout(200)
    assert page.evaluate("localStorage.getItem('odysseus-nav-order-v1')") is None
    assert page.evaluate("[...document.querySelectorAll('#icon-rail .icon-rail-btn')].map(b => b.id)"
                         ".filter(id => ['rail-delete-session', 'rail-chats', 'rail-documents', 'rail-settings'].includes(id))") == CORE_RAIL
