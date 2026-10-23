"""Phalanx lists every saved agent conversation from the database.

The active list used to be seeded from in-memory run records that age out after
24 hours, so a parent chat with no recent run vanished, and a completed turn was
filed as "finished". A conversation is durable: it stays, idle, until the person
archives it.
"""
import time

import pytest

from core import database
from src import agent_activity as activity
from src import agent_control, agent_profiles, agent_runs

ENDPOINT = "http://127.0.0.1:9/v1"


@pytest.fixture
def phalanx(api, monkeypatch):
    stored = agent_profiles.validate_profiles([{"name": "Reader", "tool_access": "all"}])
    monkeypatch.setattr(agent_profiles, "load_profiles", lambda: stored)
    activity._reset_for_tests()
    yield api
    activity._reset_for_tests()


def plain_chat(api, owner, name="c"):
    import uuid
    sid = str(uuid.uuid4())
    api.session_manager.create_session(session_id=sid, name=name, endpoint_url=ENDPOINT, model="m",
                                       rag=False, owner=owner)
    return sid


def talk(api, owner="alice"):
    donor = plain_chat(api, owner, "donor")
    res = api.as_user(owner).post("/api/agents/profiles/Reader/chat", json={"from_session": donor})
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


def rows(api, user="alice", archived=False):
    res = api.as_user(user).get("/api/agents/overview" + ("?archived=true" if archived else ""))
    assert res.status_code == 200, res.text
    return {r["session_id"]: r for r in res.json()["rows"]}


def run(sid, status, owner="alice"):
    """A finished turn of the chat itself, as the chat's own run registry records it."""
    agent_runs._remember_finished(sid, status=status, source="session", owner=owner,
                                  started_at=time.time() - 5)


def test_a_conversation_with_no_run_record_is_listed_idle(phalanx):
    sid = talk(phalanx)
    # No run was ever recorded, which is also what a restart or 24 hours leaves.
    assert not agent_runs.list_runs({sid})
    row = rows(phalanx)[sid]
    assert row["conversation"] is True and row["status"] == "idle" and row["outcome"] is None


def test_the_list_comes_from_the_database_alone_after_a_restart(phalanx):
    sid = talk(phalanx)
    run(sid, "done")
    # A restart empties every in-memory registry; the chat and its settings are in the DB.
    activity._reset_for_tests()
    agent_runs._RUNS.clear(); agent_runs._EXTERNAL.clear(); agent_runs._FINISHED.clear()
    agent_control._STEER.clear()
    row = rows(phalanx)[sid]
    assert row["conversation"] and row["status"] == "idle"
    assert row["created_at"], "stable order needs the creation time from the database"


def test_a_successful_turn_is_idle_with_outcome_done(phalanx):
    sid = talk(phalanx)
    run(sid, "done")
    row = rows(phalanx)[sid]
    assert (row["status"], row["outcome"], row["availability"]) == ("idle", "done", "idle")


def test_a_failed_turn_keeps_its_failure_outcome(phalanx):
    sid = talk(phalanx)
    run(sid, "error")
    row = rows(phalanx)[sid]
    assert row["outcome"] == "failed" and row["status"] == "failed"
    assert row["conversation"] is True


def test_an_idle_parent_with_a_working_child_reports_child_activity(phalanx):
    parent = talk(phalanx)
    run(parent, "done")
    child = plain_chat(phalanx, "alice", "worker")
    database.update_session_settings(child, {"parent_session": parent, "agent_profile": "Reader"})
    with agent_runs.track_external(child, source="session", owner="alice"):
        got = rows(phalanx)
    assert got[parent]["status"] == "idle" and got[parent]["child_activity"] == 1
    assert got[child]["parent_session"] == parent


def test_a_worker_chat_is_not_listed_as_a_conversation(phalanx):
    parent = talk(phalanx)
    worker = plain_chat(phalanx, "alice", "worker")
    database.update_session_settings(worker, {"parent_session": parent, "agent_profile": "Reader"})
    got = rows(phalanx)
    assert got[parent]["conversation"] is True
    assert got[worker]["conversation"] is False
    assert worker in got and got[worker]["parent_session"] == parent


def test_a_plain_chat_and_a_saved_agent_do_not_become_conversations(phalanx):
    plain = plain_chat(phalanx, "alice", "just a chat")
    assert plain not in rows(phalanx)
    assert not any(r["conversation"] for r in rows(phalanx).values())


def test_archive_removes_a_conversation_and_restore_brings_it_back(phalanx):
    sid = talk(phalanx)
    alice = phalanx.as_user("alice")
    assert alice.post(f"/api/agents/sessions/{sid}/archive").status_code == 200
    assert sid not in rows(phalanx)
    assert sid in rows(phalanx, archived=True)
    assert alice.post(f"/api/agents/sessions/{sid}/unarchive").status_code == 200
    assert rows(phalanx)[sid]["status"] == "idle"


def test_status_changes_do_not_reorder_conversations(phalanx):
    first, second = talk(phalanx), talk(phalanx)
    before = list(rows(phalanx))
    run(first, "error")
    with agent_runs.track_external(second, source="session", owner="alice"):
        during = list(rows(phalanx))
    assert before == during == list(rows(phalanx))
    assert before == [second, first], "newest conversation first"


@pytest.mark.security
def test_bob_never_sees_alices_conversations(phalanx):
    sid = talk(phalanx, "alice")
    run(sid, "done")
    assert sid in rows(phalanx, "alice")
    assert sid not in rows(phalanx, "bob")
    assert sid not in rows(phalanx, "bob", archived=True)
    mine = talk(phalanx, "bob")
    assert set(rows(phalanx, "bob")) == {mine}
