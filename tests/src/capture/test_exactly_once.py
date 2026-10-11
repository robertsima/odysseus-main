"""A capture reaches its destination once, however it is retried, raced or re-keyed."""
import asyncio
import threading
from datetime import timedelta

import pytest

from src import capture


@pytest.mark.parametrize("stored", [
    "Renew passport friday",
    "Renew passport 2026-10-16",
    "renew   passport @errands p2",
    "Renew passport by next friday #Admin",
])
async def test_same_task_under_a_different_key_is_not_created_twice(cap, env, stored):
    """Production bug: the duplicate check compared stored content with a title that differed from
    what was created ('Renew passport friday' vs 'Renew passport'), so a second task was made."""
    env.td.seed(stored)

    res = await cap("admin", "Renew passport", idempotency_key="second-key", destination="todoist")

    assert res["status"] == "duplicate" and res["ref"] == env.td.tasks[0]["id"]
    assert env.td.creates == 0


async def test_two_captures_of_one_line_under_two_keys_make_one_task(cap, env):
    first = await cap("admin", "Renew passport friday", idempotency_key="a")
    second = await cap("admin", "renew passport", idempotency_key="b")

    assert (first["status"], second["status"]) == ("created", "duplicate")
    assert env.td.creates == 1


async def test_waiting_style_content_is_normalised_away(cap, env):
    """Production bug: waiting tasks carry a date and '@waiting' in their content and never matched."""
    env.td.seed("Follow up with Bob 2026-10-14 @waiting")

    res = await cap("admin", "Follow up with Bob", destination="todoist")

    assert res["status"] == "duplicate" and env.td.creates == 0


async def test_concurrent_requests_with_one_key_create_once(env):
    """Production bug: check-then-write was not atomic, so two requests both saw no record and both created."""
    env.td.create_delay = 0.05
    results, errors = [], []

    def run():
        try:
            results.append(asyncio.run(capture.capture("admin", "Book the vet friday", idempotency_key="race", tz_offset=0)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    assert env.td.creates == 1
    assert len(results) == 2 and results[0]["ref"] == results[1]["ref"]
    assert sorted(r["status"] for r in results) == ["created", "created"]
    assert sum(1 for r in results if r.get("replayed")) == 1


async def test_a_create_that_timed_out_after_succeeding_is_not_repeated(cap, env, ledger):
    """Production bug: Todoist created the task but the call timed out, so no record existed and the retry made a second one."""
    env.td.timeout_after_create = True
    first = await cap("admin", "Call the landlord friday", idempotency_key="t")
    env.td.timeout_after_create = False

    second = await cap("admin", "Call the landlord friday", idempotency_key="t")

    assert first["status"] == "failed"
    assert second["status"] == "duplicate" and second["ref"] == env.td.tasks[0]["id"]
    assert env.td.creates == 1
    assert ledger() == [("admin", "t", "done")]


async def test_a_stale_pending_row_from_a_crash_is_reconciled_before_creating(cap, env, ledger):
    """The process died after the create and before the ledger write: the row is still pending."""
    first = await cap("admin", "File the taxes friday", idempotency_key="p")
    assert first["status"] == "created"
    _force_row(env, "admin", "p", status="pending", result_json=None, age=timedelta(minutes=10))

    again = await cap("admin", "File the taxes friday", idempotency_key="p")

    assert again["status"] == "duplicate" and env.td.creates == 1


async def test_a_retry_whose_duplicate_check_cannot_run_does_not_guess(cap, env, ledger):
    env.td.timeout_after_create = True
    await cap("admin", "Call the landlord friday", idempotency_key="t")
    env.td.timeout_after_create = False
    env.td.fail_list = True

    res = await cap("admin", "Call the landlord friday", idempotency_key="t")

    assert res["status"] == "failed" and env.td.creates == 1
    assert ledger() == [("admin", "t", "failed")]


async def test_a_failed_create_can_be_retried_with_the_same_key(cap, env):
    env.td.fail_create = "Todoist CLI could not reach the API"
    first = await cap("admin", "Order filters friday", idempotency_key="f")
    env.td.fail_create = None

    second = await cap("admin", "Order filters friday", idempotency_key="f")

    assert first["status"] == "failed" and "could not reach" in first["error"]
    assert second["status"] == "created" and env.td.creates == 1


async def test_replaying_a_finished_key_returns_the_first_result(cap, env):
    first = await cap("admin", "Order filters friday", idempotency_key="r")

    second = await cap("admin", "Order filters friday", idempotency_key="r")

    assert second["ref"] == first["ref"] and second["status"] == "created" and second["replayed"] is True
    assert env.td.creates == 1


async def test_without_a_key_the_same_line_on_the_same_day_is_one_capture(cap, env):
    a = await cap("admin", "Order filters friday")
    b = await cap("admin", "order filters  friday")

    assert b["ref"] == a["ref"] and env.td.creates == 1


async def test_a_calendar_capture_is_not_repeated(cap, env, events):
    await cap("admin", "Lunch with Sara tomorrow 1pm", idempotency_key="a")
    second = await cap("admin", "lunch with sara tomorrow 1pm", idempotency_key="b")

    assert second["status"] == "duplicate" and len(events("admin")) == 1


async def test_a_note_capture_is_not_repeated(cap, env):
    await cap("alice", "remind me to call mom at 5pm", idempotency_key="a")
    second = await cap("alice", "Remind me to call Mom at 5pm", idempotency_key="b")

    assert second["status"] == "duplicate" and len(env.notes.notes) == 1


async def test_a_half_finished_waiting_capture_is_completed_on_retry_without_a_second_outcome(cap, env, monkeypatch):
    """The outcome was written but the reminder note failed: the retry adds the note to the same outcome."""
    from src.tools import notes as notes_tool
    real = notes_tool.do_manage_notes
    calls = {"n": 0}

    async def flaky(content, owner=None, allow_private=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"error": "disk full", "exit_code": 1}
        return await real(content, owner=owner, allow_private=allow_private)

    monkeypatch.setattr(notes_tool, "do_manage_notes", flaky)
    first = await cap("alice", "waiting on Sam for the contract", idempotency_key="w")
    second = await cap("alice", "waiting on Sam for the contract", idempotency_key="w")

    assert first["status"] == "failed" and second["status"] == "created"
    [out] = env.outcomes.list_for("alice")
    assert len(env.notes.notes) == 1 and second["outcome_id"] == out["id"]


def _force_row(env, owner, key, *, status, result_json, age):
    from core.database import CaptureRequest

    db = env.db.SessionLocal()
    try:
        row = db.query(CaptureRequest).filter_by(owner=owner, key=key).one()
        row.status, row.result_json = status, result_json
        row.updated_at = env.clock.now - age
        db.commit()
    finally:
        db.close()
