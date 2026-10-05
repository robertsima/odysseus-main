"""A worker's child never holds more than the worker that started it.

2026-10-02: a Lead Engineer worker (52 selected tools, no delegation launchers)
called manage_agent_loadout start with an empty name, and the child came up with
`tool_access: all` (bash, bulk_email, delegate_to_agent ...). These tests pin the
cap in `agent_loadouts.cap_to_starter`, the start tool that reports it, and the
shell-less refusal that must still fire for a worker told to run commands.
"""

import json

import pytest

from src import agent_loadouts
from src import worker_preflight as wp
from src.agent_tools.loadout_tools import manage_agent_loadout

KNOWN = {"bash", "python", "read_file", "grep", "ls", "write_file", "apply_patch", "web_search",
         "bulk_email", "archive_email", "delegate_to_agent", "create_session", "orchestrate_agents"}


def caller(**overrides):
    """A bounded worker's policy: read-and-search tools only."""
    allowed = {"read_file", "grep", "ls", "web_search"}
    base = {
        "allowed_tools": set(allowed), "known_tools": set(KNOWN), "tool_access": "selected",
        "enabled_tools": sorted(allowed), "denied_tools": KNOWN - allowed,
        "skill_names": set(), "allowed_models": set(), "memory_access": "read",
        "skill_access": "all", "model_access": "current", "allowed_mcp_servers": ["*"],
        "private_vault_access": False, "shell_access": "sandbox", "delegation_policy": "explicit",
        "max_parallel_workers": 2, "approval_mode": "ask_risky", "worker_depth": 1,
    }
    base.update(overrides)
    return base


@pytest.fixture
def world(monkeypatch):
    """Chat 'worker' is a worker (it has a parent); chat 'person' is not."""
    state = {"policy": caller(), "launched": []}
    settings = {"worker": {"parent_session": "person"}, "person": {}}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: settings.get(sid, {}))
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: state["policy"])
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: [])

    async def fake_launch(**kwargs):
        state["launched"].append(kwargs)
        return {"session_id": "w-1", "session_name": "Sub-agent 1", "run_id": "r-1", "model": "m",
                "max_rounds": 0}

    monkeypatch.setattr("src.agent_control.launch_worker", fake_launch)
    return state


def _tools(profile):
    return set(profile["enabled_tools"])


async def start(world, session, **fields):
    body = {"action": "start", "task": "Look at the code", **fields}
    return await manage_agent_loadout(json.dumps(body), session, owner="u")


# ── the cap itself ───────────────────────────────────────────────────────────

async def test_a_bounded_workers_nameless_child_gets_at_most_the_workers_tools(world):
    result = await start(world, "worker")
    assert result["exit_code"] == 0
    child = world["launched"][0]["inline_profile"]
    assert child["tool_access"] == "selected"
    assert _tools(child) <= world["policy"]["allowed_tools"]
    assert not _tools(child) & {"bash", "bulk_email", "delegate_to_agent", "create_session"}
    assert child["delegation_policy"] == "explicit" and child["private_vault_access"] is False
    assert child["approval_mode"] == "ask_risky"  # never looser than the starter


async def test_the_start_result_names_the_policy_the_child_received(world):
    result = await start(world, "worker")
    assert "no loadout named" in result["policy"] and "never has more than you" in result["policy"]
    assert result["policy"] in result["response"]
    assert isinstance(result["preflight"]["tools"], list)  # a list, not the string "all"


async def test_a_parent_without_bulk_email_cannot_give_it_to_a_child_even_when_asked(world):
    result = await start(world, "worker", tool_access="selected",
                         enabled_tools=["read_file", "bulk_email", "archive_email"])
    assert result["exit_code"] == 0
    child = world["launched"][0]["inline_profile"]
    assert _tools(child) == {"read_file"}


async def test_asking_only_for_tools_the_parent_lacks_is_refused_not_widened(world):
    result = await start(world, "worker", tool_access="selected", enabled_tools=["bash", "bulk_email"])
    assert result["exit_code"] == 1 and result["blocked_reason"] == "tools_beyond_starter"
    assert not world["launched"]


async def test_tool_access_all_in_the_call_still_means_all_the_starter_has(world):
    await start(world, "worker", tool_access="all")
    child = world["launched"][0]["inline_profile"]
    assert child["tool_access"] == "selected" and "bash" not in _tools(child)


def test_a_starter_with_everything_passes_everything_but_what_it_is_denied(world):
    open_policy = caller(tool_access="all", enabled_tools=[], allowed_tools=set(KNOWN) - {"bulk_email"},
                         denied_tools={"bulk_email"}, shell_access="host", private_vault_access=True,
                         memory_access="write", model_access="all", delegation_policy="auto")
    capped, _ = agent_loadouts.cap_to_starter(None, "worker", "u", policy=open_policy)
    assert capped["tool_access"] == "all" and "bulk_email" in capped["disabled_tools"]
    # The vault is not inherited by a nameless child: it never had it.
    assert capped["private_vault_access"] is False
    assert capped["shell_access"] == "sandbox"


def test_a_named_loadout_is_capped_too(world):
    loadout = {"name": "Big", "tool_access": "all", "private_vault_access": True,
               "shell_access": "host", "delegation_policy": "auto", "max_parallel_workers": 8,
               "memory_access": "write", "model": "gpt-x", "model_access": "all"}
    capped, notes = agent_loadouts.cap_to_starter(loadout, "worker", "u", policy=world["policy"])
    assert _tools(capped) == {"read_file", "grep", "ls", "web_search"}
    assert capped["private_vault_access"] is False and capped["shell_access"] == "sandbox"
    assert capped["delegation_policy"] == "explicit" and capped["max_parallel_workers"] == 2
    assert capped["memory_access"] == "read" and capped["model_access"] == "current"
    assert capped["model"] == "gpt-x"  # the loadout author's model choice survives
    assert notes


def test_mcp_servers_are_cut_to_the_starters(world):
    narrow = caller(allowed_mcp_servers=["email"])
    capped, _ = agent_loadouts.cap_to_starter(
        {"name": "Mcp", "mcp_access": "selected", "allowed_mcp_servers": ["email", "github"]},
        "worker", "u", policy=narrow)
    assert capped["allowed_mcp_servers"] == ["email"]


async def test_a_persons_chat_keeps_its_named_loadouts_but_caps_a_nameless_child(world):
    """A named loadout is the person's own choice; a nameless child has no
    loadout to stand on, so it gets the chat's limits instead of "all"."""
    capped, notes = agent_loadouts.cap_to_starter({"name": "Big", "tool_access": "all"}, "person", "u",
                                                  policy=world["policy"])
    assert capped == {"name": "Big", "tool_access": "all"} and notes == []

    result = await start(world, "person")
    assert result["exit_code"] == 0
    child = world["launched"][0]["inline_profile"]
    assert child["tool_access"] == "selected"
    assert _tools(child) <= set(world["policy"]["allowed_tools"])


# ── read-only wording ────────────────────────────────────────────────────────

async def test_a_read_only_task_gets_the_read_only_subset_of_the_starters_tools(world):
    world["policy"] = caller(allowed_tools={"read_file", "grep", "write_file", "web_search"},
                             enabled_tools=["read_file", "grep", "write_file", "web_search"])
    result = await start(world, "worker", task="Independent design critic ONLY (read-only): review the screenshots")
    child = world["launched"][0]["inline_profile"]
    assert "write_file" not in _tools(child) and {"read_file", "grep"} <= _tools(child)
    assert "read-only subset" in result["policy"]


async def test_read_only_wording_does_not_narrow_a_task_that_runs_commands(world):
    world["policy"] = caller(allowed_tools={"read_file", "write_file"}, enabled_tools=["read_file", "write_file"])
    result = await start(world, "worker", task="Read-only review, but run the tests with pytest -q first")
    child = world["launched"][0]["inline_profile"]
    assert "write_file" in _tools(child)  # not read-only: the starter's own set, uncut
    assert "read-only subset" not in result["policy"]


# ── widening by a worker's own say-so ────────────────────────────────────────

async def test_updating_a_loadout_from_a_worker_cannot_reach_past_the_worker(monkeypatch, world):
    saved = {"profiles": [{"name": "Helper", "tool_access": "selected", "enabled_tools": ["read_file"]}]}
    monkeypatch.setattr(agent_loadouts, "_write", lambda p: saved.__setitem__("profiles", list(p)))
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    # The module the tool function lives in, not a dotted string: another test
    # file swaps `src.agent_tools` in sys.modules, and under xdist the string
    # then resolves to the wrong object.
    import sys

    monkeypatch.setattr(sys.modules[manage_agent_loadout.__module__], "_widening_authorization",
                        lambda *a, **k: type("A", (), {"ok": True, "worker": False, "unnamed": []})())
    await manage_agent_loadout(json.dumps({
        "action": "update", "name": "Helper", "tool_access": "selected",
        "enabled_tools": ["read_file", "bash", "bulk_email"]}), "worker", owner="u")
    stored = saved["profiles"][0]
    assert "bash" not in stored["enabled_tools"] and "bulk_email" not in stored["enabled_tools"]


# ── the critic loadout and the shell-less refusal ────────────────────────────

@pytest.fixture
def checkout(tmp_path, monkeypatch):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(tool_security, "owner_baseline_disabled_tools", lambda owner: set())
    repo = tmp_path / "dev" / "odysseus-main"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(wp, "known_checkouts", lambda: [str(repo.resolve())])
    return str(repo.resolve())


_NO_SHELL = {"bash", "python", "manage_git"}


@pytest.mark.parametrize("task", [
    "Independent UI Design Critic ONLY (read-only). Read the worktree files and the screenshots. "
    "Do not edit files or run builds.",
    "Read-only review of static/js/theme.js and the screenshots; no edits, and do not edit or run tests or builds.",
    "Review the worktree. Running builds is not needed; you may not run commands.",
])
def test_a_shell_less_reviewer_is_not_refused_a_workspace_review_that_forbids_commands(checkout, task):
    pf = wp.run_preflight(task, explicit_workspace=checkout, unavailable_tools=_NO_SHELL,
                          loadout_name="UI Design Critic")
    assert pf.ok, pf.blocked_payload() if not pf.ok else None


@pytest.mark.parametrize("task", [
    "Review the worktree and run the tests.",
    "Read-only review. Then run builds and report errors.",
    "Do not edit anything, run the Agamemnon theme tests and report.",
])
def test_a_shell_less_worker_told_to_run_commands_is_still_refused(checkout, task):
    pf = wp.run_preflight(task, explicit_workspace=checkout, unavailable_tools=_NO_SHELL,
                          loadout_name="UI Design Critic")
    assert pf.blocked_payload()["code"] == "SHELL_NOT_AVAILABLE"


async def test_the_critic_loadout_starts_with_a_workspace_and_a_read_only_review_task(monkeypatch, checkout):
    critic = {"name": "UI Design Critic", "tool_access": "selected",
              "enabled_tools": ["read_file", "grep", "ls", "glob"]}
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: [critic])
    monkeypatch.setattr("src.agent_profiles.get_profile", lambda n: critic if n.casefold() == "ui design critic" else None)
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: caller(
        allowed_tools={"read_file", "grep", "ls", "glob", "bash"}, tool_access="all"))
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: {})
    seen = {}

    async def preflight_only(**kwargs):
        # The real preflight, then stop: the rest of launch_worker needs a session manager.
        unavailable = wp.worker_unavailable_tools("u", critic)
        pf = wp.run_preflight(kwargs["task"], explicit_workspace=kwargs["workspace"],
                              unavailable_tools=unavailable, loadout_name=critic["name"])
        seen["pf"] = pf
        return {"session_id": "w-1", "session_name": "Critic", "run_id": "r-1", "model": "m", "max_rounds": 0}

    monkeypatch.setattr("src.agent_control.launch_worker", preflight_only)
    result = await manage_agent_loadout(json.dumps({
        "action": "start", "name": "UI Design Critic", "workspace": checkout,
        "task": "Read-only review of the Agamemnon worktree: read the files and screenshots, "
                "report contrast and layout problems. Do not edit files or run builds."}), "person", owner="u")
    assert result["exit_code"] == 0
    assert seen["pf"].ok and seen["pf"].workspace == checkout
