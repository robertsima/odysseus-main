"""A slow or failing source degrades Today instead of breaking it."""
import asyncio
import copy

import pytest

from .conftest import keys, outcome, task


def test_a_todoist_cli_that_raises_is_listed_as_degraded_and_the_rest_renders(world):
    """Production bug: one dead integration turned the whole page into a 500."""
    admin = world.api.as_admin()
    world.td.failure = RuntimeError("td crashed")
    mine = outcome("admin", "Write the spec", state="active")

    got = world.get(admin)

    assert got["degraded"] == ["todoist"]
    assert keys(got["next"]) == [f"outcome:{mine}"]


def test_a_failing_outcome_store_is_degraded_and_todoist_still_renders(world, monkeypatch):
    import src.outcomes as store

    admin = world.api.as_admin()
    world.td.tasks = [task("t1", "Pay rent", "2026-10-06")]
    monkeypatch.setattr(store, "list_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))

    got = world.get(admin)

    assert got["degraded"] == ["outcomes"]
    assert keys(got["today"]) == ["todoist:t1"]


def test_a_source_that_hangs_is_cut_off_at_its_timeout(world, monkeypatch):
    import src.today as today

    admin = world.api.as_admin()
    mine = outcome("admin", "Write the spec", state="active")

    async def hang(args, timeout=60):
        await asyncio.sleep(30)

    monkeypatch.setattr("src.todoist_calendar_sync._run_td", hang)
    monkeypatch.setattr(today, "TODOIST_TIMEOUT", 0.05)

    got = world.get(admin)

    assert got["degraded"] == ["todoist"]
    assert keys(got["next"]) == [f"outcome:{mine}"]


def test_a_nonzero_exit_from_td_is_degraded(world):
    admin = world.api.as_admin()
    world.td.returncode, world.td.stderr = 1, "Todoist CLI timed out after 60 seconds"

    assert world.get(admin)["degraded"] == ["todoist"]


@pytest.mark.parametrize("stderr", ["TODOIST_API_TOKEN is not configured",
                                    "Todoist CLI executable 'td' was not found in PATH"])
def test_todoist_that_is_not_set_up_is_skipped_quietly(world, stderr):
    """Production bug: every user without Todoist saw a permanent 'degraded' warning."""
    admin = world.api.as_admin()
    world.td.returncode, world.td.stderr = 1, stderr
    mine = outcome("admin", "Write the spec", state="active")

    got = world.get(admin)

    assert got["degraded"] == []
    assert keys(got["next"]) == [f"outcome:{mine}"]


@pytest.mark.security
def test_a_regular_user_never_reaches_the_shared_todoist_account(world):
    """Production bug: one Todoist token, read for every user, showed the admin's tasks to alice."""
    world.td.tasks = [task("t1", "Admin's private errand", "2026-10-06")]

    got = world.get(world.api.as_user("alice"))

    assert world.td.calls == []
    assert got["today"] == [] and got["degraded"] == []


def test_the_response_shape(world):
    got = world.get(world.api.as_user("alice"))

    assert set(got) == {"today", "next", "degraded", "generated_at"}
    assert got["generated_at"].startswith("2026-10-10T10:00:00")


def test_todoist_tasks_without_a_due_date_or_due_later_are_left_out(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("later", "Next month", "2026-11-01"),
                      {"id": "nodue", "content": "Someday", "priority": 1},
                      task("now", "Today", "2026-10-10")]
    snapshot = copy.deepcopy(world.td.tasks)

    got = world.get(admin)

    assert keys(got["today"]) == ["todoist:now"]
    assert world.td.tasks == snapshot
