"""A mid-run user update must get its tools without a missing-tool round."""

import pytest

pytest.skip(
    "Re-port backlog: exercises the fork's agent loop, replaced by upstream's in the 2026-09-18 sync (website/upstream-sync-2026-09-18.md)",
    allow_module_level=True,
)

from src.agent_loop import _steering_tool_additions


def test_calendar_steer_adds_calendar_without_replacing_notification_tools():
    selected = {"mcp__ntfy__ntfy_me", "search_documents"}
    pool = selected | {"manage_calendar", "manage_notes", "read_email", "send_email"}
    request = "also what is my next calendar event?"
    added = _steering_tool_additions(request, [{"role": "user", "content": request}], selected, pool, set())
    assert "manage_calendar" in added
    assert not added & {"read_email", "send_email"}
    assert selected == {"mcp__ntfy__ntfy_me", "search_documents"}


def test_steering_cannot_restore_disabled_tools_or_tools_outside_pool():
    request = "check the calendar and delegate to an agent"
    added = _steering_tool_additions(request, [{"role": "user", "content": request}], set(),
                                    {"manage_calendar", "delegate_to_agent"}, {"manage_calendar", "delegate_to_agent"})
    assert added == set()


def test_running_loop_delivers_steer_with_new_schema_and_preserves_task(monkeypatch, tmp_path):
    import asyncio
    import copy
    import json

    import core.database as database
    import src.agent_loop as al
    from src import agent_activity, agent_control, constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kwargs: {})
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)
    import src.model_context as model_context
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda *args, **kwargs: 400_000)
    monkeypatch.setattr(al, "_build_system_prompt", lambda messages, *args, **kwargs: (list(messages), []))
    monkeypatch.setattr(agent_control, "mark_injected", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_control, "pending_steer", lambda sid, **kwargs: [])
    monkeypatch.setattr(agent_control, "drain_steer_records", lambda sid, round_num, **kwargs: [
        {"id": "steer-1", "kind": "user", "text": "also check my next calendar event"}
    ] if round_num == 2 else [])
    requests = []

    async def fake_stream(candidates, messages, **kwargs):
        requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs.get("tools") or [])))
        if len(requests) == 1:
            yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [
                {"id": "call-1", "name": "search_documents", "arguments": {"query": "notification docs"}},
            ]}) + '\n\n'
        else:
            yield 'data: ' + json.dumps({"delta": "Here is the result."}) + '\n\n'
        yield 'data: [DONE]\n\n'

    async def fake_execute(*args, **kwargs):
        return "search_documents", {"output": "Documentation found", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(al, "execute_tool_block", fake_execute)

    async def run():
        return [chunk async for chunk in al.stream_agent_loop(
            "http://unused/v1", "gpt-5-test",
            [{"role": "user", "content": "Read notification documentation"}],
            relevant_tools={"search_documents"}, max_rounds=3, session_id="steer-test",
        )]

    try:
        chunks = asyncio.run(run())
        assert len(requests) == 2
        first_messages, first_tools = requests[0]
        next_messages, next_tools = requests[1]
        assert "manage_calendar" not in {s["function"]["name"] for s in first_tools}
        assert {"manage_calendar", "search_documents"} <= {s["function"]["name"] for s in next_tools}
        assert next_messages[:len(first_messages)] == first_messages
        assert any("Keep earlier unfinished" in str(m.get("content")) for m in next_messages)
        assert any('"steer_id": "steer-1"' in chunk for chunk in chunks)
    finally:
        agent_activity._reset_for_tests()
