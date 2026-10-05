"""A worker whose loadout grants delegate_to_claude_code gets it, bounded.

2026-09-28, production: the user told the admin chat "grant the preset
delegate_to_claude_code" for "Lead Engineer", a coding worker whose job is to
hand implementation to Claude Code. It could not happen:

- ``delegate_to_claude_code`` was in ``SUBAGENT_BLOCKED_TOOLS`` and withheld
  from every worker at every depth, so preflight read DEGRADED with the repair
  "drop it from enabled_tools" -- which the chat then did to the user's preset.
- The refused widening update was retried six times in a row, each time
  answered with the same long refusal, and "grant the preset ..." (no loadout
  name) could never count as the user's authorisation.
"""

import asyncio
import json
from pathlib import Path

import pytest

from src import agent_loadouts
from src import headless_agent as headless
from src.agent_tools import claude_code_tools as cct
from src.agent_tools import loadout_tools
from src.agent_tools.loadout_tools import manage_agent_loadout

CLAUDE = "delegate_to_claude_code"
GRANTED = {"tool_access": "selected", "enabled_tools": ["read_file", "manage_git", CLAUDE],
           "delegation_policy": "auto"}

# child -> parent links, as worker chats store them in `parent_session`.
CHAIN = {"lead": "user-chat", "impl": "lead"}


@pytest.fixture
def chain(monkeypatch):
    settings = {sid: {"parent_session": parent} for sid, parent in CHAIN.items()}
    settings["user-chat"] = {}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: dict(settings.get(sid, {})))
    return settings


# ── 1. who gets delegate_to_claude_code ─────────────────────────────────────

def test_a_first_level_worker_whose_loadout_names_it_gets_it(chain):
    assert CLAUDE not in headless.child_blocked_tools("lead", dict(GRANTED))
    # The rest of the hand-off tools stay off for it.
    blocked = headless.child_blocked_tools("lead", dict(GRANTED))
    assert {"send_to_session", "create_session", "delegate_to_agent", "pipeline", "manage_session"} <= blocked


def test_tool_access_all_does_not_grant_it(chain):
    assert CLAUDE in headless.child_blocked_tools("lead", {"tool_access": "all", "delegation_policy": "auto"})
    assert CLAUDE in headless.child_blocked_tools("lead", {"delegation_policy": "auto"})


def test_never_and_the_depth_limit_still_withhold_it(chain):
    assert CLAUDE in headless.child_blocked_tools("lead", dict(GRANTED, delegation_policy="never"))
    # impl is two hops down: the default limit.
    assert CLAUDE in headless.child_blocked_tools("impl", dict(GRANTED))


def test_a_parent_chat_denied_it_does_not_hand_it_on(chain):
    chain["user-chat"] = {"tool_access": "selected", "enabled_tools": ["read_file"]}
    assert CLAUDE in headless.child_blocked_tools("lead", dict(GRANTED))


def test_blocked_at_depth_rule():
    assert CLAUDE not in headless.blocked_at_depth(1, "explicit", tool_access="selected", enabled_tools=[CLAUDE])
    assert CLAUDE in headless.blocked_at_depth(1, "explicit")  # no allowlist given
    assert CLAUDE in headless.blocked_at_depth(1, "explicit", tool_access="all", enabled_tools=[CLAUDE])
    assert CLAUDE in headless.blocked_at_depth(2, "auto", tool_access="selected", enabled_tools=[CLAUDE])
    assert CLAUDE in headless.blocked_at_depth(1, "never", tool_access="selected", enabled_tools=[CLAUDE])
    # Naming the other launchers never releases them.
    other = ["delegate_to_agent", "send_to_session", "manage_session", "create_session", "pipeline"]
    assert set(other) <= headless.blocked_at_depth(1, "auto", tool_access="selected", enabled_tools=other)


async def test_run_headless_gives_the_granted_worker_the_tool(monkeypatch, chain):
    from src import agent_loop

    chain["lead"] = dict(GRANTED, parent_session="user-chat")
    seen = {}

    async def fake_loop(*_a, **kwargs):
        seen.update(kwargs)
        if False:
            yield ""

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())

    class _Sess:
        id, model, endpoint_url, owner, headers = "lead", "m", "http://x", "u", None

    await headless.run_headless(_Sess(), [], run_id=None)
    assert CLAUDE not in set(seen["disabled_tools"] or ())
    assert "send_to_session" in set(seen["disabled_tools"] or ())


# ── 2. readiness: READY when granted, DEGRADED with the exact repair ────────

KNOWN = {"bash", "read_file", "write_file", "grep", "manage_git", "manage_agent_worktree",
         "manage_agent_loadout", CLAUDE, "send_to_session", "web_search", "ask_user"}


def _policy(**overrides):
    base = {
        "allowed_tools": set(KNOWN), "known_tools": set(KNOWN),
        "skill_names": set(), "allowed_models": set(),
        "memory_access": "write", "skill_access": "all", "model_access": "all",
        "allowed_mcp_servers": ["*"], "private_vault_access": True,
        "delegation_policy": "auto", "max_parallel_workers": 2, "approval_mode": "auto",
        "tool_access": "all", "enabled_tools": [], "denied_tools": set(), "worker_depth": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def store(monkeypatch):
    saved = {"profiles": [], "policy": _policy()}
    monkeypatch.setattr(agent_loadouts, "_write", lambda profiles: saved.__setitem__("profiles", list(profiles)))
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: saved["policy"])
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr(agent_loadouts, "_mcp_state", lambda: ({}, set()))
    monkeypatch.setattr("src.retrieval_health.cached_problems", lambda: [])
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: {})
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: None)
    loadout_tools._WIDENING_REFUSALS.clear()
    yield saved
    loadout_tools._WIDENING_REFUSALS.clear()


def _stored(store, name="Lead Engineer"):
    return next(p for p in store["profiles"] if p["name"] == name)


async def _create(tools, **extra):
    body = {"action": "create", "name": "Lead Engineer", "tool_access": "selected", "enabled_tools": tools,
            **extra}
    result = await manage_agent_loadout(json.dumps(body), "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    return result


async def _preflight(**extra):
    return await manage_agent_loadout(json.dumps({"action": "preflight", "name": "Lead Engineer", **extra}),
                                      "chat-7", owner="u")


async def test_a_granted_preset_reads_ready(store):
    await _create(["read_file", "grep", "write_file", "manage_git", "manage_agent_worktree", CLAUDE],
                  delegation_policy="auto")
    result = await _preflight(required_tools=[CLAUDE])
    assert result["readiness"]["status"] == "READY", result["response"]
    assert CLAUDE in result["readiness"]["capabilities"]["selected_for_profile"]


async def test_delegation_never_reads_degraded_with_the_setting_to_change(store):
    await _create(["read_file", "manage_git", "manage_agent_worktree", CLAUDE], delegation_policy="never")
    result = await _preflight()
    readiness = result["readiness"]
    assert readiness["status"] == "DEGRADED"
    row = next(r for r in readiness["checks"] if r["check"] == f"tool {CLAUDE}")
    assert "delegation_policy" in row["detail"]
    assert "delegation_policy" in row["repair"] and "drop it" not in row["repair"]
    required = await _preflight(required_tools=[CLAUDE])
    assert required["readiness"]["status"] == "BLOCKED"


async def test_started_from_a_worker_at_the_limit_reads_degraded_with_the_depth_repair(store):
    await _create(["read_file", "manage_git", "manage_agent_worktree", CLAUDE], delegation_policy="auto")
    store["policy"] = _policy(worker_depth=1)  # the caller is itself a worker
    row = next(r for r in (await _preflight())["readiness"]["checks"] if r["check"] == f"tool {CLAUDE}")
    assert not row["ok"]
    assert "agent_max_worker_depth" in row["repair"]


async def test_a_caller_without_the_tool_reads_degraded(store):
    await _create(["read_file", "manage_git", "manage_agent_worktree", CLAUDE], delegation_policy="auto")
    store["policy"] = _policy(allowed_tools=KNOWN - {CLAUDE})
    row = next(r for r in (await _preflight())["readiness"]["checks"] if r["check"] == f"tool {CLAUDE}")
    assert not row["ok"] and "calling chat" in row["repair"]


async def test_a_zero_child_limit_reads_degraded(store):
    await _create(["read_file", "manage_git", "manage_agent_worktree", CLAUDE], delegation_policy="auto",
                  max_parallel_workers=0)
    row = next(r for r in (await _preflight())["readiness"]["checks"] if r["check"] == f"tool {CLAUDE}")
    assert not row["ok"] and "max_parallel_workers" in row["repair"]


# ── 3. the user's authorisation, as production phrased it ──────────────────

class _Chat:
    def __init__(self, history):
        self.id, self.owner, self.model = "chat-7", "u", "m"
        self.history = list(history)


def _chat(monkeypatch, *history):
    chat = _Chat(history)
    monkeypatch.setattr("src.ai_interaction.get_session_manager",
                        lambda: type("M", (), {"get_session": lambda self, sid: chat})())
    return chat


def _user(text, **meta):
    return {"role": "user", "content": text, "metadata": meta or None}


def _assistant(text, events=None):
    return {"role": "assistant", "content": text, "metadata": {"tool_events": events or []}}


def _update(tools):
    return json.dumps({"action": "update", "name": "Lead Engineer", "tool_access": "selected",
                       "enabled_tools": tools})


BASE_TOOLS = ["read_file", "grep", "write_file", "manage_agent_worktree"]


async def test_the_production_authorisation_message_permits_the_widening(store, monkeypatch):
    await _create(BASE_TOOLS)
    _chat(monkeypatch, _user("Giving you explicit authorization to add manage_git to the Lead Engineer preset. "
                             "Don't give preset specific repository, just all git repo guidelines inside that "
                             "development folder"))
    result = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git"]), "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    assert "manage_git" in _stored(store)["enabled_tools"]


async def test_naming_one_tool_is_not_consent_to_others(store, monkeypatch):
    await _create(BASE_TOOLS)
    _chat(monkeypatch, _user("Giving you explicit authorization to add manage_git to the Lead Engineer preset."))
    result = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git", "bash"]), "chat-7", owner="u")
    assert result["blocked_reason"] == "update_would_widen_loadout"
    assert result["not_named_by_user"] == ["bash"]
    assert "manage_git" not in _stored(store)["enabled_tools"]


async def test_grant_the_preset_resolves_to_the_loadout_just_discussed(store, monkeypatch):
    await _create(BASE_TOOLS + ["manage_git"], delegation_policy="auto")
    _chat(monkeypatch,
          _assistant("The Lead Engineer preset is only partly repaired: it has no delegate_to_claude_code."),
          _user("grant the preset delegate_to_claude_code"))
    result = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git", CLAUDE]), "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    assert CLAUDE in _stored(store)["enabled_tools"]
    assert result["readiness"]["status"] == "READY", result["response"]


async def test_the_preset_about_another_loadout_is_not_consent(store, monkeypatch):
    await _create(BASE_TOOLS)
    _chat(monkeypatch, _assistant("Odysseus Admin is ready."), _user("grant the preset delegate_to_claude_code"))
    result = await manage_agent_loadout(_update(BASE_TOOLS + [CLAUDE]), "chat-7", owner="u")
    assert result["blocked_reason"] == "update_would_widen_loadout"


async def test_yes_to_a_reply_naming_loadout_and_tool_is_consent(store, monkeypatch):
    await _create(BASE_TOOLS)
    _chat(monkeypatch, _assistant("May I permanently give Lead Engineer delegate_to_claude_code?"),
          _user("yes"))
    assert (await manage_agent_loadout(_update(BASE_TOOLS + [CLAUDE]), "chat-7", owner="u"))["exit_code"] == 0


# ── 4. a refused widening says what to do once, then stops ──────────────────

async def test_refusal_names_the_one_next_step(store, monkeypatch):
    await _create(BASE_TOOLS)
    _chat(monkeypatch, _user("make sure that agent is pulling from umni's main branch"))
    result = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git"]), "chat-7", owner="u")
    assert result["blocked_reason"] == "update_would_widen_loadout"
    assert "repeat" not in result
    assert "ask_user" in result["error"] and "After a yes, send the update once" in result["error"]
    assert "Lead Engineer" in result["next_action"]["ask_user"]
    assert "manage_git" in result["next_action"]["ask_user"]


async def test_the_same_widening_again_without_a_new_user_message_is_a_short_repeat(store, monkeypatch):
    await _create(BASE_TOOLS)
    chat = _chat(monkeypatch, _user("make sure that agent is pulling from umni's main branch"))
    first = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git"]), "chat-7", owner="u")
    # The model re-sends it with other arguments (production alternated between
    # an instructions rewrite and an enabled_tools edit).
    chat.history.append(_assistant("", [{"tool": "manage_agent_loadout", "output": first["error"]}]))
    second = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git", "bash"]), "chat-7", owner="u")
    assert second["repeat"] is True
    assert second["blocked_reason"] == "update_would_widen_loadout"
    assert len(second["error"]) < len(first["error"])
    assert "Do not send it again" in second["error"]
    third = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git"]), "chat-7", owner="u")
    assert third["repeat"] is True and third["progress_key"] == second["progress_key"]

    # The user answers (without authorising): the next refusal is a full one.
    chat.history.append(_user("what does that mean?"))
    fresh = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git"]), "chat-7", owner="u")
    assert "repeat" not in fresh


async def test_a_worker_is_told_to_report_not_to_ask(store, monkeypatch):
    await _create(BASE_TOOLS)
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid, **k: {"parent_session": "chat-7"} if sid == "w" else {})
    result = await manage_agent_loadout(_update(BASE_TOOLS + ["manage_git"]), "w", owner="u")
    assert result["blocked_reason"] == "update_would_widen_loadout"
    assert "chat that started you" in result["error"]


def test_the_loop_stops_a_tool_that_keeps_returning_repeat(monkeypatch):
    """The stall detector keys on the first 120 characters of a call; retries
    with different arguments slipped past it. A `repeat` result ends it."""
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    calls = []

    async def fake_exec(block, *a, **k):
        calls.append(block.content)
        result = {"error": "update: not saved", "blocked": True, "exit_code": 1}
        if len(calls) > 1:
            result.update(repeat=True, progress_key="widening-refused:lead engineer")
        return (block.tool_type, result)

    rounds = []

    async def fake_stream(_candidates, messages, **kwargs):
        rounds.append(1)
        # Different arguments every time, sharing no 120-character prefix
        # with the last, as the production retries did. (An ungated tool
        # stands in for manage_agent_loadout, which the loop's delegation and
        # admin gates would refuse before execution in this bare harness.)
        body = json.dumps({"plan": f"- [ ] attempt {len(rounds)} " + "x" * 200})
        text = f"```update_plan\n{body}\n```" if len(rounds) <= 3 else "Blocked: needs the user's approval."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def run():
        gen = al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "update the Lead Engineer loadout"}],
            max_rounds=20, relevant_tools={"bash"})
        return [c async for c in gen]

    chunks = asyncio.run(run())
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, [e.get("type") for e in events]
    assert "already refused" in guard["detail"]
    # First refusal, one repeat, then the loop stops before a third call.
    assert len(calls) == 2, calls


# ── 5. a worker's Claude Code run stays in its own repository ───────────────

def _git_repo(path: Path) -> Path:
    (path / ".git" / "worktrees").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


def _worktree(path: Path, repo: Path, name: str) -> Path:
    gitdir = repo / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "HEAD").write_text("ref: refs/heads/agent/x\n", encoding="utf-8")
    path.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return path


@pytest.fixture
def repos(tmp_path, monkeypatch):
    dev, trees = tmp_path / "development", tmp_path / "agent_worktrees"
    umni, other = _git_repo(dev / "umni"), _git_repo(dev / "other")
    mine = _worktree(trees / "umni-slice", umni, "umni-slice")
    theirs = _worktree(trees / "other-slice", other, "other-slice")
    monkeypatch.setattr(cct, "DEFAULT_ROOTS", (str(dev),))
    monkeypatch.setattr(cct, "DEFAULT_REPOSITORY", "")
    monkeypatch.setattr(cct, "_managed_worktree_root", lambda: trees.resolve())
    monkeypatch.setattr(cct, "_setting", lambda key, default=None: default)
    monkeypatch.setattr(cct, "_cloud_repositories", lambda: [])
    monkeypatch.delenv("ODYSSEUS_AGENT_SOURCE_REPO", raising=False)
    return {"umni": umni, "other": other, "mine": mine, "theirs": theirs}


@pytest.fixture
def worker_chat(monkeypatch, repos):
    settings = {"w": {"parent_session": "chat-7", "workspace": str(repos["umni"])}, "chat-7": {}}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: dict(settings.get(sid, {})))
    return settings


@pytest.fixture
def ran(monkeypatch):
    seen = []

    async def fake_run(repository, prompt, timeout, tools, on_process=None, model=None):
        seen.append(Path(repository))
        return {"exit_code": 0, "result": "ok", "repository": str(repository)}

    monkeypatch.setattr(cct, "_run_claude", fake_run)
    return seen


async def _delegate(session_id, **args):
    return await cct.ClaudeCodeTool().execute(json.dumps({"prompt": "implement the slice", **args}),
                                              {"session_id": session_id, "owner": "u"})


async def test_a_worker_run_defaults_to_its_workspace(repos, worker_chat, ran):
    result = await _delegate("w")
    assert result["exit_code"] == 0, result
    assert ran == [repos["umni"].resolve()]


async def test_a_worker_may_use_a_managed_worktree_of_its_repository(repos, worker_chat, ran):
    result = await _delegate("w", repository=str(repos["mine"]))
    assert result["exit_code"] == 0, result
    assert ran == [repos["mine"].resolve()]


async def test_a_worker_may_not_leave_its_repository(repos, worker_chat, ran):
    for target in (repos["other"], repos["theirs"]):
        result = await _delegate("w", repository=str(target))
        assert result["exit_code"] == 1
        assert result["blocked_reason"] == "worker_repository_outside_workspace", result
    assert ran == []


async def test_a_worker_without_a_workspace_is_refused(repos, worker_chat, ran):
    worker_chat["w"].pop("workspace")
    result = await _delegate("w")
    assert result["blocked_reason"] == "worker_no_workspace"
    assert ran == []


async def test_a_worker_may_not_use_the_cloud_runner_or_update_the_binary(repos, worker_chat, ran):
    assert (await _delegate("w", via="cloud"))["exit_code"] == 1
    update = await cct.ClaudeCodeTool().execute('{"action": "update"}', {"session_id": "w", "owner": "u"})
    assert update["exit_code"] == 1 and "worker" in update["error"]


async def test_a_persons_chat_is_not_scoped(repos, worker_chat, ran):
    result = await _delegate("chat-7", repository=str(repos["other"]))
    assert result["exit_code"] == 0, result
    assert ran == [repos["other"].resolve()]


async def test_a_worker_start_is_queued_against_its_workspace(repos, worker_chat, monkeypatch, tmp_path):
    runner = cct.ClaudeCodeTaskRunner(str(tmp_path / "tasks.json"))
    monkeypatch.setattr(cct, "get_task_runner", lambda: runner)
    started = asyncio.Event()

    async def fake_run(repository, prompt, timeout, tools, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(cct, "_run_with_auto_update", fake_run)
    result = await cct.ClaudeCodeTool().execute('{"action": "start", "prompt": "go"}',
                                                {"session_id": "w", "owner": "u"})
    try:
        assert result["exit_code"] == 0, result
        assert Path(result["repository"]) == repos["umni"].resolve()
        # The queued task counts as one of the worker's own children, which is
        # what tool_execution's capacity gate compares with its limit.
        from src import agent_control
        monkeypatch.setattr("src.agent_activity.list_runs", lambda **k: [])
        assert agent_control.live_children("w") == 1
        assert agent_control.live_children("chat-7") == 0
    finally:
        await runner.cancel(result["task_id"], owner="u")


def test_the_capacity_gate_covers_run_and_start_but_not_poll():
    from src.tool_execution import _capacity_limited_tool_call

    assert _capacity_limited_tool_call(CLAUDE, '{"prompt": "x"}')
    assert _capacity_limited_tool_call(CLAUDE, '{"action": "start", "prompt": "x"}')
    assert not _capacity_limited_tool_call(CLAUDE, '{"action": "poll", "task_id": "t"}')
