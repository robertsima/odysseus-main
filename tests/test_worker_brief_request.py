"""A worker sees the person's own words, not only the parent's paraphrase.

2026-10-01, the "Agamemnon" theme: the user asked for a layout change and got a
colour scheme because the briefs carried limits the user never set. `start`
now appends the person's latest real request, verbatim, under the brief; for a
worker's worker it comes from the root chat.
"""

import types

import pytest

from src import agent_loadouts
from src import agent_loop as al
from src.agent_tools import loadout_tools
from src.agent_tools.loadout_tools import manage_agent_loadout

pytestmark = pytest.mark.asyncio

HEADER = loadout_tools._REQUEST_HEADER


def _msg(role, content, **meta):
    return types.SimpleNamespace(role=role, content=content, metadata=meta)


def _chats(monkeypatch, chats, parents=None):
    """chats: id -> history list; parents: worker id -> parent id."""
    parents = parents or {}
    sessions = {sid: types.SimpleNamespace(owner="u", history=hist) for sid, hist in chats.items()}
    monkeypatch.setattr("src.ai_interaction.get_session_manager",
                        lambda: types.SimpleNamespace(get_session=lambda sid: sessions.get(sid)))
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid, **k: {"parent_session": parents[sid]} if sid in parents else {})


@pytest.fixture
def launched(monkeypatch):
    saved = {"profiles": []}
    monkeypatch.setattr(agent_loadouts, "_write", lambda p: saved.__setitem__("profiles", list(p)))
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr(agent_loadouts, "_mcp_state", lambda: ({}, set()))
    monkeypatch.setattr("src.retrieval_health.cached_problems", lambda: [])
    known = {"bash", "read_file", "grep", "manage_agent_loadout"}
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: {
        "allowed_tools": set(known), "known_tools": set(known), "skill_names": set(),
        "allowed_models": set(), "memory_access": "write", "skill_access": "all",
        "model_access": "all", "allowed_mcp_servers": ["*"], "private_vault_access": True,
        "delegation_policy": "auto", "max_parallel_workers": 2, "approval_mode": "auto",
        "tool_access": "all", "enabled_tools": [], "denied_tools": set()})
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)
    seen = {}

    async def fake_launch(**kwargs):
        seen.update(kwargs)
        return {"session_id": "w", "session_name": "W", "run_id": "r", "model": "m", "max_rounds": 0}

    monkeypatch.setattr("src.agent_control.launch_worker", fake_launch)
    return seen


async def _start(session, task="Build the theme", **extra):
    import json
    await manage_agent_loadout(
        '{"action": "create", "name": "Eng", "tool_access": "selected", "enabled_tools": ["read_file", "grep"]}',
        session, owner="u")
    return await manage_agent_loadout(
        json.dumps({"action": "start", "name": "Eng", "task": task, **extra}), session, owner="u")


async def test_the_persons_request_is_appended_verbatim(monkeypatch, launched):
    ask = "wanted it to be more than a color scheme. wanted the swap to change the layout"
    _chats(monkeypatch, {"c": [_msg("user", ask, source="user")]})
    result = await _start("c")
    assert result["exit_code"] == 0
    assert launched["task"] == f"Build the theme\n\n{HEADER}\n{ask}"


async def test_hand_backs_and_context_are_skipped_and_attachments_named(monkeypatch, launched):
    _chats(monkeypatch, {"c": [
        _msg("user", "match the mockup", source="user",
             attachments=[{"id": "up1", "name": "mockup.png", "mime": "image/png"}]),
        _msg("user", "[Worker result] done", source="worker"),
        _msg("user", "UNTRUSTED SOURCE DATA ...", source="user"),
    ]})
    await _start("c")
    assert "match the mockup\nAttached: mockup.png (image/png) id=up1" in launched["task"]
    assert "Worker result" not in launched["task"]


async def test_a_long_request_is_clipped(monkeypatch, launched):
    _chats(monkeypatch, {"c": [_msg("user", "x" * 5000, source="user")]})
    await _start("c")
    tail = launched["task"].split(HEADER + "\n", 1)[1]
    assert tail.startswith("x" * 2000) and "x" * 2001 not in tail
    assert "clipped at 2000" in tail


async def test_it_is_not_added_when_the_task_already_has_it(monkeypatch, launched):
    ask = "make the icon look like the mockup"
    _chats(monkeypatch, {"c": [_msg("user", ask, source="user")]})
    await _start("c", task=f"Goal: {ask}.\nStart in web/")
    assert HEADER not in launched["task"]


async def test_a_nested_worker_carries_the_root_chats_request(monkeypatch, launched):
    _chats(monkeypatch,
           {"root": [_msg("user", "the real ask", source="user")],
            "w1": [_msg("user", "orchestrator's paraphrase", source="dashboard")]},
           parents={"w1": "root"})
    await _start("w1")
    assert launched["task"].endswith(f"{HEADER}\nthe real ask")


async def test_no_person_request_leaves_the_task_alone(monkeypatch, launched):
    _chats(monkeypatch, {})
    await _start("c")
    assert launched["task"] == "Build the theme"


def _system(tools):
    messages, _ = al._build_system_prompt(
        [{"role": "user", "content": "build it"}], model="test", active_document=None, mcp_mgr=None,
        disabled_tools=set(), relevant_tools=set(tools), compact=True, suppress_skills=True)
    return "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")


async def test_the_rules_reach_a_chat_that_can_launch_workers():
    text = _system({"manage_agent_loadout", "ask_user"})
    assert "scope is the person's whole request" in text
    assert "whole user-visible outcome" in text
    assert "rendered result compared with the reference" in text
    assert "resume that same worker" in text and "send_to_session" in text
    assert "independent tool calls together in one round" in text
