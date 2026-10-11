"""Blocker builders adapt the shapes the harness already produces."""
from datetime import datetime

import pytest

from src import worker_preflight as wp
from src.blockers import Blocker, from_interrupted_run, from_needs, from_preflight, from_task_run


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    paths = []
    for name in ("odysseus-main", "other-repo"):
        repo = tmp_path / "dev" / name
        (repo / ".git").mkdir(parents=True)
        paths.append(str(repo.resolve()))
    monkeypatch.setattr(wp, "known_checkouts", lambda: list(paths))
    return paths


def _assert_actionable(blocker: Blocker) -> None:
    assert blocker.recovery in {"auto", "retry", "user", "approval"}
    if blocker.recovery in {"user", "approval"}:
        assert blocker.user_action and blocker.user_action.strip()
    assert blocker.summary and blocker.code and blocker.at


def test_workspace_required_preflight_becomes_a_retryable_blocker(checkouts):
    """A refused launch must show as a blocker, not vanish into a long error string."""
    payload = wp.run_preflight("Run the test suite and summarise failures").blocked_payload()

    blocker = from_preflight(payload)

    assert blocker.code == "WORKSPACE_REQUIRED"
    assert blocker.recovery == "retry"
    assert blocker.source["kind"] == "preflight"
    assert blocker.detail["candidates"] == checkouts
    _assert_actionable(blocker)


def test_workspace_required_with_no_checkout_needs_the_person(monkeypatch):
    """With no candidate to retry with, only the person can supply a workspace."""
    monkeypatch.setattr(wp, "known_checkouts", lambda: [])
    payload = wp.run_preflight("Run the test suite and summarise failures").blocked_payload()

    blocker = from_preflight(payload)

    assert blocker.code == "WORKSPACE_REQUIRED"
    assert blocker.recovery == "user"
    _assert_actionable(blocker)


def test_shell_not_available_preflight_names_one_step_for_the_person(checkouts):
    payload = wp.run_preflight(
        "Run pytest tests/ and report", inherited_workspace=checkouts[0],
        unavailable_tools={"bash", "python"},
    ).blocked_payload()

    blocker = from_preflight(payload)

    assert blocker.code == "SHELL_NOT_AVAILABLE"
    _assert_actionable(blocker)


def test_needs_user_line_blocks_on_the_person():
    """`Needs user:` is the one case where the worker asks a human for something."""
    text = ("Outcome: blocked, the deploy key is missing.\n"
            "Changed: nothing.\n"
            "Needs user: paste the staging deploy key into the vault note 'deploy'\n")

    blocker = from_needs(text, source={"kind": "chat", "ref": "worker-1"})

    assert blocker.code == "NEEDS_USER"
    assert blocker.recovery == "user"
    assert "deploy key" in blocker.user_action
    assert blocker.source == {"kind": "chat", "ref": "worker-1"}
    _assert_actionable(blocker)


def test_needs_parent_line_is_for_the_parent_chat_not_the_person():
    """Surfacing a parent-chat request as a person task would nag them for the agent's job."""
    blocker = from_needs("**Needs parent:** grant write access to src/app.py")

    assert blocker.code == "NEEDS_PARENT"
    assert blocker.recovery == "auto"
    _assert_actionable(blocker)


def test_needs_user_wins_when_both_lines_are_present():
    text = "Needs parent: widen my tools\nNeeds user: approve the schema change"

    blocker = from_needs(text)

    assert blocker.code == "NEEDS_USER"
    assert blocker.detail["needs"] == [
        {"who": "parent", "what": "widen my tools"},
        {"who": "user", "what": "approve the schema change"},
    ]


def test_text_without_a_needs_line_gives_no_blocker():
    assert from_needs("Outcome: done\nChecked: 12 passed") is None


def test_interrupted_run_is_retryable():
    """A restart killed the run; resuming is safe and needs nobody."""
    run = {
        "run_id": "session-ab12cd34ef", "source": "session", "session_id": "worker-1",
        "owner": "alice", "title": "Worker · fix the importer", "status": "interrupted",
        "started_at": 1_760_000_000.0, "finished_at": 1_760_000_900.0,
        "summary": {"branch": "agent/importer", "parent_session": "parent-1"},
    }

    blocker = from_interrupted_run(run)

    assert blocker.code == "RUN_INTERRUPTED"
    assert blocker.recovery == "retry"
    assert blocker.source == {"kind": "agent_run", "ref": "session-ab12cd34ef"}
    assert blocker.detail["session_id"] == "worker-1"
    _assert_actionable(blocker)


def test_failed_task_run_row_becomes_a_blocker(app_db):
    from core.database import ScheduledTask, TaskRun

    db = app_db.SessionLocal()
    try:
        db.add(ScheduledTask(id="t1", owner="alice", name="Weekly digest"))
        db.add(TaskRun(id="r1", task_id="t1", status="error", error="SMTP 535 auth failed",
                       started_at=datetime(2026, 10, 9, 8, 0), finished_at=datetime(2026, 10, 9, 8, 1)))
        db.commit()
        row = db.get(TaskRun, "r1")

        blocker = from_task_run(row)
    finally:
        db.close()

    assert blocker.code == "TASK_RUN_FAILED"
    assert blocker.source == {"kind": "task_run", "ref": "r1"}
    assert "SMTP 535" in blocker.detail["error"]
    _assert_actionable(blocker)


def test_aborted_task_run_is_retryable(app_db):
    from core.database import ScheduledTask, TaskRun

    db = app_db.SessionLocal()
    try:
        db.add(ScheduledTask(id="t2", owner="alice", name="Digest"))
        db.add(TaskRun(id="r2", task_id="t2", status="aborted", error="aborted by user"))
        db.commit()
        blocker = from_task_run(db.get(TaskRun, "r2"))
    finally:
        db.close()

    assert blocker.recovery == "retry"
    _assert_actionable(blocker)


def test_blocker_round_trips_through_json():
    blocker = from_needs("Needs user: pick a database")

    assert Blocker.from_dict(blocker.to_dict()) == blocker


def test_from_dict_repairs_a_user_blocker_stored_without_an_action():
    """A blocker saved by older code must still render with a step for the person."""
    blocker = Blocker.from_dict({"code": "X", "summary": "s", "recovery": "user"})

    assert blocker.user_action


def test_constructing_a_user_blocker_without_an_action_is_refused():
    with pytest.raises(ValueError):
        Blocker(code="X", summary="s", recovery="user", user_action=None, source={}, detail={}, at="")
