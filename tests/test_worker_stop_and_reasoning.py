"""An agent can actually stop a worker it started, and a loadout can lower
how much a ChatGPT-subscription model thinks per step.

"Stop the scout" used to reach the worker only as a message (message_agent);
it read it and ran ten more rounds until the user cancelled it by hand.
"""
import asyncio
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def loadout_tool(monkeypatch):
    from src import agent_activity, agent_control, agent_loadouts
    from src.agent_tools import loadout_tools

    # The calling chat's policy is read from the database; stop does not use it.
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda session_id, owner: {})

    runs = [
        {"run_id": "session-a", "status": "running", "title": "Worker · Scout",
         "session_id": "w1", "summary": {"target_session": "w1"}},
        {"run_id": "session-b", "status": "completed", "title": "Worker · Old",
         "session_id": "w2", "summary": {"target_session": "w2"}},
    ]
    monkeypatch.setattr(agent_activity, "list_runs", lambda **kw: runs if kw.get("session_id") == "parent" else [])
    stopped = []

    async def fake_stop(run_id):
        stopped.append(run_id)
        return {"stopped": True, "how": "headless"}

    monkeypatch.setattr(agent_control, "stop_run", fake_stop)
    return loadout_tools, stopped


def _dispatch(module, args):
    return module.manage_agent_loadout(json.dumps(args), session_id="parent", owner="alice")


def test_stop_stops_the_only_running_worker(loadout_tool):
    module, stopped = loadout_tool
    out = _run(_dispatch(module, {"action": "stop"}))
    assert stopped == ["session-a"] and out["exit_code"] == 0


def test_stop_by_worker_session_and_refuses_foreign_runs(loadout_tool):
    module, stopped = loadout_tool
    out = _run(_dispatch(module, {"action": "stop", "worker_session": "w1"}))
    assert stopped == ["session-a"] and out["exit_code"] == 0
    stopped.clear()
    out = _run(_dispatch(module, {"action": "stop", "run_id": "session-zzz"}))
    assert stopped == [] and out["exit_code"] == 1


def test_loadout_reasoning_effort_is_validated_and_copied_to_the_chat():
    from src import agent_profiles, session_settings

    prof = agent_profiles.validate_profiles([{"name": "Scout", "reasoning_effort": "LOW"}])[0]
    assert prof["reasoning_effort"] == "low"
    assert agent_profiles.session_patch(prof)["agent_reasoning_effort"] == "low"
    assert agent_profiles.validate_profiles([{"name": "Plain"}])[0]["reasoning_effort"] == ""
    with pytest.raises(ValueError):
        agent_profiles.validate_profiles([{"name": "Bad", "reasoning_effort": "turbo"}])
    assert session_settings.validate_patch({"agent_reasoning_effort": ""}) == {"agent_reasoning_effort": None}


def test_chatgpt_request_sends_the_chat_or_global_effort_only_when_set(monkeypatch):
    import core.database as database
    import src.settings as settings
    from src import llm_core

    per_chat = {"s-low": {"agent_reasoning_effort": "low"}}
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: per_chat.get(sid, {}))
    glob = {"chatgpt_reasoning_effort": ""}
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: glob.get(key, default))

    def payload(cache_key):
        return llm_core._build_chatgpt_responses_payload(
            "gpt-5.6-sol", [{"role": "user", "content": "hi"}], 1.0, 0,
            stream=True, cache_key=cache_key)

    assert payload("s-low")["reasoning"] == {"effort": "low"}
    assert "reasoning" not in payload("s-other")          # default: nothing sent
    glob["chatgpt_reasoning_effort"] = "medium"
    assert payload("s-other")["reasoning"] == {"effort": "medium"}
    assert payload("s-low")["reasoning"] == {"effort": "low"}   # the loadout wins


def test_agent_cards_are_restored_after_the_chat_re_renders():
    wb = (ROOT / "static/js/workbench.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static/js/sessions.js").read_text(encoding="utf-8")
    assert "odysseus:history-rendered" in sessions and "odysseus:history-rendered" in wb
    restore = wb.split("function restoreChatCards()", 1)[1].split("\nfunction ", 1)[0]
    assert "!card.isConnected" in restore and "isLive(run.status)" in restore
