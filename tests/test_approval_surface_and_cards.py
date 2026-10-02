"""Approval surface, live approval cards and publish status (2026-10-02).

Production incidents behind these tests:

* A scheduled skill audit ran session-less skill tests whose task told the agent
  to create a document. The untrusted-context gate then asked for an approval
  nobody could answer, the test ended "inconclusive" after a full model round,
  and the code created and immediately denied a record per call.
* Tool-approval cards outlived their in-memory record (TTL, replacement, the next
  user message) and kept their buttons; "approve" answered 409 with no reason.
* A worker told the admin chat a publish request was waiting after it had been
  approved and spent, and new commits had no request at all.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Task 1: headless runs fail fast
# ---------------------------------------------------------------------------

def _run_gated_bash_turn(monkeypatch, **loop_kwargs):
    from src.prompt_security import untrusted_context_message
    import src.agent_loop as agent_loop
    from src.tool_approvals import tool_approval_store

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def fake_stream(*args, **kwargs):
        yield "data: " + json.dumps({"delta": "```bash\nprintf paused\n```"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    async def collect():
        return [
            chunk async for chunk in agent_loop.stream_agent_loop(
                "http://local.test/v1",
                "small-local-model",
                [
                    {"role": "user", "content": "run it"},
                    untrusted_context_message("stored context", "untrusted"),
                ],
                max_rounds=1,
                relevant_tools={"bash"},
                owner="surface-owner",
                **loop_kwargs,
            )
        ]

    events = []
    for chunk in asyncio.run(collect()):
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    pending = tool_approval_store.pending_for_sessions(
        owner="surface-owner", session_ids={"", "surface-session"},
    )
    return events, pending


def test_a_run_without_an_approval_surface_gets_a_tool_error_and_no_record(monkeypatch):
    events, pending = _run_gated_bash_turn(monkeypatch, approval_surface=False)

    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert outputs, events
    assert "none can be given in this run" in outputs[0]["output"]
    assert not any(e.get("ask_user") for e in events)
    assert pending == []


def test_a_session_less_run_defaults_to_no_approval_surface(monkeypatch):
    events, pending = _run_gated_bash_turn(monkeypatch)  # no session_id

    assert not any(e.get("ask_user") for e in events)
    assert pending == []


def test_a_chat_run_still_raises_the_card(monkeypatch):
    from src.tool_approvals import tool_approval_store

    events, pending = _run_gated_bash_turn(monkeypatch, session_id="surface-session")

    assert any(e.get("ask_user", {}).get("kind") == "tool_approval" for e in events)
    assert len(pending) == 1
    tool_approval_store.retire_for_session(owner="surface-owner", session_id="surface-session")


def test_skill_test_task_keeps_its_fixture_inline():
    from routes.skills_routes import _skill_test_task

    task = _skill_test_task({"name": "tidy", "when_to_use": "after editing notes"})

    assert "create_document" not in task
    assert "inline" in task
    assert "do not create documents" in task
    assert "after editing notes" in task


def test_unattended_skill_test_runs_with_no_approval_surface_and_waits_for_quiet(monkeypatch):
    import src.agent_loop as agent_loop
    import src.interactive_gate as gate
    from routes import skills_routes

    order = []
    seen = {}

    async def quiet(label=""):
        order.append("quiet")
        return False

    async def fake_loop(*args, **kwargs):
        order.append("loop")
        seen.update(kwargs)
        yield "data: " + json.dumps({"delta": "done"}) + "\n\n"

    async def fake_eval(*args, **kwargs):
        return {"verdict": "pass", "summary": "ok", "issues": []}

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", quiet)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(skills_routes, "_eval_skill_run", fake_eval)

    transcript, verdict = asyncio.run(
        skills_routes._run_skill_test_once("md", "task", "http://x", "m", {}, "alice")
    )

    assert order == ["quiet", "loop"]
    assert seen["approval_surface"] is False
    assert verdict["verdict"] == "pass"
    assert "done" in transcript


def test_scheduled_audit_visits_each_real_owner(monkeypatch):
    from routes import skills_routes

    class Auth:
        def list_users(self):
            return [{"username": "alice"}, {"username": "bob"}, {"username": ""}]

    monkeypatch.setattr("core.auth.get_auth_manager", lambda: Auth())
    assert skills_routes.scheduled_audit_owners() == ["alice", "bob"]

    class NoUsers:
        def list_users(self):
            return []

    monkeypatch.setattr("core.auth.get_auth_manager", lambda: NoUsers())
    assert skills_routes.scheduled_audit_owners() == [None]
    # The nightly loop must pass each owner instead of owner=None.
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "owner=None, max_skills=batch" not in app_src
    assert "scheduled_audit_owners()" in app_src


# ---------------------------------------------------------------------------
# Task 2: cards reflect live state
# ---------------------------------------------------------------------------

def _create(store, owner="alice", session_id="s1", content="echo hi"):
    from src.tool_capabilities import capabilities_for_action

    return store.create_replacing(
        owner=owner, session_id=session_id, origin_run_id="r", tool_name="bash",
        content=content, workspace=None, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", content),
    )


def test_payload_carries_expires_at_and_ttl_is_a_setting(monkeypatch):
    from src import tool_approvals as ta

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 90 if key == ta.APPROVAL_TTL_SETTING else default)
    store = ta.ToolApprovalStore()
    before = time.time()
    pending, _ = _create(store)

    assert pending.public_payload()["expires_at"] == pytest.approx(before + 90, abs=5)

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "junk")
    assert ta.configured_approval_ttl_seconds() == ta.DEFAULT_APPROVAL_TTL_SECONDS
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 5)
    assert ta.configured_approval_ttl_seconds() == 30


def test_create_returns_the_ids_it_replaced_and_status_says_superseded():
    from src import tool_approvals as ta

    store = ta.ToolApprovalStore(ttl_seconds=600)
    first, replaced = _create(store)
    assert replaced == []
    second, replaced = _create(store, content="echo two")

    assert replaced == [first.approval_id]
    assert store.status(first.approval_id, owner="alice", session_id="s1") == "superseded"
    assert store.status(second.approval_id, owner="alice", session_id="s1") == "pending"
    # Another owner or chat learns nothing about it.
    assert store.status(second.approval_id, owner="mallory", session_id="s1") == "expired"
    assert store.status(first.approval_id, owner="mallory", session_id="s1") == "expired"


def test_retire_for_session_ids_returns_what_it_dropped():
    from src import tool_approvals as ta

    store = ta.ToolApprovalStore(ttl_seconds=600)
    pending, _ = _create(store)
    tainted, ids = store.retire_for_session_ids(owner="alice", session_id="s1")

    assert tainted is True
    assert ids == [pending.approval_id]
    assert store.status(pending.approval_id, owner="alice", session_id="s1") == "superseded"
    # The bool-returning form still works for existing callers.
    assert store.retire_for_session(owner="alice", session_id="s1") is False


def test_an_expired_record_reads_expired(monkeypatch):
    from src import tool_approvals as ta

    store = ta.ToolApprovalStore(ttl_seconds=1)
    pending, _ = _create(store)
    real = time.time
    monkeypatch.setattr(ta.time, "time", lambda: real() + 5)

    assert store.status(pending.approval_id, owner="alice", session_id="s1") == "expired"
    assert store.has_pending_for_session("s1") is False


class _Event(dict):
    pass


def _session_with_card(approval_id):
    ask = {"kind": "tool_approval", "approval_id": approval_id}
    meta = {"tool_events": [{"ask_user": ask}], "_db_id": None}
    return SimpleNamespace(id="s1", history=[SimpleNamespace(metadata=meta)]), ask


def test_saved_card_is_marked_with_a_lapse_but_never_over_an_answer():
    from routes.chat_routes import _mark_tool_approval_resolved

    sess, ask = _session_with_card("a1")
    _mark_tool_approval_resolved(sess, "a1", "superseded")
    assert ask["resolved"] == "superseded"

    sess, ask = _session_with_card("a2")
    ask["resolved"] = "approve"
    assert _mark_tool_approval_resolved(sess, "a2", "expired") is False
    assert ask["resolved"] == "approve"
    assert _mark_tool_approval_resolved(sess, "a2", "bogus") is False


def test_agents_status_route_and_child_status_use_the_live_store():
    from routes.agents_routes import _child_with_live_approval

    child = {"run_id": "r", "status": "waiting_approval", "summary": {"target_session": "w1"}}
    assert _child_with_live_approval(child, {"w1": 1})["status"] == "waiting_approval"
    assert _child_with_live_approval(child, {})["status"] == "approval_expired"
    done = {"run_id": "r2", "status": "completed", "summary": {}}
    assert _child_with_live_approval(done, {}) is done


def test_approval_status_endpoint_is_owner_scoped(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import agents_routes
    from src import tool_approvals as ta

    store = ta.ToolApprovalStore(ttl_seconds=600)
    monkeypatch.setattr(ta, "tool_approval_store", store)
    pending, _ = _create(store, owner="alice", session_id="a1")

    class Mgr:
        def get_sessions_for_user(self, user):
            return {"a1": SimpleNamespace(id="a1")} if user == "alice" else {}

    user = {"name": "alice"}
    monkeypatch.setattr(agents_routes, "effective_user", lambda request: user["name"])
    app = FastAPI()
    app.include_router(agents_routes.setup_agents_routes(Mgr()))
    client = TestClient(app)
    url = f"/api/agents/approvals/{pending.approval_id}/status"

    live = client.get(url, params={"session_id": "a1"}).json()
    assert live["status"] == "pending"
    assert live["expires_at"] == pending.expires_at

    _create(store, owner="alice", session_id="a1", content="echo newer")
    assert client.get(url, params={"session_id": "a1"}).json()["status"] == "superseded"

    user["name"] = "bob"
    assert client.get(url, params={"session_id": "a1"}).status_code == 404


def test_frontend_cards_check_liveness_and_the_intercept_has_no_timer():
    renderer = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    stream = (ROOT / "static/js/chatStream.js").read_text(encoding="utf-8")
    dashboard = (ROOT / "static/js/agentsDashboard.js").read_text(encoding="utf-8")

    assert "/api/agents/approvals/" in renderer
    assert "_watchApprovalLiveness(card, aq, !!renderOptions.restored)" in renderer
    assert "restored: true" in renderer
    assert "odysseus:tool-approval-cancel" in renderer
    assert "odysseus:tool-approval-cancel" in stream
    assert "setTimeout" not in stream.split("document.addEventListener('odysseus:tool-approval', ()")[1].split("}, true);")[0]
    assert "approval_expired" in dashboard


# ---------------------------------------------------------------------------
# Task 3: publish status from the real request
# ---------------------------------------------------------------------------

@pytest.fixture
def worktree_cfg(tmp_path, monkeypatch):
    from src.agent_worktree import approval as approval_mod
    from src.agent_worktree.config import WorktreeConfig

    cfg = WorktreeConfig(
        publish_enabled=True,
        repo_slug="o/r",
        source_repo=str(tmp_path / "repo"),
        worktree_root=str(tmp_path / "wt"),
        state_dir=str(tmp_path / "state"),
        base_branch="dev",
        approval_ttl_s=900,
        api_base="https://api.github.test",
        app_id="",
        installation_id="",
        private_key_path="",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN",
    )
    monkeypatch.setattr(approval_mod, "session_lineage", lambda sid, limit=4: {
        "worker": ["worker", "admin-chat"], "admin-chat": ["admin-chat"], "other": ["other"],
    }.get(sid, [sid] if sid else []))
    return cfg


def _request(approval_mod, cfg, *, branch="agent/x", sha="a" * 40, owner="alice", session="worker"):
    return approval_mod.create_request(
        repo="o/r", branch=branch, head_sha=sha, base_branch="dev", title="t", body="",
        changed_files=["f.py"], files_digest="d", sensitive={}, sensitive_digest="s",
        requested_by=owner, session_id=session, cfg=cfg,
    )


def test_list_requests_filters_by_owner_and_session_lineage(worktree_cfg):
    from src.agent_worktree import approval as approval_mod

    mine = _request(approval_mod, worktree_cfg, owner="alice", session="worker")
    _request(approval_mod, worktree_cfg, owner="bob", session="worker")
    _request(approval_mod, worktree_cfg, owner="alice", session="other")

    got = approval_mod.list_requests(worktree_cfg, owner="alice", session_id="admin-chat")
    assert [r["id"] for r in got] == [mine["id"]]
    assert len(approval_mod.list_requests(worktree_cfg)) == 3
    assert approval_mod.list_requests(worktree_cfg, owner="alice", session_id="other") != got


def test_publish_status_names_the_real_state(worktree_cfg):
    from src.agent_worktree import approval as approval_mod

    kw = dict(owner="alice", session_id="worker", cfg=worktree_cfg, repo="o/r")
    none = approval_mod.publish_status_for_branch("agent/x", head_sha="b" * 40, **kw)
    assert none["state"] == "unpublished"
    assert "call request_publish" in none["next_step"]

    rec = _request(approval_mod, worktree_cfg, sha="b" * 40)
    waiting = approval_mod.publish_status_for_branch("agent/x", head_sha="b" * 40, **kw)
    assert waiting["state"] == "awaiting_approval"
    assert waiting["latest_request"]["id"] == rec["id"]

    approval_mod.mark_published(rec["id"], {"head_sha": "b" * 40}, cfg=worktree_cfg)
    path = approval_mod._record_path(worktree_cfg, rec["id"])
    stored = json.loads(Path(path).read_text())
    stored["status"] = approval_mod.STATUS_USED
    Path(path).write_text(json.dumps(stored))
    published = approval_mod.publish_status_for_branch("agent/x", head_sha="b" * 40, **kw)
    assert published["state"] == "published"

    newer = approval_mod.publish_status_for_branch("agent/x", head_sha="c" * 40, **kw)
    assert newer["state"] == "unpublished"
    assert newer["published_head_sha"] == "b" * 40
    assert "No open request: call request_publish" in newer["next_step"]


def test_failed_publish_is_marked_failed_and_not_reported_as_published(worktree_cfg):
    from src.agent_worktree import approval as approval_mod

    rec = _request(approval_mod, worktree_cfg, sha="d" * 40)
    view = approval_mod.mark_failed(rec["id"], "git push rejected", cfg=worktree_cfg)

    assert view["status"] == approval_mod.STATUS_FAILED
    assert view["publish_error"]["message"] == "git push rejected"
    with pytest.raises(approval_mod.ApprovalError, match="publishing it failed"):
        approval_mod.grant(rec["id"], cfg=worktree_cfg)
    state = approval_mod.publish_status_for_branch(
        "agent/x", head_sha="d" * 40, owner="alice", session_id="worker", cfg=worktree_cfg, repo="o/r",
    )
    assert state["state"] == "publish_failed"
    assert "No open request: call request_publish" in state["next_step"]


def test_publish_after_consume_failure_marks_the_record_failed(worktree_cfg, monkeypatch):
    from src.agent_worktree import approval as approval_mod
    from src.agent_worktree import service

    rec = _request(approval_mod, worktree_cfg, sha="e" * 40)
    live = {"head_sha": "e" * 40, "files_digest": "d", "sensitive_digest": "s", "sensitive": {}, "dirty": False}

    async def fake_summary(*a, **k):
        return live

    async def failing_push(*a, **k):
        raise service.WorktreeError("git operation failed: remote rejected")

    monkeypatch.setattr(service, "_worktree_dir", lambda cfg, branch: str(worktree_cfg.state_dir))
    monkeypatch.setattr(service, "_worktree_exists", lambda path: True)
    monkeypatch.setattr(service, "_verify_membership", lambda cfg, path: None)
    monkeypatch.setattr(service, "_summary", fake_summary)
    monkeypatch.setattr(service, "_push_approved", failing_push)
    monkeypatch.setattr(approval_mod, "consume", lambda *a, **k: {})

    stored = approval_mod.get_request(rec["id"], cfg=worktree_cfg)
    with pytest.raises(service.WorktreeError):
        asyncio.run(service._publish_locked(worktree_cfg, rec["id"], "code", stored, "agent/x"))

    assert approval_mod.get_request(rec["id"], cfg=worktree_cfg)["status"] == approval_mod.STATUS_FAILED


def test_followup_note_says_the_request_is_spent():
    from src import agent_control

    note = agent_control._PUBLISH_FOLLOWUP_NOTE
    assert "approved and has gone out" in note
    assert "that request is spent" in note
    assert "new request_publish" in note
    assert "manage_agent_worktree status" in note


def test_publish_service_docstring_no_longer_promises_a_retry():
    src = (ROOT / "src/agent_worktree/service.py").read_text(encoding="utf-8")
    assert "A failed push leaves the grant unused" not in src
    assert "marked failed" in src


def test_open_needs_drops_a_publish_need_with_nothing_open(monkeypatch):
    from src import open_needs

    saved = {}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid: {})
    monkeypatch.setattr("core.database.update_session_settings", lambda sid, patch: saved.update(patch))
    monkeypatch.setattr(open_needs, "_approval_is_open", lambda sid: False)

    text = "Needs user: approve the publish request in the chat\nNeeds user: pick a colour"
    needs = open_needs.record("chat-1", text)

    assert needs == ["pick a colour"]
    assert saved[open_needs.KEY]["needs"] == ["pick a colour"]

    monkeypatch.setattr(open_needs, "_approval_is_open", lambda sid: True)
    assert open_needs.record("chat-1", text) == [
        "approve the publish request in the chat", "pick a colour",
    ]


def test_open_needs_keeps_a_non_publish_approval_need(monkeypatch):
    from src import open_needs

    monkeypatch.setattr("core.database.get_session_settings", lambda sid: {})
    monkeypatch.setattr("core.database.update_session_settings", lambda sid, patch: None)
    monkeypatch.setattr(open_needs, "_approval_is_open", lambda sid: False)

    assert open_needs.record("c", "Needs user: approve the plan") == ["approve the plan"]


def test_worktree_tool_scopes_list_and_show_request(worktree_cfg, monkeypatch):
    from src.agent_tools import worktree_tools
    from src.agent_worktree import approval as approval_mod

    mine = _request(approval_mod, worktree_cfg, owner="alice", session="worker")
    theirs = _request(approval_mod, worktree_cfg, owner="bob", session="worker")
    monkeypatch.setattr("src.agent_worktree.config.load_config", lambda: worktree_cfg)

    tool = worktree_tools.AgentWorktreeTool()
    ctx = {"owner": "alice", "session_id": "admin-chat"}
    listed = asyncio.run(tool.execute(json.dumps({"action": "list_requests"}), ctx))
    assert [r["id"] for r in listed["requests"]] == [mine["id"]]
    shown = asyncio.run(tool.execute(json.dumps({"action": "show_request", "request_id": theirs["id"]}), ctx))
    assert "error" in shown
