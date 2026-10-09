"""The person who owns a chat sees and stops that chat's workers; nobody else does.

The agent strip above the composer reads /api/workbench/runs. It was admin-only,
so on a shared install a regular user could not see or stop the workers of
their own chat (2026-10-08).
"""
import pytest

from src import agent_activity as act

pytestmark = pytest.mark.security


@pytest.fixture
def chats(api):
    act._reset_for_tests()
    alice, bob = api.as_user("alice"), api.as_user("bob")
    alice_chat = alice.post("/api/session", data={"name": "build", "skip_validation": "true"}).json()["id"]
    bob_chat = bob.post("/api/session", data={"name": "notes", "skip_validation": "true"}).json()["id"]
    alice_run = act.run_started(alice_chat, "session", "Worker: Lead Engineer", owner="alice")
    bob_run = act.run_started(bob_chat, "session", "Worker: bob's", owner="bob")
    yield {"alice": alice, "bob": bob, "alice_chat": alice_chat, "bob_chat": bob_chat,
           "alice_run": alice_run, "bob_run": bob_run}
    act._reset_for_tests()


def test_the_owner_lists_the_runs_of_their_chat(chats):
    response = chats["alice"].get(f"/api/workbench/runs?session_id={chats['alice_chat']}")

    assert response.status_code == 200
    assert [row["run_id"] for row in response.json()["runs"]] == [chats["alice_run"]]


def test_another_user_cannot_list_that_chat(chats):
    response = chats["bob"].get(f"/api/workbench/runs?session_id={chats['alice_chat']}")

    assert response.status_code == 404


def test_a_regular_user_cannot_list_every_run(chats):
    assert chats["alice"].get("/api/workbench/runs").status_code == 403


def test_another_user_cannot_stop_or_read_the_run(chats):
    bob, run = chats["bob"], chats["alice_run"]

    assert bob.post(f"/api/workbench/runs/{run}/stop").status_code == 404
    assert bob.post(f"/api/workbench/runs/{run}/wrap-up").status_code == 404
    assert bob.get(f"/api/workbench/runs/{run}").status_code == 404


def test_the_admin_still_sees_every_run(api, chats):
    rows = api.as_admin().get("/api/workbench/runs").json()["runs"]

    assert {chats["alice_run"], chats["bob_run"]} <= {row["run_id"] for row in rows}
