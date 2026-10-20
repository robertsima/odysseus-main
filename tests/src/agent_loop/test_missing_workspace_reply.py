"""The canned "No active workspace is set" reply answers only the person's words.

2026-10-20: a user attached two contract templates and asked for the bracketed
fields to be filled in. The contracts said "this Project", the check read the
attached text as the request, and the turn ended with the canned reply and no
model call. Setting a workspace to get past it then broke the chat's tool list.
"""
import asyncio

import src.agent_loop as al


def _run(monkeypatch, content):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    calls = []

    async def _fake_stream(*args, **kwargs):
        calls.append(1)
        yield 'data: {"delta": "Filled in."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _go():
        gen = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test", [{"role": "user", "content": content}],
            max_rounds=1, relevant_tools=None, owner="admin", workspace=None,
        )
        return "".join([c async for c in gen])

    return asyncio.run(_go()), calls


def test_an_attached_document_saying_this_project_still_reaches_the_model(monkeypatch):
    content = (
        "Fill out the information in brackets in these two templates."
        "\n\n[Document content — SDS Master Agreement]:\n"
        "The Consultant shall deliver this Project for [CLIENT NAME] by [DATE]. "
        "All work in this repository of deliverables belongs to the Client."
    )
    text, calls = _run(monkeypatch, content)

    assert calls, "the model was never asked"
    assert "No active workspace is set" not in text


def test_this_project_in_the_persons_own_words_is_not_a_workspace_request(monkeypatch):
    text, calls = _run(monkeypatch, "Draft a scope of work for this project with Dono Fuel.")

    assert calls
    assert "No active workspace is set" not in text
