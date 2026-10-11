"""POST /api/today/action: start, resume, defer, wait and done."""
import copy
from datetime import datetime

import pytest

from src import outcomes as store
from src.notes_markdown import NoteRecord
from src.notes_store import STORE

from .conftest import NOW, epoch, interrupted_run, keys, outcome, person_blocker, task


def _linked(owner, kind, ref):
    found = store.find_by_link(owner, kind, ref)
    assert len(found) == 1, found
    return found[0]


# ── defer and wait ───────────────────────────────────────────────────────────

def test_defer_hides_a_todoist_task_without_writing_to_todoist(world):
    """Production bug: snoozing a task moved its Todoist due date, so it vanished from Todoist's own Today."""
    admin = world.api.as_admin()
    world.td.tasks = [task("t1", "Pay rent", "2026-10-06", priority=4)]
    before = copy.deepcopy(world.td.tasks)

    res = world.act(admin, key="todoist:t1", action="defer")

    assert res.status_code == 200, res.text
    assert world.td.writes() == [] and world.td.tasks == before
    linked = _linked("admin", "todoist_task", "t1")
    assert linked["snoozed_until"].startswith("2026-10-11T09:00:00")
    assert linked["due_at"] is None and linked["priority"] is None
    got = world.get(admin)
    assert got["today"] == [] and got["next"] == []


def test_a_deferred_item_comes_back_after_the_snooze(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t1", "Pay rent", "2026-10-06")]
    world.act(admin, key="todoist:t1", action="defer", until="2026-10-12T08:30:00")

    hidden = world.get(admin)
    world.clock["now"] = datetime(2026, 10, 12, 8, 31)
    shown = world.get(admin)

    assert hidden["today"] == []
    assert [i["title"] for i in shown["today"]] == ["Pay rent"]


def test_wait_parks_a_todoist_task_with_who_and_when_and_changes_nothing_in_todoist(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t1", "Get the quote", "2026-10-06", priority=3)]
    before = copy.deepcopy(world.td.tasks)

    res = world.act(admin, key="todoist:t1", action="wait", waiting_on="Dana", until="2026-10-15")

    assert res.status_code == 200, res.text
    assert world.td.writes() == [] and world.td.tasks == before
    linked = _linked("admin", "todoist_task", "t1")
    assert linked["state"] == "waiting" and linked["waiting_on"] == "Dana"
    assert linked["follow_up_at"].startswith("2026-10-15T09:00:00")
    assert linked["due_at"] is None and linked["priority"] is None
    assert world.get(admin)["today"] == []


@pytest.mark.parametrize("action,extra", [("defer", {}), ("wait", {"waiting_on": "Dana"})])
def test_no_action_changes_an_outcomes_due_date_or_priority(world, action, extra):
    """Production bug: a snooze rewrote the person's own due date or priority."""
    alice = world.api.as_user("alice")
    oid = outcome("alice", "File the return", priority=2, due_at="2026-10-09T00:00:00")
    before = store.get("alice", oid)

    res = world.act(alice, key=f"outcome:{oid}", action=action, **extra)

    assert res.status_code == 200, res.text
    after = store.get("alice", oid)
    assert (after["due_at"], after["priority"]) == (before["due_at"], before["priority"])
    assert after["state"] != before["state"] or after["snoozed_until"] != before["snoozed_until"]


def test_wait_needs_to_know_who_it_is_waiting_on(world):
    alice = world.api.as_user("alice")
    oid = outcome("alice", "Get the quote")

    res = world.act(alice, key=f"outcome:{oid}", action="wait")

    assert res.status_code == 400
    assert store.get("alice", oid)["state"] == "open"


# ── done ─────────────────────────────────────────────────────────────────────

def test_done_on_an_outcome_records_the_persons_confirmation(world):
    alice = world.api.as_user("alice")
    oid = outcome("alice", "Ship the parser", state="active")

    res = world.act(alice, key=f"outcome:{oid}", action="done")

    got = store.get("alice", oid)
    assert res.status_code == 200, res.text
    assert got["state"] == "done" and got["confirmed_by_person"] is True


def test_done_on_a_todoist_task_completes_it_in_todoist_and_notes_it_on_a_linked_outcome(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t1", "Pay rent", "2026-10-06")]
    oid = outcome("admin", "Rent", links=[("todoist_task", "t1")], snoozed_until="2026-10-20T09:00:00")

    res = world.act(admin, key="todoist:t1", action="done")

    assert res.status_code == 200, res.text
    assert world.td.writes() == [["task", "complete", "t1"]]
    evidence = store.get("admin", oid)["evidence"]
    assert [(e["kind"], e["ref"], e["by"]) for e in evidence] == [("note", "todoist:t1", "person")]


def test_a_failed_todoist_completion_is_an_error_and_records_no_evidence(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t1", "Pay rent", "2026-10-06")]
    oid = outcome("admin", "Rent", links=[("todoist_task", "t1")], snoozed_until="2026-10-20T09:00:00")
    world.td.refuse[("task", "complete")] = "task not found"

    res = world.act(admin, key="todoist:t1", action="done")

    assert res.status_code == 502
    assert store.get("admin", oid)["evidence"] == []


def test_done_on_a_due_note_archives_it_and_keeps_its_due_date(world):
    alice = world.api.as_user("alice")
    STORE.save(NoteRecord(id="n1", owner="alice", title="Water plants", due_date="2026-10-10T16:00:00Z"),
               enforce_readonly=False)

    res = world.act(alice, key="note:n1", action="done")

    note = STORE.find("n1", "alice")
    assert res.status_code == 200, res.text
    assert note.archived is True and note.due_date == "2026-10-10T16:00:00Z"
    assert world.get(alice)["today"] == []


def test_a_calendar_event_offers_only_open(world):
    alice = world.api.as_user("alice")
    from .test_ranking import _event

    _event("alice", "ev1", "Standup", datetime(2026, 10, 10, 14))

    item = world.get(alice)["today"][0]
    assert item["actions"] == ["open"]
    assert world.act(alice, key="event:ev1", action="done").status_code == 400


def test_an_action_the_item_does_not_allow_is_a_400_and_an_unknown_one_too(world):
    alice = world.api.as_user("alice")
    oid = outcome("alice", "Ship the parser", state="active")

    assert world.act(alice, key=f"outcome:{oid}", action="launch").status_code == 400
    assert world.act(alice, key="outcome:nope", action="defer").status_code == 404
    assert world.act(alice, key="todoist:t1", action="defer").status_code == 404  # no Todoist for alice


# ── start ────────────────────────────────────────────────────────────────────

def test_start_turns_a_note_into_an_active_outcome_with_a_seeded_chat(world, monkeypatch):
    import src.today as today

    alice = world.api.as_user("alice")
    monkeypatch.setattr(today, "_default_chat_endpoint",
                        lambda owner: ("http://models.test/v1/chat/completions", "test-model", {}))
    STORE.save(NoteRecord(id="n1", owner="alice", title="Book the dentist", due_date="2026-10-10T16:00:00Z"),
               enforce_readonly=False)

    res = world.act(alice, key="note:n1", action="start")

    body = res.json()
    assert res.status_code == 200, res.text
    linked = _linked("alice", "note", "n1")
    assert linked["state"] == "active" and linked["title"] == "Book the dentist"
    chat = [link for link in linked["links"] if link["kind"] == "chat"][0]
    assert body["open_url"] == f"/?session={chat['ref']}"
    session = world.api.session_manager.get_session(chat["ref"])
    assert session.owner == "alice" and "Book the dentist" in session.history[0].content
    assert STORE.find("n1", "alice").due_date == "2026-10-10T16:00:00Z"


def test_start_without_a_default_model_says_so_and_changes_nothing(world, monkeypatch):
    import src.today as today

    alice = world.api.as_user("alice")
    monkeypatch.setattr(today, "_default_chat_endpoint", lambda owner: None)
    oid = outcome("alice", "Write the spec")

    res = world.act(alice, key=f"outcome:{oid}", action="start")

    assert res.status_code == 409
    assert store.get("alice", oid)["state"] == "open"


def test_start_on_an_outcome_that_already_has_a_chat_reopens_that_chat(world):
    alice = world.api.as_user("alice")
    oid = outcome("alice", "Write the spec", links=[("chat", "c1")])

    res = world.act(alice, key=f"outcome:{oid}", action="start")

    assert res.json()["open_url"] == "/?session=c1"
    assert store.get("alice", oid)["state"] == "active"


# ── resume ───────────────────────────────────────────────────────────────────

@pytest.fixture
def resumed(monkeypatch):
    import src.today as today

    calls = []

    async def fake(owner, session_id):
        calls.append((owner, session_id))
        return {"session_id": session_id, "response": "picked it back up"}

    monkeypatch.setattr(today, "_resume_worker", fake)
    return calls


def test_resume_relaunches_the_worker_chat_of_an_outcome(world, resumed):
    alice = world.api.as_user("alice")
    oid = outcome("alice", "Fix the importer", state="active", assignee="agent:w1", links=[("chat", "w1")])

    res = world.act(alice, key=f"outcome:{oid}", action="resume")

    assert res.status_code == 200, res.text
    assert resumed == [("alice", "w1")]
    assert res.json()["open_url"] == "/?session=w1"


def test_resume_on_an_interrupted_run_relaunches_its_worker(world, resumed):
    alice = world.api.as_user("alice")
    run = interrupted_run("alice", "w7", epoch(NOW) - 3600)

    res = world.act(alice, key=f"run:{run}", action="resume")

    assert res.status_code == 200, res.text
    assert resumed == [("alice", "w7")]


def test_resume_on_an_outcome_without_a_worker_only_opens_its_chat(world, resumed):
    alice = world.api.as_user("alice")
    oid = outcome("alice", "Plan the trip", state="active", links=[("chat", "c9")])

    res = world.act(alice, key=f"outcome:{oid}", action="resume")

    assert resumed == []
    assert res.json()["open_url"] == "/?session=c9"


def test_a_worker_that_preflight_refuses_becomes_a_blocker_on_the_outcome(world, monkeypatch):
    """Production bug: a refused resume looked like success and the outcome stayed 'active'."""
    import src.today as today

    alice = world.api.as_user("alice")
    oid = outcome("alice", "Fix the importer", state="active", assignee="agent:w1", links=[("chat", "w1")])

    async def refused(owner, session_id):
        return {"error": "Worker not started: no workspace", "status": "blocked", "code": "WORKSPACE_INVALID",
                "problems": [{"code": "WORKSPACE_INVALID", "message": "no workspace"}], "exit_code": 1}

    monkeypatch.setattr(today, "_resume_worker", refused)

    res = world.act(alice, key=f"outcome:{oid}", action="resume")

    body = res.json()
    assert res.status_code == 200 and body["ok"] is False
    assert body["blocker"]["code"] == "WORKSPACE_INVALID" and body["blocker"]["user_action"]
    got = store.get("alice", oid)
    assert got["state"] == "blocked" and got["blocker"]["code"] == "WORKSPACE_INVALID"


async def test_the_resume_adapter_calls_send_to_session_for_the_owners_worker(monkeypatch):
    """Production bug: resume messaged the chat in chat mode, so the worker never ran its tools."""
    import json

    import src.agent_tools.session_tools as session_tools
    import src.today as today

    seen = {}

    async def fake_send(content, session_id=None, owner=None):
        seen.update(content=json.loads(content), session_id=session_id, owner=owner)
        return {"session_id": "w1", "response": "ok"}

    monkeypatch.setattr(session_tools, "send_to_session", fake_send)

    result = await today._resume_worker("alice", "w1")

    assert result["response"] == "ok"
    assert seen["owner"] == "alice"
    assert seen["content"]["session_id"] == "w1" and seen["content"]["mode"] == "agent"
    assert seen["content"]["message"]
