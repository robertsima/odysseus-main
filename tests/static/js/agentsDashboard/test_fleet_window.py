"""The Phalanx window and fleet controls (static/js/agentsDashboard.js): its
panes, the keyboard, the archive and the composer's agent mode.

``/api/agents/overview`` is replaced where a test needs the fleet to change.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import AGENT_ROWS, OTHER_ID, drag, expect, open_agents, probe, settle

pytestmark = pytest.mark.browser


def fleet_width(page):
    return page.evaluate("document.querySelector('#agents-dashboard .ag-fleet').getBoundingClientRect().width")


def test_the_fleet_and_detail_panes_resize_by_drag_and_keyboard_and_remember_it(open_app):
    page = open_app(1440)
    open_agents(page)
    start = fleet_width(page)
    grip = page.locator("#agents-dashboard [data-ag-splitter]")
    box = grip.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    drag(page, (x, y), (x + 90, y))
    page.wait_for_function(
        "(w) => document.querySelector('#agents-dashboard .ag-fleet').getBoundingClientRect().width > w + 40", arg=start)
    widened = fleet_width(page)
    assert page.evaluate("Number(localStorage.getItem('odysseus-agents-fleet-width'))") >= widened - 2
    grip.focus()
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(
        "(w) => document.querySelector('#agents-dashboard .ag-fleet').getBoundingClientRect().width < w", arg=widened)


def test_expansion_lives_on_the_title_bar_only(open_app):
    page = open_app(1440)
    open_agents(page)
    assert page.locator('#agents-dashboard [data-ag="expand"]').count() == 0
    # Measure after the open animation: mid-animation widths raced the check.
    settle(page, ".agents-modal-content")
    before = probe(page, ".agents-modal-content")
    page.click("#ag-maximize")
    page.wait_for_function(
        "([w, h]) => { const r = document.querySelector('.agents-modal-content').getBoundingClientRect();"
        " return r.width > w + 1 || r.height > h + 1; }",
        arg=[before["width"], before["height"]])


def test_the_fleet_is_selectable_by_keyboard_and_a_refresh_keeps_focus(open_app):
    page = open_app(1440)
    totals = {"running": 2, "finished_24h": 1}
    page.route("**/api/agents/overview*", lambda route: route.fulfill(
        json={"rows": AGENT_ROWS, "totals": totals, "profiles": [], "chats": []}))
    open_agents(page)
    card = f'.ag-card-select[data-sid="{OTHER_ID}"]'
    page.locator(card).focus()
    page.keyboard.press("Enter")
    page.wait_for_function("(sid) => document.querySelector('#ag-detail').dataset.sessionId === sid", arg=OTHER_ID)
    # Selecting moves focus to the agent's name, so a screen reader announces it.
    assert page.evaluate("document.activeElement.className") == "ag-detail-name"
    page.locator(card).focus()
    totals["running"] = 5
    page.evaluate("document.querySelector('#agents-dashboard [data-ag=\"refresh\"]').click()")
    expect(page.locator('.ag-seg[data-bucket="active"] b')).to_have_text("5")
    assert page.evaluate("document.activeElement.dataset.sid") == OTHER_ID
    assert page.evaluate("document.activeElement.classList.contains('ag-card-select')")


def test_opening_the_archive_drops_a_filter_that_would_hide_it(open_app):
    page = open_app(1440)
    archived = dict(AGENT_ROWS[2], session_id="agent-old", name="Old reviewer", archived=True)

    def serve(route):
        rows = [archived] if "archived=true" in route.request.url else AGENT_ROWS
        route.fulfill(json={"rows": rows, "totals": {}, "profiles": [], "chats": []})

    page.route("**/api/agents/overview*", serve)
    open_agents(page)
    page.click('.ag-seg[data-bucket="active"]')
    expect(page.locator(".ag-seg.on")).to_have_attribute("data-bucket", "active")
    page.click('[data-ag="archive-view"]')
    # A finished, archived chat is not "active": the filter must not hide it.
    expect(page.locator('.ag-card[data-sid="agent-old"]')).to_have_count(1)


def test_agent_mode_shows_the_agents_menu_where_the_prompt_button_was(open_app):
    page = open_app(1440)
    shown = "(id) => getComputedStyle(document.getElementById(id)).display !== 'none'"
    assert page.evaluate(shown, "overflow-preset-btn") and not page.evaluate(shown, "overflow-agents-btn")
    page.evaluate("document.body.classList.add('composer-agent-mode')")
    assert page.evaluate(shown, "overflow-agents-btn") and not page.evaluate(shown, "overflow-preset-btn")
