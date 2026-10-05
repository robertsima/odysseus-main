"""A turn that mentions a task gets the task tool's schema, not every admin tool's.

The loop sends the model the tool schemas for the round. Admin tools (tasks,
settings, documents, agent control ...) are only added for the ones the user's
words named. A call site that dropped that breakdown fell back to the whole
admin set, silently, on every round of the turn: seventeen extra schemas of
prompt on any message that said "task" or "doc".
"""
import asyncio
import json

import src.agent_loop as al


def _tools_sent_to_the_model(monkeypatch, message):
    sent = []

    async def fake_model(_candidates, messages, **kwargs):
        sent.append({t["function"]["name"] for t in (kwargs.get("tools") or []) if "function" in t})
        yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o",
            [{"role": "user", "content": message}],
            relevant_tools={"ask_user", "update_plan", "read_file"},
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())
    return sent


def test_only_the_admin_tools_the_request_named_are_sent(monkeypatch):
    message = "add a task to remind me tomorrow and check my settings"

    sent = _tools_sent_to_the_model(monkeypatch, message)

    named = al._detect_admin_tools([{"role": "user", "content": message}])
    assert named, "the request names admin tools, or this test proves nothing"
    assert sent, "the model was never called"
    for tools in sent:
        assert tools & al._ADMIN_TOOLS <= named, sorted((tools & al._ADMIN_TOOLS) - named)
