"""The chat route does not add its own skills list to an agent turn.

The agent loop injects the skills index for every agent turn (and leaves it out
on low-signal ones). The chat route's preface added a second copy, so each
agent request carried the list twice (2026-10-06 diagnostics), and that copy
always armed the untrusted-context tool gate, even when every skill shown was
an unmodified shipped one that the loop's own copy does not arm it for.
"""
from types import SimpleNamespace

from src.chat_processor import ChatProcessor


class _Skills:
    def index_for(self, **_kwargs):
        return [{"name": "diagnosing-bugs", "description": "Diagnosis loop", "category": "dev"}]


def test_an_agent_turn_preface_carries_no_skills_list():
    processor = ChatProcessor(memory_manager=None, personal_docs_manager=None,
                              skills_manager=_Skills())

    preface, *_ = processor.build_context_preface(
        "fix the failing test", SimpleNamespace(id=None), use_memory=False, use_rag=False,
        agent_mode=True, use_skills=True, allow_private=False,
    )

    assert not any("diagnosing-bugs" in str(m.get("content")) for m in preface)
