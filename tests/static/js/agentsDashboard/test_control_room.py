"""The Phalanx as a live control room (static/js/agentsDashboard.js): what a
background refresh leaves alone, how runs and steering messages read, which
workers stay on screen, and how the window behaves when you open a chat.

The canned API serves a fixed fleet; ``Overview`` replaces what
``/api/agents/overview`` answers so a test can change the fleet between polls.
"""
from __future__ import annotations

import re

import pytest

from tests.helpers.static_app import (
    AGENT_ROWS, OTHER_ID, PARENT_ID, SESSION_ID, WORKER_ID, expect, open_agents, open_workbench, select_agent, settle,
)

pytestmark = pytest.mark.browser

SHELL = "#agents-dashboard .ag-body"


class Overview:
    """The fleet ``/api/agents/overview`` answers, editable between polls."""

    def __init__(self, page, **config):
        self.rows = [dict(row) for row in AGENT_ROWS]
        self.totals = {"running": 2, "finished_24h": 1}
        self.extra = config
        page.route("**/api/agents/overview*", self._serve)

    def row(self, sid):
        return next(r for r in self.rows if r["session_id"] == sid)

    def _serve(self, route):
        route.fulfill(json={"rows": self.rows, "totals": self.totals, "profiles": [], "chats": [], **self.extra})


def pill_classes(page, selector):
    return page.evaluate("(s) => document.querySelector(s).className.split(/\\s+/)", selector)


def activate(page, selector):
    """Reach a control by keyboard: focus scrolls it into view, Enter presses it."""
    page.locator(selector).focus()
    page.keyboard.press("Enter")


def refresh_now(page):
    page.evaluate("document.querySelector('#agents-dashboard [data-ag=\"refresh\"]').click()")


def test_background_refresh_updates_counts_without_rebuilding_the_window_or_losing_a_draft(open_app):
    page = open_app(1440)
    overview = Overview(page)
    open_agents(page)
    select_agent(page, PARENT_ID)
    page.click("#ag-tab-steering")
    page.fill("#ag-steer", "skip the tests and finish the migration")
    page.evaluate("document.getElementById('ag-steer').setSelectionRange(5, 9)")
    page.evaluate(f"window.__shell = document.querySelector('{SHELL}'); window.__card = document.querySelector('.ag-card')")

    overview.totals = {"running": 7, "finished_24h": 1}
    refresh_now(page)
    expect(page.locator('.ag-seg[data-bucket="active"] b')).to_have_text("7")

    assert page.evaluate(f"document.querySelector('{SHELL}') === window.__shell"), "the window shell was rebuilt"
    assert page.evaluate("document.getElementById('ag-steer').value") == "skip the tests and finish the migration"
    assert page.evaluate("document.activeElement.id") == "ag-steer"
    assert page.evaluate("[document.activeElement.selectionStart, document.activeElement.selectionEnd]") == [5, 9]


def test_elapsed_time_ticks_in_place_without_redrawing_the_fleet(open_app):
    page = open_app(1440)
    open_agents(page)
    duration = page.locator(".ag-card .ag-row-dur").first
    before = duration.inner_text()
    page.evaluate("window.__card = document.querySelector('.ag-card')")
    expect(duration).not_to_have_text(before)
    assert page.evaluate("document.querySelector('.ag-card') === window.__card"), "the tick redrew the fleet"


def test_steering_messages_show_where_they_stopped_and_never_claim_the_agent_acted(open_app):
    page = open_app(1440)
    overview = Overview(page)
    now = overview.row(PARENT_ID)["started_at"] + 100
    overview.row(PARENT_ID)["steer"] = {"messages": [
        {"id": "m1", "text": "waiting message", "state": "queued", "live": True, "queued_at": now},
        {"id": "m2", "text": "handed over", "state": "injected", "round": 7, "queued_at": now,
         "timestamps": {"injected": now + 40}},
        {"id": "m3", "text": "dropped message", "state": "cancelled", "queued_at": now,
         "timestamps": {"cancelled": now + 5}},
        {"id": "m4", "text": "invented state", "state": "completed", "queued_at": now},
    ]}
    open_agents(page)
    select_agent(page, PARENT_ID)
    page.click("#ag-tab-steering")
    rows = page.locator(".ag-steer-log .ag-child")
    expect(rows).to_have_count(4)

    def row(text):
        return page.locator(".ag-steer-log .ag-child", has_text=text)

    # Still waiting: its clock keeps running (no finish time), and it is not "done".
    waiting = row("waiting message")
    expect(waiting).not_to_have_class(re.compile(r"\bdone\b"))
    assert waiting.locator(".ag-row-dur").get_attribute("data-finished") == ""
    # Injected: the trail ends there. Its clock stops at the time it reached that state.
    injected = row("handed over")
    expect(injected).to_have_class(re.compile(r"\bdone\b"))
    expect(injected.locator(".wb-pill")).to_have_class(re.compile(r"\bok\b"))
    assert injected.locator(".ag-row-dur").get_attribute("data-finished") == str(now + 40)
    assert "round 7" in injected.inner_text()
    expect(row("dropped message").locator(".wb-pill")).to_have_class(re.compile(r"\bwarn\b"))
    # A state the server does not define reads as waiting. There is no
    # "completed" or "acted on" outcome to show.
    unknown = row("invented state").locator(".wb-pill")
    expect(unknown).to_have_class(re.compile(r"\bwarn\b"))
    expect(unknown).not_to_have_class(re.compile(r"\bok\b"))


def test_a_worker_that_ran_out_of_rounds_reads_as_partial_work_not_a_failure(open_app, static_app):
    page = open_app(1440)
    overview = Overview(page)
    overview.row(OTHER_ID)["status"] = "incomplete"
    static_app.state.run_status = "incomplete"
    open_agents(page)
    classes = pill_classes(page, f'.ag-card[data-sid="{OTHER_ID}"] .wb-pill')
    assert "warn" in classes and "bad" not in classes, classes
    # The Workbench run view agrees.
    open_workbench(page)
    state = page.locator("#ag-run-state")
    expect(state).to_have_text("incomplete")
    assert page.evaluate("document.querySelector('#ag-run-state').closest('.wb-pill, [class*=pill]')?.className || ''") .find("bad") == -1


def test_live_workers_stay_on_screen_and_finished_ones_fold_until_asked_for(open_app):
    page = open_app(1440)
    overview = Overview(page)
    overview.rows.append(dict(AGENT_ROWS[1], session_id="agent-done", name="↳ Scribe: notes", status="finished",
                              parent_session=PARENT_ID, parent_name="Lead engineer", latest="Wrote notes"))
    open_agents_with(page, 4)
    cards = lambda: page.locator(".ag-card").evaluate_all("els => els.map(e => e.dataset.sid)")  # noqa: E731
    assert WORKER_ID in cards() and "agent-done" not in cards()
    toggle = page.locator(f'.ag-workers-toggle[data-sid="{PARENT_ID}"]')
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(page.locator('.ag-card[data-sid="agent-done"]')).to_have_count(1)
    expect(toggle).to_have_attribute("aria-expanded", "true")


def open_agents_with(page, count):
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_function("(n) => document.querySelectorAll('#agents-dashboard .ag-card').length >= n", arg=count - 1)
    settle(page, ".agents-modal-content")


def test_a_long_fleet_is_paged_and_archiving_asks_first_and_keeps_history(open_app, static_app):
    page = open_app(1440)
    overview = Overview(page)
    overview.rows = [dict(AGENT_ROWS[2], session_id=f"agent-{i}", name=f"Reviewer {i}", started_at=1000 + i)
                     for i in range(11)]
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_selector(".ag-card")
    assert page.locator(".ag-card").count() == 8
    nav = page.locator(".ag-fleet-pages")
    expect(nav).to_have_count(1)
    nav.get_by_role("button", name=re.compile("Older")).click()
    assert page.locator(".ag-card").count() == 3
    # Archiving needs a confirmation; declining sends nothing.
    select_agent(page, page.locator(".ag-card").first.get_attribute("data-sid"))
    dialogs = []
    page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.click('#ag-detail [data-ag="archive-agent"]')
    page.wait_for_function("true")
    assert dialogs and "history stay preserved" in dialogs[0]
    assert not [p for p, _ in static_app.state.posts if p.endswith("/archive")]
    page.once("dialog", lambda d: d.accept())
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/archive")):
        page.click('#ag-detail [data-ag="archive-agent"]')


def test_opening_a_worker_in_the_workbench_or_a_chat_keeps_the_room_open(open_app):
    page = open_app(1440)
    open_agents(page)
    select_agent(page, OTHER_ID)
    activate(page, '#ag-detail [data-ag="open-chat"]')
    # The room moves beside the chat it opened instead of covering it.
    page.wait_for_function("document.getElementById('agents-dashboard').classList.contains('modal-right-docked')")
    assert page.evaluate("!document.getElementById('agents-dashboard').hidden")
    select_agent(page, PARENT_ID)
    activate(page, '#ag-detail [data-ag="inspect-run"]')
    page.wait_for_function("!document.getElementById('workbench-modal').classList.contains('hidden')")
    # The click handler finishes after the Workbench opens; give it time to
    # (wrongly) close the room before looking.
    page.wait_for_timeout(500)
    assert page.evaluate("!document.getElementById('agents-dashboard').hidden")
    assert not page.errors, page.errors


def test_the_chat_that_is_already_open_offers_no_open_control(open_app):
    page = open_app(1440)
    overview = Overview(page)
    overview.rows.append(dict(AGENT_ROWS[2], session_id=SESSION_ID, name="Orders migration", status="running"))
    open_agents_with(page, 4)
    page.wait_for_selector(f'.ag-card[data-sid="{SESSION_ID}"]')
    assert page.locator(f'.ag-card[data-sid="{SESSION_ID}"] [data-ag="open-chat"]').count() == 0
    select_agent(page, SESSION_ID)
    expect(page.locator("#ag-detail .ag-this-chat")).to_have_count(1)
    assert page.locator('#ag-detail [data-ag="open-chat"]').count() == 0
    select_agent(page, OTHER_ID)
    expect(page.locator('#ag-detail [data-ag="open-chat"]')).to_have_count(1)


def test_a_room_the_user_closed_stays_closed_until_their_next_message(open_app):
    page = open_app(1440)
    open_agents(page)
    page.click("#close-agents-dashboard")
    page.wait_for_function("document.getElementById('agents-dashboard').hidden")
    page.evaluate("window.agentsDashboard.openForRun()")
    settle(page)
    assert page.evaluate("document.getElementById('agents-dashboard').hidden"), "a run reopened a closed room"
    # The next turn (idle to busy) lets a delegation show the room again.
    page.evaluate("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', {detail: {active: false}}))")
    page.evaluate("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', {detail: {active: true}}))")
    page.evaluate("window.agentsDashboard.openForRun()")
    page.wait_for_function("!document.getElementById('agents-dashboard').hidden")


def test_the_workbench_shortcut_is_offered_only_to_callers_who_can_use_it(open_app):
    page = open_app(1440)
    open_agents(page)
    assert page.locator('#agents-dashboard [data-ag="workbench"]').count() == 1
    page.evaluate("document.getElementById('rail-workbench').style.display = 'none'")
    page.evaluate("window.agentsDashboard.close()")
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_selector(".ag-card")
    assert page.locator('#agents-dashboard [data-ag="workbench"]').count() == 0


def test_a_sub_agent_has_a_conversation_tab_that_reads_its_own_saved_history(open_app):
    page = open_app(1440)
    seen = []
    history = {"history": [
        {"role": "user", "content": "Map the schema", "metadata": {}},
        {"role": "assistant", "content": "Three tables.", "metadata": {
            "model": "openai/gpt-5", "tool_events": [{"tool": "read_file", "command": "schema.sql", "exit_code": 0}]}},
    ], "total": 2}

    def serve(route):
        seen.append(route.request.url)
        route.fulfill(json=history)

    page.route(f"**/api/history/{WORKER_ID}*", serve)
    open_agents(page)
    select_agent(page, PARENT_ID)
    expect(page.locator("#ag-tab-conversation")).to_have_count(0)
    select_agent(page, WORKER_ID)
    page.click("#ag-tab-conversation")
    expect(page.locator("#ag-detail .ag-convo-msg")).to_have_count(2)
    expect(page.locator("#ag-detail .ag-convo-tools")).to_have_count(1)
    assert any(f"/api/history/{WORKER_ID}" in url for url in seen)


def test_replying_from_the_room_sends_in_that_chat_and_keeps_the_room_open(open_app):
    page = open_app(1440)
    open_agents(page)
    select_agent(page, OTHER_ID)
    page.click("#ag-tab-steering")
    page.fill("#ag-reply", "Next: check the release notes")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/chat_stream")) as sent:
        page.click('#ag-detail [data-ag="reply"]')
    assert "Next: check the release notes" in (sent.value.post_data or "")
    assert page.evaluate("window.sessionModule.getCurrentSessionId()") == OTHER_ID
    assert page.evaluate("!document.getElementById('agents-dashboard').hidden")
