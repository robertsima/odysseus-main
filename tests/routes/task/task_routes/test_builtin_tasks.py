"""Built-in (housekeeping) tasks.

The email and calendar built-ins read the user's mail and send it to a model,
so they ship paused (``ship_paused``) and run only once the user turns each
one on. Turning Tasks on, or reverting one of them to its defaults, must not
start them.
"""
from routes.task.task_routes import _task_to_dict
from src.database import ScheduledTask
from src.task_scheduler import HOUSEKEEPING_DEFAULTS

OPT_IN = {action for action, defs in HOUSEKEEPING_DEFAULTS.items() if defs.get("ship_paused")}


def _builtins(client):
    response = client.get("/api/tasks")
    assert response.status_code == 200, response.text
    return [t for t in response.json()["tasks"] if t.get("action") in HOUSEKEEPING_DEFAULTS]


def test_turning_tasks_on_leaves_the_opt_in_builtins_paused(api):
    alice = api.as_user("alice")

    response = alice.post("/api/tasks/onboarding", json={"enabled": True})

    assert response.status_code == 200, response.text
    statuses = {t["action"]: t["status"] for t in _builtins(alice)}
    assert OPT_IN and OPT_IN <= set(statuses)
    assert {statuses[action] for action in OPT_IN} == {"paused"}
    assert {status for action, status in statuses.items() if action not in OPT_IN} == {"active"}


def test_reverting_an_opt_in_builtin_leaves_it_paused(api):
    alice = api.as_user("alice")
    alice.post("/api/tasks/onboarding", json={"enabled": True})
    task = next(t for t in _builtins(alice) if t["action"] in OPT_IN)
    assert alice.put(f"/api/tasks/{task['id']}", json={"status": "active"}).status_code == 200

    response = alice.post(f"/api/tasks/{task['id']}/revert")

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "paused"


def test_the_task_payload_names_its_crew_member():
    # The task list files a crew member's tasks under that member.
    task = ScheduledTask(id="t1", owner="alice", name="Check in", crew_member_id="crew-1")

    assert _task_to_dict(task)["crew_member_id"] == "crew-1"
