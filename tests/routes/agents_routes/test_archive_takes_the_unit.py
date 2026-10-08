"""Archiving a Phalanx agent archives its workers with it; Restore brings the unit back.

Archive used to flag only the parent chat. Its idle workers stayed live, the
overview reported them with a parent that was no longer listed, and the fleet
promoted every one of them to a top-level unit: archiving one old agent made
five new ones appear (reported 2026-10-07).
"""
import pytest

from core import database
from src import agent_activity as activity

WORKERS = 5


@pytest.fixture
def unit(api):
    """Alice's parent chat with five finished workers, each with a recent run."""
    activity._reset_for_tests()
    alice = api.as_user("alice")

    def chat(name):
        created = alice.post("/api/session", data={"name": name, "skip_validation": "true"})
        assert created.status_code == 200, created.text
        return created.json()["id"]

    parent = chat("Release lead")
    workers = [chat(f"↳ Worker {n}") for n in range(WORKERS)]
    for worker in workers:
        database.update_session_settings(worker, {"parent_session": parent})
        run_id = activity.run_started(worker, "session", "Worker task", owner="alice",
                                      data={"target_session": worker, "parent_session": parent})
        activity.run_finished(worker, "session", run_id, "done", status="completed", owner="alice",
                              data={"target_session": worker, "parent_session": parent})
    yield alice, parent, workers
    activity._reset_for_tests()


def overview(client, archived=False):
    response = client.get("/api/agents/overview" + ("?archived=true" if archived else ""))
    assert response.status_code == 200, response.text
    return {row["session_id"]: row for row in response.json()["rows"]}


def test_archiving_a_parent_does_not_leave_its_workers_in_the_fleet(unit):
    alice, parent, workers = unit
    before = overview(alice)
    assert {sid: before[sid]["parent_session"] for sid in workers} == dict.fromkeys(workers, parent)

    archived = alice.post(f"/api/agents/sessions/{parent}/archive")
    assert archived.status_code == 200, archived.text

    fleet = overview(alice)
    assert not set(workers) & set(fleet), "workers of an archived parent surfaced as their own units"
    archive = overview(alice, archived=True)
    assert set(archive) == {parent, *workers}
    assert {archive[sid]["parent_session"] for sid in workers} == {parent}


def test_restoring_a_parent_brings_back_the_workers_archived_with_it(unit):
    alice, parent, workers = unit
    assert alice.post(f"/api/agents/sessions/{parent}/archive").status_code == 200

    restored = alice.post(f"/api/agents/sessions/{parent}/unarchive")
    assert restored.status_code == 200, restored.text

    assert overview(alice, archived=True) == {}
    fleet = overview(alice)
    assert {sid: fleet[sid]["parent_session"] for sid in workers} == dict.fromkeys(workers, parent)


def test_active_child_blocks_parent_archive_without_hiding_anyone(unit):
    alice, parent, workers = unit
    run_id = activity.run_started(workers[0], "session", "Still working", owner="alice")
    assert alice.post(f"/api/agents/sessions/{parent}/archive").status_code == 409
    assert overview(alice, archived=True) == {}
    assert {parent, *workers} <= overview(alice).keys()
    activity.run_finished(workers[0], "session", run_id, "done", status="completed", owner="alice")


def test_failed_lineage_read_does_not_archive_a_parent(unit, monkeypatch):
    alice, parent, workers = unit
    original = database.get_session_settings

    def unreadable(sid, *, strict=False):
        if sid == workers[0]:
            if strict:
                raise RuntimeError("database read unavailable")
            return {}
        return original(sid, strict=strict)

    monkeypatch.setattr(database, "get_session_settings", unreadable)
    assert alice.post(f"/api/agents/sessions/{parent}/archive").status_code == 409
    assert overview(alice, archived=True) == {}


def test_workers_stranded_by_an_earlier_parent_archive_stay_with_that_parent(unit, api):
    """Data archived before archive took the unit: only the parent is flagged."""
    alice, parent, workers = unit
    api.session_manager.archive_session(parent)

    assert not set(workers) & set(overview(alice))
    archive = overview(alice, archived=True)
    assert set(archive) == {parent, *workers}
    assert {archive[sid]["parent_session"] for sid in workers} == {parent}

    # Restoring one stranded worker restores the parent it belongs under.
    assert alice.post(f"/api/agents/sessions/{workers[0]}/unarchive").status_code == 200
    fleet = overview(alice)
    assert parent in fleet and fleet[workers[0]]["parent_session"] == parent


@pytest.mark.security
def test_only_the_owner_restores_a_unit_even_after_a_restart(unit, api):
    """Archived chats are not resident after a restart, so restore checks the DB owner."""
    alice, parent, workers = unit
    bob = api.as_user("bob")
    assert bob.post(f"/api/agents/sessions/{parent}/archive").status_code == 404
    assert alice.post(f"/api/agents/sessions/{parent}/archive").status_code == 200
    for sid in (parent, *workers):
        api.session_manager.sessions.pop(sid, None)

    assert bob.post(f"/api/agents/sessions/{workers[0]}/unarchive").status_code == 404
    assert overview(alice, archived=True).keys() == {parent, *workers}

    assert alice.post(f"/api/agents/sessions/{parent}/unarchive").status_code == 200
    fleet = overview(alice)
    assert {sid: fleet[sid]["parent_session"] for sid in workers} == dict.fromkeys(workers, parent)
