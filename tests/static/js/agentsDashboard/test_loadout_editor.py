"""The chat settings editor in the Phalanx (static/js/agentsDashboard.js): what
Save sends for tool and connection access, and how a loadout is applied.

The canned API has no catalog, so each test serves one and records the
``PATCH /api/session/<id>/settings`` the editor sends.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import AGENT_ROWS, PARENT_ID, expect, select_agent

pytestmark = pytest.mark.browser

TOOLS = ["read_file", "grep", "bash", "web_search"]
CATALOG = {"tools": [{"name": name, "group": "Core"} for name in TOOLS], "skills": [],
           "mcp_servers": [{"id": "github", "name": "GitHub"}], "models": []}


class Fleet:
    """The lead's stored config, the catalog, and the settings saves."""

    def __init__(self, page, config, profiles=()):
        row = dict(AGENT_ROWS[0], config=config)
        self.saves: list[dict] = []
        self.loadout_posts: list[dict] = []
        page.route("**/api/agents/overview*", lambda route: route.fulfill(json={
            "rows": [row], "totals": {}, "profiles": [{"name": p} for p in profiles], "chats": [],
            "can_edit_loadouts": True}))
        page.route("**/api/agents/catalog", lambda route: route.fulfill(json=CATALOG))
        page.route(f"**/api/session/{PARENT_ID}/settings", self._settings)
        page.route(f"**/api/agents/sessions/{PARENT_ID}/loadout", self._loadout)

    def _settings(self, route):
        body = json.loads(route.request.post_data or "{}")
        self.saves.append(body)
        route.fulfill(json={"settings": body})

    def _loadout(self, route):
        self.loadout_posts.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"ok": True})


def open_editor(page):
    open_agents_one(page)
    page.click('#ag-detail [data-ag="config-toggle"]')
    page.wait_for_selector(".ag-loadout-editor [data-config]")


def open_agents_one(page):
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_selector(".ag-card")
    select_agent(page, PARENT_ID)


def save(page, fleet):
    before = len(fleet.saves)
    page.click('[data-ag="save-config"]')
    for _ in range(100):
        if len(fleet.saves) > before:
            return fleet.saves[-1]
        page.wait_for_timeout(50)
    raise AssertionError("the editor sent no settings")


def test_saving_an_explicit_allowlist_sends_the_allowlist_and_only_the_explicit_denials(open_app):
    page = open_app(1440)
    fleet = Fleet(page, {"tool_access": "selected", "enabled_tools": ["read_file", "grep"],
                         "disabled_tools": ["bash"], "allowed_mcp_servers": ["*"]})
    open_editor(page)
    sent = save(page, fleet)
    assert sent["tool_access"] == "selected"
    # The allowlist is stored as an allowlist, with the MCP grant spelled as a wildcard.
    assert sent["enabled_tools"] == ["read_file", "grep", "mcp__*"]
    # Only what the user denied is sent. The complement over the catalog the
    # browser happens to hold (here web_search) must not become a denial.
    assert sent["disabled_tools"] == ["bash"]
    assert sent["allowed_mcp_servers"] == ["*"]


def test_a_chat_saved_before_positive_tool_policy_keeps_its_denials_and_gets_an_allowlist(open_app):
    page = open_app(1440)
    fleet = Fleet(page, {"disabled_tools": ["bash"]})
    open_editor(page)
    sent = save(page, fleet)
    assert sent["tool_access"] == "selected"
    assert sent["disabled_tools"] == ["bash"]
    assert sent["enabled_tools"] == ["read_file", "grep", "web_search", "mcp__*"]


def test_a_chat_with_no_connections_saves_no_connection_grant(open_app):
    page = open_app(1440)
    fleet = Fleet(page, {"tool_access": "selected", "enabled_tools": ["read_file"], "allowed_mcp_servers": []})
    open_editor(page)
    sent = save(page, fleet)
    assert sent["allowed_mcp_servers"] == []
    assert sent["enabled_tools"] == ["read_file"]


def test_choosing_a_loadout_applies_it_on_the_server_instead_of_copying_it_in_the_browser(open_app):
    page = open_app(1440)
    fleet = Fleet(page, {"tool_access": "all"}, profiles=("Reviewer",))
    open_editor(page)
    page.select_option("[data-ag-basis]", "Reviewer")
    for _ in range(100):
        if fleet.loadout_posts:
            break
        page.wait_for_timeout(50)
    assert fleet.loadout_posts == [{"profile": "Reviewer"}]
    # The browser never writes a copy of the loadout's settings itself.
    assert fleet.saves == []


def test_applying_a_plugin_changes_capabilities_and_keeps_unsaved_personality(open_app):
    page = open_app(1440)
    fleet = Fleet(page, {"tool_access": "all", "agent_instructions": "Stored instructions"})
    # The plugin catalog is its own widget; stand in for it and press "Apply" the way it does.
    page.evaluate("window.OdysseusPluginCatalog = {mount: (el, opts) => { window.__plugin = opts; }}")
    open_editor(page)
    instructions = page.locator('[data-config="agent_instructions"]')
    instructions.fill("Be terse and cite files")
    instructions.press("Tab")  # leaving the field commits the edit to the draft
    expect(page.locator("#ag-config-msg")).to_have_text("Unsaved changes")
    page.evaluate("window.__plugin.onApplied({settings: {tool_access: 'selected', enabled_tools: ['grep']}})")
    page.wait_for_selector(".ag-loadout-editor [data-config]")
    sent = save(page, fleet)
    # The plugin set the tools; the personality typed before it was applied is still there.
    assert sent["tool_access"] == "selected" and sent["enabled_tools"] == ["grep", "mcp__*"]
    assert sent["agent_instructions"] == "Be terse and cite files"
