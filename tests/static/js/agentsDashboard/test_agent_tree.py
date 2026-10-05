"""The real chat and Phalanx rendering historical, waiting and nested workers
(static/js/agentsDashboard.js, workbench.js, chatRenderer.js), with the chat
and fleet fixtures swapped for these tests.

Ported from the CDP-driven tests/test_agent_tree_timing_browser.py.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from tests.helpers.static_app import (
    AGENT_ROWS, HISTORY, PARENT_ID, SESSION_ID, WORKER_ID, expect, settle, wait_ready,
)

pytestmark = pytest.mark.browser


def open_chat(new_page, static_app, *, width, height, style="agamemnon", rows=None, history=None):
    page = new_page(width, height)
    page.add_init_script("localStorage.setItem('odysseus-page-style-v1', "
                         + json.dumps(json.dumps({"value": style, "updated_at": 1})) + ");")
    if rows is not None:
        totals = {"running": 1, "needs_input": 1, "waiting_approval": 1, "finished_24h": 2}
        page.route("**/api/agents/overview*", lambda route: route.fulfill(json={
            "rows": rows, "totals": totals, "profiles": [], "chats": []}))
    if history is not None:
        page.route("**/api/history/*", lambda route: route.fulfill(json=history))
    page.goto(static_app.url + f"/#{SESSION_ID}")
    wait_ready(page)
    return page


def durations(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('#chat-history .agent-round-duration')].map(n => n.textContent.trim())")


@pytest.mark.parametrize("style, width", [("agamemnon", 1440), ("agamemnon", 390), ("classic", 390)])
def test_each_round_of_a_multi_round_reply_shows_its_duration(new_page, static_app, style, width):
    history = copy.deepcopy(HISTORY)
    history["history"][1] = {**history["history"][1], "content": "Checked the migration.\n\nOutlined risks.",
                             "metadata": {"round_texts": ["Checked the migration.", "Outlined risks."],
                                          "round_durations_s": [63.24, 5.0]}}
    page = open_chat(new_page, static_app, width=width, height=1180 if width < 500 else 920, style=style, history=history)
    page.wait_for_selector("#chat-history .agent-round-duration")
    assert durations(page) == ["Round · 1m 3s", "Round · 5.0s"]
    # The reply after it was never timed, so it shows no duration.
    assert page.evaluate("!document.querySelector('#chat-history .msg:last-child .agent-round-duration')")


def test_a_single_timed_round_shows_its_duration_and_an_untimed_legacy_reply_does_not(new_page, static_app):
    history = copy.deepcopy(HISTORY)
    history["history"][1] = {**history["history"][1], "content": "Here is the migration plan.",
                             "metadata": {"round_durations_s": [2.34]}}
    page = open_chat(new_page, static_app, width=390, height=1180, history=history)
    page.wait_for_selector("#chat-history .agent-round-duration")
    assert durations(page) == ["Round · 2.3s"]
    assert page.evaluate("!document.querySelectorAll('#chat-history .msg')[3].querySelector('.agent-round-duration')")


def nested_fleet():
    rows = copy.deepcopy(AGENT_ROWS)
    rows[0].update(status="needs_input", children_running=0)
    rows[1]["status"] = "waiting_approval"
    rows.extend([
        {"session_id": "agent-grandchild", "name": "Catalog probe", "status": "running", "source": "session",
         "parent_session": WORKER_ID, "model": "openai/gpt-5", "config": {}},
        {"session_id": "agent-old", "name": "Old audit", "status": "finished", "source": "session",
         "parent_session": PARENT_ID, "model": "openai/gpt-5", "config": {}},
    ])
    return rows


def set_filter(page, value):
    page.evaluate("(v) => { const f = document.querySelector('#ag-filter'); f.value = v;"
                  " f.dispatchEvent(new Event('input', {bubbles: true})); }", value)


@pytest.mark.parametrize("style, width", [("agamemnon", 1440), ("agamemnon", 390), ("classic", 390)])
def test_nested_workers_are_indented_filtered_with_their_ancestors_and_folded(new_page, static_app, style, width):
    page = open_chat(new_page, static_app, width=width, height=1180 if width < 500 else 920, style=style, rows=nested_fleet())
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_selector('.ag-card[data-sid="agent-grandchild"]')
    settle(page, ".agents-modal-content")
    # Two chats need the user (the lead's questions, a worker's approval); one is running.
    badges = page.evaluate("Object.fromEntries([...document.querySelectorAll('#agents-dashboard .ag-seg')]"
                           ".map(b => [b.dataset.bucket, b.querySelector('b')?.textContent ?? null]))")
    assert badges == {"attention": "2", "active": "1", "recent": None}, badges
    expect(page.locator("#agents-dashboard .ag-group-attn .wb-count")).to_have_text("1 tree")

    cards = page.evaluate("Object.fromEntries([...document.querySelectorAll('#agents-dashboard .ag-card')]"
                          ".map(c => [c.dataset.sid, {left: c.getBoundingClientRect().left,"
                          " pill: c.querySelector('.wb-pill').className}]))")
    assert cards["agent-grandchild"]["left"] > cards[WORKER_ID]["left"] > cards[PARENT_ID]["left"], cards
    assert re.search(r"\brun\b", cards["agent-grandchild"]["pill"])
    assert re.search(r"\bwarn\b", cards[WORKER_ID]["pill"])
    assert "agent-old" not in cards, "a finished worker is folded until asked for"

    # A search for a descendant keeps its ancestors but not an unrelated branch.
    set_filter(page, "Catalog probe")
    page.wait_for_function("document.querySelectorAll('#agents-dashboard .ag-card').length === 3")
    assert set(page.evaluate("[...document.querySelectorAll('#agents-dashboard .ag-card')].map(c => c.dataset.sid)")) == {
        PARENT_ID, WORKER_ID, "agent-grandchild"}
    set_filter(page, "")
    toggle = page.locator(f'.ag-workers-toggle[data-sid="{PARENT_ID}"]')
    expect(toggle).to_have_attribute("aria-expanded", "false")
    controls = toggle.get_attribute("aria-controls")
    assert page.evaluate("(id) => !!document.getElementById(id)", controls)
    # A native button: activating it keeps focus through the fleet re-render.
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("(sid) => document.activeElement === document.querySelector(`.ag-workers-toggle[data-sid=\"${sid}\"]`)",
                         PARENT_ID)
    expect(page.locator('.ag-card[data-sid="agent-old"]')).to_have_count(1)


def test_a_chat_run_card_settles_when_the_poll_reports_the_run_finished_without_a_finish_event(open_app, static_app):
    page = open_app(390)
    running = page.locator(".agent-run-card.running")
    expect(running).to_have_count(1)
    static_app.state.run_status = "finished"
    card = page.locator(".agent-run-card")
    expect(card).not_to_have_class(re.compile(r"\brunning\b"), timeout=20_000)
    expect(card.locator(".wb-pill")).not_to_have_class(re.compile(r"\brun\b"))
