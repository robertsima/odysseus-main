"""A lead engineer worker may start implementors, one level down, no deeper.

User chat (depth 0) -> lead (1) -> implementors (2). Before this, every
worker lost every launcher, so a lead could only do the implementation itself.
"""

import pytest

from src import agent_control, agent_runs
from src import headless_agent as headless

# child -> parent links, as worker chats store them in `parent_session`.
CHAIN = {"lead": "user-chat", "impl": "lead", "impl-child": "impl"}


@pytest.fixture
def chain(monkeypatch):
    settings = {sid: {"parent_session": parent} for sid, parent in CHAIN.items()}
    settings["user-chat"] = {}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: dict(settings.get(sid, {})))
    return settings


def test_depth_counts_worker_hops(chain):
    assert headless.worker_depth("user-chat") == 0
    assert headless.worker_depth("lead") == 1
    assert headless.worker_depth("impl") == 2


def test_a_parent_chain_cycle_reads_as_too_deep(monkeypatch):
    loop = {"a": {"parent_session": "b"}, "b": {"parent_session": "a"}}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: loop.get(sid, {}))
    assert headless.worker_depth("a") >= headless.max_worker_depth()


def test_a_lead_keeps_the_launchers_and_an_implementor_does_not(chain):
    lead = headless.child_blocked_tools("lead", {"delegation_policy": "auto"})
    assert not (lead & headless.NESTABLE_LAUNCH_TOOLS)
    # Everything else that starts work elsewhere stays off even for a lead.
    assert {"delegate_to_claude_code", "send_to_session", "create_session"} <= lead

    impl = headless.child_blocked_tools("impl", {"delegation_policy": "auto"})
    assert headless.NESTABLE_LAUNCH_TOOLS <= impl


def test_never_means_never_for_a_worker_too(chain):
    blocked = headless.child_blocked_tools("lead", {"delegation_policy": "never"})
    assert headless.NESTABLE_LAUNCH_TOOLS <= blocked


def test_depth_setting_of_one_restores_the_old_no_nesting_rule(chain, monkeypatch):
    monkeypatch.setattr(headless, "max_worker_depth", lambda: 1)
    assert headless.NESTABLE_LAUNCH_TOOLS <= headless.child_blocked_tools("lead", {"delegation_policy": "auto"})


async def test_launch_below_the_limit_is_refused(chain):
    with pytest.raises(ValueError, match="nesting limit"):
        await agent_control.launch_worker(owner="u", task="write the tests", parent_session="impl")


class _Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata or {}


class _Chat:
    def __init__(self, sid):
        self.id, self.name, self.model = sid, sid, "m"
        self.history = []

    def add_message(self, message):
        self.history.append(message)

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.history]


class _Manager:
    def __init__(self, *chats):
        self.chats = {c.id: c for c in chats}

    def get_session(self, sid):
        return self.chats.get(sid)

    def save_sessions(self):
        pass


@pytest.fixture
def idle(monkeypatch, chain):
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: False)
    replies = {"lead": "Both implementors done; feature shipped.", "user-chat": "Your feature is done."}
    ran = []

    async def fake_headless(sess, messages, **kwargs):
        ran.append(sess.id)
        return replies[sess.id], []

    monkeypatch.setattr(headless, "run_headless", fake_headless)
    return ran


async def test_a_leads_final_reply_reaches_the_chat_that_started_it(idle, monkeypatch):
    monkeypatch.setattr(agent_control, "live_children", lambda sid: 0)
    user, lead, impl = _Chat("user-chat"), _Chat("lead"), _Chat("impl")
    lead.add_message(_Msg("user", "build the feature with two implementors"))

    await agent_control._hand_off(_Manager(user, lead, impl), "lead", impl, "part A", "A done", "completed", "u")

    assert idle == ["lead", "user-chat"]
    handed = [m for m in user.history if m.metadata.get("source") == "worker"]
    assert handed and "Both implementors done" in handed[-1].content


async def test_a_lead_with_implementors_still_running_does_not_report_yet(idle, monkeypatch):
    monkeypatch.setattr(agent_control, "live_children", lambda sid: 1 if sid == "lead" else 0)
    user, lead, impl = _Chat("user-chat"), _Chat("lead"), _Chat("impl")

    await agent_control._hand_off(_Manager(user, lead, impl), "lead", impl, "part A", "A done", "completed", "u")

    assert idle == ["lead"]
    assert not user.history
