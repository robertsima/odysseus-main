"""Multi-chat run visibility, per-sub-agent stop, and sub-agent tool policy.

Before these changes only the browser tab that sent a message knew a chat was
running; a sub-agent could only be stopped by stopping its parent turn; and a
headless child skipped the operator's global disabled-tools setting.
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import agent_runs, constants
from src import headless_agent


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL.clear()
    agent_runs._FINISHED.clear()
    yield
    act._reset_for_tests()
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL.clear()
    agent_runs._FINISHED.clear()


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def test_streamed_runs_are_listed_running_then_finished_and_scoped():
    release = asyncio.Event()

    async def gen():
        yield _sse({"delta": "hi"})
        await release.wait()
        yield "data: [DONE]\n\n"

    agent_runs.start("chat-a", gen(), owner="alice")
    await asyncio.sleep(0)
    rows = agent_runs.list_runs({"chat-a"})
    assert [(r["session_id"], r["status"]) for r in rows] == [("chat-a", "running")]
    assert agent_runs.list_runs({"someone-elses"}) == []

    release.set()
    await agent_runs._RUNS["chat-a"].task
    row = agent_runs.list_runs({"chat-a"})[0]
    assert row["status"] == "done" and row["finished_at"] >= row["started_at"]


def test_background_turns_mark_the_chat_busy():
    with agent_runs.track_external("chat-b", source="bg_job", owner="alice"):
        assert agent_runs.is_busy("chat-b") and not agent_runs.is_active("chat-b")
        assert agent_runs.list_runs({"chat-b"})[0]["status"] == "running"
    assert not agent_runs.is_busy("chat-b")
    assert agent_runs.list_runs({"chat-b"})[0]["status"] == "done"


class _Sess:
    id = "child"
    endpoint_url = "http://llm.test/v1"
    model = "m"
    headers = {}
    owner = "alice"
    context_length = 0


async def test_request_stop_ends_one_subagent_and_keeps_its_partial(monkeypatch):
    started = asyncio.Event()

    async def slow_loop(url, model, messages, **kwargs):
        yield _sse({"delta": "Checked a.py so far"})
        started.set()
        await asyncio.sleep(30)
        yield _sse({"delta": " and more"})

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    outcome = {}
    task = asyncio.ensure_future(headless_agent.run_headless(
        _Sess(), [{"role": "user", "content": "go"}], run_id="session-abc",
        activity_session_id="parent", outcome=outcome))
    await started.wait()
    assert "session-abc" in headless_agent.running_ids()
    assert headless_agent.steering_run_id("child") == "session-abc"
    assert headless_agent.request_stop("session-abc") is True
    text, _events = await asyncio.wait_for(task, 5)
    assert outcome == {"stopped": True}
    assert text.startswith("Checked a.py so far") and "stopped by the user" in text
    assert "session-abc" not in headless_agent.running_ids()
    assert headless_agent.steering_run_id("child") is None
    assert headless_agent.request_stop("session-abc") is False


def _pin_owner_privileges(monkeypatch):
    """Pin the per-user privilege half of `owner_baseline_disabled_tools`.

    That function asks `core.auth` for the owner's privileges and denies
    bash/python/read_file/write_file when `can_use_bash` is false. Two things
    make that environment-dependent rather than deployment-dependent here:
    `AuthManager.is_configured` is only "does ANY user exist", and
    `get_privileges` returns the fail-closed DEFAULT_PRIVILEGES for a username
    it has never heard of -- and `can_use_bash` is false in those defaults.

    So any other test in the suite that writes a user into `data/auth.json`
    (several do, and the file is untracked, so it also survives between runs)
    silently switches those four denials on for every owner this file invents.
    An assertion about the operator's own `disabled_tools` would then be
    measuring the leftover fixture instead. Pin the lookup to a fully
    privileged owner, which is what "the operator's list is the whole baseline"
    means.
    """
    import core.auth as core_auth

    class _FullyPrivileged:
        is_configured = True

        def get_privileges(self, username):
            return dict(core_auth.ADMIN_PRIVILEGES)

    monkeypatch.setattr(core_auth, "get_auth_manager", lambda: _FullyPrivileged())


async def test_headless_children_get_the_global_disabled_tools(monkeypatch):
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import src.settings as settings
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: ["bash", "web_fetch"] if key == "disabled_tools" else default)

    # The owner's admin status is pinned rather than resolved from the
    # environment. owner_baseline_disabled_tools now also carries the public-user
    # blocklist, and owner_is_admin_or_single_user fails closed, so without this
    # the assertion below would be measuring whether the test runner happens to
    # have a loadable auth manager instead of what this test is about: the
    # operator's global `disabled_tools` reaching a headless child.
    import src.tool_security as tool_security
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    # ...and the other half of that same environment lookup, which the pin
    # above does not cover. See _pin_owner_privileges.
    _pin_owner_privileges(monkeypatch)

    await headless_agent.run_headless(_Sess(), [], run_id=None)
    assert {"bash", "web_fetch", "send_to_session"} <= set(seen["disabled_tools"])

    # A chat continuing itself (subagent=False) still honours the operator, and
    # is not handed the anti-fan-out set: it is the parent, not a child.
    await headless_agent.run_headless(_Sess(), [], subagent=False)
    assert set(seen["disabled_tools"]) == {"bash", "web_fetch"}


@pytest.mark.parametrize("frame", [
    'event: error\ndata: {"status": 401, "text": "credentials expired"}\n\n',
    'data: {"type": "stream_error", "status": 503, "error": "provider unavailable"}\n\n',
])
async def test_headless_promotes_terminal_stream_errors(monkeypatch, frame):
    async def failed_loop(url, model, messages, **kwargs):
        yield frame

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", failed_loop)
    with pytest.raises(headless_agent.HeadlessStreamError) as raised:
        await headless_agent.run_headless(_Sess(), [], run_id="failed-worker")
    assert raised.value.status in {401, 503}
    assert "failed" in str(raised.value).lower()
    assert "failed-worker" not in headless_agent.running_ids()


async def test_headless_refreshes_session_auth_before_streaming(monkeypatch):
    seen = {}
    async def healthy_loop(url, model, messages, **kwargs):
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import routes.chat_helpers as chat_helpers
    monkeypatch.setattr(agent_loop, "stream_agent_loop", healthy_loop)
    monkeypatch.setattr(chat_helpers, "resolve_session_auth",
                        lambda sess, session_id, owner=None: seen.update(
                            session_id=session_id, owner=owner))
    await headless_agent.run_headless(_Sess(), [])
    assert seen == {"session_id": "child", "owner": "alice"}


async def test_headless_steer_id_does_not_overwrite_its_wrapper_activity_run(monkeypatch):
    """The dashboard's worker record and the loop's telemetry are distinct.

    Reusing the wrapper id for ``stream_agent_loop(... run_id=...)`` turns its
    source into ``odysseus`` and replaces the wrapper summary.  Steering needs
    the wrapper id for queue isolation, but activity accounting must retain
    both records.
    """
    seen = {}

    async def fake_loop(_url, _model, _messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    wrapper = act.run_started(
        "child", "session", "Worker · audit", run_id="worker-run",
        owner="alice", data={"target_session": "child", "mode": "agent"},
    )
    await headless_agent.run_headless(
        _Sess(), [], run_id=wrapper, activity_session_id="child", source="session", owner="alice",
    )
    record = act.get_run(wrapper)
    assert seen["steer_run_id"] == wrapper
    assert record["source"] == "session"
    assert record["summary"]["target_session"] == "child"


async def test_dashboard_steer_binds_to_and_is_drained_by_headless_wrapper(monkeypatch):
    """The loop's telemetry id is not the headless wrapper steering id."""
    started = asyncio.Event()
    release = asyncio.Event()
    drained = []

    async def fake_loop(_url, _model, _messages, **kwargs):
        started.set()
        await release.wait()
        from src import agent_control
        drained.extend(agent_control.drain_steer("child", run_id=kwargs["steer_run_id"]))
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    from src import agent_control
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    task = asyncio.create_task(headless_agent.run_headless(_Sess(), [], run_id="wrapper-run"))
    await started.wait()
    steer = agent_control.steer("child", "report only the findings")
    assert steer["run_id"] == "wrapper-run"
    release.set()
    await task
    assert drained == ["report only the findings"]


async def test_headless_stop_cancels_only_its_undrained_steer(monkeypatch):
    started = asyncio.Event()

    async def slow_loop(_url, _model, _messages, **_kwargs):
        started.set()
        await asyncio.sleep(30)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    from src import agent_control
    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    task = asyncio.create_task(headless_agent.run_headless(_Sess(), [], run_id="stop-wrapper"))
    await started.wait()
    steer = agent_control.steer("child", "do not leak into a later run")
    assert headless_agent.request_stop("stop-wrapper")
    await task
    row = next(row for row in agent_control.steer_history("child") if row["id"] == steer["id"])
    assert row["state"] == "cancelled"


async def test_a_non_admin_headless_child_is_denied_the_public_blocklist(monkeypatch):
    """The same merge point also has to stop a child being handed tools the
    execution gate would refuse — see tests/test_non_admin_phantom_tools.py."""
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import src.settings as settings
    import src.tool_security as tool_security
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: [] if key == "disabled_tools" else default)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)

    await headless_agent.run_headless(_Sess(), [], subagent=False)
    assert NON_ADMIN_BLOCKED_TOOLS <= set(seen["disabled_tools"])


def test_bg_job_is_a_run_from_launch_and_kill_closes_it(monkeypatch, tmp_path):
    from src import bg_jobs

    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "_kill", lambda pid: None)

    class _Proc:
        pid = 4242

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **k: _Proc())
    rec = bg_jobs.launch("sleep 100", "chat-c")
    run = act.get_run(f"bg_job-{rec['id']}")
    assert run["status"] == "running" and run["summary"]["job_id"] == rec["id"]
    bg_jobs.kill(rec["id"])
    assert act.get_run(f"bg_job-{rec['id']}")["status"] == "cancelled"


# ── a worker that ran out of rounds is not a worker that finished ─────────
#
# The round cap is a budget, and spending it mid-task is a normal outcome: the
# worker did real work and simply stopped. Reporting that as `completed` told
# the dashboard it was fine and handed the parent chat a result with nothing in
# it to act on.


class _Worker:
    id, name = "w-1", "Runner"


def _manager(sessions):
    class _M:
        def get_session(self, sid):
            return sessions.get(sid)

        def save_sessions(self):
            pass
    return _M()


class _Chat:
    def __init__(self, sid):
        self.id, self.name, self.model = sid, sid, "m"
        # A real `run_headless` reads both off the session: the endpoint to call
        # and a window, the latter so `_resolve_context_length` does not go to
        # the network probing for one.
        self.endpoint_url, self.context_length = "http://llm.test/v1", 200_000
        self.messages = []

    def add_message(self, message):
        self.messages.append(message)

    def get_context_messages(self):
        return []


async def test_a_cut_off_worker_is_reported_incomplete_not_completed(monkeypatch):
    from src import agent_control
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    worker_chat = _Chat("w-1")
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager({"w-1": worker_chat}))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))

    async def fake_headless(sess, messages, **kwargs):
        kwargs["outcome"]["rounds_exhausted"] = True
        kwargs["outcome"]["rounds"] = 6
        return "Read five files.\n\n(ran out of rounds: ...)", [{"tool": "read_file"}]

    monkeypatch.setattr(headless, "run_headless", fake_headless)

    rec = await agent_control.launch_worker(owner="alice", task="audit the handler")
    await agent_control._WORKERS[rec["run_id"]]

    assert act.get_run(rec["run_id"])["status"] == "incomplete"
    # The partial work is still what gets saved — the status is the warning.
    assert worker_chat.messages[-1].content.startswith("Read five files.")
    # The budget the run was cut off by is reported with it, so "ran out of
    # rounds" can be read against a number without reopening the loadout.
    # 0 is "no round ceiling", which is the default for a worker now: a round
    # counter only ever ended real work mid-task, and what actually bounds a run
    # (tool-call ceiling, timeout, tool policy, the stop control) is unchanged.
    from src.agent_profiles import DEFAULT_ROUNDS

    assert rec["max_rounds"] == DEFAULT_ROUNDS == 0


async def test_the_parent_is_told_the_worker_was_cut_off(monkeypatch):
    from src import agent_control

    parent = _Chat("parent")
    # Mid-turn, so the hand-off stops at persisting the message for the next
    # turn — which is exactly the text under test.
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)

    await agent_control._hand_off(_manager({"parent": parent}), "parent", _Worker(),
                                  "audit the handler", "Read five files.", "incomplete", "alice")

    inject = parent.messages[-1].content
    assert "[Worker Runner ran out of rounds]" in inject
    assert "partial" in inject and "pick the task up from where it stopped" in inject


async def test_a_finished_worker_hand_off_is_unchanged(monkeypatch):
    from src import agent_control

    parent = _Chat("parent")
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)

    await agent_control._hand_off(_manager({"parent": parent}), "parent", _Worker(),
                                  "audit the handler", "All five files are clean.", "completed", "alice")

    inject = parent.messages[-1].content
    assert "[Worker Runner finished]" in inject and "cut off" not in inject


# ── a worker finishing must not widen the chat it reports back to ─────────
#
# The hand-off resumes the PARENT — the user's own foreground chat — headlessly.
# It used to pass `disabled_tools=None`, an argument that meant both "no extra
# denials" and "not a sub-agent" at once, so the chat's own `disabled_tools`
# never reached the loop; and it never passed an approval mode at all. A chat
# with `bash` switched off and `approval_mode: ask_all` therefore ran `bash`,
# ungated, the moment a worker reported back.


def _capture_loop(monkeypatch, seen):
    """Stand in for the agent loop and record the policy kwargs it was given."""
    import src.agent_loop as agent_loop

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield _sse({"delta": "continued"})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)


def _pin_owner_baseline(monkeypatch):
    """Pin the owner-level merge so these tests measure the chat's own policy.

    `owner_baseline_disabled_tools` reads three things that are not the subject
    of these tests -- the operator's global setting, the public-user blocklist
    (which fails closed) and the owner's per-user privileges -- so without
    pinning all three the assertions below would be measuring the test runner's
    environment.
    """
    import src.settings as settings
    import src.tool_security as tool_security

    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: [] if key == "disabled_tools" else default)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    _pin_owner_privileges(monkeypatch)


async def test_a_worker_hand_off_keeps_the_parent_chats_own_policy(monkeypatch):
    import core.database as core_db
    from src import agent_control

    seen = {}
    _capture_loop(monkeypatch, seen)
    _pin_owner_baseline(monkeypatch)
    monkeypatch.setattr(core_db, "get_session_settings",
                        lambda sid: {"disabled_tools": ["bash", "write_file"],
                                     "approval_mode": "ask_all"} if sid == "parent" else {})
    # Idle, so the hand-off continues the chat rather than parking the message.
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: False)

    parent = _Chat("parent")
    await agent_control._hand_off(_manager({"parent": parent}), "parent", _Worker(),
                                  "audit the handler", "Read five files.", "completed", "alice")

    # The chat's own denials are in force for the continuation ...
    assert {"bash", "write_file"} <= set(seen["disabled_tools"])
    # ... and so is its approval mode, which was previously never passed at all,
    # so a chat that asks before every change executed without asking.
    assert seen["approval_mode"] == "ask_all"
    # But this is the parent, not a child: the anti-fan-out set is for children,
    # and a chat that delegated once may delegate again.
    assert not (headless_agent.SUBAGENT_BLOCKED_TOOLS & set(seen["disabled_tools"]))
    # It really did run — an assertion on kwargs alone would pass on a no-op.
    assert parent.messages[-1].content == "continued"


async def test_a_detached_worker_runs_under_its_own_chats_approval_mode(monkeypatch):
    """The other half of the same decision, settled the strict way.

    `stream_agent_loop` documents that headless callers are not gated because
    "nobody would be there to answer", and this test used to pin that: a
    detached child passed no approval mode at all. It now passes the mode of
    the chat it runs in, whichever kind of run it is, because a worker's own
    chat carries the mode its profile set when the child was created and a held
    call is recorded by `tool_approvals.request` and listed by the Agent
    Control Room — so the question is answerable there too, and an unanswered
    call is one that never ran. The assertion moved in the direction that
    gates MORE, never less.
    """
    import core.database as core_db

    seen = {}
    _capture_loop(monkeypatch, seen)
    _pin_owner_baseline(monkeypatch)
    monkeypatch.setattr(core_db, "get_session_settings",
                        lambda sid: {"approval_mode": "ask_all", "disabled_tools": ["bash"]})

    await headless_agent.run_headless(_Sess(), [])
    assert seen["approval_mode"] == "ask_all"
    # A child still gets the anti-fan-out set, and NOT the chat's own denials:
    # `subagent=True` means "extra denials only", so `bash` above is not merged.
    assert headless_agent.SUBAGENT_BLOCKED_TOOLS <= set(seen["disabled_tools"])


async def test_a_headless_child_cannot_start_a_worker_via_the_loadout_tool(monkeypatch):
    """`manage_agent_loadout` with action="start" calls `launch_worker`.

    It starts another agent, which is exactly what SUBAGENT_BLOCKED_TOOLS is
    for. The second half of this test checks that the tool's own gates are open
    for a fresh child, i.e. that the entry in the blocked set is what does the
    work rather than being belt-and-braces.
    """
    import core.database as core_db
    from src import agent_control, agent_loadouts

    seen = {}
    _capture_loop(monkeypatch, seen)
    _pin_owner_baseline(monkeypatch)
    # `caller_policy` reads the chat's policy with strict=True and fails closed
    # if it cannot, so the stub has to accept that keyword.
    monkeypatch.setattr(core_db, "get_session_settings", lambda sid, **kw: {})

    await headless_agent.run_headless(_Sess(), [])
    assert "manage_agent_loadout" in set(seen["disabled_tools"])

    # A fresh child passes the tool's own gates: `delegation_policy` defaults to
    # "explicit" (only "never" refuses) and nothing has been started from it.
    assert agent_loadouts.caller_policy("child", "alice")["delegation_policy"] != "never"
    assert agent_control.live_children("child") == 0

async def test_the_chat_that_started_a_worker_sees_it_start_and_finish(monkeypatch):
    """The agent strip above the composer is driven by the PARENT chat's feed.

    A worker's run_started/run_finished are filed under the worker's own chat,
    so without these two parent-side events the strip drew a row that never
    resolved and was then reconciled away as interrupted — which is why
    sub-agents stopped appearing in the chat that launched them.
    """
    from src import agent_control
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    parent_chat, worker_chat = _Chat("parent"), _Chat("w-9")
    sessions = {"parent": parent_chat, "w-9": worker_chat}
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager(sessions))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)

    async def fake_headless(sess, messages, **kwargs):
        kwargs["outcome"]["rounds_exhausted"] = True
        return "Looked at four files.", [{"tool": "read_file"}]

    monkeypatch.setattr(headless, "run_headless", fake_headless)

    rec = await agent_control.launch_worker(
        owner="alice", task="audit the notes UI", parent_session="parent")
    await agent_control._WORKERS[rec["run_id"]]

    parent_events = [ev for ev in act.history("parent") if ev["run_id"] == rec["run_id"]]
    kinds = [ev["kind"] for ev in parent_events]
    assert "message" in kinds and "status" in kinds

    closing = next(ev for ev in parent_events if ev["kind"] == "status")
    assert closing["data"]["status"] == "incomplete"
    assert closing["data"]["rounds_exhausted"] is True
    assert "work outstanding" in closing["title"]

    # And the parent can list the run, which is what stops the strip's
    # reconcile pass from deciding the run it could not find was interrupted.
    listed = act.list_runs(session_id="parent")
    assert [r["run_id"] for r in listed] == [rec["run_id"]]
    assert listed[0]["status"] == "incomplete"


async def test_an_exhausted_run_is_never_extended(monkeypatch):
    """Automatic continuation legs were reverted: a 20-round loadout could run
    ~100 rounds. A run that reports its rounds exhausted ends as incomplete."""
    from src import agent_control
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    worker_chat = _Chat("w-3")
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager({"w-3": worker_chat}))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))
    legs = []

    async def fake_headless(sess, messages, **kwargs):
        legs.append(list(messages))
        kwargs["outcome"]["rounds_exhausted"] = True
        return "leg 1 partial", [{"tool": "read_file"}]

    monkeypatch.setattr(headless, "run_headless", fake_headless)

    rec = await agent_control.launch_worker(
        owner="alice", task="audit the handler", model=None, profile_name=None)
    await agent_control._WORKERS[rec["run_id"]]

    assert len(legs) == 1, "an explicit budget must not be extended"
    assert act.get_run(rec["run_id"])["status"] == "incomplete"
    assert not hasattr(agent_control, "CONTINUATION_LEGS")


async def test_a_worker_making_no_progress_is_not_handed_another_budget(monkeypatch):
    from src import agent_control
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    worker_chat = _Chat("w-4")
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager({"w-4": worker_chat}))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))
    calls = []

    async def spinning(sess, messages, **kwargs):
        calls.append(1)
        kwargs["outcome"]["rounds_exhausted"] = True
        return "still thinking", []          # no tool calls == no progress

    monkeypatch.setattr(headless, "run_headless", spinning)

    rec = await agent_control.launch_worker(owner="alice", task="spin")
    await agent_control._WORKERS[rec["run_id"]]

    assert len(calls) == 1
    assert act.get_run(rec["run_id"])["status"] == "incomplete"


async def test_headless_workers_get_the_tool_call_budget(monkeypatch):
    """Rounds are advisory, so `agent_max_tool_calls` is a worker's real bound.
    Headless runs never passed it: a worker ran 108 rounds with no ceiling."""
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import src.settings as settings
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: 500 if key == "agent_max_tool_calls" else default)

    await headless_agent.run_headless(_Sess(), [])
    # Same margin chat_routes puts above the configured budget.
    assert seen["max_tool_calls"] == 550

    await headless_agent.run_headless(_Sess(), [], max_tool_calls=7)
    assert seen["max_tool_calls"] == 7

    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: "unlimited" if key == "agent_max_tool_calls" else default)
    await headless_agent.run_headless(_Sess(), [])
    assert seen["max_tool_calls"] == 0


async def test_a_worker_cut_off_by_the_tool_budget_hands_back_as_incomplete(monkeypatch):
    async def fake_loop(url, model, messages, **kwargs):
        yield _sse({"type": "agent_step", "round": 42})
        yield _sse({"type": "tool_output", "tool": "read_file", "command": "a.py",
                    "output": "x", "exit_code": 0})
        yield _sse({"type": "budget_exceeded", "limit": 550, "used": 550})
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    outcome = {}
    text, _events = await headless_agent.run_headless(
        _Sess(), [], run_id="budget-run", activity_session_id="parent", outcome=outcome)
    assert outcome["rounds_exhausted"] is True and outcome["budget_exhausted"] is True
    assert outcome["rounds"] == 42
    assert "ran out of tool calls" in text and "550" in text and "partial work" in text


def test_workers_cannot_start_workers_through_loadouts_or_orchestration():
    assert {"orchestrate_agents", "manage_agent_loadout"} <= headless_agent.SUBAGENT_BLOCKED_TOOLS
    # Peer messaging between running agents stays available.
    assert "message_agent" not in headless_agent.SUBAGENT_BLOCKED_TOOLS


# ── Round-budget wrap-up: an explicit profile budget is a wrap-up point ──


async def test_headless_forwards_wrap_up_round_and_defaults_to_none(monkeypatch):
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)

    await headless_agent.run_headless(_Sess(), [])
    assert seen["wrap_up_round"] == 0

    await headless_agent.run_headless(_Sess(), [], max_rounds=30, wrap_up_round=30)
    assert seen["wrap_up_round"] == 30 and seen["max_rounds"] == 30


async def test_a_run_that_wraps_up_at_its_budget_is_a_finished_one(monkeypatch):
    async def fake_loop(url, model, messages, **kwargs):
        yield _sse({"type": "tool_output", "tool": "read_file", "command": "a.py", "output": "x", "exit_code": 0})
        yield _sse({"type": "round_budget_reached", "round": 30, "budget": 30})
        yield _sse({"delta": "Findings so far. Unfinished: b.py."})
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    outcome = {}
    text, _events = await headless_agent.run_headless(
        _Sess(), [], run_id="wrap-run", activity_session_id="parent", outcome=outcome, wrap_up_round=30)

    assert outcome == {"round_budget_reached": 30}
    assert text == "Findings so far. Unfinished: b.py."
    notes = [ev for ev in act.history("parent") if ev["kind"] == "note"]
    assert any("round budget of 30" in ev["title"] for ev in notes)


async def test_a_wrap_up_with_no_answer_hands_back_as_incomplete(monkeypatch):
    async def fake_loop(url, model, messages, **kwargs):
        yield _sse({"type": "round_budget_reached", "round": 30, "budget": 30})
        yield _sse({"type": "round_budget_unanswered", "round": 30, "budget": 30})
        yield _sse({"delta": "I gathered some search results but couldn't pull a clean answer together."})
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    outcome = {}
    text, _events = await headless_agent.run_headless(_Sess(), [], outcome=outcome, wrap_up_round=30)

    assert outcome["rounds_exhausted"] is True and outcome["rounds"] == 30
    assert "round_budget_reached" not in outcome
    assert "ran out of rounds" in text and "partial work" in text


def _launch_env(monkeypatch, sid):
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    worker_chat = _Chat(sid)
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager({sid: worker_chat}))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))
    seen = []

    async def fake_headless(sess, messages, **kwargs):
        seen.append(kwargs)
        if kwargs.get("wrap_up_round"):
            kwargs["outcome"]["round_budget_reached"] = kwargs["wrap_up_round"]
        return "Here is what I found; not yet done: the tests.", [{"tool": "read_file"}]

    monkeypatch.setattr(headless, "run_headless", fake_headless)
    return worker_chat, seen


async def test_a_saved_loadouts_budget_asks_the_worker_to_wrap_up(monkeypatch):
    from src import agent_control, agent_profiles

    _worker, seen = _launch_env(monkeypatch, "w-budget")
    profiles = {p["name"]: p for p in agent_profiles.validate_profiles([
        {"name": "Brownfield Contribution Scout", "max_rounds": 30},
        {"name": "Open-ended", "max_rounds": 0},
    ])}
    monkeypatch.setattr(agent_profiles, "get_profile", lambda name: profiles.get(name))

    rec = await agent_control.launch_worker(
        owner="alice", task="survey the handler", profile_name="Brownfield Contribution Scout")
    await agent_control._WORKERS[rec["run_id"]]
    assert seen[-1]["max_rounds"] == 30 and seen[-1]["wrap_up_round"] == 30
    # Wrapping up with an answer is a finished run, not an incomplete one.
    assert act.get_run(rec["run_id"])["status"] == "completed"

    rec = await agent_control.launch_worker(owner="alice", task="survey the handler", profile_name="Open-ended")
    await agent_control._WORKERS[rec["run_id"]]
    assert seen[-1]["wrap_up_round"] == 0


async def test_defaults_nobody_chose_stay_advisory(monkeypatch):
    """No profile, a model-only worker, and a workflow's inline profile (whose
    12 is a default) never get a wrap-up round."""
    from src import agent_control
    import core.database as database

    _worker, seen = _launch_env(monkeypatch, "w-default")
    monkeypatch.setattr(database, "update_session_settings", lambda sid, patch: dict(patch))

    rec = await agent_control.launch_worker(owner="alice", task="survey the handler")
    await agent_control._WORKERS[rec["run_id"]]
    assert seen[-1]["wrap_up_round"] == 0

    rec = await agent_control.launch_worker(owner="alice", task="survey the handler", model="other-model")
    await agent_control._WORKERS[rec["run_id"]]
    assert seen[-1]["wrap_up_round"] == 0

    rec = await agent_control.launch_worker(
        owner="alice", task="survey the handler", preflight=False, handoff=False,
        inline_profile={"name": "research-1", "max_rounds": 12, "tool_access": "none"})
    await agent_control._WORKERS[rec["run_id"]]
    assert seen[-1]["max_rounds"] == 12 and seen[-1]["wrap_up_round"] == 0
