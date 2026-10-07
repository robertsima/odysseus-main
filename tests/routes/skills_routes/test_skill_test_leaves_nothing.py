"""A skill test reads skills and leaves nothing behind.

2026-10-06: every audit test lost a round when the gate refused `manage_skills
view`, and the manual tester could ask to approve a create_document whose
document then stayed in the library. A test now runs read-only: the skill read
runs, and documents, notes and memories are refused with no approval to grant.
"""
import asyncio
import json

import pytest

import routes.skills_routes as skills_routes

pytestmark = pytest.mark.security

SKILL_MD = """---
name: diagnosing-bugs
description: Reproduce a bug, then narrow it to one cause
when_to_use: A failing test or a crash needs a cause
requires_toolsets: [grep]
---
1. Reproduce it.
2. Bisect until one change explains it.
"""

ROUNDS = [
    '```create_document\n{"title": "Test sample", "content": "sample bug report"}\n```',
    '```manage_notes\n{"action": "add", "content": "sample note"}\n```',
    '```manage_memory\n{"action": "add", "text": "sample memory"}\n```',
    '```manage_skills\n{"action": "view", "name": "diagnosing-bugs"}\n```',
    "The sample bug narrows to one off-by-one change.",
]


def _run_manual_test(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    replies = iter(ROUNDS)

    async def fake_stream(*args, **kwargs):
        yield "data: " + json.dumps({"delta": next(replies, "Done.")}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    executed = []

    async def fake_execute(block, **kwargs):
        executed.append(block.tool_type)
        return f"{block.tool_type}: ok", {"results": "ok", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    async def fake_judge(*args, **kwargs):
        return {"verdict": "pass", "confidence": 1.0, "summary": "ok", "issues": []}

    monkeypatch.setattr(skills_routes, "_eval_skill_run", fake_judge)

    key = ("tester", "diagnosing-bugs")
    skills_routes._skill_test_jobs[key] = {"status": "running", "log": [], "verdict": None}
    task = skills_routes._skill_test_task({"when_to_use": "A failing test needs a cause"})
    asyncio.run(skills_routes._run_skill_test_job(
        key, "diagnosing-bugs", SKILL_MD, task, "http://local.test/v1", "small-local-model",
        None, "tester",
    ))
    return skills_routes._skill_test_jobs.pop(key), executed


def test_a_skill_test_reads_its_skill_and_creates_nothing(monkeypatch):
    job, executed = _run_manual_test(monkeypatch)

    assert job["status"] == "done", job.get("approval")
    assert "approval" not in job
    assert executed == ["manage_skills"]


def test_the_test_task_wording_selects_no_tool_groups_of_its_own():
    # "a short document", "a note", "in your reply" and "show" each pulled in a
    # tool group (documents, notes, email, ui), so one audit run got 14 email
    # tools. The skill's own context is what should pick tools.
    from src.agent_loop import _classify_agent_request

    task = skills_routes._skill_test_task({"when_to_use": ""})
    intent = _classify_agent_request([{"role": "user", "content": task}], task)
    assert not intent.get("domains")
