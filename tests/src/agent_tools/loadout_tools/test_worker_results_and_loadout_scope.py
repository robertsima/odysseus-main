"""A parent gets its finished workers' whole results, and one task does not
rewrite a saved loadout.

2026-09-28, an admin chat running a two-worker research task:

1. It could not get a finished worker's full result mid-turn (status gave a
   400-char excerpt; the 12k hand-off was not in the running turn), so it
   asked both workers to repeat themselves with send_to_session, and they
   regenerated ~17k chars from memory, without tools, for minutes.
2. Preflight read "repositories" in a web-research task as repository work
   and refused a web_search-only worker for lacking read_file/grep/ls.
3. The parent then widened the saved "Memory Data Structure Innovator"
   loadout from [web_search] to eleven tools, for good, without the user.
4. A worker ran gpt-6-sol although its loadout allowed only gpt-5.6-sol.
5. A web-only worker was told to "reconcile against the AI Mind note".
6. The blocked run's status row named the PARENT chat as its worker_session.
"""

import json

import pytest

from src import agent_loadouts
from src import worker_preflight as wp
from src.agent_tools import loadout_tools
from src.agent_tools.loadout_tools import manage_agent_loadout


KNOWN = {"bash", "read_file", "write_file", "grep", "ls", "glob", "get_workspace", "web_search",
         "search_documents", "manage_memory", "search_chats", "manage_agent_loadout", "send_to_session"}


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


class _Chat:
    def __init__(self, sid="chat-7", owner="u", model="gpt-6-sol", history=None):
        self.id, self.name, self.owner, self.model = sid, sid, owner, model
        self.endpoint_url, self.headers, self.context_length = "http://llm.test/v1", {}, 0
        self.history = list(history or [])

    def get_context_messages(self):
        return []

    def add_message(self, msg):
        self.history.append(msg)


class _Manager:
    def __init__(self, *chats):
        self.sessions = {c.id: c for c in chats}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


def _use_manager(monkeypatch, *chats):
    manager = _Manager(*chats)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: manager)
    return manager


@pytest.fixture
def store(monkeypatch):
    saved = {"profiles": []}
    monkeypatch.setattr(agent_loadouts, "_write", lambda profiles: saved.__setitem__("profiles", list(profiles)))
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: _policy())
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr(agent_loadouts, "_mcp_state", lambda: ({}, set()))
    monkeypatch.setattr("src.retrieval_health.cached_problems", lambda: [])
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **k: {})
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: None)
    return saved


def _stored(store, name):
    return next(p for p in store["profiles"] if p["name"] == name)


async def _create(name, tools, **extra):
    body = {"action": "create", "name": name, "tool_access": "selected", "enabled_tools": tools, **extra}
    result = await manage_agent_loadout(json.dumps(body), "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    return result


@pytest.fixture
def launched(monkeypatch):
    calls = []

    async def fake_launch(**kwargs):
        calls.append(kwargs)
        profile = kwargs.get("inline_profile") or {}
        return {"session_id": "w-1", "session_name": "W", "run_id": "session-0000000001",
                "model": kwargs.get("model") or profile.get("model") or "gpt-5.6-sol", "max_rounds": 14}

    monkeypatch.setattr("src.agent_control.launch_worker", fake_launch)
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)
    return calls


# ── 1. a finished worker's whole result is one recall away ───────────────────

@pytest.fixture
def activity(monkeypatch, tmp_path):
    from src import agent_activity, constants, tool_output_store

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(tool_output_store, "_index", lambda *a, **k: 0)
    agent_activity._reset_for_tests()
    loadout_tools._RESULT_REFS.clear()
    loadout_tools._last_status_checks.clear()
    yield agent_activity
    agent_activity._reset_for_tests()
    loadout_tools._RESULT_REFS.clear()


def _finished_worker(activity, text, *, run_id="session-aaaaaaaaaa", worker="w-1", status="completed"):
    activity.run_started(worker, "session", "Worker · Innovator", run_id=run_id, owner="u",
                         data={"parent_session": "chat-7", "target_session": worker,
                               "profile": "Memory Data Structure Innovator"})
    activity.run_finished(worker, "session", run_id, f"Worker · Innovator {status}", status=status, owner="u",
                          data={"target_session": worker, "result_excerpt": text[:400]})
    return _Chat(worker, history=[
        {"role": "user", "content": "research", "metadata": {"source": "dashboard"}},
        {"role": "assistant", "content": text, "metadata": {"run_id": run_id, "status": status}},
    ])


async def test_status_of_a_finished_run_returns_its_whole_result_as_a_ref(store, activity, monkeypatch):
    from src import tool_output_store

    text = "FINDINGS " + ("x" * 25_000) + " END-OF-RESULT"  # past collect_worker_result's 20k cap
    _use_manager(monkeypatch, _finished_worker(activity, text), _Chat("chat-7"))

    result = await manage_agent_loadout('{"action": "status", "run_id": "session-aaaaaaaaaa"}',
                                        "chat-7", owner="u")

    row = result["runs"][0]
    assert row["result_excerpt"].startswith("FINDINGS")  # the short excerpt stays
    assert row["result_chars"] == len(text)
    stored = tool_output_store.load(row["result_ref"])
    assert stored.endswith(text) and "END-OF-RESULT" in stored
    assert row["read_result"] == f'recall_tool_output {{"ref": "{row["result_ref"]}"}}'
    assert "recall_tool_output" in result["response"]
    assert "Do not send_to_session the worker" in result["response"]
    assert tool_output_store.load_record(row["result_ref"])["session_id"] == "chat-7"

    # Checking again (or by loadout name) hands back the same stored copy.
    again = await manage_agent_loadout(
        '{"action": "status", "name": "Memory Data Structure Innovator"}', "chat-7", owner="u")
    assert again["runs"][0]["result_ref"] == row["result_ref"]


async def test_recall_reads_the_stored_worker_result(store, activity, monkeypatch):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    _use_manager(monkeypatch, _finished_worker(activity, "The design is a two-level log-structured index."),
                 _Chat("chat-7"))
    result = await manage_agent_loadout('{"action": "status"}', "chat-7", owner="u")
    ref = result["runs"][0]["result_ref"]
    recalled = await RecallToolOutputTool().execute(json.dumps({"ref": ref}), {"session_id": "chat-7"})
    assert "two-level log-structured index" in recalled["results"]


async def test_a_running_worker_gets_no_result_ref(store, activity, monkeypatch):
    activity.run_started("w-2", "session", "Worker · Researcher", run_id="session-bbbbbbbbbb", owner="u",
                         data={"parent_session": "chat-7", "target_session": "w-2"})
    result = await manage_agent_loadout('{"action": "status"}', "chat-7", owner="u")
    assert "result_ref" not in result["runs"][0]
    assert "recall_tool_output" not in result["response"]


async def test_another_owners_worker_result_is_not_stored(store, activity, monkeypatch):
    worker = _finished_worker(activity, "secret findings")
    worker.owner = "someone-else"
    _use_manager(monkeypatch, worker, _Chat("chat-7"))
    assert loadout_tools.stored_worker_result("session-aaaaaaaaaa", owner="u", caller_session="chat-7") is None


async def test_send_to_session_to_a_finished_worker_names_its_stored_result(store, activity, monkeypatch):
    from src.agent_tools import session_tools as st

    worker = _finished_worker(activity, "Original result: B-tree of episodes, ranked by recency.")
    manager = _use_manager(monkeypatch, worker, _Chat("chat-7"))
    monkeypatch.setattr(st, "get_session_manager", lambda: manager)
    monkeypatch.setattr("routes.chat_helpers.resolve_session_auth", lambda *a, **k: None, raising=False)

    async def fake_llm(url, model, messages, **kwargs):
        return "From memory: something about B-trees."

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)

    out = await st.send_to_session(json.dumps({"session_id": "w-1", "message": "Resend your full result"}),
                                   session_id="chat-7", owner="u")

    # Not blocked: the message still went, and the reply is there.
    assert out["response"] == "From memory: something about B-trees."
    hint = out["stored_worker_result"]
    assert hint["run_id"] == "session-aaaaaaaaaa"
    assert "recall_tool_output" in out["note"] and hint["ref"] in out["note"]


# ── 2. repositories on the web are research, not a workspace ─────────────────

WEB_TASK = ("Research agent memory data structures. Use recent technical sources, repositories, issue "
            "trackers and papers; compare their trade-offs and report the three most promising designs.")


def test_a_web_research_task_about_repositories_does_not_need_a_workspace(tmp_path):
    assert not wp.task_needs_workspace(WEB_TASK)
    assert not wp.task_needs_workspace("Survey GitHub repositories and repos on GitHub for vector stores")
    assert not wp.task_needs_workspace("Read the GitHub repository of Letta and summarise its memory design")
    assert not wp.task_needs_write("Write a report comparing the GitHub repositories")
    # The parent chat had a workspace, which made the old match a hard block.
    pf = wp.run_preflight(WEB_TASK, inherited_workspace=str(tmp_path),
                          unavailable_tools=set(wp.READ_TOOLS) | set(wp.WRITE_TOOLS))
    assert pf.ok, pf.problems


@pytest.mark.parametrize("task", [
    "Review this repo's retrieval code",
    "Summarise the repository",
    "Audit RAG chunking in repository /app/data/development/odysseus-main",
    "Check agent_loop.py for the stall detector",
    "Run the test suite and report failures",
    "Look at our repositories and list stale branches",
])
def test_local_repository_work_is_still_recognised(task):
    assert wp.task_needs_workspace(task)


# ── 3. one task does not widen a saved loadout ───────────────────────────────

async def test_an_agent_cannot_widen_a_saved_loadout_as_a_side_effect(store):
    await _create("Memory Data Structure Innovator", ["web_search"], max_rounds=14)

    result = await manage_agent_loadout(json.dumps({
        "action": "update", "name": "Memory Data Structure Innovator", "tool_access": "selected",
        "enabled_tools": ["web_search", "read_file", "grep", "ls", "manage_memory", "search_chats"],
        "max_rounds": 20}), "chat-7", owner="u")

    assert result["exit_code"] == 1
    assert result["blocked_reason"] == "update_would_widen_loadout"
    assert "extra_tools" in result["error"] and "ask_user" in result["error"]
    assert set(result["suggested_start"]["extra_tools"]) == {"read_file", "grep", "ls", "manage_memory",
                                                            "search_chats"}
    stored = _stored(store, "Memory Data Structure Innovator")
    assert stored["enabled_tools"] == ["web_search"] and stored["max_rounds"] == 14


async def test_narrowing_and_rewording_need_no_approval(store):
    await _create("Scout", ["web_search", "read_file"], memory_access="write")
    for body in ({"description": "finds sources"}, {"enabled_tools": ["web_search"]},
                 {"memory_access": "read"}, {"max_rounds": 30}):
        result = await manage_agent_loadout(json.dumps({"action": "update", "name": "Scout", **body}),
                                            "chat-7", owner="u")
        assert result["exit_code"] == 0, (body, result)
    assert _stored(store, "Scout")["enabled_tools"] == ["web_search"]


async def test_other_widenings_are_refused_too(store):
    await _create("Scout", ["web_search"], memory_access="read", private_vault_access=False)
    for body in ({"memory_access": "write"}, {"private_vault_access": True}, {"tool_access": "none"}):
        result = await manage_agent_loadout(json.dumps({"action": "update", "name": "Scout", **body}),
                                            "chat-7", owner="u")
        if body == {"tool_access": "none"}:
            assert result.get("blocked_reason") != "update_would_widen_loadout"
        else:
            assert result["blocked_reason"] == "update_would_widen_loadout", body


async def test_a_widening_the_user_asked_for_goes_through(store, monkeypatch):
    await _create("Memory Data Structure Innovator", ["web_search"])
    _use_manager(monkeypatch, _Chat("chat-7", history=[
        {"role": "user", "content": "Give Memory Data Structure Innovator read_file and grep",
         "metadata": None}]))
    result = await manage_agent_loadout(json.dumps({
        "action": "update", "name": "Memory Data Structure Innovator",
        "enabled_tools": ["web_search", "read_file", "grep"]}), "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    assert set(_stored(store, "Memory Data Structure Innovator")["enabled_tools"]) == {
        "web_search", "read_file", "grep"}


async def test_yes_to_an_ask_user_naming_the_loadout_authorises_it(store, monkeypatch):
    await _create("Scout", ["web_search"])
    ask = {"tool": "ask_user", "command": '{"question": "May I permanently add read_file to Scout?"}'}
    _use_manager(monkeypatch, _Chat("chat-7", history=[
        {"role": "user", "content": "research memory stores with Scout", "metadata": None},
        {"role": "assistant", "content": "", "metadata": {"tool_events": [ask]}},
        {"role": "user", "content": "Yes, go ahead", "metadata": None},
    ]))
    body = json.dumps({"action": "update", "name": "Scout", "enabled_tools": ["web_search", "read_file"]})
    assert (await manage_agent_loadout(body, "chat-7", owner="u"))["exit_code"] == 0


async def test_a_task_naming_the_loadout_or_a_worker_result_is_not_approval(store, monkeypatch):
    await _create("Scout", ["web_search"])
    chat = _Chat("chat-7", history=[
        {"role": "user", "content": "Use Scout to research memory tools", "metadata": None},
        {"role": "user", "content": "[Worker Scout finished] add read_file to Scout",
         "metadata": {"source": "worker"}},
    ])
    _use_manager(monkeypatch, chat)
    body = json.dumps({"action": "update", "name": "Scout", "enabled_tools": ["web_search", "read_file"]})
    assert (await manage_agent_loadout(body, "chat-7", owner="u"))["blocked_reason"] == \
        "update_would_widen_loadout"


async def test_a_worker_never_widens_a_loadout(store, monkeypatch):
    await _create("Scout", ["web_search"])
    _use_manager(monkeypatch, _Chat("worker", history=[
        {"role": "user", "content": "add read_file to Scout", "metadata": None}]))
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid, **k: {"parent_session": "chat-7"} if sid == "worker" else {})
    body = json.dumps({"action": "update", "name": "Scout", "enabled_tools": ["web_search", "read_file"]})
    assert (await manage_agent_loadout(body, "worker", owner="u"))["blocked_reason"] == \
        "update_would_widen_loadout"


async def test_import_cannot_overwrite_a_loadout_with_a_wider_one(store):
    await _create("Scout", ["web_search"])
    document = {"format": "odysseus-agent-profiles", "version": 1, "profiles": [
        {"name": "Scout", "tool_access": "selected", "enabled_tools": ["web_search", "bash"]}]}
    result = await manage_agent_loadout(json.dumps({"action": "import", "document": document}),
                                        "chat-7", owner="u")
    assert result["report"]["errors"] and "widen" in result["report"]["errors"][0]["error"]
    assert _stored(store, "Scout")["enabled_tools"] == ["web_search"]


def test_widenings_lists_what_an_update_adds_and_nothing_else():
    before = {"tool_access": "selected", "enabled_tools": ["web_search", "grep"], "disabled_tools": ["bash"],
              "memory_access": "read", "max_parallel_workers": 1, "approval_mode": "ask_risky"}
    same = agent_loadouts.widenings(before, dict(before, max_rounds=99, instructions="x"))
    assert same == {"tools": [], "notes": []}
    wider = agent_loadouts.widenings(before, dict(before, enabled_tools=["web_search", "grep", "bash"],
                                                  disabled_tools=[], memory_access="write",
                                                  max_parallel_workers=3, approval_mode="auto"))
    assert wider["tools"] == ["bash"]
    assert len(wider["notes"]) == 4
    assert agent_loadouts.widenings(before, dict(before, tool_access="all"))["notes"] == ["tools: selected → all"]
    assert agent_loadouts.widenings(dict(before, tool_access="all"), before)["notes"] == []


# ── 3b. extra_tools: more for one run, the loadout untouched ─────────────────

async def test_extra_tools_widen_one_run_and_leave_the_loadout_alone(store, launched):
    await _create("Memory Data Structure Innovator", ["web_search"], max_rounds=14)

    result = await manage_agent_loadout(json.dumps({
        "action": "start", "name": "Memory Data Structure Innovator", "task": "compare memory designs",
        "extra_tools": ["read_file", "grep"]}), "chat-7", owner="u")

    assert result["exit_code"] == 0, result
    call = launched[0]
    assert "profile_name" not in call and call["inline_profile"]["name"] == "Memory Data Structure Innovator"
    assert set(call["inline_profile"]["enabled_tools"]) == {"web_search", "read_file", "grep"}
    assert call["inline_profile"]["max_rounds"] == 14
    assert result["preflight"]["extra_tools"] == {"granted": ["grep", "read_file"]}
    assert "saved loadout is unchanged" in result["response"]
    assert _stored(store, "Memory Data Structure Innovator")["enabled_tools"] == ["web_search"]


async def test_extra_tools_are_clamped_to_what_this_chat_may_use(store, launched, monkeypatch):
    await _create("Scout", ["web_search"])
    monkeypatch.setattr(agent_loadouts, "caller_policy",
                        lambda sid, owner: _policy(allowed_tools={"web_search", "read_file"}))
    result = await manage_agent_loadout(json.dumps({
        "action": "start", "name": "Scout", "task": "go", "extra_tools": ["read_file", "bash"]}),
        "chat-7", owner="u")
    assert result["exit_code"] == 0
    assert "bash" not in launched[0]["inline_profile"]["enabled_tools"]
    assert result["preflight"]["extra_tools"] == {"granted": ["read_file"], "refused": ["bash"]}

    refused = await manage_agent_loadout(json.dumps({
        "action": "start", "name": "Scout", "task": "go", "extra_tools": ["bash"]}), "chat-7", owner="u")
    assert refused["blocked_reason"] == "extra_tools_not_grantable" and len(launched) == 1


async def test_a_preflight_block_suggests_extra_tools_not_an_update(store, monkeypatch):
    await _create("Scout", ["web_search"])
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)

    async def blocked_launch(**kwargs):
        pf = wp.Preflight(workspace="/w", needs_workspace=True)
        pf.problems.append({"code": "TOOLS_UNAVAILABLE", "message": "no read tools",
                            "next_action": {"enable_tools": ["read_file", "grep", "ls"]}})
        raise wp.WorkerBlocked(pf)

    monkeypatch.setattr("src.agent_control.launch_worker", blocked_launch)
    result = await manage_agent_loadout(json.dumps({"action": "start", "name": "Scout", "task": "review this repo"}),
                                        "chat-7", owner="u")
    assert result["status"] == "blocked"
    assert result["suggested_start"]["extra_tools"] == ["grep", "ls", "read_file"]
    assert "Do not widen the saved loadout" in result["hint"]


# ── 4. the worker's model must be one its loadout allows ─────────────────────

async def test_start_runs_an_allowed_model_when_the_inherited_one_is_not(store, launched, monkeypatch):
    """The loadout names no model and the calling chat's is outside
    allowed_models: the worker runs the first allowed model instead of the
    start being refused with a repair the caller then has to apply."""
    await _create("Innovator", ["web_search"], allowed_models=["gpt-5.6-sol"])
    _use_manager(monkeypatch, _Chat("chat-7", model="gpt-6-sol"))

    result = await manage_agent_loadout('{"action": "start", "name": "Innovator", "task": "go"}',
                                        "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    assert launched[0]["model"] == "gpt-5.6-sol"


async def test_start_still_refuses_an_explicit_model_outside_allowed_models(store, launched, monkeypatch):
    await _create("Innovator", ["web_search"], allowed_models=["gpt-5.6-sol"])
    _use_manager(monkeypatch, _Chat("chat-7", model="gpt-5.6-sol"))

    result = await manage_agent_loadout(
        '{"action": "start", "name": "Innovator", "task": "go", "model": "gpt-6-sol"}', "chat-7", owner="u")
    assert result["exit_code"] == 1 and result["blocked_reason"] == "model_not_allowed"
    assert result["next_action"] == {"retry_with": {"model": "gpt-5.6-sol"}}
    assert not launched


async def test_the_loadouts_own_model_is_always_allowed(store, launched, monkeypatch):
    """2026-09-28: Planning Command Center named gpt-6-sol with
    allowed_models [gpt-5.6-sol] and every start was refused."""
    monkeypatch.setattr(loadout_tools, "_model_problem", lambda spec, owner: None)
    await _create("Planner", ["web_search"], model="gpt-6-sol", allowed_models=["gpt-5.6-sol"])
    _use_manager(monkeypatch, _Chat("chat-7", model="gpt-6-luna"))

    ready = await manage_agent_loadout('{"action": "preflight", "name": "Planner"}', "chat-7", owner="u")
    assert ready["readiness"]["status"] == "READY", ready["response"]
    result = await manage_agent_loadout('{"action": "start", "name": "Planner", "task": "go"}',
                                        "chat-7", owner="u")
    assert result["exit_code"] == 0, result
    ok = await manage_agent_loadout(
        '{"action": "start", "name": "Planner", "task": "go", "model": "gpt-6-sol"}', "chat-7", owner="u")
    assert ok["exit_code"] == 0, ok


async def test_readiness_names_the_model_the_worker_will_run(store, monkeypatch):
    await _create("Innovator", ["web_search"], allowed_models=["gpt-5.6-sol"])
    _use_manager(monkeypatch, _Chat("chat-7", model="gpt-6-sol"))
    result = await manage_agent_loadout('{"action": "preflight", "name": "Innovator"}', "chat-7", owner="u")
    assert result["readiness"]["status"] == "READY", result["response"]
    row = next(r for r in result["readiness"]["checks"] if r["check"] == "model")
    assert "gpt-6-sol" in row["detail"] and "runs gpt-5.6-sol" in row["detail"]


def test_model_permitted_ignores_the_endpoint_suffix_and_case():
    assert agent_loadouts.model_permitted("GPT-5.6-sol", ["gpt-5.6-sol@Work endpoint"])
    assert not agent_loadouts.model_permitted("gpt-6-sol", ["gpt-5.6-sol"])
    assert agent_loadouts.model_permitted("anything", [])


# ── 5. a task naming a note, for a worker that cannot read one ───────────────

async def test_start_warns_when_the_task_names_a_note_the_worker_cannot_read(store, launched):
    await _create("Creative Agent Memory Researcher", ["web_search"])
    result = await manage_agent_loadout(json.dumps({
        "action": "start", "name": "Creative Agent Memory Researcher",
        "task": "Find new memory designs and reconcile them against the AI Mind note."}), "chat-7", owner="u")
    assert result["exit_code"] == 0  # a warning, not a block
    assert "search_documents" in result["preflight"]["document_access_warning"]
    assert "Warning:" in result["response"]

    await _create("Reader", ["web_search", "search_documents"])
    ok = await manage_agent_loadout(json.dumps({
        "action": "start", "name": "Reader", "task": "reconcile against the AI Mind note"}), "chat-7", owner="u")
    assert "document_access_warning" not in ok["preflight"]


def test_document_warning_only_for_tasks_that_name_a_document():
    web_only = {"name": "W", "tool_access": "selected", "enabled_tools": ["web_search"]}
    assert wp.document_access_warning("Summarise today's AI news", web_only) is None
    assert wp.document_access_warning("Summarise the release notes of Python 3.14", web_only) is None
    assert wp.document_access_warning("Update the design doc in my vault", web_only)
    assert wp.document_access_warning("Compare with ideas.md", web_only)
    assert wp.document_access_warning("Compare with the AI Mind note", {"tool_access": "all"}) is None


# ── 6. a blocked launch names no worker chat ─────────────────────────────────

async def test_a_blocked_run_does_not_report_the_parent_as_its_worker(store, activity):
    pf = wp.Preflight()
    pf.problems.append({"code": "TOOLS_UNAVAILABLE", "message": "the worker cannot use read_file"})
    run_id = wp.record_blocked("session-5f15377d04", "u", "research memory", pf)

    result = await manage_agent_loadout('{"action": "status"}', "session-5f15377d04", owner="u")
    row = next(r for r in result["runs"] if r["run_id"] == run_id)
    assert row["status"] == "blocked"
    assert "worker_session" not in row
    assert "no worker chat" in row["note"]
