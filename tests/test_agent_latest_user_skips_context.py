"""The agent loop's notion of "what the user said" must skip appended context.

2026-09-12 logs: a two-word follow-up ("It's done - continue") was classified
from the RAG block appended after it (`latest='UNTRUSTED SOURCE DATA...'`).
That block listed skill slugs, so every registered skill was treated as
explicitly invoked and all of their dependencies were loaded.
"""
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
                        lambda: {"manage_calendar", "bash", "read_file", "edit_file", "grep"})
    tools, unknown = _skill_declared_tools(
        [{"requires_toolsets": ["calendar", "git", "File editing", "interpretive dance", "grep"]}], set()
    )
    assert {"manage_calendar", "bash", "read_file", "edit_file", "grep"} <= tools
    assert unknown == {"interpretive dance"}
