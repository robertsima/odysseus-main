"""The `capture` tool: owner from the context, same results as the route."""
import json


from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools.capture_tools import CaptureTool


async def _call(ctx, **args):
    return await CaptureTool().execute(json.dumps({"tz_offset": 0, **args}), ctx)


async def test_a_reminder_request_lands_as_a_note_owned_by_the_context_user(env):
    res = await _call({"owner": "alice", "session_id": "chat-1"}, text="remind me to call mom at 5pm", owner="bob")

    assert res["exit_code"] == 0 and res["destination"] == "note"
    [note] = env.notes.notes.values()
    assert note.owner == "alice"


async def test_the_tool_reports_a_clarification_without_writing(env):
    res = await _call({"owner": "admin"}, text="dentist")

    assert res["exit_code"] == 0 and res["status"] == "needs_clarification"
    assert env.td.calls == [] and env.notes.notes == {}


async def test_no_user_in_the_context_is_an_error(env):
    res = await _call({}, text="buy milk")

    assert res["exit_code"] == 1 and env.td.calls == []


async def test_a_waiting_capture_is_recorded_under_the_agent_actor(env):
    res = await _call({"owner": "alice", "session_id": "chat-9"}, text="waiting on Sam for the contract")

    history = env.outcomes.history("alice", res["outcome_id"])
    assert history[0]["actor"] == "agent:chat-9"


def test_capture_is_registered_for_every_user_and_outside_the_core_toolset():
    from src.agent_loop import _CORE_TOOLSET, _DOMAIN_TOOL_MAP
    from src.tool_security import blocked_tools_for_owner

    assert "capture" in TOOL_HANDLERS and "capture" in TOOL_TAGS
    assert "capture" in _DOMAIN_TOOL_MAP["notes_calendar_tasks"]
    assert "capture" not in _CORE_TOOLSET
    assert "capture" not in blocked_tools_for_owner("alice")
