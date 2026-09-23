"""A chat's offered tools only grow, so the cached prompt prefix survives.

Tool selection is re-ranked each turn, and the tool list (and the system
prompt keyed on it) sits ahead of the whole history in the provider's prefix
cache. A second turn that picked one different tool re-billed the entire
chat; a discover_tools load did the same on the next turn when the tool
dropped out again. Stale tools must still never be offered: what the policy
now disables, what a turn deliberately prunes, or what no longer exists.
"""
import json

import pytest

import src.agent_loop as al
from src.agent_loop import _STICKY_TOOLS, _sticky_tool_selection
from tests.test_same_turn_tool_attachment import _collect, _patch


@pytest.fixture(autouse=True)
def _fresh():
    _STICKY_TOOLS.clear()
    yield
    _STICKY_TOOLS.clear()


def test_selection_grows_across_turns_in_one_chat():
    assert _sticky_tool_selection("s", {"read_file", "grep"}) == {"read_file", "grep"}
    assert _sticky_tool_selection("s", {"web_search"}) == {"read_file", "grep", "web_search"}
    # another chat is unaffected
    assert _sticky_tool_selection("t", {"web_search"}) == {"web_search"}
    # no session: nothing is remembered
    assert _sticky_tool_selection(None, {"ls"}) == {"ls"}


def test_disabled_pruned_and_vanished_tools_are_dropped():
    _sticky_tool_selection("s", {"read_file", "bash", "mcp__gone__x", "list_emails"})
    out = _sticky_tool_selection(
        "s", {"read_file"}, disabled={"bash"}, excluded={"list_emails"},
        offerable={"read_file", "bash", "list_emails"},
    )
    assert out == {"read_file"}
    # a tool disabled once is not resurrected from memory when re-enabled
    assert _sticky_tool_selection("s", {"grep"}) == {"read_file", "grep"}


def test_the_set_restarts_past_the_cap(monkeypatch):
    monkeypatch.setattr(al, "_STICKY_TOOLS_MAX", 3)
    _sticky_tool_selection("s", {"a", "b", "c"})
    assert _sticky_tool_selection("s", {"d"}) == {"d"}


def _turn(monkeypatch, sent, rounds, *, disabled_tools=None):
    async def _fake_stream(_candidates, messages, **kwargs):
        idx = len(sent)
        sent.append({(t.get("function") or {}).get("name") for t in (kwargs.get("tools") or [])})
        text, calls = rounds[min(idx, len(rounds) - 1)]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        if calls:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o",
        # Not "check the loadouts": that names an admin keyword, which routes
        # manage_agent_loadout into round 1's schema list and leaves nothing
        # for discovery to add. See test_same_turn_tool_attachment.
        [{"role": "user", "content": "Hand this to a subagent"}],
        max_rounds=4, relevant_tools={"read_file"},
        disabled_tools=disabled_tools, session_id="chat-1",
    ))


def test_a_discovered_tool_is_still_offered_on_the_next_turn(monkeypatch):
    def _result(block):
        if block.tool_type == "discover_tools":
            return {"output": "Loaded", "exit_code": 0,
                    "loaded_names": ["manage_agent_loadout"], "continue_same_turn": True}
        return {"output": "ok", "exit_code": 0}

    _patch(monkeypatch, [], _result)
    discover = [{"name": "discover_tools", "arguments": json.dumps({"query": "loadout"})}]
    first = []
    _turn(monkeypatch, first, [(None, discover), ("Done.", None)])
    assert "manage_agent_loadout" not in first[0] and "manage_agent_loadout" in first[1]

    second = []
    _turn(monkeypatch, second, [("Still here.", None)])
    assert second[0] == first[-1]          # same tool list: the prefix stays cached

    third = []
    _turn(monkeypatch, third, [("Done.", None)], disabled_tools={"manage_agent_loadout"})
    assert "manage_agent_loadout" not in third[0]
