"""status tells a worker that reported itself blocked from a launch that was refused.

Both are `blocked`. Only the refused launch has no worker chat; the reporting
worker has one, and the parent needs its id to read the result or send it back.
"""
from src.agent_tools.loadout_tools import manage_agent_loadout
from tests.src.agent_tools.loadout_tools.test_loadout_repo_tools import runs, store  # noqa: F401


async def test_a_worker_that_reported_blocked_keeps_its_chat_in_the_status_row(store, runs):
    runs.run_finished("w-1", "session", "session-6caedad9d9", "Worker blocked", status="blocked", owner="u",
                      data={"needs_user": ["paste the key"]})

    result = await manage_agent_loadout(
        '{"action": "status", "run_id": "session-6caedad9d9"}', "chat-7", owner="u")

    row = result["runs"][0]
    assert row["status"] == "blocked" and row["worker_session"] == "w-1"
    assert not row.get("note")


async def test_a_refused_launch_still_has_no_worker_chat(store, runs):
    runs.run_started("chat-7", "session", "Worker: refused", run_id="session-refused", owner="u",
                     data={"mode": "agent"})
    runs.run_finished("chat-7", "session", "session-refused", "refused", status="blocked", owner="u",
                      data={"error": "no workspace"})

    result = await manage_agent_loadout('{"action": "status", "run_id": "session-refused"}', "chat-7", owner="u")

    row = result["runs"][0]
    assert row.get("worker_session") is None and "refused before start" in row["note"]
