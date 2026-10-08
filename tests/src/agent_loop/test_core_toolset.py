"""A workspace agent turn offers one fixed core toolset, every round and every turn.

Per-turn selection read the message's wording: "check my calendar then fix the
failing test" in a workspace chat merged the calendar domain into the Terminus
set, the next turn's wording picked another set, and on the ChatGPT route only
part of the declared list was callable per round. Each change re-billed the
prompt and each miss cost a `discover_tools` round. With `agent_core_toolset`
on, a chat with a workspace gets the core coding tools its policy allows, the
same list on every round, and reaches anything else through `discover_tools`.
Chats without a workspace keep the per-turn selection.
"""
import json

import pytest

import src.agent_loop as al
from src.agent_loop import _STICKY_TOOLS
from tests.src.agent_loop.test_same_turn_tool_attachment import _collect, _patch

# The core coding set a workspace turn is offered (agent_loop._CORE_TOOLSET).
_CORE_TOOLSET = frozenset({
    "read_file", "write_file", "edit_file", "apply_patch", "bash", "python",
    "grep", "glob", "ls", "get_workspace", "preview_file", "update_plan",
    "web_search", "web_fetch", "recall_tool_output", "ask_user",
    "manage_skills", "discover_tools", "manage_bg_jobs", "recall_chat_history",
    "manage_memory", "search_documents",
})
CHATGPT_URL = "https://chatgpt.com/backend-api/codex/responses"
CHATGPT_MODEL = "gpt-6-luna"


class _FakeIndex:
    """Retrieval that matches nothing beyond the ambient tools."""

    def get_tools_for_query(self, query, k):
        from src.tool_index import ALWAYS_AVAILABLE
        return set(ALWAYS_AVAILABLE)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    from src import constants, shell_access, stable_tools

    _STICKY_TOOLS.clear()
    stable_tools.reset_for_tests()
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: _FakeIndex())
    # A full host shell, so bash/python are offered unless policy says no.
    monkeypatch.setattr(shell_access, "resolve_for_session", lambda _sid: "host")
    yield
    _STICKY_TOOLS.clear()
    stable_tools.reset_for_tests()


def _names(schemas):
    return [(s.get("function") or s).get("name") for s in schemas or []]


def _turn(monkeypatch, text, *, workspace, url="https://api.openai.com/v1", model="gpt-4o",
          session_id=None, disabled_tools=None, settings=None, active_document=None):
    """Run one turn: round 1 reads a file, round 2 runs a command, round 3 answers.

    Returns one (offered names, allowed_tools) pair per model request.
    """
    requests = []
    _patch(monkeypatch, [])
    overrides = dict(settings or {})
    monkeypatch.setattr(al, "get_setting",
                        lambda key, default=None: overrides.get(key, default), raising=False)
    rounds = [
        [{"name": "read_file", "arguments": json.dumps({"path": "tests/test_x.py"})}],
        [{"name": "grep", "arguments": json.dumps({"pattern": "def test_", "path": "tests"})}],
        None,
    ]

    async def _fake_stream(_candidates, _messages, **kwargs):
        idx = len(requests)
        requests.append((_names(kwargs.get("tools")), kwargs.get("allowed_tools")))
        calls = rounds[min(idx, len(rounds) - 1)]
        if calls:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Fixed."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        url, model, [{"role": "user", "content": text}],
        max_rounds=4, session_id=session_id, workspace=workspace,
        disabled_tools=disabled_tools, allow_private=False, active_document=active_document,
    ))
    assert len(requests) == 3, requests
    return requests


def test_workspace_turn_offers_the_core_set_whatever_the_wording(monkeypatch, tmp_path):
    requests = _turn(monkeypatch, "check my calendar then fix the failing test", workspace=str(tmp_path))

    first = set(requests[0][0])
    assert _CORE_TOOLSET <= first, sorted(_CORE_TOOLSET - first)
    # The calendar wording does not pull its domain in; discover_tools reaches it.
    assert "manage_calendar" not in first
    # Every round offers exactly the same list, in the same order.
    assert all(names == requests[0][0] for names, _allowed in requests)


def test_the_set_is_the_same_on_the_next_turn_and_all_of_it_is_callable(monkeypatch, tmp_path):
    ws = str(tmp_path)
    first = _turn(monkeypatch, "fix the failing test", workspace=ws,
                  url=CHATGPT_URL, model=CHATGPT_MODEL, session_id="chat-core")
    second = _turn(monkeypatch, "now draft an email to Sam about the release", workspace=ws,
                   url=CHATGPT_URL, model=CHATGPT_MODEL, session_id="chat-core")

    declared = first[0][0]
    assert _CORE_TOOLSET <= set(declared)
    for names, allowed in first + second:
        assert names == declared
        # Declared and callable are the same list: no per-round narrowing.
        assert allowed is not None and sorted(allowed) == sorted(declared)


@pytest.mark.security
def test_a_core_tool_the_policy_denies_is_not_offered(monkeypatch, tmp_path):
    requests = _turn(monkeypatch, "fix the failing test", workspace=str(tmp_path),
                     disabled_tools={"bash", "web_fetch"})

    for names, _allowed in requests:
        # The core set cut by the policy, nothing re-added from the wording.
        assert set(names) == _CORE_TOOLSET - {"bash", "web_fetch"}


@pytest.mark.security
def test_shell_off_keeps_bash_and_python_out_of_the_core_set(monkeypatch, tmp_path):
    from src import shell_access

    monkeypatch.setattr(shell_access, "resolve_for_session", lambda _sid: "off")
    requests = _turn(monkeypatch, "fix the failing test", workspace=str(tmp_path))

    for names, _allowed in requests:
        assert set(names) == _CORE_TOOLSET - {"bash", "python"}


def test_a_chat_without_a_workspace_keeps_per_turn_selection(monkeypatch):
    requests = _turn(monkeypatch, "check my calendar for tomorrow", workspace=None)

    names = set(requests[0][0])
    assert "manage_calendar" in names
    assert not {"write_file", "apply_patch", "bash"} & names


def test_setting_off_restores_per_turn_selection(monkeypatch, tmp_path):
    requests = _turn(monkeypatch, "check my calendar then fix the failing test",
                     workspace=str(tmp_path), settings={"agent_core_toolset": False})

    # The old Terminus merge: file tools plus the calendar domain the wording named.
    names = set(requests[0][0])
    assert "manage_calendar" in names and "read_file" in names


def test_tools_the_loadout_enables_join_the_core_set(monkeypatch, tmp_path):
    import core.database

    monkeypatch.setattr(core.database, "get_session_settings",
                        lambda _sid, **_k: {"enabled_tools": ["manage_calendar"]})
    requests = _turn(monkeypatch, "fix the failing test", workspace=str(tmp_path), session_id="chat-loadout")

    for names, _allowed in requests:
        assert set(names) == _CORE_TOOLSET | {"manage_calendar"}


def test_an_open_document_brings_the_document_tools(monkeypatch, tmp_path):
    # Per-turn selection added them for an open document; core mode keeps that.
    requests = _turn(monkeypatch, "tighten the intro", workspace=str(tmp_path),
                     active_document={"id": "doc-1", "title": "Release notes", "content": "Intro"})

    assert {"edit_document", "update_document"} <= set(requests[0][0])
