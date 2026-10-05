"""Messages leave a chat's context without being deleted (src/chat_archive.py).

Compaction used to replace older messages with a summary and delete them, a
long turn trimmed older messages out of the request without a word, and
earlier turns' tool output was never replayed. Now compaction and the agent's
own truncate move the messages to ``chat_message_archive``, the summary and
the trim say so, and ``recall_chat_history`` reads the archived and live
messages, tool output included.
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import QueuePool

from core.models import ChatMessage


@pytest.fixture
def managed(monkeypatch, make_test_db):
    """A SessionManager on its own SQLite file (patched, never reloaded)."""
    import core.database as database
    import core.models as models
    import core.session_manager as sm_mod

    factory = make_test_db(poolclass=QueuePool).SessionLocal
    monkeypatch.setattr(sm_mod, "SessionLocal", factory)
    monkeypatch.setattr(database, "SessionLocal", factory)
    sm = sm_mod.SessionManager()
    monkeypatch.setattr(models, "get_session_manager_instance", lambda: sm)
    return sm, SimpleNamespace(SessionLocal=factory, db=database)


def _chat(sm, sid="chat", n=12):
    sm.create_session(session_id=sid, name="t", endpoint_url="x", model="m", rag=False, owner="u")
    sm.add_message(sid, ChatMessage("system", "persona: terse"))
    for i in range(n):
        meta = None
        if i == 3:
            meta = {"tool_events": [{"round": 1, "tool": "bash", "command": "npm test",
                                     "output": "Tests: 41 passed, 2 failed (checkout.spec.ts)", "exit_code": 1}]}
        sm.add_message(sid, ChatMessage("user" if i % 2 == 0 else "assistant", f"msg{i}", metadata=meta))
    return sm.get_session(sid)


def _rows(env, model, sid="chat"):
    db = env.SessionLocal()
    try:
        return [(r.role, r.content) for r in db.query(model).filter(model.session_id == sid).order_by(model.timestamp)]
    finally:
        db.close()


def test_compaction_archives_instead_of_deleting(managed):
    from src import context_compactor as cc

    sm, env = managed
    session = _chat(sm)
    cc._update_session_history(session, 6, "the user is building checkout", system_msg_count=3)

    live = _rows(env, env.db.ChatMessage)
    archived = _rows(env, env.db.ArchivedChatMessage)
    # The persona stays first, the summary replaces msg0..msg5, the rest stays.
    assert live[0] == ("system", "persona: terse")
    assert live[1][0] == "system" and "the user is building checkout" in live[1][1]
    assert "6 earlier messages of this chat were moved to its archive" in live[1][1]
    assert [c for _, c in live[2:]] == [f"msg{i}" for i in range(6, 12)]
    assert [c for _, c in archived] == [f"msg{i}" for i in range(6)]
    # The in-memory history the model is sent matches the live rows.
    assert [m.content for m in sm.get_session("chat").history][2:] == [f"msg{i}" for i in range(6, 12)]


def test_a_second_compaction_archives_the_first_summary(managed):
    from src import context_compactor as cc

    sm, env = managed
    session = _chat(sm)
    cc._update_session_history(session, 4, "first", compaction_count=1)
    cc._update_session_history(sm.get_session("chat"), 4, "second", compaction_count=2)

    live = _rows(env, env.db.ChatMessage)
    summaries = [c for r, c in live if r == "system" and "[Conversation summary]" in c]
    assert len(summaries) == 1 and "second" in summaries[0]
    archived = [c for _, c in _rows(env, env.db.ArchivedChatMessage)]
    assert any("first" in c for c in archived)
    assert "msg0" in archived and "msg7" in archived


def test_recall_reads_archived_and_live_messages_with_tool_output(managed):
    from src import context_compactor as cc
    from src.chat_archive import RecallChatHistoryTool

    sm, _env = managed
    session = _chat(sm)
    cc._update_session_history(session, 6, "summary")
    tool = RecallChatHistoryTool()
    ctx = {"session_id": "chat"}

    overview = asyncio.run(tool.execute("{}", ctx))["results"]
    assert "6 archived" in overview

    found = asyncio.run(tool.execute('{"query": "checkout.spec.ts failed"}', ctx))["results"]
    assert "msg3" in found and "archived" in found

    index = next(line for line in found.splitlines() if "msg3" in line).split()[1]
    full = asyncio.run(tool.execute({"message": index, "after": 1}, ctx))["results"]
    assert "[tool bash] npm test (exit 1)" in full
    assert "Tests: 41 passed, 2 failed" in full
    assert "msg4" in full

    page = asyncio.run(tool.execute({"start": 0, "count": 3}, ctx))["results"]
    assert "#0 system" in page and "(next: start=3" in page


def test_recall_without_a_chat_is_an_error():
    from src.chat_archive import RecallChatHistoryTool

    result = asyncio.run(RecallChatHistoryTool().execute("{}", {}))
    assert result["exit_code"] == 1


def test_agent_truncate_archives_and_deleting_the_chat_removes_the_archive(managed):
    sm, env = managed
    _chat(sm)
    moved = sm.keep_last_messages("chat", 4, archive_reason="agent_truncate")

    assert moved == 9
    assert len(_rows(env, env.db.ChatMessage)) == 4
    assert len(_rows(env, env.db.ArchivedChatMessage)) == 9

    assert sm.delete_session("chat")
    assert _rows(env, env.db.ArchivedChatMessage) == []


def test_plain_replace_and_user_truncate_still_delete(managed):
    """Only the context-management paths archive; a user's edit or /truncate deletes."""
    sm, env = managed
    _chat(sm)
    sm.keep_last_messages("chat", 4)
    assert _rows(env, env.db.ArchivedChatMessage) == []
