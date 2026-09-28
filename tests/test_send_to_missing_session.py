"""A tool that hits a missing chat fails that tool call, not the whole run.

Production (2026-09-27 22:52:49): the admin chat's 54th round called
send_to_session with the id of a chat that no longer existed.
``SessionManager.get_session`` raises ``KeyError`` for an id with no DB row;
send_to_session expected None, so the KeyError escaped execute_tool_block and
stream_agent_loop, and agent_runs logged
``[agent-run] d6c51a17-... failed: 'Session 21709724-... not found'`` --
53 rounds of work ended as a failed run instead of a tool error the model
could read and recover from.
"""
import asyncio
import json
from collections import namedtuple

import pytest

import src.agent_tools.session_tools as session_tools
import src.tool_execution as tool_execution

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])

MISSING = "21709724-3fd5-4a12-ac4b-8e52002ff012"


class _RaisingManager:
    """Behaves like core.session_manager.SessionManager for an unknown id."""

    def get_session(self, session_id):
        raise KeyError(f"Session {session_id} not found")

    def save_sessions(self):
        pass


@pytest.fixture
def missing_manager(monkeypatch):
    monkeypatch.setattr(session_tools, "get_session_manager", lambda: _RaisingManager())


@pytest.mark.parametrize("mode", ["chat", "agent"])
def test_send_to_a_deleted_chat_is_a_tool_error(missing_manager, mode):
    content = json.dumps({"session_id": MISSING, "message": "status?", "mode": mode})
    result = asyncio.run(session_tools.send_to_session(content, "parent-chat", owner="admin"))
    assert result["exit_code"] == 1
    assert MISSING in result["error"]
    assert "not found" in result["error"]


def test_find_session_answers_none_for_a_missing_chat():
    assert session_tools._find_session(_RaisingManager(), MISSING) is None
    assert session_tools._find_session(_RaisingManager(), None) is None


def test_an_unexpected_tool_exception_becomes_the_tools_error_result(monkeypatch):
    async def boom(block, **kwargs):
        raise KeyError(f"Session {MISSING} not found")

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", boom)
    desc, result = asyncio.run(tool_execution.execute_tool_block(
        ToolBlock("send_to_session", "{}"),
        session_id="parent-chat",
        security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
    ))
    assert desc == "send_to_session: ERROR"
    assert result["exit_code"] == 1
    assert result["error"] == f"send_to_session failed: KeyError: Session {MISSING} not found"


def test_cancellation_still_propagates(monkeypatch):
    async def cancelled(block, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tool_execution.execute_tool_block(
            ToolBlock("bash", "true"),
            security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
        ))


class _KnownManager(_RaisingManager):
    def __init__(self, *ids):
        self._ids = set(ids)

    def get_session(self, session_id):
        if session_id in self._ids:
            return object()
        return super().get_session(session_id)


def test_a_session_link_prefix_resolves_to_the_chat_id():
    """2026-09-27: send_to_session('session-63b7708f-...') failed three times.
    list_sessions renders chats as `#session-<id>` links and worker run ids
    share the `session-` prefix, so models pass the link target as the id."""
    chat = "63b7708f-c396-4b9d-b92e-3e4d2b7479c0"
    manager = _KnownManager(chat)
    assert session_tools._resolve_target_sid(manager, f"session-{chat}") == chat
    assert session_tools._resolve_target_sid(manager, f"#session-{chat}") == chat
    assert session_tools._resolve_target_sid(manager, chat) == chat
    assert session_tools._resolve_target_sid(manager, "new") == "new"
    # An unknown id comes back unchanged so the caller's not-found path runs.
    assert session_tools._resolve_target_sid(manager, f"session-{MISSING}") == f"session-{MISSING}"


def test_a_worker_run_id_resolves_to_the_worker_chat(monkeypatch):
    worker = "50da18d9-0e82-4f3a-8362-fca250152c6e"
    runs = {"session-0123456789": {"run_id": "session-0123456789", "summary": {"target_session": worker}}}
    monkeypatch.setattr(session_tools.activity, "get_run", lambda rid: runs.get(rid))
    manager = _KnownManager(worker)
    assert session_tools._resolve_target_sid(manager, "session-0123456789") == worker
    # A run whose worker chat is gone still fails as not found.
    assert session_tools._resolve_target_sid(_KnownManager(), "session-0123456789") == "session-0123456789"


def test_the_not_found_error_says_where_valid_ids_come_from(missing_manager):
    content = json.dumps({"session_id": f"session-{MISSING}", "message": "status?", "mode": "chat"})
    result = asyncio.run(session_tools.send_to_session(content, "parent-chat", owner="admin"))
    assert result["exit_code"] == 1
    assert "list_sessions" in result["error"]
    assert "worker_session" in result["error"]
    assert "not its run_id" in result["error"]
