"""Workers are checked before they start: workspace, file tools, write access."""

import json
from pathlib import Path

import pytest

from src import worker_preflight as wp


@pytest.fixture(autouse=True)
def admin_owner(monkeypatch):
    # Pinned: owner_is_admin_or_single_user fails closed without an auth
    # manager, and a non-admin's baseline would hide the file tools.
    import src.tool_security as tool_security
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    # The machine's own global disabled-tools setting is not what these test.
    monkeypatch.setattr(tool_security, "owner_baseline_disabled_tools", lambda owner: set())


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    """Two real checkouts outside DATA_DIR, reported by get_workspace's discovery."""
    paths = []
    for name in ("odysseus-main", "other-repo"):
        repo = tmp_path / "dev" / name
        (repo / ".git").mkdir(parents=True)
        paths.append(str(repo.resolve()))
    monkeypatch.setattr(wp, "known_checkouts", lambda: list(paths))
    return paths


def test_repository_task_without_workspace_is_refused_with_the_choices(checkouts):
    pf = wp.run_preflight("Run the test suite and summarise failures", inherited_workspace=None)
    assert not pf.ok
    payload = pf.blocked_payload()
    assert payload["status"] == "blocked" and payload["code"] == "WORKSPACE_REQUIRED"
    assert payload["problems"][0]["candidates"] == checkouts
    assert payload["next_action"]["retry_with"] == {"workspace": checkouts[0]}
    assert checkouts[0] in payload["error"]


def test_a_checkout_the_task_names_is_bound(checkouts):
    pf = wp.run_preflight("Read-only audit of the repository in odysseus-main; do not edit files")
    assert pf.ok and pf.workspace == checkouts[0]
    assert pf.workspace_source == "named in the task"
    assert set(wp.READ_TOOLS) <= pf.forced_tools
    assert not (set(wp.WRITE_TOOLS) & pf.forced_tools)


def test_the_only_checkout_is_bound(checkouts, monkeypatch):
    monkeypatch.setattr(wp, "known_checkouts", lambda: [checkouts[1]])
    pf = wp.run_preflight("grep the codebase for TODOs")
    assert pf.ok and pf.workspace == checkouts[1] and pf.workspace_source == "only checkout"


def test_the_parent_chats_workspace_is_inherited(checkouts):
    pf = wp.run_preflight("Fix the bug in src/app.py", inherited_workspace=checkouts[1])
    assert pf.ok and pf.workspace == checkouts[1] and pf.workspace_source == "parent chat"
    assert pf.needs_write and set(wp.WRITE_TOOLS) <= pf.forced_tools


def test_an_explicit_bad_workspace_is_refused(checkouts, tmp_path):
    pf = wp.run_preflight("Summarise the repo", explicit_workspace=str(tmp_path / "missing"))
    assert pf.blocked_payload()["code"] == "WORKSPACE_INVALID"


def test_repository_work_needs_the_read_tools(checkouts):
    pf = wp.run_preflight("Review the repository", explicit_workspace=checkouts[0],
                          unavailable_tools={"read_file", "grep"})
    assert pf.blocked_payload()["code"] == "TOOLS_UNAVAILABLE"
    assert pf.blocked_payload()["next_action"] == {"enable_tools": ["read_file", "grep"]}


def test_a_change_request_needs_a_write_tool(checkouts):
    pf = wp.run_preflight("Implement the new handler in the config module", explicit_workspace=checkouts[0],
                          unavailable_tools=set(wp.WRITE_TOOLS))
    assert pf.blocked_payload()["code"] == "WRITE_NOT_ALLOWED"


def test_read_only_wording_or_requirement_wins(checkouts):
    pf = wp.run_preflight("Fix nothing: review the code only, do not edit files",
                          explicit_workspace=checkouts[0], unavailable_tools=set(wp.WRITE_TOOLS))
    assert pf.ok and not pf.needs_write
    pf = wp.run_preflight("Update the tests", explicit_workspace=checkouts[0],
                          unavailable_tools=set(wp.WRITE_TOOLS), requires=["read_only"])
    assert pf.ok


def test_explicit_requirements_and_unknown_ones(checkouts):
    assert wp.run_preflight("Summarise news", requires=["workspace"]).blocked_payload()["code"] == "WORKSPACE_REQUIRED"
    assert wp.run_preflight("x", requires=["teleport"]).blocked_payload()["code"] == "UNKNOWN_REQUIREMENT"


def test_a_task_with_no_repository_work_starts_with_a_warning(checkouts):
    pf = wp.run_preflight("Summarise today's AI news")
    assert pf.ok and pf.workspace is None and not pf.forced_tools
    assert pf.warnings


def test_loadout_allowlist_counts_as_unavailable():
    profile = {"tool_access": "selected", "enabled_tools": ["read_file", "grep", "ls"], "disabled_tools": ["bash"]}
    unavailable = wp.worker_unavailable_tools(None, profile)
    assert "write_file" in unavailable and "bash" in unavailable and "read_file" not in unavailable


# ── the file tools admit repository roots inside DATA_DIR ─────────────────

def test_repository_roots_inside_data_dir_are_usable(tmp_path, monkeypatch):
    from src import constants
    import src.tool_execution as te
    from src.agent_worktree import repository_sync

    data = tmp_path / "data"
    repo = data / "development" / "odysseus-main"
    (repo / ".git").mkdir(parents=True)
    (repo / "app.py").write_text("print('hi')\n")
    (data / "settings.json").write_text("{}")
    (data / "agent_worktree").mkdir()
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setattr(repository_sync, "git_repository_roots", lambda: (data / "development",))
    te._REPO_SUBDIRS_CACHE.clear()

    assert te.vet_workspace(str(repo)) == str(repo.resolve())
    assert not te._is_app_state_path(str((repo / "app.py").resolve()))
    # Everything else in DATA_DIR stays refused.
    assert te._is_app_state_path(str((data / "settings.json").resolve()))
    te._REPO_SUBDIRS_CACHE.clear()


def test_a_root_that_is_data_dir_itself_is_not_admitted(tmp_path, monkeypatch):
    from src import constants
    import src.tool_execution as te
    from src.agent_worktree import repository_sync

    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("{}")
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setattr(repository_sync, "git_repository_roots", lambda: (data,))
    te._REPO_SUBDIRS_CACHE.clear()
    assert te._is_app_state_path(str((data / "settings.json").resolve()))
    assert te.vet_workspace(str(data)) is None
    te._REPO_SUBDIRS_CACHE.clear()


# ── send_to_session runs the check ───────────────────────────────────────

class _Session:
    def __init__(self, sid, owner="alice"):
        self.id, self.name, self.owner = sid, "Parent", owner
        self.endpoint_url, self.model, self.headers, self.context_length = "http://llm.test/v1", "m", {}, 0
        self.history = []

    def get_context_messages(self):
        return []

    def add_message(self, msg):
        self.history.append(msg)


class _Manager:
    def __init__(self):
        self.sessions = {"parent-1": _Session("parent-1")}
        self.created = []

    def get_session(self, sid):
        return self.sessions.get(sid)

    def create_session(self, session_id, name, endpoint_url, model, rag, owner):
        sess = _Session(session_id, owner)
        self.sessions[session_id] = sess
        self.created.append(session_id)
        return sess

    def save_sessions(self):
        pass


@pytest.fixture
def send_env(tmp_path, monkeypatch, checkouts):
    from src import agent_activity as act
    from src import constants
    from src.agent_tools import session_tools as st

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "state"))
    act._reset_for_tests()
    mgr = _Manager()
    monkeypatch.setattr(st, "get_session_manager", lambda: mgr)
    monkeypatch.setattr(st, "_caller_workspace", lambda sid: None)
    yield st, mgr, act
    act._reset_for_tests()


async def test_send_to_session_refuses_before_creating_a_chat(send_env):
    st, mgr, act = send_env
    out = await st.send_to_session(json.dumps({"session_id": "new", "message": "Run pytest on the repo"}),
                                   session_id="parent-1", owner="alice")
    assert out["status"] == "blocked" and out["code"] == "WORKSPACE_REQUIRED"
    assert mgr.created == []
    run = act.list_runs(session_id="parent-1")[0]
    assert run["status"] == "blocked"


async def test_send_to_session_hands_the_worker_its_workspace_and_tools(send_env, monkeypatch, checkouts):
    st, mgr, act = send_env
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as al
    monkeypatch.setattr(al, "stream_agent_loop", fake_loop)
    out = await st.send_to_session(
        json.dumps({"session_id": "new", "message": "Read-only audit of the repository, do not edit",
                    "workspace": checkouts[1]}),
        session_id="parent-1", owner="alice")
    assert out["preflight"]["workspace"] == checkouts[1]
    assert seen["workspace"] == checkouts[1]
    assert set(wp.READ_TOOLS) <= seen["forced_tools"]
