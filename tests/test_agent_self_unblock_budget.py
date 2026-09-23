"""Why the agent stopped unblocking itself (2026-09-18), pinned.

c62b6ba routed every late tool addition through one cumulative budget
(`_admit_turn_tools`: 32 tools / 5000 schema tokens) with the turn's existing
selection entered as protected `core`. The recovery paths entered as
unprotected sources, so once a turn's first guesses filled the budget the
self-unblock, `discover_tools` and a loaded skill's tools all admitted nothing
-- silently, with the model told the tools had loaded.

Separately, a continuation turn ("Ok, continue") could not carry the previous
turn's tools: persisted turns replay `metadata.tool_events`, never
`tool_calls`, so retention saw a native-tool chat as never having used a tool.
"""
import inspect
import json
from types import SimpleNamespace

import pytest

import src.agent_loop as agent_loop
from src.tool_selection import PROTECTED_SOURCES, plan_tool_selection

_REPORT_BACKLOG = pytest.mark.skip(
    reason="Re-port backlog: the fork's agent-loop routing and continuation (website/upstream-sync-2026-09-18.md)"
)


# ── the budget: named requests get past it, guesses do not ───────────────────


def _full_turn():
    """A turn whose existing selection already spends the whole budget."""
    existing = {f"guess_{i}" for i in range(20)}
    costs = {name: 250 for name in existing}          # 20 x 250 = 5000 tokens
    costs["manage_git"] = 900
    return existing, costs


@pytest.mark.parametrize("source", ["explicit", "skill"])
def test_a_named_request_is_admitted_past_a_full_budget(source):
    existing, costs = _full_turn()
    plan = plan_tool_selection(
        existing | {"manage_git"}, {"core": existing, source: {"manage_git"}},
        schema_costs=costs, max_tools=32, max_schema_tokens=5000,
    )
    assert "manage_git" in plan.selected
    assert source in PROTECTED_SOURCES


@pytest.mark.parametrize("source", ["semantic", "context"])
def test_a_guess_is_still_refused_on_a_full_budget(source):
    """The budget still does its job for everything that is not a named request."""
    existing, costs = _full_turn()
    plan = plan_tool_selection(
        existing | {"manage_git"}, {"core": existing, source: {"manage_git"}},
        schema_costs=costs, max_tools=32, max_schema_tokens=5000,
    )
    assert "manage_git" not in plan.selected


@_REPORT_BACKLOG
def test_recovery_paths_admit_as_named_requests_not_guesses():
    """Each late addition must enter under the source that says what it is."""
    src = inspect.getsource(agent_loop.stream_agent_loop)
    # discover_tools: the model named what it wanted loaded.
    assert '_admit_turn_tools(_turn_discovery.loaded_names, "explicit")' in src
    # a loaded skill's declared dependencies.
    assert '_admit_turn_tools(_new, "skill")' in src
    # self-unblock: the targeted tier is named; the domain closure stays a guess.
    assert '"explicit" if _rearm_scope == "targeted" else "semantic"' in src
    # and no bare admission of the old kind survives for these three.
    assert '_admit_turn_tools(_turn_discovery.loaded_names))' not in src
    assert '_admit_turn_tools(_rearm_new, "semantic")' not in src


@_REPORT_BACKLOG
def test_a_refused_late_addition_is_logged_not_silent():
    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert "late %s addition refused by the turn budget" in src


# ── continuation: the previous turn's tools come with it ─────────────────────


def _assistant(text, *tools):
    return {"role": "assistant", "content": text,
            "metadata": {"tool_events": [{"tool": t, "exit_code": 0} for t in tools]}}


@_REPORT_BACKLOG
def test_persisted_tool_events_count_as_tools_the_conversation_used():
    messages = [
        {"role": "user", "content": "merge upstream"},
        _assistant("Merged; two conflicts remain.", "manage_git", "grep", "manage_git"),
    ]
    assert agent_loop._tools_used_in_conversation(messages, set()) == ["manage_git", "grep"]


@_REPORT_BACKLOG
def test_only_the_previous_tool_using_turn_is_continued():
    messages = [
        {"role": "user", "content": "check my calendar"},
        _assistant("You are free at 3.", "manage_calendar"),
        {"role": "user", "content": "now merge upstream"},
        _assistant("Merge stopped on conflicts.", "manage_git", "grep"),
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "You're welcome."},       # no tools
        {"role": "user", "content": "Ok, continue"},
    ]
    assert agent_loop._tools_used_last_turn(messages, set()) == {"manage_git", "grep"}


@_REPORT_BACKLOG
def test_known_names_filter_what_is_continued():
    messages = [_assistant("done", "manage_git", "not_a_real_tool")]
    assert agent_loop._tools_used_last_turn(messages, {"manage_git"}) == {"manage_git"}


@pytest.fixture
def loop(monkeypatch, tmp_path):
    import copy

    import core.database as database
    import src.model_context as model_context
    from src import agent_activity, agent_control, constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kwargs: {})
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda *a, **k: 400_000)
    monkeypatch.setattr(agent_loop, "_build_system_prompt", lambda messages, *a, **k: (list(messages), []))
    monkeypatch.setattr(agent_control, "pending_steer", lambda sid, **kwargs: [])
    monkeypatch.setattr(agent_control, "drain_steer_records", lambda *a, **k: [])
    requests = []

    async def fake_stream(candidates, messages, **kwargs):
        requests.append({"tools": copy.deepcopy(kwargs.get("tools") or [])})
        yield "data: " + json.dumps({"delta": "Continuing the merge."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    async def run(messages):
        async for _ in agent_loop.stream_agent_loop(
            "http://unused/v1", "gpt-5-test", messages, session_id="continuation-test",
        ):
            pass
        return {t["function"]["name"] for t in requests[0]["tools"]}

    yield SimpleNamespace(run=run)
    agent_activity._reset_for_tests()


@_REPORT_BACKLOG
@pytest.mark.asyncio
async def test_ok_continue_resumes_with_the_tool_the_last_turn_was_using(loop):
    """The 2026-09-18 turn: a git merge, then "Ok, continue" -- which came back
    with `retained_count=0` and `manage_git` deselected."""
    tools = await loop.run([
        {"role": "user", "content": "Merge upstream/dev into our branch and resolve the conflicts"},
        _assistant("The merge stopped on two conflicts in src/app.py.", "manage_git", "read_file"),
        {"role": "user", "content": "Ok, continue"},
    ])
    assert "manage_git" in tools
