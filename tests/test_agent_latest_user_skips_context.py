"""The agent loop's notion of "what the user said" must skip appended context.

2026-09-12 logs: a two-word follow-up ("It's done - continue") was classified
from the RAG block appended after it (`latest='UNTRUSTED SOURCE DATA...'`).
That block listed skill slugs, so every registered skill was treated as
explicitly invoked and all of their dependencies were loaded.
"""

import pytest

pytest.skip(
    "Re-port backlog: exercises the fork's agent loop, replaced by upstream's in the 2026-09-18 sync (website/upstream-sync-2026-09-18.md)",
    allow_module_level=True,
)

from src.agent_loop import _explicitly_named_skills, _extract_last_user_message, _skill_declared_tools
from src.prompt_security import untrusted_context_message

REQUEST = "It's done - continue"


def test_appended_untrusted_context_is_not_the_request():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": REQUEST},
        untrusted_context_message("vault", "skills: verify-and-push-mvp-slices, daily-command-center"),
    ]
    latest = _extract_last_user_message(messages)
    assert latest == REQUEST
    skills = [{"name": "verify-and-push-mvp-slices"}, {"name": "daily-command-center"}]
    assert _explicitly_named_skills(latest, skills) == []


def test_context_and_multimodal_blocks_are_skipped():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": REQUEST}]},
        {"role": "user", "content": "[Context — current date/time]\n2026-09-12"},
    ]
    assert _extract_last_user_message(messages) == REQUEST


def test_prose_toolsets_resolve_to_real_tools(monkeypatch):
    import src.tool_policy as tool_policy

    monkeypatch.setattr(tool_policy, "known_tool_names",
                        lambda: {"manage_calendar", "bash", "read_file", "edit_file", "grep",
                                 "web_search", "web_fetch", "search_documents"})
    tools, unknown = _skill_declared_tools(
        [{"requires_toolsets": ["calendar", "git", "File editing", "web search or retrieval",
                                "search_documents when internal context is relevant",
                                "interpretive dance", "grep"]}], set()
    )
    assert {"manage_calendar", "bash", "read_file", "edit_file", "grep", "web_search", "web_fetch",
            "search_documents"} <= tools
    assert unknown == {"interpretive dance"}


def test_followup_retrieval_and_turn_count_ignore_synthetic_user_messages():
    from src.agent_loop import _harness_directive, _recent_context_for_retrieval, _user_turn_count

    messages = [
        {"role": "user", "content": "read the notification documentation"},
        {"role": "user", "content": "continue"},
        {"role": "user", "content": "[Context — current date/time]\ncalendar event"},
        untrusted_context_message("vault", "email inbox calendar"),
        _harness_directive("try tools again"),
        {"role": "user", "content": "[Message from agent session 'worker' -- a PEER AGENT, not your user. ] email"},
    ]
    assert _extract_last_user_message(messages) == "continue"
    assert _recent_context_for_retrieval(messages) == "continue\nread the notification documentation"
    assert _user_turn_count(messages) == 2


def test_human_steering_remains_user_intent():
    from src.agent_loop import _recent_context_for_retrieval, _user_turn_count

    text = "[Mid-task instruction from the user] also check my next meeting"
    messages = [{"role": "user", "content": text}]
    assert _extract_last_user_message(messages) == text
    assert _recent_context_for_retrieval(messages) == text
    assert _user_turn_count(messages) == 1


def test_background_memory_writes_respect_session_access_and_fail_closed(monkeypatch):
    import core.database as database
    from routes.chat_helpers import _session_allows_memory_writes

    for access, expected in (("write", True), ("read", False), ("none", False)):
        monkeypatch.setattr(database, "get_session_settings", lambda _sid, a=access: {"memory_access": a})
        assert _session_allows_memory_writes("session") is expected

    def unavailable(_sid):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "get_session_settings", unavailable)
    assert _session_allows_memory_writes("session") is False


def test_extraction_snapshot_is_bounded_filtered_and_stable():
    from types import SimpleNamespace
    from routes.chat_helpers import _snapshot_session_for_extraction

    history = []
    for i in range(50):
        history.extend((
            {"role": "user", "content": f"human {i}"},
            {"role": "assistant", "content": f"answer {i}"},
            untrusted_context_message("tool output", f"synthetic {i}"),
        ))
    sess = SimpleNamespace(
        history=history, message_count=150, owner="alice", name="Long chat",
        get_context_messages=lambda: list(history),
    )
    snapshot = _snapshot_session_for_extraction(sess, limit=12)
    frozen = snapshot.get_context_messages()

    assert len(frozen) == 12
    assert frozen[-2:] == [
        {"role": "user", "content": "human 49"},
        {"role": "assistant", "content": "answer 49"},
    ]
    assert all("synthetic" not in msg["content"] for msg in frozen)
    assert snapshot.message_count == 150
    assert snapshot.owner == "alice" and snapshot.name == "Long chat"
    history[-2]["content"] = "mutated after scheduling"
    assert snapshot.get_context_messages()[-2]["content"] == "human 49"


def test_post_response_does_not_snapshot_when_extractors_are_disabled(monkeypatch):
    from types import SimpleNamespace
    from routes import chat_helpers

    monkeypatch.setattr(
        chat_helpers, "_snapshot_session_for_extraction",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("snapshot should be lazy")),
    )
    monkeypatch.setattr(chat_helpers, "needs_auto_name", lambda _name: False)
    sess = SimpleNamespace(
        history=[{"role": "user", "content": "hi"}], message_count=1,
        endpoint_url="http://model", model="model", headers={}, name="Named", owner="alice",
    )
    chat_helpers.run_post_response_tasks(
        sess, SimpleNamespace(), "session", "hi", "hello", None,
        {"auto_memory": False, "auto_skills": False}, None, None, None,
        allow_background_extraction=False,
    )
