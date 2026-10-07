"""One skill filter for the agent loop's index and the keyword-matched
"Relevant skills" block (2026-10-01).

Before, the loop compared `requires_toolsets` with native tool names only (so a
connected Penpot never showed its skill), and the keyword match filtered
nothing. The chat route's own copy of the index is gone (2026-10-06); see
tests/src/chat_processor/test_skills_index_left_to_agent_loop.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import integration_registry, skill_toolsets
from src.skill_toolsets import SkillVisibility

PENPOT_TOOL = "mcp__penpot_studio__build_design"


class _Mcp:
    def __init__(self, tools=(), statuses=None):
        self._tools = list(tools)
        self._statuses = statuses or {}

    def get_all_tools(self, *_a, **_k):
        return [
            {"server_id": "penpot_studio", "server_name": "Penpot Studio",
             "qualified_name": q, "name": q.split("__")[-1], "is_disabled": False}
            for q in self._tools
        ]

    def get_all_openai_schemas(self, *_a, **_k):
        return []

    def get_tool_descriptions_for_prompt(self, *_a, **_k):
        return ""

    def get_all_statuses(self):
        return dict(self._statuses)


def _write_skill(root, name, *, requires="", integration="", source="learned", extra=""):
    skill_dir = root / "skills" / "general" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}", f"description: {extra or 'design mockups in penpot'}",
          "version: 1.0.0", "category: general", "tags: [penpot, design]"]
    if requires:
        fm.append(f"requires_toolsets: [{requires}]")
    if integration:
        fm.append(f"requires_integration: {integration}")
    fm += [f"status: published", "confidence: 0.9", f"source: {source}",
           "created: 2026-01-01T00:00:00Z", "---", "", "## When to Use", "- design a penpot mockup",
           "", "## Procedure", "1. open penpot", ""]
    (skill_dir / "SKILL.md").write_text("\n".join(fm), encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A data dir with a penpot skill, and switches for what is connected."""
    import src.constants as constants

    skill_toolsets.reset_cache()
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    state = SimpleNamespace(available=set(), mcp=_Mcp())
    monkeypatch.setattr(integration_registry, "available_ids", lambda manager=None: set(state.available))
    # The chat route has no manager of its own and asks for the global one.
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: state.mcp)
    _write_skill(tmp_path, "penpot-design-workflow", requires=PENPOT_TOOL, integration="penpot")
    _write_skill(tmp_path, "plain-notes", extra="plain notes")
    state.root = tmp_path

    def connect():
        state.available = {"penpot"}
        state.mcp = _Mcp([PENPOT_TOOL], {"penpot_studio": {"status": "connected"}})
        skill_toolsets.reset_cache()

    state.connect = connect
    yield state
    skill_toolsets.reset_cache()


def _index_names(world, **kwargs):
    from src.agent_loop import _build_base_prompt

    _, block = _build_base_prompt(set(), world.mcp, False, None, **kwargs)
    return set(getattr(block, "names", ()))


def _relevant_names(world, monkeypatch, message="design a penpot mockup"):
    from src.agent_loop import _build_system_prompt

    monkeypatch.setattr("src.agent_loop._extract_last_user_message", lambda _m: message)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda _o: {"skill_max_injected": 5}, raising=False)
    msgs, _ = _build_system_prompt(
        [{"role": "user", "content": message}], "m", None, world.mcp, owner=None,
    )
    skills_msgs = [m for m in msgs if (m.get("metadata") or {}).get("source") == "skills"]
    text = "\n".join(m["content"] for m in skills_msgs)
    return text, skills_msgs


def test_visibility_lists_connected_mcp_tools_and_integrations(world):
    world.connect()
    vis = skill_toolsets.skill_visibility(set(), world.mcp)
    assert PENPOT_TOOL in vis.active_toolsets
    assert "penpot_studio" in vis.active_toolsets and "Penpot Studio" in vis.active_toolsets
    assert vis.available_integrations == frozenset({"penpot"})
    # Native tools are there too, minus what policy disabled.
    assert "read_file" in vis.active_toolsets
    assert "read_file" not in skill_toolsets.skill_visibility({"read_file"}, world.mcp).active_toolsets


def test_visibility_fails_open(world, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(integration_registry, "available_ids", boom)
    skill_toolsets.reset_cache()
    vis = skill_toolsets.skill_visibility(set(), world.mcp)
    assert vis.available_integrations is None
    assert skill_toolsets.visible_skills([{"requires_integration": "penpot"}], vis)


def test_connected_penpot_shows_the_skill_on_every_path(world, monkeypatch):
    world.connect()
    assert "penpot-design-workflow" in _index_names(world)                      # index
    text, _ = _relevant_names(world, monkeypatch)                               # keyword match
    assert "### penpot-design-workflow" in text


def test_absent_penpot_hides_the_skill_on_every_path(world, monkeypatch):
    names = _index_names(world)                                                 # index
    assert "penpot-design-workflow" not in names and "plain-notes" in names
    text, _ = _relevant_names(world, monkeypatch)                               # keyword match
    assert "### penpot-design-workflow" not in text and "penpot-design-workflow" not in text


def test_tool_gone_hides_skill_even_when_integration_is_up(world):
    world.connect()
    world.mcp = _Mcp([], {"penpot_studio": {"status": "connected"}})
    skill_toolsets.reset_cache()
    assert "penpot-design-workflow" not in _index_names(world)


def test_loadout_scope_applies_to_the_index(world):
    world.connect()
    assert _index_names(world, skill_scope={"plain-notes"}) == {"plain-notes"}
    assert _index_names(world, skill_scope=set()) == set()


def test_one_learned_skill_does_not_arm_the_gate_when_only_shipped_skills_are_shown(world, monkeypatch):
    # The learned skill the owner has is hidden by loadout scope; what is shown is shipped.
    import src.builtin_skills as builtin_skills
    from src.agent_loop import _build_system_prompt

    world.connect()
    _write_skill(world.root, "shipped-one", extra="shipped procedure", source="bundled")
    _write_skill(world.root, "my-learned", extra="learned procedure", source="learned")
    monkeypatch.setattr(builtin_skills, "is_shipped_skill", lambda s: s.get("source") == "bundled")
    monkeypatch.setattr("src.agent_loop._extract_last_user_message", lambda _m: "zzz qqq")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda _o: {"skill_max_injected": 0}, raising=False)

    def gate(scope):
        msgs, _ = _build_system_prompt(
            [{"role": "user", "content": "zzz qqq"}], "m", None, world.mcp, owner=None, skill_scope=scope,
        )
        sk = [m for m in msgs if (m.get("metadata") or {}).get("source") == "skills"]
        assert sk, "the index block should be present"
        return sk[0]["metadata"]["tool_gate_untrusted"]

    assert gate({"shipped-one"}) is False
    assert gate(None) is True  # the learned skill is shown, so the gate arms


def test_routing_text_comes_from_available_manifests_only(world, monkeypatch):
    monkeypatch.setattr(integration_registry, "get",
                        lambda _i: SimpleNamespace(prompt="Penpot: build with the studio tools."))
    assert skill_toolsets.integration_routing_text(set()) == ""
    assert skill_toolsets.integration_routing_text({"penpot"}) == "Penpot: build with the studio tools."

    from src.agent_loop import _build_base_prompt

    world.connect()
    prompt, _ = _build_base_prompt(set(), world.mcp, False, None)
    assert "Penpot: build with the studio tools." in prompt
    skill_toolsets.reset_cache()
    world.available = set()
    prompt, _ = _build_base_prompt(set(), world.mcp, False, None)
    assert "Penpot: build with the studio tools." not in prompt


def test_server_instruction_marker_is_not_prompt_text(monkeypatch):
    monkeypatch.setattr(integration_registry, "get", lambda _i: SimpleNamespace(prompt="from_server_instructions"))
    assert skill_toolsets.integration_routing_text({"x"}) == ""


def test_manage_skills_list_and_search_hide_skills_for_absent_integrations(world):
    import asyncio
    import json

    from src.tools.system import do_manage_skills

    out = asyncio.run(do_manage_skills(json.dumps({"action": "list"})))
    assert "penpot-design-workflow" not in out["results"] and "plain-notes" in out["results"]
    world.connect()
    out = asyncio.run(do_manage_skills(json.dumps({"action": "list"})))
    assert "penpot-design-workflow" in out["results"]
    out = asyncio.run(do_manage_skills(json.dumps({"action": "search", "query": "penpot design mockups"})))
    assert "penpot-design-workflow" in out["results"]


def test_jellyfin_is_not_named_in_prompts_or_intent_keywords():
    import inspect

    import src.agent_loop as agent_loop
    import src.tool_index as tool_index

    for module in (agent_loop, tool_index):
        assert "jellyfin" not in inspect.getsource(module).lower()
