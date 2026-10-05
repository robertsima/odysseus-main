"""Background work started after a reply is held by a strong reference until it ends.

asyncio keeps only a weak reference to a bare create_task() result, so the
garbage collector can drop the task before its body runs and the work (here the
auto-naming of a new chat) silently never happens. The chat helpers register
each task in _BG_TASKS and release it when it finishes.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from routes import chat_helpers


@pytest.mark.asyncio
async def test_the_auto_name_task_is_held_until_it_finishes(monkeypatch):
    gate = asyncio.Event()
    named = []

    async def slow_auto_name(session_manager, sess):
        await gate.wait()
        named.append(sess)

    monkeypatch.setattr(chat_helpers, "auto_name_session", slow_auto_name)
    sess = MagicMock(name="session")
    sess.name = "Chat"
    sess.history = []

    chat_helpers.run_post_response_tasks(
        sess, MagicMock(), "chat-1", "hello", "hi there", None,
        {"auto_memory": False, "auto_skills": False}, MagicMock(), MagicMock(), None,
    )

    assert len(chat_helpers._BG_TASKS) == 1
    gate.set()
    await asyncio.gather(*chat_helpers._BG_TASKS)
    await asyncio.sleep(0)
    assert named == [sess]
    assert not chat_helpers._BG_TASKS
