"""Approving a skill test's paused action allows that one action only.

The skill-test card says "Allow once" and has no chat to carry a wider
scope. The resumed run must keep the approval gate armed for every action
after the approved one.
"""
import pytest

from routes import skills_routes
from src.tool_approvals import tool_approval_store
from src.tool_capabilities import capabilities_for_action

pytestmark = pytest.mark.security

SKILL = {"name": "tidy-csv", "id": "skill-1", "owner": "alice"}


@pytest.fixture
def paused_test(api, monkeypatch):
    monkeypatch.setattr(api.module.skills_manager, "load", lambda owner=None, **kw: [dict(SKILL)])
    content = "printf exact"
    pending = tool_approval_store.create(
        owner="alice", session_id="", origin_run_id="run-1", tool_name="bash", content=content,
        workspace=None, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", content),
    )
    monkeypatch.setitem(skills_routes._skill_test_jobs, ("alice", SKILL["name"]), {
        "status": "awaiting_approval", "approval": {"approval_id": pending.approval_id},
        "log": [], "task": "tidy the file", "_run": {"md": "# tidy", "owner": "alice"}, "_transcript": [],
    })
    resumed = []

    async def fake_resume(*args, **kwargs):
        resumed.append(kwargs["exact_approval"])

    monkeypatch.setattr(skills_routes, "_run_skill_test_job", fake_resume)
    return pending, resumed


def test_approving_a_skill_test_action_does_not_lift_the_gate_for_the_rest_of_the_run(api, paused_test):
    pending, resumed = paused_test

    response = api.as_user("alice").post(
        f"/api/skills/{SKILL['name']}/test-approval",
        json={"approval_id": pending.approval_id, "decision": "approve"},
    )

    assert response.status_code == 200, response.text
    [grant] = resumed
    assert grant.allow_remaining_actions is False
