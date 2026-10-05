"""Agent activity in the chat transcript (static/js/chatRenderer.js,
agentThread.js, workbench.js and cardState.js): a turn's tool calls, a
worker's handed-back result and a live worker card all start collapsed, and
opening one is remembered across a reload.
"""
from __future__ import annotations

import copy
import re

import pytest

from tests.helpers.static_app import HISTORY, SESSION_ID, expect, settle, wait_ready

pytestmark = pytest.mark.browser

WORKER_RESULT = {
    "role": "user", "content": "[Worker Scout finished]\nTask: map the schema\nResult: three tables",
    "metadata": {"source": "worker", "from_session": "w1", "from_session_name": "Scout",
                 "timestamp": "2026-10-01T10:00:00Z"},
}
TOOL_EVENTS = [{"tool": "web_search", "command": "orders schema"}, {"tool": "web_search", "command": "locks"},
               {"tool": "read_file", "command": "schema.sql"}]


def open_chat(new_page, static_app, *, tools=False, worker_result=False, width=1440):
    page = new_page(width)
    history = copy.deepcopy(HISTORY)
    if tools:
        history["history"][1]["metadata"] = {"tool_events": TOOL_EVENTS}
    if worker_result:
        history["history"].append(WORKER_RESULT)
        history["total"] = len(history["history"])
    page.route("**/api/history/*", lambda route: route.fulfill(json=history))
    page.goto(static_app.url + f"/#{SESSION_ID}")
    page.wait_for_function("document.querySelectorAll('#chat-history .msg').length >= 3")
    wait_ready(page, chat=False)
    return page


def test_a_turns_tool_calls_start_behind_one_line_of_activity_and_open_on_request(new_page, static_app):
    page = open_chat(new_page, static_app, tools=True)
    toggle = page.locator(".agent-thread .ats-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    first_call = page.locator('.agent-thread .agent-thread-node[data-tool="web_search"]').first
    expect(first_call).to_be_hidden()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(first_call).to_be_visible()


def test_a_workers_handed_back_result_is_one_collapsed_line_and_stays_open_once_opened(new_page, static_app):
    page = open_chat(new_page, static_app, worker_result=True)
    result = page.locator(".msg-worker-result")
    toggle = result.locator(".msg-worker-toggle")
    # Messages carry the time they were saved, which places live cards among them.
    expect(result).to_have_attribute("data-ts", WORKER_RESULT["metadata"]["timestamp"])
    expect(result).to_have_class(re.compile(r"\bcollapsed\b"))
    expect(result.locator(".body")).to_be_hidden()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(result.locator(".body")).to_be_visible()
    page.reload()
    wait_ready(page, chat=False)
    expect(page.locator(".msg-worker-result .msg-worker-toggle")).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".msg-worker-result .body")).to_be_visible()


def test_a_running_worker_card_is_collapsed_until_opened_and_remembers_it(new_page, static_app):
    page = open_chat(new_page, static_app)
    head = page.locator(".agent-run-card[data-run='run-1'] .agent-run-head")
    expect(head).to_have_attribute("aria-expanded", "false")
    steps = page.locator(".agent-run-card[data-run='run-1'] .agent-run-steps")
    expect(steps).to_be_hidden()
    head.click()
    expect(head).to_have_attribute("aria-expanded", "true")
    expect(steps).to_be_visible()
    page.reload()
    wait_ready(page, chat=False)
    settle(page)
    expect(page.locator(".agent-run-card[data-run='run-1'] .agent-run-head")).to_have_attribute("aria-expanded", "true")


RAIL_JS = """
() => {
  const thread = document.querySelector('.agent-thread');
  const box = thread.getBoundingClientRect();
  const rail = getComputedStyle(thread, '::before');
  const railCentre = parseFloat(rail.left) + parseFloat(rail.width) / 2;
  const dot = thread.querySelector('.agent-thread-node:not(.agent-thread-hidden) .agent-thread-dot').getBoundingClientRect();
  const node = thread.querySelector('.agent-thread-node:last-child');
  const end = getComputedStyle(node, '::after');
  const nodeBox = node.getBoundingClientRect();
  return {rail: railCentre, step: dot.left + dot.width / 2 - box.left,
          terminating: nodeBox.left - box.left + parseFloat(end.left) + parseFloat(end.width) / 2,
          endContent: end.content};
}
"""


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_the_timeline_dots_sit_on_the_rail_at_every_width(new_page, static_app, width):
    page = open_chat(new_page, static_app, tools=True, width=width)
    page.locator(".agent-thread .ats-toggle").click()
    page.locator(".agent-thread .agent-thread-node").last.locator(".agent-thread-header").click()
    settle(page)
    centres = page.evaluate(RAIL_JS)
    assert abs(centres["step"] - centres["rail"]) <= 0.6, centres
    assert centres["endContent"] != "none"
    assert abs(centres["terminating"] - centres["rail"]) <= 0.6, centres
