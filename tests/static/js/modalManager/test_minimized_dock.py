"""The dock of minimized windows (static/js/modalManager.js): chips for
minimized tool windows sit clear of what you type in, so the chat stays usable
with windows tucked away.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import (
    OTHER_ID, assert_usable, expect, open_agents, open_workbench, probe, select_agent, settle,
)

pytestmark = pytest.mark.browser

MINIMIZE = ".modal-minimize-btn, .minimize-btn"
CONTROLS = ("#message", ".send-btn")


SLACK = 4  # px: the chip's edge may touch the textarea's border


def overlap(a: dict, b: dict) -> bool:
    return (a["left"] < b["right"] and b["left"] < a["right"]
            and a["top"] < b["bottom"] - SLACK and b["top"] + SLACK < a["bottom"])


def chip_boxes(page) -> list[dict]:
    return page.evaluate("[...document.querySelectorAll('#minimized-dock .minimized-dock-chip')].map(c => {"
                         " const r = c.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; })")


def composer_controls(page) -> list[dict]:
    """Every drawn control of the composer: the text box, send and the toolbar."""
    return page.evaluate("[...document.querySelectorAll('.chat-input-bar textarea, .chat-input-bar button,"
                         " .chat-input-bar select, .chat-input-bar input')]"
                         ".filter(e => e.offsetParent && e.getBoundingClientRect().width > 0)"
                         ".map(e => { const r = e.getBoundingClientRect(); return {name: e.id || e.className,"
                         " left: r.left, right: r.right, top: r.top, bottom: r.bottom}; })")


def assert_clear_of_the_composer(page, chips: int) -> None:
    settle(page, "#minimized-dock")
    boxes = chip_boxes(page)
    assert len(boxes) == chips, boxes
    controls = composer_controls(page)
    assert len(controls) >= 2, controls
    covered = [c["name"] for c in controls if any(overlap(box, c) for box in boxes)]
    assert not covered, (covered, boxes)
    for selector in CONTROLS:
        assert_usable(page, selector)


def test_two_minimized_windows_leave_the_composer_uncovered_on_a_desktop(open_app):
    page = open_app(1440)
    open_agents(page)
    page.locator(f"#agents-dashboard {MINIMIZE}").first.click()
    open_workbench(page)
    page.locator(f"#workbench-modal {MINIMIZE}").first.click()
    expect(page.locator("#minimized-dock .minimized-dock-chip")).to_have_count(2)
    assert_clear_of_the_composer(page, 2)


@pytest.mark.parametrize("width", [700, 390])
def test_the_room_tucked_away_to_show_a_chat_leaves_the_composer_uncovered_on_a_phone(open_app, width):
    page = open_app(width)
    open_agents(page)
    select_agent(page, OTHER_ID)
    # Opening a chat from the room tucks the room into the dock on a narrow screen.
    page.locator('#ag-detail [data-ag="open-chat"]').focus()
    page.keyboard.press("Enter")
    expect(page.locator("#minimized-dock .minimized-dock-chip")).to_have_count(1)
    assert_clear_of_the_composer(page, 1)
