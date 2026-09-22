"""A connected, permitted tool becomes callable in the same user turn.

Observed 2026-09-21: the MCP prompt note told the model to say it lacked the
exact tool so it would be attached, but nothing in upstream's loop listened.
The turn ended on "I don't have the exact manage_agent_loadout tool attached
this turn", the user said "try again", then "u do have it", and got the same
answer. `discover_tools` was offered in the schema list but always refused,
because the loop never gave the executor a discovery context.
"""

import asyncio
import json

import src.agent_loop as al
from src.agent_loop import (
    _missing_tools_to_attach,
    _scope_skills,
    _skill_scope_from_settings,
)
from src.tool_discovery import TurnToolDiscovery


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch(monkeypatch, exec_calls, exec_result=None):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        exec_calls.append(block.tool_type)
        if exec_result:
            return block.tool_type, exec_result(block)
        return block.tool_type, {"output": "ok", "exit_code": 0}
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run(monkeypatch, rounds, *, disabled_tools=None):
    """Each entry in ``rounds`` is (text, native_calls) for that round."""
    sent = []

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
    events = _events(_collect(al.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o",
        [{"role": "user", "content": "Create the Planning Command Center loadout"}],
        max_rounds=5,
        relevant_tools={"read_file"},
        disabled_tools=disabled_tools,
    )))
    return events, sent


REFUSAL = "I don't have the exact `manage_agent_loadout` tool attached this turn, so I can't create it."
CALL = [{"name": "manage_agent_loadout", "arguments": json.dumps({"action": "status"})}]


def test_named_missing_tool_is_attached_and_called_in_the_same_turn(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    events, sent = _run(monkeypatch, [(REFUSAL, None), (None, CALL), ("Created.", None)])
    assert "manage_agent_loadout" not in sent[0]
    assert "manage_agent_loadout" in sent[1]
    assert calls == ["manage_agent_loadout"]
    attached = [e for e in events if e.get("type") == "tools_attached"]
    assert attached and attached[0]["tools"] == ["manage_agent_loadout"]


def test_a_disabled_tool_is_never_attached_by_naming_it(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    events, sent = _run(
        monkeypatch, [(REFUSAL, None), (None, CALL)],
        disabled_tools={"manage_agent_loadout"},
    )
    assert len(sent) == 1
    assert calls == []
    assert not any(e.get("type") == "tools_attached" for e in events)


def test_rearm_is_bounded(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    # The model keeps refusing, naming a new tool each time it gets one.
    rounds = [
        ("I do not have manage_agent_loadout available.", None),
        ("I do not have manage_skills available.", None),
        ("I do not have manage_settings available.", None),
        ("I do not have manage_session available.", None),
    ]
    _events_, sent = _run(monkeypatch, rounds)
    assert len(sent) == 1 + al._MAX_TOOL_REARMS


def test_discovered_tools_join_the_next_round(monkeypatch):
    calls = []

    def _result(block):
        if block.tool_type == "discover_tools":
            return {"output": "Loaded", "exit_code": 0, "loaded_names": ["manage_agent_loadout"],
                    "continue_same_turn": True}
        return {"output": "ok", "exit_code": 0}

    _patch(monkeypatch, calls, _result)
    discover = [{"name": "discover_tools", "arguments": json.dumps({"query": "manage_agent_loadout"})}]
    _events_, sent = _run(monkeypatch, [(None, discover), (None, CALL), ("Created.", None)])
    assert "discover_tools" in sent[0]
    assert "manage_agent_loadout" in sent[1]
    assert calls == ["discover_tools", "manage_agent_loadout"]


# ── The refusal detector ────────────────────────────────────────────────

PERMITTED = {"manage_agent_loadout", "mcp__todoist__todoist", "web_search", "mcp__bsky-mcp__search_posts"}


def test_the_logged_refusals_are_detected():
    assert _missing_tools_to_attach(
        "I don’t have the exact `manage_agent_loadout` tool attached this turn.",
        sent={"read_file"}, permitted=PERMITTED,
    ) == {"manage_agent_loadout"}
    assert _missing_tools_to_attach(
        "I still do not have the exact mcp__todoist__todoist function in my callable tool set.",
        sent=set(), permitted=PERMITTED,
    ) == {"mcp__todoist__todoist"}
    assert _missing_tools_to_attach(
        "mcp__bsky-mcp__search_posts is not attached this turn.", sent=set(), permitted=PERMITTED,
    ) == {"mcp__bsky-mcp__search_posts"}


def test_answers_that_mention_tools_do_not_rearm():
    # Used and sent this round.
    assert not _missing_tools_to_attach(
        "I used web_search. It did not find anything newer.", sent={"web_search"}, permitted=PERMITTED,
    )
    # Named, but not in a refusal sentence.
    assert not _missing_tools_to_attach(
        "Results from web_search are below. The weather is not great.", sent=set(), permitted=PERMITTED,
    )
    # A long report that discusses tools is not a refusal.
    report = "## Audit\n" + ("The profile does not bind manage_agent_loadout here. " * 60)
    assert not _missing_tools_to_attach(report, sent=set(), permitted=PERMITTED)
    # Not permitted.
    assert not _missing_tools_to_attach(
        "I don't have manage_tokens available.", sent=set(), permitted=PERMITTED,
    )


# ── Discovery signals the same turn continues ───────────────────────────

def test_discovery_says_the_turn_continues():
    schema = {"type": "function", "function": {"name": "manage_agent_loadout", "description": "Create loadouts",
                                                "parameters": {"type": "object", "properties": {}}}}
    disc = TurnToolDiscovery([schema])
    result = asyncio.run(disc.discover("manage_agent_loadout"))
    assert result["loaded_names"] == ["manage_agent_loadout"]
    assert result["continue_same_turn"] is True
    assert "same turn" in result["output"]
    assert disc.permitted_names() == {"manage_agent_loadout"}
    assert TurnToolDiscovery([schema], disabled_tools={"manage_agent_loadout"}).permitted_names() == set()


# ── Profile skill scope ─────────────────────────────────────────────────

def test_skill_scope_follows_the_saved_profile():
    skills = [{"name": "todoist-planning"}, {"name": "Daily-Command-Center"}, {"name": "unrelated"}]
    assert _skill_scope_from_settings({}) is None
    assert _scope_skills(skills, None) == skills
    assert _scope_skills(skills, _skill_scope_from_settings({"skill_access": "none"})) == []
    scope = _skill_scope_from_settings({"skill_access": "selected",
                                        "skill_names": ["todoist-planning", "daily-command-center"]})
    assert [s["name"] for s in _scope_skills(skills, scope)] == ["todoist-planning", "Daily-Command-Center"]


# ── Skill dependencies resolve through the canonical resolver ──────────

class _FakeMcp:
    def __init__(self, tools):
        self._tools = tools

    def get_all_tools(self, *_a, **_k):
        return list(self._tools)


_PLANNING_MCP = _FakeMcp([
    {"qualified_name": "mcp__todoist__todoist", "server_id": "todoist", "server_name": "Todoist"},
    {"qualified_name": "mcp__lotus__mood_summarize_period", "server_id": "lotus", "server_name": "Lotus"},
    {"qualified_name": "mcp__lotus__mood_detect_low_energy_patterns", "server_id": "lotus", "server_name": "Lotus"},
])


def test_planning_skill_dependencies_resolve_to_callable_tools():
    from src.skill_toolsets import skill_declared_tools

    skill = {"name": "daily-command-center",
             "requires_toolsets": ["todoist", "lotus", "calendar", "search_documents", "not a real toolset"]}
    tools, unknown = skill_declared_tools([skill], set(), _PLANNING_MCP)
    assert {"mcp__todoist__todoist", "mcp__lotus__mood_summarize_period",
            "mcp__lotus__mood_detect_low_energy_patterns", "manage_calendar", "search_documents"} <= tools
    assert unknown == {"not a real toolset"}


def test_policy_denied_dependency_is_dropped_but_not_reported_as_bad_metadata():
    from src.skill_toolsets import skill_declared_tools

    skill = {"name": "todoist-planning", "requires_toolsets": ["todoist"]}
    tools, unknown = skill_declared_tools([skill], {"mcp__todoist__todoist"}, _PLANNING_MCP)
    assert tools == set() and unknown == set()


def test_loaded_skill_attaches_its_mcp_dependency_for_the_next_round(monkeypatch):
    """`manage_skills view` of a skill declaring a server name binds that server."""
    calls = []
    _patch(monkeypatch, calls)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: _PLANNING_MCP, raising=False)

    class _Sm:
        def __init__(self, *_a, **_k):
            pass

        def load(self, owner=None):
            return [{"name": "todoist-planning", "requires_toolsets": ["todoist"]}]

    import services.memory.skills as skills_mod
    monkeypatch.setattr(skills_mod, "SkillsManager", _Sm)
    monkeypatch.setattr(_PLANNING_MCP, "get_all_openai_schemas", lambda *_a, **_k: [
        {"type": "function", "function": {"name": "mcp__todoist__todoist", "description": "Todoist CLI",
                                          "parameters": {"type": "object", "properties": {}}}},
    ], raising=False)
    view = [{"name": "manage_skills", "arguments": json.dumps({"action": "view", "name": "todoist-planning"})}]
    todo = [{"name": "mcp__todoist__todoist", "arguments": json.dumps({"args": ["today", "--json"]})}]
    _events_, sent = _run(monkeypatch, [(None, view), (None, todo), ("Planned.", None)])
    assert "mcp__todoist__todoist" not in sent[0]
    assert "mcp__todoist__todoist" in sent[1]
