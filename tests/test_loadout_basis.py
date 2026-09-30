"""A chat's settings versus the loadout they are based on.

The Control Room edits a chat's own copy of a loadout, and the loadout library
edits the loadouts. They were two near-identical forms with nothing saying
which was which (2026-09-30), so the chat editor now reads "This chat's
settings — based on Lead Engineer (2 changes)" and can save its settings back
to the loadout or reset to it. These pin the three pieces that has to be true
for: the change count, the chat → loadout inverse, and the routes.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import agent_loadouts, agent_profiles

LEAD = {
    "name": "Lead", "description": "tech lead", "model": "gpt-6-luna", "max_rounds": 80,
    "tool_access": "selected", "enabled_tools": ["bash", "read_file", "mcp__github_read__*"],
    "mcp_access": "selected", "allowed_mcp_servers": ["github_read"],
    "approval_mode": "auto", "delegation_policy": "auto", "max_parallel_workers": 2,
    "instructions": "Own the delivery.", "temperature": 0.2,
}
SHAPES = [
    {"name": "Plain"},
    LEAD,
    {"name": "NoMcp", "mcp_access": "none", "memory_access": "none", "skill_access": "selected", "skill_names": ["x"]},
    {"name": "Narrow", "tool_access": "selected", "enabled_tools": ["web_search"], "shell_access": "off",
     "private_vault_access": True},
    {"name": "Nothing", "tool_access": "none"},
]


def _profile(raw):
    return agent_profiles.validate_profiles([raw])[0]


@pytest.mark.parametrize("raw", SHAPES, ids=[s["name"] for s in SHAPES])
def test_a_fresh_copy_has_no_changes_and_saves_back_unchanged(raw):
    profile = _profile(raw)
    copy = agent_profiles.session_patch(profile)
    assert agent_profiles.loadout_changes(profile, copy) == []
    back = _profile(agent_profiles.profile_from_session(profile, copy))
    assert agent_profiles.session_patch(back) == copy
    # What a chat does not hold is the loadout's own.
    for key in ("name", "description", "model", "max_rounds"):
        assert back[key] == profile[key]


def test_changes_are_named_once_per_control():
    profile = _profile(LEAD)
    copy = agent_profiles.session_patch(profile)
    edited = dict(copy, delegation_policy="never", agent_instructions="Something else",
                  enabled_tools=copy["enabled_tools"] + ["grep"], tool_access="selected")
    assert agent_profiles.loadout_changes(profile, edited) == ["instructions", "tools", "delegation"]


def test_blank_and_missing_settings_are_not_changes():
    profile = _profile({"name": "Plain"})
    copy = agent_profiles.session_patch(profile)
    copy.update(disabled_tools=[], agent_instructions="", agent_temperature=None)
    copy.pop("approval_mode", None)
    assert agent_profiles.loadout_changes(profile, copy) == []
    assert agent_profiles.loadout_changes(profile, dict(copy, approval_mode="ask_all")) == ["approvals"]


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch):
    import core.database as db
    import src.auth_helpers as helpers
    import src.tool_security as security
    from routes.agents_routes import setup_agents_routes

    saved = {"profiles": [], "writes": 0}
    chats = {"s1": {}, "s2": {}}
    auth = {"admin": True}

    def write(profiles):
        saved["profiles"] = agent_profiles.validate_profiles(list(profiles))
        saved["writes"] += 1

    def update(sid, patch):
        for key, value in patch.items():
            if value is None:
                chats[sid].pop(key, None)
            else:
                chats[sid][key] = value
        return dict(chats[sid])

    monkeypatch.setattr(agent_loadouts, "_write", write)
    monkeypatch.setattr(agent_profiles, "load_profiles", lambda: list(saved["profiles"]))
    monkeypatch.setattr(agent_profiles, "load_profiles_with_problems", lambda: (list(saved["profiles"]), []))
    monkeypatch.setattr(agent_profiles, "propagate_profile_edits", lambda old, new: {"Lead": 1})
    monkeypatch.setattr(db, "get_session_settings", lambda sid, **kw: dict(chats.get(sid, {})))
    monkeypatch.setattr(db, "update_session_settings", update)
    monkeypatch.setattr(helpers, "require_user", lambda request: "alice")
    monkeypatch.setattr(helpers, "is_delegated_credential", lambda request: False)
    monkeypatch.setattr(security, "owner_is_admin_or_single_user", lambda user: auth["admin"])

    class _Sessions:
        def get_sessions_for_user(self, user):
            return {"s1": object(), "s2": object()}

    import routes.agents_routes as ar
    monkeypatch.setattr(ar, "effective_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(setup_agents_routes(_Sessions()))
    client = TestClient(app)
    saved["profiles"] = [_profile(LEAD)]
    chats["s1"].update(agent_profiles.session_patch(saved["profiles"][0]))
    return client, saved, chats, auth


def test_save_to_loadout_makes_the_chats_settings_the_loadouts(env):
    client, saved, chats, _auth = env
    chats["s1"]["delegation_policy"] = "never"
    chats["s1"]["agent_instructions"] = "Review only."
    res = client.post("/api/agents/sessions/s1/save-to-loadout", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is False and body["chats_updated"] == 1
    lead = saved["profiles"][0]
    assert lead["delegation_policy"] == "never" and lead["instructions"] == "Review only."
    # Kept from the loadout: what a chat does not hold.
    assert lead["model"] == "gpt-6-luna" and lead["max_rounds"] == 80 and lead["description"] == "tech lead"
    assert agent_profiles.loadout_changes(lead, chats["s1"]) == []


def test_save_as_new_loadout_bases_the_chat_on_it(env):
    client, saved, chats, _auth = env
    chats["s2"].update(delegation_policy="auto", memory_access="write")
    res = client.post("/api/agents/sessions/s2/save-to-loadout", json={"new_name": "Scout"})
    assert res.status_code == 200, res.text
    assert res.json()["created"] is True
    assert [p["name"] for p in saved["profiles"]] == ["Lead", "Scout"]
    assert chats["s2"]["agent_profile"] == "Scout"
    assert client.post("/api/agents/sessions/s2/save-to-loadout", json={"new_name": "lead"}).status_code == 409


def test_saving_to_a_loadout_is_for_admins(env):
    client, saved, _chats, auth = env
    auth["admin"] = False
    assert client.post("/api/agents/sessions/s1/save-to-loadout", json={}).status_code == 403
    assert saved["writes"] == 0


def test_a_chat_without_a_loadout_needs_a_name(env):
    client, _saved, _chats, _auth = env
    assert client.post("/api/agents/sessions/s2/save-to-loadout", json={}).status_code == 400
    assert client.post("/api/agents/sessions/nope/save-to-loadout", json={}).status_code == 404


def test_the_overview_reports_each_chats_basis(env):
    from routes.agents_routes import _loadout_basis

    _client, saved, chats, _auth = env
    by_name = {p["name"].casefold(): p for p in saved["profiles"]}
    assert _loadout_basis(chats["s1"], by_name) == {"name": "Lead", "exists": True, "changes": []}
    chats["s1"]["shell_access"] = "off"
    assert _loadout_basis(chats["s1"], by_name)["changes"] == ["shell"]
    assert _loadout_basis({"agent_profile": "Gone"}, by_name) == {"name": "Gone", "exists": False, "changes": []}
    assert _loadout_basis({}, by_name) is None
