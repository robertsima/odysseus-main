"""Fixtures for Today: a fixed clock, a fake Todoist CLI and seeding helpers."""
import json
import os
from datetime import datetime

import pytest

from src import agent_activity as act
from src import outcomes as store
from src.blockers import Blocker

NOW = datetime(2026, 10, 10, 10, 0, 0)  # a Saturday, naive UTC
UTC_HEADERS = {"X-Tz-Name": "UTC"}


class FakeTd:
    """Stands in for the `td` CLI: records every call, answers `today --json`."""

    def __init__(self):
        self.tasks = []
        self.calls = []
        self.failure = None
        self.returncode = 0
        self.stderr = ""
        self.refuse = {}  # ("task", "complete") -> stderr, for a command td rejects

    async def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        if self.failure:
            raise self.failure
        if tuple(args[:2]) in self.refuse:
            return 1, "", self.refuse[tuple(args[:2])]
        if self.returncode:
            return self.returncode, "", self.stderr
        if list(args[:1]) == ["today"]:
            return 0, json.dumps(self.tasks), ""
        return 0, "{}", ""

    def writes(self):
        return [c for c in self.calls if c[:1] != ["today"]]


def task(task_id, content, due, priority=1, **extra):
    return {"id": task_id, "content": content, "due": {"date": due}, "priority": priority,
            "url": f"https://app.todoist.com/app/task/{task_id}", **extra}


@pytest.fixture
def td(monkeypatch):
    import src.todoist_calendar_sync as sync

    fake = FakeTd()
    monkeypatch.setattr(sync, "_run_td", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    import src.today as today

    state = {"now": NOW}
    monkeypatch.setattr(today, "_utcnow", lambda: state["now"])
    return state


def _forget_runs():
    """The run registry reloads from DATA_DIR, which the api fixture shares between tests."""
    act._reset_for_tests()
    try:
        os.remove(act._runs_file())
    except OSError:
        pass


@pytest.fixture
def world(api, clock, td):
    """The app with a fixed clock and a fake Todoist; `world.get(client)` reads Today."""
    _forget_runs()

    class World:
        def __init__(self):
            self.api, self.clock, self.td = api, clock, td

        def get(self, client):
            res = client.get("/api/today", headers=UTC_HEADERS)
            assert res.status_code == 200, res.text
            return res.json()

        def act(self, client, **body):
            return client.post("/api/today/action", json=body, headers=UTC_HEADERS)

    yield World()
    _forget_runs()


def keys(section):
    return [item["key"] for item in section]


def outcome(owner, title, *, state="open", blocker=None, links=(), **fields):
    created = store.create(owner, title, state=state, **fields)
    assert created["ok"], created
    oid = created["outcome"]["id"]
    for kind, ref in links:
        assert store.add_link(owner, oid, kind, ref)["ok"]
    if blocker is not None:
        assert store.set_blocker(owner, oid, blocker)["ok"]
    return oid


def person_blocker(recovery="user"):
    return Blocker(code="NEEDS_USER", summary="The worker needs input from you.", recovery=recovery,
                   user_action="Paste the staging deploy key into note 'deploy'.",
                   source={"kind": "chat", "ref": "w1"})


def interrupted_run(owner, session_id, finished_at, *, status="interrupted", title="Worker · fix the importer"):
    """A finished worker run; `finished_at` is epoch seconds because the store stamps real time."""
    run_id = act.run_started(session_id, "session", title, owner=owner,
                             data={"target_session": session_id, "mode": "agent"})
    act.run_finished(session_id, "session", run_id, title, status=status, owner=owner,
                     data={"target_session": session_id})
    act._runs[run_id]["finished_at"] = finished_at
    act._runs[run_id]["started_at"] = finished_at - 60
    return run_id


def epoch(dt):
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc).timestamp()
