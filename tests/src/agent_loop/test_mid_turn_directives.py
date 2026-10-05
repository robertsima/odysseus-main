"""Notes the harness adds in the middle of a turn go in as user messages, never as system.

llm_core hoists every system-role message into the instructions prefix of the
request, so a system message appended mid-turn changes the prefix and the whole
conversation misses the prompt cache on every later round. The harness's own
notes ("finish the checklist") therefore travel as a tail user message. An
unfinished task checklist is the easiest way to make the loop write one.
"""
import asyncio
import json

import src.agent_loop as al


def _run_a_turn_that_ends_with_an_open_checklist(monkeypatch):
    """Round 1 writes a checklist, later rounds answer in prose without finishing it.

    Returns the messages the model saw in every round.
    """
    rounds = []

    async def fake_model(_candidates, messages, **kwargs):
        rounds.append([dict(m) for m in messages])
        if len(rounds) == 1:
            call = {"id": "call_plan", "name": "update_plan",
                    "arguments": json.dumps({"plan": "- [ ] read the failing test\n- [ ] fix it"})}
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "All done."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_tool(block, *args, **kwargs):
        plan = json.loads(block.content)["plan"]
        return block.tool_type, {"output": "plan saved", "plan_update": {"plan": plan}, "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "execute_tool_block", fake_tool)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m",
            [{"role": "user", "content": "fix the failing test"}],
            relevant_tools={"update_plan"},
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())
    return rounds


def test_a_checklist_nudge_is_a_user_message_and_the_system_prefix_never_changes(monkeypatch):
    rounds = _run_a_turn_that_ends_with_an_open_checklist(monkeypatch)

    assert len(rounds) >= 3, "the loop ended without nudging the unfinished checklist"
    system_prefix = [m["content"] for m in rounds[0] if m["role"] == "system"]
    for later in rounds[1:]:
        assert [m["content"] for m in later if m["role"] == "system"] == system_prefix
    notes = [m for m in rounds[-1] if m["role"] == "user" and m["content"].startswith("[Harness directive")]
    assert notes, "the nudge did not reach the model as a harness note"
