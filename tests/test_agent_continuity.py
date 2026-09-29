"""Agents carry a request through instead of stopping at the first obstacle.

From the 2026-09-29 logs: 15 Lead Engineer runs on one request delivered 3
fixes, none ran out of rounds, and the user typed "retry" / "fix it" /
"hello???" about 17 times. The pieces pinned here:

- a turn that did real work and ends "Blocked…" is asked once to clear the
  blocker or name what it needs (`Needs user:` / `Needs parent:`);
- a chat's task checklist (update_plan / todowrite) is saved, shown on later
  turns, and a turn that leaves steps open is asked to carry on;
- a worker's hand-back never trips the "No active workspace" short-circuit;
- a worker launch finds its workspace from a path in the task, or from a
  repository name several checkouts share;
- an approved publish continues the chat that asked;
- a mistyped publish request id gets the open requests as a hint.

The hand-off budget itself is in tests/test_worker_completion_no_runaway.py.
"""

import asyncio
import json
import time

import pytest

import src.agent_loop as al
from src import agent_activity as act
from src import agent_control, agent_runs, constants, headless_agent, task_checklist
from src import worker_preflight as wp


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_loop(monkeypatch, exec_calls, exec_result=None, settings=None):
    import core.database as db

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(db, "get_session_settings", lambda sid, **_k: dict(settings or {}))

    async def _fake_exec(block, *a, **k):
        exec_calls.append(block.tool_type)
        if exec_result:
            return block.tool_type, exec_result(block)
        return block.tool_type, {"output": "ok", "exit_code": 0}
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, rounds, *, messages=None, session_id=None, workspace=None):
    """Each entry in ``rounds`` is (text, native_calls) for that model call."""
    sent = []

    async def _fake_stream(_candidates, msgs, **kwargs):
        idx = len(sent)
        sent.append([dict(m) for m in msgs])
        text, calls = rounds[min(idx, len(rounds) - 1)]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        if calls:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _events(_collect(al.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o",
        messages or [{"role": "user", "content": "Fix the failing test in src/app.py"}],
        max_rounds=8, relevant_tools={"read_file", "update_plan"},
        session_id=session_id, workspace=workspace,
    )))
    return events, sent


READ = [{"name": "read_file", "arguments": json.dumps({"path": "src/app.py"})}]


def _last_text(msgs):
    return str(msgs[-1].get("content") or "")


# ── self-unblock check ──────────────────────────────────────────────────────

def test_the_logged_blocked_answers_are_recognised_and_finished_ones_are_not():
    for text in (
        "**Blocked before implementation or publication.** I verified Umni at /app/data/development/dog-trainer",
        "I stopped before making changes. `origin` is verified.",
        "I could not establish a safe, non-duplicate slice from the repository evidence, so I stopped.",
        "The Lead Engineer couldn’t proceed: **bash is denied by this chat's tool policy**",
        "The AppRoot test mock was updated, but the fix could not be verified or published.",
        "Report so far.\n**Blocked: cannot verify or publish this fix from the requested `main` base.**",
    ):
        assert al._reports_blocked(text), text
    for text in (
        "Fixed and submitted for publication.\n- I couldn't run typecheck with Node 22.",
        "**PR #14 conflict resolution is committed locally and awaiting publication approval.**",
        "I couldn't find any emails from Bob this week.",
        "Done.\n\nBlocked items: none",
        "Blocked: the push needs approval.\nNeeds user: approve request 5ca5dd15",
    ):
        assert not al._reports_blocked(text), text


def test_a_turn_that_reports_blocked_is_asked_once_to_clear_it(monkeypatch):
    calls = []
    _patch_loop(monkeypatch, calls)
    events, sent = _run_loop(monkeypatch, [
        (None, READ),
        ("**Blocked before making changes.** Dependencies are not installed.", None),
        (None, READ),
        ("Installed them; the test passes now.", None),
    ])
    assert len(sent) == 4
    directive = _last_text(sent[2])
    assert "Before you stop" in directive and "install" in directive
    assert "Needs user:" in directive and "Needs parent:" not in directive
    assert any("Checking whether that blocker can be cleared" in str(e.get("delta") or "") for e in events)


def test_the_check_runs_once_and_accepts_a_named_need(monkeypatch):
    calls = []
    _patch_loop(monkeypatch, calls)
    _events_, sent = _run_loop(monkeypatch, [
        (None, READ),
        ("Blocked: the push needs approval.", None),
        ("Blocked: still waiting.", None),
    ])
    assert len(sent) == 3, "one check, then the turn ends"

    calls = []
    _patch_loop(monkeypatch, calls)
    _events_, sent = _run_loop(monkeypatch, [
        (None, READ),
        ("Blocked: the push needs approval.\nNeeds user: approve publish request 5ca5dd15", None),
    ])
    assert len(sent) == 2, "a stop that says what it needs is accepted"


def test_a_turn_that_did_no_work_is_not_checked(monkeypatch):
    calls = []
    _patch_loop(monkeypatch, calls)
    _events_, sent = _run_loop(monkeypatch, [("I could not access that site from here.", None)])
    assert len(sent) == 1


def test_a_worker_is_offered_needs_parent(monkeypatch):
    calls = []
    _patch_loop(monkeypatch, calls, settings={"parent_session": "parent-chat"})
    _events_, sent = _run_loop(monkeypatch, [
        (None, READ),
        ("I stopped before editing: bash is denied.", None),
        ("Needs parent: bash in this worker's workspace", None),
    ], session_id="worker-1")
    assert "Needs parent:" in _last_text(sent[2])
    # And the standing note says who reads its answer.
    assert any("You were started by another chat" in str(m.get("content")) for m in sent[0])


# ── task checklist ──────────────────────────────────────────────────────────

def _plan_result(block):
    if block.tool_type == "update_plan":
        plan = json.loads(block.content)["plan"]
        return {"plan_update": {"plan": plan}, "output": "saved", "exit_code": 0}
    return {"output": "ok", "exit_code": 0}


def test_a_turn_that_leaves_its_checklist_open_is_asked_to_carry_on(monkeypatch):
    calls = []
    _patch_loop(monkeypatch, calls, exec_result=_plan_result)
    open_plan = [{"name": "update_plan", "arguments": json.dumps(
        {"plan": "- [x] delete the stale worktrees\n- [ ] fix the CI run"})}]
    done_plan = [{"name": "update_plan", "arguments": json.dumps(
        {"plan": "- [x] delete the stale worktrees\n- [x] fix the CI run"})}]
    _events_, sent = _run_loop(monkeypatch, [
        (None, open_plan),
        ("Deleted the stale worktrees.", None),
        (None, done_plan),
        ("Both done: worktrees deleted and CI fixed.", None),
    ])
    assert len(sent) == 4
    directive = _last_text(sent[2])
    assert "still has open items" in directive and "- [ ] fix the CI run" in directive


def test_an_untouched_old_checklist_does_not_hold_a_turn_open(monkeypatch):
    calls = []
    record = {"plan": "- [ ] an old step", "updated_at": time.time()}
    _patch_loop(monkeypatch, calls, settings={"task_checklist": record})
    _events_, sent = _run_loop(monkeypatch, [(None, READ), ("Answered.", None)], session_id="chat-1")
    assert len(sent) == 2


def test_an_open_checklist_is_shown_beside_the_request(monkeypatch):
    calls = []
    record = {"plan": "- [x] step one\n- [ ] step two", "updated_at": time.time()}
    _patch_loop(monkeypatch, calls, settings={"task_checklist": record})
    _events_, sent = _run_loop(monkeypatch, [("ok", None)], session_id="chat-1")
    notes = [m for m in sent[0] if "## Task checklist for this chat (1/2 done" in str(m.get("content"))]
    assert notes and "- [ ] step two" in notes[0]["content"]

    record = {"plan": "- [x] step one\n- [x] step two", "updated_at": time.time()}
    _patch_loop(monkeypatch, calls, settings={"task_checklist": record})
    _events_, sent = _run_loop(monkeypatch, [("ok", None)], session_id="chat-1")
    assert not any("## Task checklist" in str(m.get("content")) for m in sent[0])


def test_checklist_storage_round_trip(monkeypatch):
    import core.database as db

    store = {}
    monkeypatch.setattr(db, "update_session_settings",
                        lambda sid, patch: store.setdefault(sid, {}).update(
                            {k: v for k, v in patch.items() if v is not None}) or store[sid])
    monkeypatch.setattr(db, "get_session_settings", lambda sid, **_k: store.get(sid, {}))
    task_checklist.save("c1", "- [ ] a\n- [x] b\n- [~] dropped")
    record = task_checklist.load("c1")
    assert task_checklist.open_items(record["plan"]) == ["a"]
    assert task_checklist.counts(record["plan"]) == (2, 3)
    # An old checklist is from another request.
    store["c1"]["task_checklist"]["updated_at"] = time.time() - task_checklist.MAX_AGE_S - 5
    assert task_checklist.load("c1") is None
    assert task_checklist.from_todos([
        {"content": "write test", "status": "completed"},
        {"content": "fix bug", "status": "in_progress"},
        {"content": "open PR", "status": "pending"},
    ]) == "- [x] write test\n- [ ] fix bug (in progress)\n- [ ] open PR"


# ── the workspace short-circuit only answers a person ───────────────────────

def test_a_worker_hand_back_that_quotes_its_task_is_not_a_workspace_request(monkeypatch):
    calls = []
    _patch_loop(monkeypatch, calls)
    hand_back = {
        "role": "user",
        "content": ("[Worker Lead Engineer finished]\nTask: Inspect the current repo/main/worktrees and fix "
                    "AppRoot.test.tsx.\n\nResult:\nFixed and submitted for publication."),
        "metadata": {"source": "worker"},
    }
    events, sent = _run_loop(monkeypatch, [("The fix is in; approval is pending.", None)],
                             messages=[{"role": "user", "content": "fix the jest failure"}, hand_back])
    assert len(sent) == 1
    assert not any("No active workspace is set" in str(e.get("delta") or "") for e in events)


def _sent_tools(monkeypatch, messages, settings):
    calls = []
    _patch_loop(monkeypatch, calls, settings=settings)
    seen = []

    async def _fake_stream(_candidates, msgs, **kwargs):
        seen.append({(t.get("function") or {}).get("name") for t in (kwargs.get("tools") or [])})
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop("https://api.openai.com/v1", "gpt-4o", messages, max_rounds=2,
                                  relevant_tools={"read_file", "send_to_session", "manage_agent_loadout"},
                                  session_id="chat-1"))
    return seen[0]


def test_a_follow_up_after_a_hand_back_keeps_the_launchers_the_request_authorised(monkeypatch):
    """Under the default `explicit` policy the gate read the hand-back as the
    request, found no delegation wording in it, and closed send_to_session:
    the follow-up could never send the worker back."""
    hand_back = {"role": "user", "content": "[Worker Lead Engineer finished]\nTask: fix it\n\nResult:\nBlocked.",
                 "metadata": {"source": "worker"}}
    person = {"role": "user", "content": "Use the Lead Engineer to fix the Jest failure"}
    tools = _sent_tools(monkeypatch, [person, hand_back], {"delegation_policy": "explicit"})
    assert "send_to_session" in tools
    # `never` still means never.
    tools = _sent_tools(monkeypatch, [person, hand_back], {"delegation_policy": "never"})
    assert "send_to_session" not in tools
    # A person's own turn is judged on their words, as before.
    tools = _sent_tools(monkeypatch, [{"role": "user", "content": "What changed in the Jest suite?"}],
                        {"delegation_policy": "explicit"})
    assert "send_to_session" not in tools


# ── launch preflight finds the workspace ────────────────────────────────────

@pytest.fixture(autouse=True)
def _admin_owner(monkeypatch):
    import src.tool_security as tool_security
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(tool_security, "owner_baseline_disabled_tools", lambda owner: set())


@pytest.fixture
def umni_checkouts(tmp_path, monkeypatch):
    """The 2026-09-29 layout: one main checkout and linked worktrees of it,
    all with origin robertsima/Umni."""
    paths = []
    for name in ("dog-trainer", "dog-trainer-brief", "dog-trainer-checkins"):
        repo = tmp_path / "dev" / name
        repo.mkdir(parents=True)
        if name == "dog-trainer":
            (repo / ".git").mkdir()
        else:
            (repo / ".git").write_text("gitdir: ../dog-trainer/.git/worktrees/" + name, encoding="utf-8")
        paths.append(str(repo.resolve()))
    monkeypatch.setattr(wp, "known_checkouts", lambda: list(paths))
    monkeypatch.setattr(wp, "_origin_repo_name", lambda path: "Umni")
    return paths


def test_a_repository_name_shared_by_worktrees_picks_the_main_checkout(umni_checkouts):
    pf = wp.run_preflight("Implement the next Umni slice and run its tests")
    assert pf.ok and pf.workspace == umni_checkouts[0]


def test_a_path_in_the_task_picks_its_checkout(umni_checkouts):
    pf = wp.run_preflight(f"Fix the failing test in {umni_checkouts[2]}/mobile/App.tsx and commit it")
    assert pf.ok and pf.workspace == umni_checkouts[2]
    assert pf.workspace_source == "path in the task"


def test_a_managed_worktree_path_in_the_task_is_the_workspace(umni_checkouts, tmp_path, monkeypatch):
    from src.agent_worktree import ownership

    root = tmp_path / "agent_worktrees"
    leaf = root / "_repos" / "umni-50d95770" / "fix-pr15-app-root-test"
    (leaf / "mobile").mkdir(parents=True)
    (leaf / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")
    monkeypatch.setattr(ownership, "managed_worktree_root", lambda: root.resolve())
    pf = wp.run_preflight(f"Use {leaf}/mobile on branch agent/umni/fix-pr15. Fix the test and commit.")
    assert pf.ok and pf.workspace == str(leaf.resolve())


# ── publishing is a pause ───────────────────────────────────────────────────

class _Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata or {}


class _Chat:
    def __init__(self, sid, owner="alice"):
        self.id, self.name, self.model, self.owner = sid, sid, "m", owner
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
def _activity(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: False)
    yield
    act._reset_for_tests()


def _approved_worker_chat():
    chat = _Chat("worker-1")
    chat.add_message(_Msg("user", "Fix AppRoot.test.tsx and request publication", {"source": "dashboard"}))
    chat.add_message(_Msg("assistant", "Committed; publication request 5ca5dd15 is pending."))
    chat.add_message(_Msg("user", "[Publish approved by admin] Pushed agent/umni/x @ f3440c331646; draft PR: "
                                  "https://github.com/robertsima/Umni/pull/16.",
                          {"source": "publish_decision", "request_id": "5ca5dd15"}))
    return chat


async def test_an_approved_publish_continues_the_chat_and_hands_the_result_up(_activity, monkeypatch):
    import core.database as db

    seen, handed = {}, []

    async def fake_headless(sess, messages, **kwargs):
        seen["messages"], seen["disabled"] = messages, set(kwargs.get("disabled_tools") or ())
        return "PR #16 is open and its CI run passed.", []

    async def fake_hand_off(manager, parent_id, chat, task, reply, status, owner):
        handed.append((parent_id, reply, status))

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    monkeypatch.setattr(agent_control, "_hand_off", fake_hand_off)
    monkeypatch.setattr(agent_control, "_auto_continue_limit", lambda: 3)
    monkeypatch.setattr(db, "get_session_settings",
                        lambda sid, **_k: {"parent_session": "admin-chat"} if sid == "worker-1" else {})
    chat = _approved_worker_chat()
    await agent_control._publish_followup(_Manager(chat), "worker-1", None, "5ca5dd15")

    assert "approved and has gone out" in seen["messages"][-1]["content"]
    assert chat.history[-1].metadata["source"] == "publish_followup"
    assert handed == [("admin-chat", "PR #16 is open and its CI run passed.", "completed")]


async def test_no_follow_up_when_the_person_already_wrote_after_the_decision(_activity, monkeypatch):
    ran = []

    async def fake_headless(sess, messages, **kwargs):
        ran.append(1)
        return "x", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    monkeypatch.setattr(agent_control, "_auto_continue_limit", lambda: 3)
    chat = _approved_worker_chat()
    chat.add_message(_Msg("user", "great, merge it"))
    await agent_control._publish_followup(_Manager(chat), "worker-1", None, "5ca5dd15")
    assert ran == []


async def test_follow_ups_off_means_an_approval_only_notes_the_chat(_activity, monkeypatch):
    monkeypatch.setattr(agent_control, "_auto_continue_limit", lambda: 0)
    agent_control.schedule_publish_followup(_Manager(_approved_worker_chat()), "worker-1", None, "5ca5dd15")
    assert not agent_control._PENDING_HANDOFFS


# ── a mistyped publish request id ───────────────────────────────────────────

def test_an_unknown_request_id_lists_the_close_open_requests(monkeypatch):
    from src.agent_tools import worktree_tools
    from src.agent_worktree import approval as approval_mod

    monkeypatch.setattr(approval_mod, "list_requests", lambda cfg=None: [
        {"id": "5920c8ebbb87419a9ab29e17447cfa92", "branch": "agent/umni/umni-delivery-slice", "status": "pending"},
        {"id": "e61ec75ece1747959737a71086400609", "branch": "agent/umni/resolve-pr14", "status": "used"},
    ])
    hint = worktree_tools._similar_request_hint("5920c8eb87419a9ab29e17447cfa92", None)
    assert "5920c8ebbb87419a9ab29e17447cfa92" in hint and "e61ec75e" not in hint
