"""Text from different agent rounds is not glued together.

Saved replies on 2026-09-28 read "…the local `main` ref.The note edit is
saved…" and "…fully repaired.The **AI Mind document…": the route concatenated
every round's text deltas with nothing between them. The live stream put each
round in its own bubble, so only the reloaded transcript showed it.
"""

import json

import pytest

import routes.chat_routes as chat_routes
from tests.src.foreground_model_routing.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


def _delta(text, **extra):
    return f"data: {json.dumps({'delta': text, **extra})}\n\n"


def _event(**data):
    return f"data: {json.dumps(data)}\n\n"


def _emitted_text(emitted):
    text = ""
    for chunk in emitted:
        if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(chunk[6:])
        except ValueError:
            continue
        if "delta" in data and not data.get("thinking"):
            text += data["delta"]
    return text


async def _run(monkeypatch, chunks):
    captured = {}
    endpoint = _chat_stream_endpoint(
        monkeypatch, "agent", captured, agent_chunks=chunks, capture_completion=True,
    )
    response = await endpoint(_RouteRequest("agent"))
    emitted = [chunk async for chunk in response.body_iterator]
    saved_args, _kw = captured["saved"][0]
    return saved_args[3], _emitted_text(emitted)


@pytest.mark.asyncio
async def test_rounds_are_separated_in_the_saved_reply_and_the_live_stream(monkeypatch):
    chunks = [
        _delta("I will verify against origin rather than relying on the local `main` ref."),
        _event(type="tool_start", tool="edit_file", round=1),
        _event(type="tool_output", tool="edit_file", output="ok", round=1),
        _event(type="agent_step", round=2),
        _delta("thinking about it", thinking=True),
        _delta("The note edit "),
        _delta("is saved."),
        _event(type="agent_step", round=3),
        _delta("The **AI Mind** document is next."),
        "data: [DONE]\n\n",
    ]
    saved, live = await _run(monkeypatch, chunks)

    expected = (
        "I will verify against origin rather than relying on the local `main` ref."
        "\n\nThe note edit is saved."
        "\n\nThe **AI Mind** document is next."
    )
    assert saved == expected
    # The client accumulates exactly what the server saved.
    assert live == expected
    assert "thinking about it" not in saved


@pytest.mark.asyncio
async def test_existing_breaks_and_whitespace_deltas_are_respected(monkeypatch):
    chunks = [
        _delta("First round.\n"),
        _event(type="agent_step", round=2),
        # Whitespace alone does not use up the pending break.
        _delta(" "),
        _delta("\nSecond round."),
        _event(type="agent_step", round=3),
        _delta("\n\nThird round."),
        "data: [DONE]\n\n",
    ]
    saved, live = await _run(monkeypatch, chunks)
    # The whitespace delta passes through as-is; "\nSecond round." gets the
    # one newline it is short of; "\n\nThird round." needs nothing.
    assert saved == "First round.\n \n\nSecond round.\n\nThird round."
    assert live == saved


@pytest.mark.asyncio
async def test_single_round_text_is_untouched(monkeypatch):
    chunks = [_delta("Hello "), _delta("world."), "data: [DONE]\n\n"]
    saved, live = await _run(monkeypatch, chunks)
    assert saved == "Hello world."
    assert live == saved


@pytest.mark.asyncio
async def test_first_text_after_tools_gets_no_leading_break(monkeypatch):
    chunks = [
        _event(type="tool_start", tool="ls", round=1),
        _event(type="tool_output", tool="ls", output="a b", round=1),
        _event(type="agent_step", round=2),
        _delta("Found two files."),
        "data: [DONE]\n\n",
    ]
    saved, live = await _run(monkeypatch, chunks)
    assert saved == "Found two files."
    assert live == saved


@pytest.mark.parametrize(
    ("previous", "incoming", "expected"),
    [
        ("", "Text", ""),
        ("   ", "Text", ""),
        ("ref.", "The note", "\n\n"),
        ("ref.\n", "The note", "\n"),
        ("ref.\n\n", "The note", ""),
        ("ref.", "\nThe note", "\n"),
        ("ref.", "\n\nThe note", ""),
        ("ref.\n", "\nThe note", ""),
    ],
)
def test_round_text_separator(previous, incoming, expected):
    assert chat_routes._round_text_separator(previous, incoming) == expected
