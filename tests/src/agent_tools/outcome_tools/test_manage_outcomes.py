"""manage_outcomes: owner comes from the context, confirmation never from the model."""
import json

import pytest

from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools.outcome_tools import ManageOutcomesTool

ALICE = {"owner": "alice", "session_id": "worker-1"}
BOB = {"owner": "bob", "session_id": "bob-chat"}


@pytest.fixture
def run(outcomes):
    async def call(ctx, **args):
        return await ManageOutcomesTool().execute(json.dumps(args), ctx)
    return call


async def _create(run, ctx=ALICE, **kw):
    res = await run(ctx, action="create", title=kw.pop("title", "Ship the importer"), **kw)
    assert res["exit_code"] == 0, res
    return res["outcome"]


async def test_create_is_owned_by_the_context_not_the_arguments(run, outcomes):
    """The model must not be able to write into another user's outcomes by naming them."""
    out = await _create(run, owner="bob", user="bob")

    assert out["owner"] == "alice" and out["source"] == "agent"
    assert outcomes.list_for("bob") == []
    assert outcomes.history("alice", out["id"])[0]["actor"] == "agent:worker-1"


@pytest.mark.security
async def test_another_users_outcome_is_invisible_whatever_owner_is_passed(run, outcomes):
    bob_out = outcomes.create("bob", "Bob's thing")["outcome"]

    listed = await run(ALICE, action="list", owner="bob")
    got = await run(ALICE, action="get", id=bob_out["id"], owner="bob")
    moved = await run(ALICE, action="set_state", id=bob_out["id"], state="dropped", owner="bob")

    assert listed["outcomes"] == []
    assert got["exit_code"] == 1 and moved["exit_code"] == 1
    assert outcomes.get("bob", bob_out["id"])["state"] == "open"


@pytest.mark.security
async def test_the_model_cannot_confirm_done_for_the_person(run, outcomes):
    """Production bug: an agent marking its own work done. Confirmation is the person's alone."""
    out = await _create(run)

    for extra in ({"confirmed_by_person": True}, {"confirm": True}, {"confirmed": True}):
        res = await run(ALICE, action="set_state", id=out["id"], state="done", **extra)
        assert res["exit_code"] == 1 and "evidence" in res["error"]

    got = outcomes.get("alice", out["id"])
    assert got["state"] == "open" and got["confirmed_by_person"] is False


@pytest.mark.security
async def test_update_cannot_smuggle_state_confirmation_or_evidence(run, outcomes):
    out = await _create(run)

    res = await run(ALICE, action="update", id=out["id"], title="Renamed", state="done",
                    confirmed_by_person=True, evidence=[{"kind": "commit", "ref": "x"}])

    got = outcomes.get("alice", out["id"])
    assert res["exit_code"] == 0 and got["title"] == "Renamed"
    assert got["state"] == "open" and got["confirmed_by_person"] is False and got["evidence"] == []


async def test_evidence_then_done_succeeds_and_is_attributed_to_the_agent(run, outcomes):
    out = await _create(run)

    added = await run(ALICE, action="add_evidence", id=out["id"], evidence_kind="pull_request",
                      ref="https://github.com/o/r/pull/7", detail="Importer fix", by="person")
    done = await run(ALICE, action="set_state", id=out["id"], state="done")

    got = outcomes.get("alice", out["id"])
    assert added["exit_code"] == 0 and done["exit_code"] == 0
    assert got["state"] == "done" and got["evidence"][0]["by"] == "agent:worker-1"
    assert got["confirmed_by_person"] is False


async def test_an_agent_cannot_set_a_priority(run):
    created = await run(ALICE, action="create", title="x", priority=1)

    assert created["exit_code"] == 1 and "person" in created["error"]


async def test_set_blocker_and_clear_blocker(run, outcomes):
    out = await _create(run, state="active")

    blocked = await run(ALICE, action="set_blocker", id=out["id"], summary="Needs the staging key",
                        user_action="Paste the staging key into the vault note 'deploy'")
    cleared = await run(ALICE, action="clear_blocker", id=out["id"])

    assert blocked["outcome"]["state"] == "blocked"
    assert blocked["outcome"]["blocker"]["recovery"] == "user"
    assert blocked["outcome"]["blocker"]["source"] == {"kind": "chat", "ref": "worker-1"}
    assert cleared["outcome"]["state"] == "active" and cleared["outcome"]["blocker"] is None


async def test_a_blocker_that_needs_the_person_must_say_what_to_do(run):
    out = await _create(run)

    res = await run(ALICE, action="set_blocker", id=out["id"], summary="Stuck")

    assert res["exit_code"] == 1 and "user_action" in res["error"]


async def test_link_and_unlink(run, outcomes):
    out = await _create(run)

    linked = await run(ALICE, action="link", id=out["id"], link_kind="todoist_task", ref="8431", label="Pay rent")
    unlinked = await run(ALICE, action="unlink", id=out["id"], link_id=linked["link"]["id"])

    assert linked["exit_code"] == 0 and unlinked["exit_code"] == 0
    assert outcomes.get("alice", out["id"])["links"] == []


async def test_list_filters_by_states(run):
    a = await _create(run, title="a", state="active")
    await _create(run, title="b")

    res = await run(ALICE, action="list", states=["active"])

    assert [o["id"] for o in res["outcomes"]] == [a["id"]]


async def test_unknown_action_and_missing_owner_are_errors_not_exceptions(run):
    assert (await run(ALICE, action="delete"))["exit_code"] == 1
    assert (await run({"session_id": "x"}, action="list"))["exit_code"] == 1
    assert (await ManageOutcomesTool().execute("{not json", ALICE))["exit_code"] == 1


def test_manage_outcomes_is_registered_everywhere_it_should_be():
    from src.agent_loop import _CORE_TOOLSET, _DOMAIN_TOOL_MAP
    from src.tool_capabilities import ToolEffect, action_is_read, capabilities_for_action, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    assert "manage_outcomes" in TOOL_HANDLERS and "manage_outcomes" in TOOL_TAGS
    assert "manage_outcomes" in BUILTIN_TOOL_DESCRIPTIONS
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "manage_outcomes")
    actions = schema["function"]["parameters"]["properties"]["action"]["enum"]
    assert set(actions) == {"list", "get", "create", "update", "set_state", "link", "unlink",
                            "add_evidence", "set_blocker", "clear_blocker"}
    props = schema["function"]["parameters"]["properties"]
    assert "confirmed_by_person" not in props and "owner" not in props
    assert "manage_outcomes" in _DOMAIN_TOOL_MAP["notes_calendar_tasks"]
    assert "manage_outcomes" not in _CORE_TOOLSET
    assert "manage_outcomes" not in NON_ADMIN_BLOCKED_TOOLS

    assert capabilities_for_tool("manage_outcomes").known
    for read in ("list", "get"):
        assert action_is_read("manage_outcomes", json.dumps({"action": read}))
        assert ToolEffect.WRITE_PRIVATE not in capabilities_for_action(
            "manage_outcomes", json.dumps({"action": read})).effects
    for write in ("create", "update", "set_state", "link", "unlink", "add_evidence", "set_blocker", "clear_blocker"):
        assert not action_is_read("manage_outcomes", json.dumps({"action": write}))
        effects = capabilities_for_action("manage_outcomes", json.dumps({"action": write})).effects
        assert ToolEffect.WRITE_PRIVATE in effects and ToolEffect.DESTRUCTIVE not in effects
