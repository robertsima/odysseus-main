"""After a streak of rounds that each make one read-only call, the loop asks the
model to batch.

2026-10-07 bundle: 307 of 343 rounds made exactly one call, mostly one file read
or one `sed -n` range, on 100-160k prompts. A round costs ~7 s of model latency
however small its call, so a worker reading a file at a time took 291 rounds.
"""
import asyncio
import json

import src.agent_loop as al


def _run_turn(monkeypatch, rounds_of_calls):
    """Each entry of ``rounds_of_calls`` is one round's list of (tool, args).
    Returns the messages the model saw in every round."""
    seen = []

    async def fake_model(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        index = len(seen) - 1
        if index < len(rounds_of_calls):
            calls = [{"id": f"call_{index}_{j}", "name": name, "arguments": json.dumps(args)}
                     for j, (name, args) in enumerate(rounds_of_calls[index])]
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_tool(block, *args, **kwargs):
        return block.tool_type, {"output": "line 1\nline 2", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "execute_tool_block", fake_tool)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m",
            [{"role": "user", "content": "find why the parser drops the last line"}],
            relevant_tools={"read_file", "grep", "bash"},
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())
    return seen


def _nudges(messages):
    return [m for m in messages if m["role"] == "user"
            and m["content"].startswith("[Harness directive") and "one read-only call" in m["content"]]


def test_four_single_read_rounds_get_a_batching_note(monkeypatch):
    seen = _run_turn(monkeypatch, [
        [("read_file", {"path": "src/parser.py"})],
        [("grep", {"pattern": "last_line", "path": "src"})],
        [("bash", {"command": "cd /repo && sed -n '40,80p' src/lexer.py"})],
        [("read_file", {"path": "tests/test_parser.py"})],
    ])

    assert not _nudges(seen[3]), "the note came before the streak was complete"
    assert len(_nudges(seen[4])) == 1


def test_rounds_that_already_batch_get_no_note(monkeypatch):
    seen = _run_turn(monkeypatch, [
        [("read_file", {"path": f"src/a{i}.py"}), ("read_file", {"path": f"src/b{i}.py"})]
        for i in range(5)
    ])

    assert not any(_nudges(round_messages) for round_messages in seen)


def test_a_shell_command_that_writes_breaks_the_streak(monkeypatch):
    seen = _run_turn(monkeypatch, [
        [("read_file", {"path": "src/parser.py"})],
        [("read_file", {"path": "src/lexer.py"})],
        [("bash", {"command": "cat src/parser.py > /tmp/copy.py"})],
        [("read_file", {"path": "src/tokens.py"})],
        [("read_file", {"path": "src/ast.py"})],
    ])

    assert not any(_nudges(round_messages) for round_messages in seen)
