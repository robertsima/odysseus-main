"""A chat against the real backend and the scripted model
(tests/helpers/live_app.py): the reply streams in, is sanitized, is saved, and
a worker's result is handed back into the chat that started it.
"""
from __future__ import annotations

import pytest

from tests.helpers.live_app import (
    PROBE_FENCE, PROBE_HANDBACK, PROBE_STREAM, WORKER_LOADOUT, WORKER_RESULT,
)
from tests.helpers.static_app import expect

pytestmark = [pytest.mark.browser, pytest.mark.xdist_group("live_app")]

# A worker run and its hand-back take a few seconds on a cold runner.
SLOW_MS = 60_000


def open_chat(live_app, live_page, name: str, loadout: str | None = None):
    sid = live_app.new_chat(name, loadout=loadout)
    page = live_page(1440, path=f"/#{sid}")
    expect(page.locator("#ag-session-label")).to_have_text(name)
    return sid, page


def composer(page):
    # The page holds a second, hidden #message; the composer has the role.
    return page.get_by_role("textbox", name="Message input")


def event_handlers(locator) -> list[str]:
    return locator.evaluate_all(
        "(els) => els.flatMap(root => [root, ...root.querySelectorAll('*')]).flatMap(e => [...e.attributes]"
        ".filter(a => a.name.toLowerCase().startsWith('on')).map(a => `${e.tagName}[${a.name}]`))")


def test_reply_streams_in_sanitized_and_survives_a_reload(live_app, live_page):
    sid, page = open_chat(live_app, live_page, "Streaming probe")
    composer(page).fill(f"Stream a reply. {PROBE_STREAM}")
    live_app.model.arm("stream")
    try:
        page.click(".send-btn")
        live_app.model.wait_reached("stream")
        # Mid-reply: the first half is on screen in a bubble still streaming.
        streaming = page.locator("#chat-history .msg-ai.streaming")
        expect(streaming).to_contain_text("FIRST-HALF")
        expect(streaming).not_to_contain_text("SECOND-HALF")
    finally:
        live_app.model.release("stream")
    reply = page.locator("#chat-history .msg-ai").filter(has_text="END-OF-PROBE-REPLY")
    expect(reply).to_have_count(1)
    expect(page.locator("#chat-history .msg-ai.streaming")).to_have_count(0)
    expect(reply).to_contain_text("FIRST-HALF")
    expect(reply).to_contain_text("SECOND-HALF")
    # The model's raw <img> is kept, its onerror handler is not.
    expect(reply.locator('img[alt="probe image"]')).to_have_count(1)
    assert event_handlers(reply) == []
    assert page.evaluate("window.__probeXss") is None

    # Saved by the backend: a reload draws the turn again from history,
    # through the same sanitizer.
    page.reload()
    expect(page.locator("#ag-session-label")).to_have_text("Streaming probe")
    expect(page.locator("#chat-history .msg-user")).to_contain_text(PROBE_STREAM)
    reloaded = page.locator("#chat-history .msg-ai").filter(has_text="END-OF-PROBE-REPLY")
    expect(reloaded).to_have_count(1)
    expect(reloaded.locator('img[alt="probe image"]')).to_have_count(1)
    assert event_handlers(reloaded) == []


def test_nested_code_fence_renders_as_one_code_block(live_app, live_page):
    _, page = open_chat(live_app, live_page, "Fence probe")
    composer(page).fill(f"Show a nested fence. {PROBE_FENCE}")
    page.click(".send-btn")
    reply = page.locator("#chat-history .msg-ai").filter(has_text="END-OF-PROBE-REPLY")
    expect(reply).to_have_count(1)
    codes = reply.locator("pre code").all_text_contents()
    assert any("print('inner block')" in c and "STILL-INSIDE-OUTER-FENCE" in c for c in codes), codes


def save_loadouts(live_app) -> None:
    profiles = [
        {"name": "Browser Lead", "description": "browser test chat", "tool_access": "selected",
         "enabled_tools": ["manage_agent_loadout", "send_to_session"], "delegation_policy": "explicit",
         "max_parallel_workers": 1, "private_vault_access": False},
        {"name": WORKER_LOADOUT, "description": "browser test worker", "tool_access": "selected",
         "enabled_tools": ["get_workspace"], "private_vault_access": False, "max_parallel_workers": 0,
         "model": ""},
    ]
    with live_app.client() as client:
        resp = client.post("/api/auth/settings", json={"agent_profiles": profiles})
        assert resp.status_code == 200, resp.text
        assert {p["name"] for p in resp.json()["agent_profiles"]} >= {"Browser Lead", WORKER_LOADOUT}


def test_worker_result_is_handed_back_into_the_chat_that_started_it(live_app, live_page):
    save_loadouts(live_app)
    sid, page = open_chat(live_app, live_page, "Hand-back probe", loadout="Browser Lead")
    page.click("#mode-agent-btn")
    expect(page.locator("#mode-agent-btn")).to_have_attribute("aria-pressed", "true")
    composer(page).fill(f"Have the {WORKER_LOADOUT} summarise the fleet notes and tell me what it reports. "
                        f"{PROBE_HANDBACK}")
    # The worker answers only once the chat's own turn has finished on
    # screen. A hand-back that lands while that turn is still streaming is
    # not shown until the chat is reopened (seen 2026-10-04); this flow
    # covers the ordinary case of a worker that takes longer than the turn.
    live_app.model.arm("worker")
    try:
        page.click(".send-btn")
        expect(page.locator("#chat-history .msg-ai").filter(has_text="PARENT-ACK")).to_have_count(1, timeout=SLOW_MS)
        page.wait_for_function("(sid) => !window.chatModule.hasActiveStream(sid)", arg=sid, timeout=SLOW_MS)
        live_app.model.wait_reached("worker", timeout=SLOW_MS / 1000)
    finally:
        live_app.model.release("worker")

    # The worker's result arrives as an inbound message, then the chat's own
    # follow-up reply quotes it.
    handback = page.locator("#chat-history .msg-worker-result")
    follow_up = page.locator("#chat-history .msg-ai").filter(has_text="PARENT-FINAL")
    try:
        expect(handback).to_have_count(1, timeout=SLOW_MS)
        expect(follow_up).to_have_count(1, timeout=SLOW_MS)
    except AssertionError as exc:
        with live_app.client() as client:
            history = client.get(f"/api/history/{sid}").json()["history"]
        saved = [(m["role"], (m.get("metadata") or {}).get("source"), str(m.get("content"))[:80]) for m in history]
        raise AssertionError(f"{exc}\nsaved: {saved}\n{live_app.diagnosis()}") from None
    expect(follow_up).to_contain_text(WORKER_RESULT)
    order = page.evaluate("[...document.querySelectorAll('#chat-history .msg')].map(m =>"
                          " m.classList.contains('msg-worker-result') ? 'handback'"
                          " : m.textContent.includes('PARENT-FINAL') ? 'follow-up' : 'other')")
    assert order.index("handback") < order.index("follow-up"), order

    with live_app.client() as client:
        history = client.get(f"/api/history/{sid}").json()["history"]
    sources = [(m["role"], (m.get("metadata") or {}).get("source")) for m in history]
    assert ("user", "worker") in sources and ("assistant", "worker_followup") in sources, sources
