"""The Phalanx soldiers (static/js/agentsDashboard.js, static/index.html and
static/branding/agamemnon-agent-marks.svg): every soldier the UI can draw
resolves to artwork in the page, the artwork is the canonical set, and the
role and model colour come from the agent, never from its status.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import AGENT_ROWS, OTHER_ID, PARENT_ID, WORKER_ID, open_agents, select_agent

pytestmark = pytest.mark.browser

CANONICAL_JS = """
async () => {
  const text = await (await fetch('/static/branding/agamemnon-agent-marks.svg')).text();
  const sheet = new DOMParser().parseFromString(text, 'image/svg+xml');
  const out = {};
  for (const symbol of sheet.querySelectorAll('symbol')) {
    out[symbol.id] = symbol.querySelector('path')?.getAttribute('d') || null;
  }
  return out;
}
"""

PAGE_SYMBOLS_JS = """
() => Object.fromEntries([...document.querySelectorAll('svg symbol[id^="soldier-"]')]
  .map((symbol) => [symbol.id, symbol.querySelector('path')?.getAttribute('d') || null]))
"""

DANGLING_JS = """
() => [...document.querySelectorAll('#agents-dashboard use[href^="#"]')]
  .map((use) => use.getAttribute('href').slice(1))
  .filter((id) => !document.getElementById(id))
"""


def variant(page, sid):
    return page.evaluate("(sid) => document.querySelector(`.ag-card[data-sid=\"${sid}\"] .ag-bot`).dataset.soldierVariant", sid)


def test_the_soldiers_in_the_page_are_the_canonical_artwork_and_every_one_in_use_exists(open_app):
    page = open_app(1440)
    open_agents(page)
    select_agent(page, PARENT_ID)
    canonical = {id_: d for id_, d in page.evaluate(CANONICAL_JS).items() if id_.startswith("soldier-")}
    roles = {"soldier-primary", "soldier-worker", "soldier-scout", "soldier-reviewer", "soldier-specialist"}
    assert roles <= canonical.keys()
    assert all(canonical[role] for role in roles) and len({canonical[role] for role in roles}) == 5
    embedded = page.evaluate(PAGE_SYMBOLS_JS)
    assert {role: embedded.get(role) for role in roles} == {role: canonical[role] for role in roles}
    # Every choice in the appearance picker, and every sprite on screen, has
    # its artwork in the page.
    choices = page.evaluate("[...document.querySelectorAll('input[name=\"ag-appearance\"]')].map(i => i.value).filter(Boolean)")
    assert choices and all(f"soldier-{choice}" in embedded for choice in choices), choices
    assert page.evaluate(DANGLING_JS) == []


def test_a_soldier_takes_its_role_from_the_agent_and_its_colour_from_the_model(open_app):
    page = open_app(1440)
    rows = [dict(row) for row in AGENT_ROWS]
    reviewer = next(row for row in rows if row["session_id"] == OTHER_ID)
    reviewer["source"] = "session"  # a sub-agent, not a lead: the name decides
    rows += [
        dict(AGENT_ROWS[1], session_id="agent-build", name="Builder", source="claude_code", status="finished",
             parent_session=None, parent_name=None),
        dict(AGENT_ROWS[1], session_id="agent-misc", name="Archivist", source="system", status="running",
             parent_session=None, parent_name=None),
    ]
    page.route("**/api/agents/overview*", lambda route: route.fulfill(
        json={"rows": rows, "totals": {}, "profiles": [], "chats": []}))
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_function("document.querySelectorAll('#agents-dashboard .ag-card').length === 5")
    # Role comes from source and name; a running scout and a finished builder
    # show that the status never picks the soldier.
    assert variant(page, PARENT_ID) == "primary"
    assert variant(page, WORKER_ID) == "scout"
    assert variant(page, OTHER_ID) == "reviewer"
    assert variant(page, "agent-build") == "worker"
    assert variant(page, "agent-misc") == "specialist"
    families = page.evaluate("(ids) => ids.map(id => { const b = document.querySelector(`.ag-card[data-sid=\"${id}\"] .ag-bot`);"
                             " return [b.dataset.modelFamily, b.style.getPropertyValue('--agent-model-color')]; })",
                             [PARENT_ID, WORKER_ID, OTHER_ID])
    assert [family for family, _ in families] == ["anthropic", "openai", "google"]
    assert len({color for _, color in families}) == 3 and all(color for _, color in families)
    # Model colour is the Phalanx's: the chat and the Workbench do not use it.
    assert page.evaluate("[...document.querySelectorAll('[data-model-family]')].filter(e => !e.closest('#agents-dashboard')).length") == 0
