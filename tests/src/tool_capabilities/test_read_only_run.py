"""A read-only run may read skills and may never write, even with an approval.

The skill tester runs with the skill under test as untrusted context. That armed
the gate, and the gate then refused `manage_skills view` too (reading a skill is
a private read), so every 2026-10-06 audit test lost a round to it. A manual
test could also approve a write such as create_document and leave the document
behind. A read-only run fixes both: skill reads pass, and anything that writes
is refused outright, with no approval card that could let it through.
"""
import json

import pytest

from src.tool_capabilities import ToolRunSecurityContext

pytestmark = pytest.mark.security


def _ctx(**kwargs):
    return ToolRunSecurityContext(external_untrusted_context_seen=True, read_only=True, **kwargs)


@pytest.mark.parametrize("action", ["view", "list", "search", "view_ref"])
def test_a_read_only_run_may_read_skills_after_untrusted_context(action):
    decision = _ctx().decision_for("manage_skills", json.dumps({"action": action, "name": "x"}))
    assert decision.allowed


@pytest.mark.parametrize("tool,args", [
    ("create_document", {"title": "Test sample", "content": "x"}),
    ("manage_notes", {"action": "add", "content": "sample note"}),
    ("manage_memory", {"action": "add", "text": "sample memory"}),
    ("manage_skills", {"action": "edit", "name": "x", "content": "y"}),
    ("write_file", {"path": "sample.txt", "content": "x"}),
    ("bash", {"command": "touch sample.txt"}),
])
def test_a_read_only_run_refuses_writes_with_no_way_to_approve_them(tool, args):
    # approval_gate_bypassed stands in for an approved exact action.
    for ctx in (_ctx(), _ctx(approval_gate_bypassed=True), _ctx(approval_mode="auto")):
        decision = ctx.decision_for(tool, json.dumps(args))
        assert not decision.allowed and decision.final, (tool, ctx)


def test_an_ordinary_run_keeps_its_approvable_gate():
    ctx = ToolRunSecurityContext(external_untrusted_context_seen=True)
    decision = ctx.decision_for("create_document", json.dumps({"title": "t", "content": "c"}))
    assert not decision.allowed and not decision.final
