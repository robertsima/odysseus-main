"""A coding loadout's worker gets the repository tools its loadout grants.

2026-09-26: the admin chat started "Lead Engineer" to audit and improve RAG in
a repository checkout. The worker finished with "Blocked before
implementation ... this chat's tool policy denies `manage_git` and
`manage_agent_worktree`", while the parent chat had both and the loadout had
just been "repaired" to pass preflight. Three separate faults combined:

- ``manage_agent_worktree`` sat in ``SUBAGENT_BLOCKED_TOOLS``, the anti-fan-out
  set, although it starts no agent (agent_loop's own delegation-gate comment
  says so). Every worker lost it whatever its loadout granted.
- An ``update`` that added a tool to a stored selected-tools loadout was
  silently undone by the loadout's own stored ``disabled_tools`` — the
  complement snapshot written at create time, which lists every tool that was
  not granted then.
- ``preflight`` only checked the names the loadout already binds, so a loadout
  whose worker would lose a granted tool, or a file-writing loadout with no git
  tool at all, read READY.

Then the parent spent ~130k-token rounds calling ``status`` in a loop, after
first calling ``poll`` (not an action).
"""

import asyncio
import time

import pytest

from src import agent_loadouts
from src import headless_agent as headless
from src.agent_tools.loadout_tools import manage_agent_loadout


KNOWN = {"bash", "read_file", "write_file", "edit_file", "apply_patch", "grep", "manage_git",
         "manage_agent_worktree", "manage_agent_loadout", "send_to_session", "delegate_to_claude_code"}


def _policy(**overrides):
    base = {
        "allowed_tools": set(KNOWN), "known_tools": set(KNOWN),
        "skill_names": set(), "allowed_models": set(),
        "memory_access": "write", "skill_access": "all", "model_access": "all",
        "allowed_mcp_servers": ["*"], "private_vault_access": True,
        "delegation_policy": "auto", "max_parallel_workers": 2, "approval_mode": "auto",
        "tool_access": "all", "enabled_tools": [], "denied_tools": set(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def store(monkeypatch):
    saved = {"profiles": []}
    monkeypatch.setattr(agent_loadouts, "_write", lambda profiles: saved.__setitem__("profiles", list(profiles)))
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: _policy())
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr(agent_loadouts, "_mcp_state", lambda: ({}, set()))
    monkeypatch.setattr("src.retrieval_health.cached_problems", lambda: [])
    # The calling chat is a person's chat (depth 0), so what it starts is a
    # first-level worker.
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: {})
    return saved


def _stored(store, name="Lead Engineer"):
    return next(p for p in store["profiles"] if p["name"] == name)


# ── 1. a worker keeps the repository tools its loadout grants ────────────────

def test_manage_agent_worktree_is_not_treated_as_a_launcher(monkeypatch):
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid, **k: {"parent_session": "user-chat"} if sid == "lead" else {})
    blocked = headless.child_blocked_tools("lead", {"delegation_policy": "auto"})
    assert "manage_agent_worktree" not in blocked
    assert "manage_git" not in blocked
    # Every real launcher is still off for a worker.
    assert {"send_to_session", "create_session", "delegate_to_claude_code", "pipeline"} <= blocked


async def test_a_detached_worker_is_not_denied_the_worktree_tool(monkeypatch):
    from src import agent_loop

    seen = {}

    async def fake_loop(*_a, **kwargs):
        seen.update(kwargs)
        if False:
            yield ""

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid, **k: {"parent_session": "user-chat"} if sid == "lead" else {})
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())

    class _Sess:
        id, model, endpoint_url, owner, headers = "lead", "m", "http://x", "u", None

    await headless.run_headless(_Sess(), [], run_id=None)
    assert "manage_agent_worktree" not in set(seen["disabled_tools"] or ())


# ── 2. an update that grants a tool actually grants it ───────────────────────

class _Chat:
    def __init__(self, text):
        self.owner, self.model = "u", "m"
        self.history = [{"role": "user", "content": text, "metadata": None}]


async def test_update_adding_repo_tools_is_not_undone_by_the_stored_complement(store, monkeypatch):
    created = await manage_agent_loadout(
        '{"action": "create", "name": "Lead Engineer", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "grep", "write_file", "manage_agent_loadout"]}', "c", owner="u")
    assert created["exit_code"] == 0, created
    # The create stored a complement snapshot that names the git tools.
    assert "manage_git" in _stored(store)["disabled_tools"]

    # Adding tools to a saved loadout is the user's call: here they asked.
    chat = _Chat("Repair Lead Engineer: add manage_git and manage_agent_worktree")
    monkeypatch.setattr("src.ai_interaction.get_session_manager",
                        lambda: type("M", (), {"get_session": lambda self, sid: chat})())
    updated = await manage_agent_loadout(
        '{"action": "update", "name": "Lead Engineer", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "grep", "write_file", "manage_agent_loadout",'
        ' "manage_git", "manage_agent_worktree"]}', "c", owner="u")

    assert updated["exit_code"] == 0, updated
    stored = _stored(store)
    assert {"manage_git", "manage_agent_worktree"} <= set(stored["enabled_tools"])
    assert not ({"manage_git", "manage_agent_worktree"} & set(stored["disabled_tools"]))


def test_a_tool_both_enabled_and_disabled_is_reported_not_silently_dropped():
    profile, notes = agent_loadouts.clamp(
        {"name": "Lead", "tool_access": "selected", "enabled_tools": ["read_file", "manage_git"],
         "disabled_tools": ["manage_git"]}, _policy())
    assert "manage_git" not in profile["enabled_tools"]
    assert any("manage_git" in note and "disabled_tools" in note for note in notes)


# ── 3. preflight cannot pass while the worker would be crippled ──────────────

async def test_preflight_flags_a_file_writing_loadout_without_git(store):
    await manage_agent_loadout(
        '{"action": "create", "name": "Lead Engineer", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "grep", "write_file", "apply_patch"]}', "c", owner="u")
    result = await manage_agent_loadout('{"action": "preflight", "name": "Lead Engineer"}', "c", owner="u")
    readiness = result["readiness"]
    assert readiness["status"] == "DEGRADED"
    failed = {row["check"]: row for row in readiness["checks"] if not row["ok"]}
    assert "manage_git" in failed["repository tools"]["detail"]
    assert "manage_git" in failed["repository tools"]["repair"]


async def test_preflight_names_granted_tools_a_worker_cannot_have(store):
    await manage_agent_loadout(
        '{"action": "create", "name": "Lead Engineer", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "manage_git", "delegate_to_claude_code"]}', "c", owner="u")
    result = await manage_agent_loadout('{"action": "preflight", "name": "Lead Engineer"}', "c", owner="u")
    readiness = result["readiness"]
    assert readiness["status"] == "DEGRADED"
    denied = {row["tool"]: row for row in readiness["capabilities"]["denied"]}
    assert denied["delegate_to_claude_code"]["reason"] == "worker_policy"
    assert "manage_git" not in denied
    assert "delegate_to_claude_code" in result["response"]

    # Required and withheld from workers: BLOCKED, not READY.
    required = await manage_agent_loadout(
        '{"action": "preflight", "name": "Lead Engineer", "required_tools": ["delegate_to_claude_code"]}',
        "c", owner="u")
    assert required["readiness"]["status"] == "BLOCKED"


async def test_a_git_capable_coding_loadout_reads_ready(store):
    await manage_agent_loadout(
        '{"action": "create", "name": "Lead Engineer", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "grep", "write_file", "manage_git", "manage_agent_worktree"]}',
        "c", owner="u")
    result = await manage_agent_loadout('{"action": "preflight", "name": "Lead Engineer"}', "c", owner="u")
    assert result["readiness"]["status"] == "READY", result["response"]


async def test_start_warns_when_a_repository_task_gets_no_git_tools(monkeypatch, store):
    await manage_agent_loadout(
        '{"action": "create", "name": "Lead Engineer", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "grep", "write_file"]}', "c", owner="u")
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)

    async def fake_launch(**kwargs):
        return {"session_id": "w-1", "session_name": "Lead 1", "run_id": "session-1", "model": "m",
                "max_rounds": 0}

    monkeypatch.setattr("src.agent_control.launch_worker", fake_launch)
    result = await manage_agent_loadout(
        '{"action": "start", "name": "Lead Engineer",'
        ' "task": "Audit and improve RAG chunking in repository /app/data/development/odysseus-main"}',
        "c", owner="u")
    assert result["exit_code"] == 0
    assert "manage_git" in result["response"]
    assert "manage_git" in result["preflight"]["missing_repository_tools"]


# ── 4. poll is status, and status can wait ───────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_status_backoff():
    from src.agent_tools import loadout_tools

    loadout_tools._last_status_checks.clear()
    yield
    loadout_tools._last_status_checks.clear()


def test_poll_is_classified_as_a_read_like_status():
    from src.tool_capabilities import action_is_read

    for alias in ("poll", "wait", "check"):
        assert action_is_read("manage_agent_loadout", '{"action": "%s", "run_id": "x"}' % alias)


@pytest.fixture
def runs(monkeypatch, tmp_path):
    from src import agent_activity, constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_activity.run_started("w-1", "session", "Worker · Lead", run_id="session-6caedad9d9", owner="u",
                               data={"parent_session": "chat-7", "target_session": "w-1",
                                     "profile": "Lead Engineer"})
    agent_activity.run_started("w-2", "session", "Worker · Other", run_id="session-other", owner="u",
                               data={"parent_session": "chat-7", "target_session": "w-2"})
    yield agent_activity
    agent_activity._reset_for_tests()


@pytest.mark.parametrize("alias", ["poll", "wait", "check"])
async def test_poll_is_an_alias_of_status_and_run_id_narrows_it(store, runs, alias):
    result = await manage_agent_loadout(
        '{"action": "%s", "run_id": "session-6caedad9d9"}' % alias, "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    assert [row["run_id"] for row in result["runs"]] == ["session-6caedad9d9"]
    assert result["running"] == 1


async def test_status_for_an_unknown_run_id_says_so(store, runs):
    result = await manage_agent_loadout('{"action": "status", "run_id": "session-nope"}', "chat-7", owner="u")
    assert result["exit_code"] == 1
    assert "session-nope" in result["error"]


async def test_status_wait_returns_as_soon_as_the_run_finishes(store, runs):
    async def finish_soon():
        await asyncio.sleep(0.3)
        runs.run_finished("w-1", "session", "session-6caedad9d9", "Worker · Lead completed",
                          status="completed", owner="u", data={"result_excerpt": "done"})

    finisher = asyncio.ensure_future(finish_soon())
    started = time.monotonic()
    result = await manage_agent_loadout(
        '{"action": "status", "run_id": "session-6caedad9d9", "wait": 20}', "chat-7", owner="u")
    await finisher
    assert time.monotonic() - started < 5
    assert result["runs"][0]["status"] == "completed"
    assert result["running"] == 0
    assert result["waited_seconds"] >= 0


async def test_status_wait_is_bounded_and_says_the_run_is_still_going(store, runs, monkeypatch):
    from src.agent_tools import loadout_tools

    monkeypatch.setattr(loadout_tools, "_STATUS_WAIT_POLL_SECONDS", 0.05)
    started = time.monotonic()
    result = await manage_agent_loadout(
        '{"action": "poll", "run_id": "session-6caedad9d9", "wait_seconds": 0.3}', "chat-7", owner="u")
    assert time.monotonic() - started < 3
    assert result["running"] == 1
    assert "still running" in result["response"]
    # The worker's result arrives by itself; the parent is told not to spin.
    assert "hand" in result["response"].lower()


async def test_re_checking_a_running_worker_backs_off_instead_of_spinning(store, runs, monkeypatch):
    """The 2026-09-26 parent re-ran status every round while its worker ran."""
    from src.agent_tools import loadout_tools

    monkeypatch.setattr(loadout_tools, "_STATUS_BACKOFF_START_S", 0.2)
    monkeypatch.setattr(loadout_tools, "_STATUS_WAIT_POLL_SECONDS", 0.05)
    body = '{"action": "status", "run_id": "session-6caedad9d9"}'
    first = await manage_agent_loadout(body, "chat-7", owner="u")
    assert "waited_seconds" not in first
    second = await manage_agent_loadout(body, "chat-7", owner="u")
    assert second["waited_seconds"] >= 0.2
    assert "do not keep checking" in second["response"]
    # Nothing new happened, so the loop's stall detector sees the same state.
    assert first["progress_key"] == second["progress_key"]
