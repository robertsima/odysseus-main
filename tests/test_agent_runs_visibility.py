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
    assert headless_agent.request_stop("session-abc") is True
    text, _events = await asyncio.wait_for(task, 5)
    assert outcome == {"stopped": True}
    assert text.startswith("Checked a.py so far") and "stopped by the user" in text
    assert "session-abc" not in headless_agent.running_ids()
    assert headless_agent.request_stop("session-abc") is False


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

    await headless_agent.run_headless(_Sess(), [], run_id=None)
    assert {"bash", "web_fetch", "send_to_session"} <= set(seen["disabled_tools"])

    # A chat continuing itself (subagent=False) still honours the operator, and
    # is not handed the anti-fan-out set: it is the parent, not a child.
    await headless_agent.run_headless(_Sess(), [], subagent=False)
    assert set(seen["disabled_tools"]) == {"bash", "web_fetch"}


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
    from src.agent_profiles import DEFAULT_ROUNDS

    assert rec["max_rounds"] == DEFAULT_ROUNDS


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

    `owner_baseline_disabled_tools` reads the operator's global setting and
    fails closed on the public-user blocklist, so without pinning both, the
    assertions below would be measuring the test runner's environment.
    """
    import src.settings as settings
    import src.tool_security as tool_security

    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: [] if key == "disabled_tools" else default)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)


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


async def test_a_detached_worker_is_still_never_gated_for_approval(monkeypatch):
    """The other half of the same decision.

    `stream_agent_loop` documents that headless callers are not gated because
    "nobody would be there to answer". That holds for a detached child, which
    runs in a chat the user did not open — so a sub-agent run must keep passing
    no approval mode even when the chat it runs in has one stored.
    """
    import core.database as core_db

    seen = {}
    _capture_loop(monkeypatch, seen)
    _pin_owner_baseline(monkeypatch)
    monkeypatch.setattr(core_db, "get_session_settings",
                        lambda sid: {"approval_mode": "ask_all", "disabled_tools": ["bash"]})

    await headless_agent.run_headless(_Sess(), [])
    assert seen["approval_mode"] is None


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
    monkeypatch.setattr(core_db, "get_session_settings", lambda sid: {})

    await headless_agent.run_headless(_Sess(), [])
    assert "manage_agent_loadout" in set(seen["disabled_tools"])

    # A fresh child passes the tool's own gates: `delegation_policy` defaults to
    # "explicit" (only "never" refuses) and nothing has been started from it.
    assert agent_loadouts.caller_policy("child", "alice")["delegation_policy"] != "never"
    assert agent_control.live_children("child") == 0
