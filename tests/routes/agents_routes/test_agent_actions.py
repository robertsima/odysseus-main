"""Talk to an agent, assign it a task, and recover when the task cannot start.

Everything goes through HTTP with the real app: the owner scope, the access
sentences, the structured blocker with its recovery actions, and the launch
route's retry with ``requires`` / ``extra_tools``.
"""
import uuid

import pytest

from src import agent_profiles

ENDPOINT = "http://127.0.0.1:9/v1"


def _profiles(*raw):
    return agent_profiles.validate_profiles(list(raw))


@pytest.fixture
def agents(api, monkeypatch):
    stored = _profiles(
        {"name": "Reader", "tool_access": "selected", "enabled_tools": ["read_file", "grep", "ls", "web_search"],
         "quick_actions": [{"label": "Summarise", "prompt": "Summarise {document}"}]},
        {"name": "Researcher", "tool_access": "selected", "enabled_tools": ["web_search"], "model": ""},
        {"name": "Everything", "tool_access": "all"},
    )
    monkeypatch.setattr(agent_profiles, "load_profiles", lambda: stored)

    def chat(owner, **kw):
        sid = str(uuid.uuid4())
        api.session_manager.create_session(session_id=sid, name="c", endpoint_url=ENDPOINT, model="m",
                                           rag=False, owner=owner)
        if kw:
            from core.database import update_session_settings
            update_session_settings(sid, kw)
        return sid

    api.chat = chat
    return api


@pytest.mark.security
def test_bob_cannot_read_access_for_alices_chat(agents):
    sid = agents.chat("alice")
    assert agents.as_user("alice").get("/api/agents/profiles/Reader/access", params={"session_id": sid}).status_code == 200
    assert agents.as_user("bob").get("/api/agents/profiles/Reader/access", params={"session_id": sid}).status_code == 404


def test_access_has_talk_assign_advanced_and_quick_actions(agents):
    out = agents.as_admin().get("/api/agents/profiles/Reader/access").json()
    assert out["name"] == "Reader"
    assert out["talk"]["summary"].startswith("Can read project files")
    assert out["assign"]["summary"].endswith("Results return to this chat.")
    assert out["assign"]["blocker"] is None
    assert "read_file" in out["advanced"]["tools"]
    assert out["quick_actions"] == [{"label": "Summarise", "prompt": "Summarise {document}"}]


def test_unknown_agent_is_404(agents):
    assert agents.as_admin().get("/api/agents/profiles/Nobody/access").status_code == 404


def test_document_the_agent_cannot_open_is_a_document_blocker_with_an_excerpt_action(agents):
    sid = agents.chat("admin")
    out = agents.as_admin().get("/api/agents/profiles/Reader/access", params={
        "session_id": sid, "task": "Revise the editor document 'DA-02' and tighten clause 4"}).json()
    blocker = out["assign"]["blocker"]
    assert blocker["code"] == "DOCUMENT_UNREADABLE"
    ids = [a["id"] for a in blocker["actions"]]
    assert "attach_excerpt" in ids
    assert "worker" not in blocker["summary"].lower()
    allow = next((a for a in blocker["actions"] if a["id"] == "allow_for_this_run"), None)
    assert allow and allow["extra_tools"] == ["manage_documents"]


@pytest.mark.security
def test_allow_for_this_run_never_offers_a_tool_the_starting_chat_lacks(agents):
    # The chat that would start the task has been narrowed to web search only.
    sid = agents.chat("admin", tool_access="selected", enabled_tools=["web_search"])
    out = agents.as_admin().get("/api/agents/profiles/Reader/access", params={
        "session_id": sid, "task": "Revise the editor document 'DA-02' and tighten clause 4"}).json()
    blocker = out["assign"]["blocker"]
    assert blocker["code"] == "DOCUMENT_UNREADABLE"
    assert "allow_for_this_run" not in [a["id"] for a in blocker["actions"]]
    assert "attach_excerpt" in [a["id"] for a in blocker["actions"]]


def test_writing_task_blocks_a_read_only_agent_and_read_only_requires_clears_it(agents, tmp_path, monkeypatch):
    import src.worker_preflight as pf

    repo = str(tmp_path)
    monkeypatch.setattr(pf, "known_checkouts", lambda: [repo])
    monkeypatch.setattr(pf, "_vetted", lambda paths: list(paths))
    alice = agents.as_admin()
    sid = agents.chat("admin")
    task = "Fix the bug in src/app.py and edit the file"
    out = alice.get("/api/agents/profiles/Reader/access", params={"session_id": sid, "task": task}).json()
    blocker = out["assign"]["blocker"]
    assert blocker["code"] in ("WRITE_NOT_ALLOWED", "WORKSPACE_REQUIRED")
    again = alice.get("/api/agents/profiles/Reader/access", params={
        "session_id": sid, "task": task, "workspace": repo, "requires": "read_only"}).json()
    assert again["assign"]["blocker"] is None


def test_launch_returns_a_structured_409_and_a_retry_with_requires_starts(agents, monkeypatch):
    import src.worker_preflight as pf

    monkeypatch.setattr(pf, "known_checkouts", lambda: [])
    started = {}

    async def fake_launch(**kwargs):
        started.update(kwargs)
        return {"run_id": "r1", "session_id": "w1"}

    from src import agent_control
    alice = agents.as_admin()
    sid = agents.chat("admin")
    task = "Read the code in this repo and explain how requests are routed"
    res = alice.post("/api/agents/launch", json={"task": task, "profile": "Reader", "parent_session": sid})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == body["blocker"]["code"] == "WORKSPACE_REQUIRED"
    assert "run_without_files" in {a["id"] for a in body["blocker"]["actions"]}

    monkeypatch.setattr(agent_control, "launch_worker", fake_launch)
    retry = alice.post("/api/agents/launch", json={"task": task, "profile": "Reader", "parent_session": sid,
                                                   "requires": ["no_workspace"]})
    assert retry.status_code == 200
    assert started["requires"] == ["no_workspace"]


def test_launch_with_extra_tools_runs_a_one_off_copy_and_leaves_the_saved_agent_alone(agents, monkeypatch):
    started = {}

    async def fake_launch(**kwargs):
        started.update(kwargs)
        return {"run_id": "r1", "session_id": "w1"}

    from src import agent_control
    monkeypatch.setattr(agent_control, "launch_worker", fake_launch)
    alice = agents.as_admin()
    sid = agents.chat("admin")
    res = alice.post("/api/agents/launch", json={
        "task": "Look at the editor document 'DA-02' and list its headings", "profile": "Researcher",
        "parent_session": sid, "extra_tools": ["manage_documents"]})
    assert res.status_code == 200
    assert started["profile_name"] is None
    assert "manage_documents" in started["inline_profile"]["enabled_tools"]
    assert "manage_documents" not in agent_profiles.get_profile("Researcher")["enabled_tools"]


@pytest.mark.security
def test_launch_refuses_extra_tools_the_starting_chat_lacks(agents, monkeypatch):
    from src import agent_control

    async def fake_launch(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("started with tools the chat does not hold")

    monkeypatch.setattr(agent_control, "launch_worker", fake_launch)
    sid = agents.chat("admin", tool_access="selected", enabled_tools=["web_search"])
    res = agents.as_admin().post("/api/agents/launch", json={
        "task": "List the headings", "profile": "Researcher", "parent_session": sid,
        "extra_tools": ["manage_documents"]})
    assert res.status_code == 400


def test_document_blocker_blocks_launch_until_the_excerpt_is_in_the_task(agents, monkeypatch):
    from src import agent_control

    async def fake_launch(**kwargs):
        return {"run_id": "r1", "session_id": "w1"}

    monkeypatch.setattr(agent_control, "launch_worker", fake_launch)
    alice = agents.as_admin()
    sid = agents.chat("admin")
    task = "Revise the editor document 'DA-02' and tighten clause 4"
    blocked = alice.post("/api/agents/launch", json={"task": task, "profile": "Reader", "parent_session": sid})
    assert blocked.status_code == 409 and blocked.json()["code"] == "DOCUMENT_UNREADABLE"
    anyway = alice.post("/api/agents/launch", json={"task": task, "profile": "Reader", "parent_session": sid,
                                                    "start_anyway": True})
    assert anyway.status_code == 200


def test_talk_creates_an_owned_chat_with_the_agent_applied(agents):
    alice, bob = agents.as_user("alice"), agents.as_user("bob")
    donor = agents.chat("alice")
    res = alice.post("/api/agents/profiles/Reader/chat", json={"from_session": donor})
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]
    from core.database import get_session_settings
    settings = get_session_settings(sid)
    assert settings["agent_profile"] == "Reader"
    assert settings["tool_access"] == "selected" and "read_file" in settings["enabled_tools"]
    assert sid in agents.session_manager.get_sessions_for_user("alice")
    assert sid not in agents.session_manager.get_sessions_for_user("bob")
    assert bob.post("/api/agents/profiles/Reader/chat", json={"from_session": donor}).status_code == 404


def test_talk_to_an_unknown_agent_is_404(agents):
    assert agents.as_user("alice").post("/api/agents/profiles/Nobody/chat", json={}).status_code == 404


def test_profiles_list_carries_quick_actions(agents):
    rows = agents.as_user("alice").get("/api/agents/profiles").json()["profiles"]
    reader = next(r for r in rows if r["name"] == "Reader")
    assert reader["quick_actions"] == [{"label": "Summarise", "prompt": "Summarise {document}"}]
