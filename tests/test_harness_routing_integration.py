"""End-to-end seams for bounded, turn-local capability discovery.

The provider stream and non-discovery side effects are fake.  Selection,
canonical schemas, parsing, discovery dispatch, and loop round transitions are
real so these tests catch wiring regressions rather than merely testing a mock.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

import src.agent_loop as agent_loop
from src.tool_discovery import TurnToolDiscovery
from src.tool_execution import execute_tool_block as dispatch_tool
from src.tool_selection import plan_tool_selection
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _name(schema):
    return (schema.get("function") or {}).get("name") or schema.get("name")


def _schema(name):
    return next(copy.deepcopy(s) for s in FUNCTION_TOOL_SCHEMAS if _name(s) == name)


def _native_call(name, arguments, call_id):
    return "data: " + json.dumps({
        "type": "tool_calls",
        "calls": [{"id": call_id, "name": name, "arguments": json.dumps(arguments)}],
    }) + "\n\n"


def _delta(text):
    return "data: " + json.dumps({"delta": text}) + "\n\n"


def _collect(gen):
    async def run():
        return [chunk async for chunk in gen]
    return asyncio.run(run())


@pytest.fixture
def admin_owner(monkeypatch, tmp_path):
    """A real configured admin, reached through the production auth accessor."""
    from core.auth import AuthManager
    import core.auth as auth_module
    import src.auth_helpers as auth_helpers

    manager = AuthManager(str(tmp_path / "auth.json"))
    assert manager.create_user("routing-admin", "routing-test-password", is_admin=True)
    monkeypatch.setattr(auth_module, "get_auth_manager", lambda: manager)
    monkeypatch.setattr(auth_helpers, "_auth_disabled", lambda: False)
    from src.tool_security import owner_is_admin_or_single_user
    assert owner_is_admin_or_single_user("routing-admin") is True
    return "routing-admin"


def _patch_basics(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False)
    # Keep a harness test hermetic: context lookup otherwise probes /models,
    # and host capability declarations may probe local executables/services.
    import src.model_context as model_context
    import src.capabilities as capabilities
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda *args, **kwargs: 32_000)
    monkeypatch.setattr(capabilities, "unavailable_tools", lambda: set())
    monkeypatch.setattr(capabilities, "capability_for_tool", lambda name: None)


def _install_dispatch_spy(monkeypatch):
    executed = []

    async def execute(block, *args, **kwargs):
        if block.tool_type == "discover_tools":
            # Retain the real dispatcher contract, including its fresh policy
            # snapshot and validation of query/max_results.
            return await dispatch_tool(block, *args, **kwargs)
        executed.append(block.tool_type)
        return block.tool_type, {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "execute_tool_block", execute, raising=False)
    return executed


def test_discovery_contract_is_bounded_stable_and_does_not_mutate_schemas():
    source = [_schema("get_workspace"), _schema("grep"), _schema("read_file")]
    pristine = copy.deepcopy(source)

    async def exercise():
        first = TurnToolDiscovery(source, max_loaded=2)
        second = TurnToolDiscovery(reversed(source), max_loaded=2)
        a = await first.discover("get_workspace grep read_file", max_results=8)
        b = await second.discover("get_workspace grep read_file", max_results=8)
        return first, a, b

    turn, result_a, result_b = asyncio.run(exercise())
    assert result_a["loaded_names"] == result_b["loaded_names"]
    assert len(result_a["loaded_names"]) == 2
    assert result_a["max_results"] == 8
    assert turn.loaded_names == set(result_a["loaded_names"])
    assert source == pristine


def test_initial_selection_is_stably_ordered_and_schema_token_bounded():
    available = {"grep", "read_file", "get_workspace", "manage_calendar"}
    candidates_a = {
        "semantic": {"manage_calendar", "read_file", "grep"},
        "core": {"get_workspace"},
    }
    candidates_b = {
        "core": ["get_workspace"],
        "semantic": ["grep", "manage_calendar", "read_file"],
    }
    costs = {name: 70 for name in available}
    first = plan_tool_selection(
        available, candidates_a, schema_costs=costs, max_tools=3, max_schema_tokens=140,
    )
    second = plan_tool_selection(
        reversed(sorted(available)), candidates_b,
        schema_costs=costs, max_tools=3, max_schema_tokens=140,
    )
    assert first.selected == second.selected == ("get_workspace", "grep")
    assert first.estimated_schema_tokens == 140
    assert set(first.deferred) == {"manage_calendar", "read_file"}


def test_complete_round_selection_is_exact_and_stably_sorted(monkeypatch):
    monkeypatch.setattr(agent_loop, "_withhold_unavailable_tools", lambda schemas: schemas)
    external = {
        "type": "function",
        "function": {
            "name": "mcp__demo__zebra",
            "description": "zebra",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    kwargs = dict(
        force_answer=False,
        is_api_model=True,
        relevant_tools={"mcp__demo__zebra", "grep"},
        needs_admin=True,
        mcp_schemas=[external],
        disabled_tools=set(),
        ody_qwen_finetune_model=False,
        last_user="admin settings",
        selection_is_complete=True,
    )
    names = [_name(schema) for schema in agent_loop._tool_schemas_for_round(**kwargs)]
    assert names == ["grep", "mcp__demo__zebra"]
    assert not set(names) & agent_loop._ADMIN_TOOLS

    kwargs["relevant_tools"] = set()
    assert agent_loop._tool_schemas_for_round(**kwargs) == []

    kwargs.update(relevant_tools={"grep"}, is_api_model=False)
    assert agent_loop._tool_schemas_for_round(**kwargs) == []


def test_selected_base_prompt_does_not_reexpand_all_admin_tools(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    prompt, _ = agent_loop._build_base_prompt(
        set(), None, True, {"discover_tools"},
        suppress_local_context=True, suppress_skills=True,
    )
    assert "```discover_tools" in prompt
    assert '"query": "calendar events"' in prompt
    assert "manage_settings" not in prompt


def test_selected_mcp_prompt_is_bounded_and_native_prompt_has_no_duplicate(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "set_active_model", lambda model: None)

    selected = {
        "type": "function",
        "function": {
            "name": "mcp__demo__selected",
            "description": "SELECTED_DESCRIPTION",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
    omitted = {
        "type": "function",
        "function": {
            "name": "mcp__demo__omitted",
            "description": "OMITTED_SECRET_DESCRIPTION",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    class Manager:
        def get_all_openai_schemas(self, disabled):
            return [omitted, selected]

        def get_tool_descriptions_for_prompt(self, disabled):
            raise AssertionError("bounded selection must not request the full MCP inventory")

    common = dict(
        model="test", active_document=None, mcp_mgr=Manager(), disabled_tools=set(),
        relevant_tools={"mcp__demo__selected"}, suppress_local_context=True,
        suppress_skills=True,
    )
    fenced, _ = agent_loop._build_system_prompt(
        [{"role": "user", "content": "use selected"}], compact=False, **common,
    )
    fenced_text = "\n".join(str(message.get("content") or "") for message in fenced)
    assert "SELECTED_DESCRIPTION" in fenced_text
    assert "OMITTED_SECRET_DESCRIPTION" not in fenced_text

    native, _ = agent_loop._build_system_prompt(
        [{"role": "user", "content": "use selected"}], compact=True,
        native_tools=True, **common,
    )
    native_text = "\n".join(str(message.get("content") or "") for message in native)
    assert "SELECTED_DESCRIPTION" not in native_text
    assert "OMITTED_SECRET_DESCRIPTION" not in native_text

    compact_fenced, _ = agent_loop._build_system_prompt(
        [{"role": "user", "content": "use selected"}], compact=True,
        native_tools=False, **common,
    )
    compact_fenced_text = "\n".join(str(message.get("content") or "") for message in compact_fenced)
    assert "SELECTED_DESCRIPTION" in compact_fenced_text
    assert "OMITTED_SECRET_DESCRIPTION" not in compact_fenced_text


def test_discovery_never_returns_or_loads_a_forbidden_exact_name():
    turn = TurnToolDiscovery(
        [_schema("get_workspace"), _schema("send_email")],
        disabled_tools={"send_email"},
    )
    result = asyncio.run(turn.discover("send_email", max_results=8))
    assert result["loaded_names"] == []
    assert turn.loaded_names == set()
    assert all(_name(schema) != "send_email" for schema in turn.loaded_tools)


def test_unknown_domain_statement_retains_discovery_without_classifier(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    import src.tool_index as tool_index
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)
    calls = []
    async def stream(_candidates, messages, **kwargs):
        calls.append(kwargs.get("tools") or [])
        assert "discover_tools" in {_name(schema) for schema in calls[-1]}
        yield _delta("What outcome do you need?")
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-test",
        [{"role": "user", "content": "My workflow is broken."}],
        max_rounds=1, owner=admin_owner, _is_teacher_run=True,
    ))
    assert len(calls) == 1


def test_native_round_one_discovers_and_round_two_attaches_and_executes(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    import src.context_compactor as compactor
    reserves = []
    def trim(messages, budget, **kwargs):
        reserves.append(kwargs.get("reserve_tokens", 0))
        return messages
    monkeypatch.setattr(compactor, "trim_for_context", trim)
    executed = _install_dispatch_spy(monkeypatch)
    rounds = []

    async def stream(_candidates, messages, **kwargs):
        names = [_name(s) for s in (kwargs.get("tools") or [])]
        rounds.append(names)
        if len(rounds) == 1:
            assert "discover_tools" in names
            assert "get_workspace" not in names
            yield _native_call("discover_tools", {"query": "get_workspace", "max_results": 1}, "d1")
        elif len(rounds) == 2:
            assert "get_workspace" in names
            yield _native_call("get_workspace", {}, "w1")
        else:
            yield _delta("done")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream, raising=False)
    caller_tools = {"discover_tools"}
    _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-test",
        [{"role": "user", "content": "Use the appropriate workspace capability."}],
        relevant_tools=caller_tools, max_rounds=3, _is_teacher_run=True, owner=admin_owner,
    ))

    assert executed == ["get_workspace"]
    assert len(reserves) >= 2
    assert reserves[1] > reserves[0]  # newly attached schemas reserve context before trimming
    assert len(rounds) == 3  # no separate model/classifier request
    assert rounds[0] == sorted(rounds[0])
    assert {"ask_user", "discover_tools", "update_plan"} <= set(rounds[0])
    assert "get_workspace" not in rounds[0]
    assert rounds[1] == sorted(rounds[1])
    assert "get_workspace" in rounds[1]
    assert all(len(names) <= 5 for names in rounds)
    assert caller_tools == {"discover_tools"}  # loop state never mutates caller-owned selection


def test_fenced_round_two_prompt_contains_discovered_tool_signature(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    executed = _install_dispatch_spy(monkeypatch)
    prompts = []

    async def stream(_candidates, messages, **kwargs):
        assert kwargs.get("tools") is None
        prompts.append("\n".join(str(m.get("content") or "") for m in messages))
        if len(prompts) == 1:
            yield _delta('```discover_tools\n{"query":"get_workspace","max_results":1}\n```')
        elif len(prompts) == 2:
            prompt = prompts[-1]
            assert "get_workspace" in prompt
            assert "```get_workspace" in prompt or '"name": "get_workspace"' in prompt or '"name":"get_workspace"' in prompt
            yield _delta("```get_workspace\n{}\n```")
        else:
            yield _delta("done")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream, raising=False)
    _collect(agent_loop.stream_agent_loop(
        "http://local.test/v1", "local-model",
        [{"role": "user", "content": "Use the appropriate workspace capability."}],
        relevant_tools={"discover_tools"}, max_rounds=3, _is_teacher_run=True, owner=admin_owner,
    ))
    assert executed == ["get_workspace"]
    assert len(prompts) == 3
    # One canonical fenced definition; tool results may mention the name but
    # must not append a second signature contract.
    assert prompts[1].count("Return the absolute path of the active workspace folder") == 1


def test_fenced_dynamic_mcp_discovery_attaches_parses_and_dispatches(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    qualified = "mcp__demo__paint_canvas"
    schema = {
        "type": "function",
        "function": {
            "name": qualified,
            "description": "Paint a named canvas color.",
            "parameters": {
                "type": "object",
                "properties": {"color": {"type": "string"}},
                "required": ["color"],
            },
        },
    }

    class Manager:
        def get_all_tools(self, disabled_map=None):
            return [{"server_id": "demo", "name": "paint_canvas", "qualified_name": qualified,
                     "annotations": {"readOnlyHint": False}}]

        def get_all_openai_schemas(self, disabled_map=None):
            return [copy.deepcopy(schema)]

        def gated_tool_names(self, disabled_map=None):
            return {qualified}

        def discover_requested_tools(self, *args, **kwargs):
            return set()

        def demoted_servers(self, disabled_map=None):
            return []

        def get_tool_descriptions_for_prompt(self, disabled_map=None):
            raise AssertionError("selected fenced rendering must use canonical schema only")

    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: Manager())
    monkeypatch.setattr(agent_loop, "_load_mcp_disabled_map", lambda: {})
    executed = _install_dispatch_spy(monkeypatch)
    prompts = []

    async def stream(_candidates, messages, **kwargs):
        assert kwargs.get("tools") is None
        prompts.append("\n".join(str(message.get("content") or "") for message in messages))
        if len(prompts) == 1:
            yield _delta(f'```discover_tools\n{{"query":"{qualified}","max_results":1}}\n```')
        elif len(prompts) == 2:
            assert prompts[-1].count('"name":"mcp__demo__paint_canvas"') == 1
            yield _delta(f'```{qualified}\n{{"color":"blue"}}\n```')
        else:
            yield _delta("done")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    _collect(agent_loop.stream_agent_loop(
        "http://local.test/v1", "local-model",
        [{"role": "user", "content": "Use the appropriate drawing capability."}],
        relevant_tools={"discover_tools"}, max_rounds=3, owner=admin_owner,
        _is_teacher_run=True,
    ))
    assert executed == [qualified]


def test_execution_rechecks_profile_revoked_between_rounds(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    import core.database as database
    state = {"settings": {"tool_access": "selected", "enabled_tools": ["get_workspace"]}}
    monkeypatch.setattr(
        database, "get_session_settings",
        lambda session_id, strict=True: copy.deepcopy(state["settings"]),
    )
    rounds = 0

    async def stream(_candidates, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            yield _native_call("discover_tools", {"query": "get_workspace", "max_results": 1}, "d1")
        else:
            state["settings"] = {"tool_access": "none", "enabled_tools": []}
            yield _native_call("get_workspace", {}, "w1")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    chunks = _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-test",
        [{"role": "user", "content": "Use the workspace capability."}],
        session_id="revoke-test", owner=admin_owner,
        relevant_tools={"discover_tools"}, max_rounds=2, _is_teacher_run=True,
    ))
    events = [json.loads(chunk[6:]) for chunk in chunks
              if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]")]
    outputs = [event for event in events if event.get("type") == "tool_output"]
    assert any(event.get("tool") == "get_workspace" and "selected tool bindings" in event.get("output", "").lower()
               for event in outputs), outputs


def test_global_revocation_removes_next_round_schema_and_blocks_stale_call(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    import src.settings as settings
    revoked = set()
    monkeypatch.setattr(settings, "load_disabled_tools_strict", lambda: frozenset(revoked))
    rounds = 0

    async def dispatch(block, **kwargs):
        result = await dispatch_tool(block, **kwargs)
        if block.tool_type == "discover_tools":
            revoked.add("get_workspace")  # administrator revokes between rounds
        return result

    async def stream(_candidates, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        names = {_name(schema) for schema in (kwargs.get("tools") or [])}
        if rounds == 1:
            assert "get_workspace" in names
            yield _native_call("discover_tools", {"query": "get_workspace"}, "d1")
        else:
            assert "get_workspace" not in names
            yield _native_call("get_workspace", {}, "stale-call")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "execute_tool_block", dispatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    chunks = _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-test",
        [{"role": "user", "content": "Use the workspace capability."}],
        relevant_tools={"discover_tools", "get_workspace"}, max_rounds=2,
        owner=admin_owner, _is_teacher_run=True,
    ))
    assert any("disabled" in chunk.lower() and "get_workspace" in chunk for chunk in chunks)


def test_disabled_discovery_match_is_neither_attached_nor_executed(monkeypatch, admin_owner):
    _patch_basics(monkeypatch)
    executed = _install_dispatch_spy(monkeypatch)
    rounds = []

    async def stream(_candidates, messages, **kwargs):
        names = [_name(s) for s in (kwargs.get("tools") or [])]
        rounds.append(names)
        if len(rounds) == 1:
            yield _native_call("discover_tools", {"query": "send_email", "max_results": 8}, "d1")
        else:
            yield _delta("No permitted match.")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream, raising=False)
    _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-test",
        [{"role": "user", "content": "Find an available communication capability."}],
        relevant_tools={"discover_tools"}, disabled_tools={"send_email"},
        max_rounds=2, _is_teacher_run=True, owner=admin_owner,
    ))
    assert all("send_email" not in names for names in rounds)
    assert "send_email" not in executed


@pytest.mark.parametrize(
    "settings, expect_discovery",
    [
        ({"tool_access": "selected", "enabled_tools": ["get_workspace"]}, True),
        ({"tool_access": "none", "enabled_tools": []}, False),
    ],
)
def test_runtime_profile_ceiling_controls_discovery(
    monkeypatch, admin_owner, settings, expect_discovery,
):
    _patch_basics(monkeypatch)
    _install_dispatch_spy(monkeypatch)
    import core.database as database
    monkeypatch.setattr(database, "get_session_settings", lambda session_id, strict=True: settings)
    rounds = []

    async def stream(_candidates, messages, **kwargs):
        names = [_name(schema) for schema in (kwargs.get("tools") or [])]
        rounds.append(names)
        yield _delta("done")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream, raising=False)
    _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-test",
        [{"role": "user", "content": "use the workspace capability"}],
        session_id="profile-test", owner=admin_owner,
        relevant_tools={"discover_tools"}, max_rounds=2, _is_teacher_run=True,
    ))
    if expect_discovery:
        # Positive profile bindings are authoritative and eager; discovery is
        # available but unnecessary for an explicitly bound capability.
        assert rounds == [["discover_tools", "get_workspace"]]
    else:
        assert rounds == [[]]


def test_concurrent_turns_do_not_share_loaded_tools():
    catalog = [_schema("get_workspace"), _schema("read_file")]
    alpha = TurnToolDiscovery(catalog)
    beta = TurnToolDiscovery(catalog)

    async def run_both():
        await asyncio.gather(
            alpha.discover("get_workspace", max_results=1),
            beta.discover("read_file", max_results=1),
        )

    asyncio.run(run_both())
    assert alpha.loaded_names == {"get_workspace"}
    assert beta.loaded_names == {"read_file"}


def test_concurrent_live_loops_isolate_round_two_and_preserve_shared_caller_set(
    monkeypatch, admin_owner,
):
    _patch_basics(monkeypatch)
    _install_dispatch_spy(monkeypatch)
    shared_selection = {"discover_tools"}
    calls = {}
    round_two = {}
    payload_costs = []

    async def stream(_candidates, messages, **kwargs):
        marker = next(
            message["content"] for message in messages
            if message.get("role") == "user"
            and message.get("content") in {"alpha capability", "beta capability"}
        )
        calls[marker] = calls.get(marker, 0) + 1
        schemas = kwargs.get("tools") or []
        names = {_name(schema) for schema in schemas}
        payload_costs.append((len(schemas), agent_loop._estimate_tool_schema_tokens(schemas)))
        target = "get_workspace" if marker == "alpha capability" else "list_models"
        other = "list_models" if target == "get_workspace" else "get_workspace"
        if calls[marker] == 1:
            assert target not in names
            yield _native_call("discover_tools", {"query": target, "max_results": 1}, marker)
        else:
            round_two[marker] = names
            assert target in names
            assert other not in names
            yield _delta("done")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)

    async def run_both():
        async def drain(marker):
            return [chunk async for chunk in agent_loop.stream_agent_loop(
                "https://api.openai.com/v1", "gpt-test",
                [{"role": "user", "content": marker}],
                relevant_tools=shared_selection, max_rounds=2,
                owner=admin_owner, _is_teacher_run=True,
            )]

        await asyncio.gather(drain("alpha capability"), drain("beta capability"))

    asyncio.run(run_both())
    assert set(round_two) == {"alpha capability", "beta capability"}
    assert shared_selection == {"discover_tools"}
    assert payload_costs
    assert all(count <= 32 and tokens <= 5_000 for count, tokens in payload_costs)


def test_routing_eval_fixture_is_permission_aware_and_substantial():
    path = Path(__file__).parent / "fixtures" / "harness_routing_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    assert 20 <= len(cases) <= 40
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert set(case["expected_tools"]) <= set(case["permitted_tools"]), case["id"]
        assert set(case["expected_tools"]).isdisjoint(case["forbidden_tools"]), case["id"]
        assert case["provenance"]
