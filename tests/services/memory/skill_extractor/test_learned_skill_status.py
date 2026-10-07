"""A skill learned from a chat is saved as a draft unless its own setting says publish.

2026-10-06: with "Auto-approve skills" on (the default), the extractor published
"Skills Editor and Theme Improvements" and "Inspect and Integrate Branch
Changes" straight from chats. Each is one task's record, not a reusable
procedure, and once published it entered every unscoped chat's skills list and
keyword matching. Publishing learned skills now has its own setting, off by
default; "Auto-approve skills" keeps governing audits.
"""
import asyncio
from types import SimpleNamespace

import pytest

from services.memory import skill_extractor


class _Skills:
    def __init__(self):
        self.added = []

    def load(self, owner=None):
        return []

    def add_skill(self, **fields):
        self.added.append(fields)
        return {"id": "learned-1", **fields}


def _extract(monkeypatch, prefs):
    async def fake_llm(*_args, **_kwargs):
        return ('{"title": "Rotate a leaked token", "problem": "p", "solution": "s", '
                '"steps": ["revoke", "reissue"], "confidence": 0.95}')

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda owner: dict(prefs))
    monkeypatch.setattr("src.event_bus.fire_event", lambda *a, **k: None)
    skills = _Skills()
    session = SimpleNamespace(
        session_id="s-1",
        get_context_messages=lambda: [
            {"role": "user", "content": "the token leaked, fix it"},
            {"role": "assistant", "content": "Revoked it and issued a new one."},
        ],
    )
    events = [
        {"tool": "bash", "output": "revoked", "exit_code": 0},
        {"tool": "edit_file", "output": "updated config", "exit_code": 0},
    ]
    entry = asyncio.run(skill_extractor.maybe_extract_skill(
        session, skills, "http://unused", "model", {}, 3, 2, owner="u", tool_events=events,
    ))
    assert entry is not None
    return skills.added[0]["status"]


@pytest.mark.parametrize("prefs", [{}, {"auto_approve_skills": True}])
def test_a_learned_skill_is_a_draft_by_default(monkeypatch, prefs):
    assert _extract(monkeypatch, prefs) == "draft"


def test_the_learned_skill_setting_publishes_it(monkeypatch):
    assert _extract(monkeypatch, {"auto_publish_learned_skills": True}) == "published"
