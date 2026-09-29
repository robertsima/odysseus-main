"""The shell has its own setting, separate from private vault access.

Until 2026-09-29 ``private_vault_access`` decided both whether an agent could
read private notes and how it got a shell (grant: unrestricted; none: the
workspace sandbox, or no shell at all). ``shell_access`` (sandbox | host |
off) now decides the shell, on a chat or a loadout; the vault grant only
decides private-note reads (and file-reading MCP servers).
"""
import asyncio
import json
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import shell_access
from src.agent_tools import ToolBlock


def _no_security_context():
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


@pytest.fixture
def dispatch(monkeypatch):
    """Run execute_tool_block for bash with given chat settings and record what
    the dispatcher decided (the bash handler itself is replaced)."""
    import src.tool_execution as execution
    from src.agent_tools import subprocess_tools

    monkeypatch.setattr(execution, "is_public_blocked_tool", lambda _tool: False)
    monkeypatch.setattr(execution, "_owner_is_admin", lambda _owner: True)
    seen = {}

    def fake_sandbox_for(ctx):
        seen["mode"] = execution.get_shell_mode()
        seen["workspace"] = execution.get_shell_sandbox_workspace()
        return None, {"error": "recorded", "exit_code": 7}

    monkeypatch.setattr(subprocess_tools, "_sandbox_for", fake_sandbox_for)

    def run(settings, *, allow_private=False, sandbox=("/ws/repo", "", ""), tool="bash", content="echo hi"):
        seen.clear()
        monkeypatch.setattr("core.database.get_session_settings", lambda sid, strict=False: dict(settings))
        monkeypatch.setattr(shell_access, "sandbox_workspace", lambda ws, sid: sandbox)
        _desc, result = asyncio.run(execution.execute_tool_block(
            ToolBlock(tool, content), session_id="sid", owner="admin", allow_private=allow_private,
            security_context=_no_security_context()))
        return result, dict(seen)

    return run


def test_default_is_the_sandbox_even_with_the_vault_grant(dispatch):
    result, seen = dispatch({"private_vault_access": True}, allow_private=True)
    assert seen == {"mode": "sandbox", "workspace": "/ws/repo"}


def test_a_chat_without_the_vault_grant_can_have_a_full_shell(dispatch):
    _result, seen = dispatch({"shell_access": "host"}, allow_private=False)
    assert seen == {"mode": "host", "workspace": None}


def test_off_refuses_the_shell_whatever_the_vault_says(dispatch):
    result, seen = dispatch({"shell_access": "off", "private_vault_access": True}, allow_private=True)
    assert seen == {}
    assert result["blocked_reason"] == "shell_off"


def test_a_broken_sandbox_refuses_rather_than_falling_back_to_the_host(dispatch):
    result, seen = dispatch({"private_vault_access": True}, allow_private=True,
                            sandbox=(None, "", "bwrap is not installed"))
    assert seen == {}
    assert result["blocked_reason"] == "shell_sandbox_unavailable"
    assert "bwrap is not installed" in result["error"]


def test_background_bash_follows_the_same_decision(dispatch, monkeypatch):
    import src.bg_jobs as bg_jobs

    launched = {}
    monkeypatch.setattr(bg_jobs, "launch", lambda cmd, **kw: launched.update(kw) or {"id": "job1", "status": "running"})
    dispatch({}, content="#!bg\necho hi")
    assert launched.get("sandbox_workspace") == "/ws/repo"
    launched.clear()
    dispatch({"shell_access": "host"}, content="#!bg\necho hi")
    assert "sandbox_workspace" in launched and launched["sandbox_workspace"] is None


def test_file_reading_mcp_tools_still_need_the_vault_grant(dispatch, monkeypatch):
    import src.tool_execution as execution

    monkeypatch.setattr(execution, "get_mcp_manager", lambda: pytest.fail("reached MCP"))
    result, _ = dispatch({"shell_access": "host"}, tool="mcp__filesystem__read_file", content='{"path":"x"}')
    assert result["exit_code"] == 1 and "private vault access" in result["error"]


# ── where a sandboxed shell runs ───────────────────────────────────────────

def test_a_workspace_the_sandbox_refuses_gets_a_scratch_folder(monkeypatch, tmp_path):
    from src import shell_sandbox

    monkeypatch.setattr(shell_sandbox, "status", lambda: {"available": True})
    monkeypatch.setattr(shell_sandbox, "workspace_problem",
                        lambda ws: "the workspace contains the app's data directory" if ws == "/app" else None)
    monkeypatch.setattr("src.constants.AGENT_WORKSPACE_DIR", str(tmp_path))
    ws, why, unavailable = shell_access.sandbox_workspace("/app", "chat 1/../x")
    assert os.path.isdir(ws) and ws.startswith(str(tmp_path)) and ".." not in os.path.relpath(ws, tmp_path)
    assert "app's data" in why and unavailable == ""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert shell_access.sandbox_workspace(str(repo), "c")[0] == os.path.realpath(repo)


# ── who may change it ──────────────────────────────────────────────────────

class _Auth:
    def is_admin(self, user):
        return user == "admin"


@pytest.fixture
def settings_api(monkeypatch):
    store = {"chat": {"private_vault_access": False}}
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, strict=False: dict(store.get(sid, {})))
    monkeypatch.setattr("src.owner_identity.auth_disabled", lambda: False)
    app = FastAPI()
    app.state.auth_manager = _Auth()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        request.state.api_token = request.headers.get("x-test-token") == "1"
        return await call_next(request)

    @app.patch("/s/{sid}")
    async def patch(request: Request, sid: str):
        body = await request.json()
        shell_access.guard_settings_change(request, sid, body)
        return {"ok": True}

    return TestClient(app)


def test_only_an_admin_gives_a_full_shell(settings_api):
    assert settings_api.patch("/s/chat", json={"shell_access": "host"}, headers={"x-test-user": "admin"}).status_code == 200
    assert settings_api.patch("/s/chat", json={"shell_access": "host"}, headers={"x-test-user": "bob"}).status_code == 403
    # Narrowing is anyone's.
    assert settings_api.patch("/s/chat", json={"shell_access": "off"}, headers={"x-test-user": "bob"}).status_code == 200


def test_an_agent_cannot_widen_its_own_chat(settings_api):
    from core.middleware import INTERNAL_TOOL_HEADER

    for body in ({"shell_access": "host"}, {"private_vault_access": True}):
        resp = settings_api.patch("/s/chat", json=body, headers={INTERNAL_TOOL_HEADER: "x", "x-test-user": "admin"})
        assert resp.status_code == 403, body
        resp = settings_api.patch("/s/chat", json=body, headers={"x-test-user": "admin", "x-test-token": "1"})
        assert resp.status_code == 403, body


# ── the one-time move of the old coupling ─────────────────────────────────

def test_chats_and_loadouts_with_the_vault_grant_keep_a_full_shell_once(monkeypatch, tmp_path):
    import core.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from contextlib import contextmanager

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)

    @contextmanager
    def db_session():
        db = factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    monkeypatch.setattr(database, "get_db_session", db_session)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    with db_session() as db:
        for sid, settings in (("vault", {"private_vault_access": True}), ("plain", {"toggles": {"bash": True}}),
                              ("chosen", {"private_vault_access": True, "shell_access": "sandbox"})):
            db.add(database.Session(id=sid, name=sid, endpoint_url="x", model="m", owner="u",
                                    settings_json=json.dumps(settings)))
    saved = {"agent_profiles": [{"name": "Planner", "private_vault_access": True}, {"name": "Coder"}]}
    monkeypatch.setattr("src.settings.load_settings", lambda: saved)
    monkeypatch.setattr("src.settings.save_settings", lambda s: saved.update(s))

    assert shell_access.migrate_legacy() == {"chats": 1, "loadouts": 1}
    with db_session() as db:
        rows = {r.id: json.loads(r.settings_json) for r in db.query(database.Session).all()}
    assert rows["vault"]["shell_access"] == "host"
    assert "shell_access" not in rows["plain"]
    assert rows["chosen"]["shell_access"] == "sandbox"
    assert saved["agent_profiles"][0]["shell_access"] == "host" and "shell_access" not in saved["agent_profiles"][1]

    # Afterwards, giving a chat the vault grant does not give it a full shell.
    with db_session() as db:
        db.query(database.Session).filter(database.Session.id == "plain").update(
            {"settings_json": json.dumps({"private_vault_access": True})})
    assert shell_access.migrate_legacy().get("skipped") == 1
    with db_session() as db:
        plain = json.loads(db.query(database.Session).filter(database.Session.id == "plain").one().settings_json)
    assert "shell_access" not in plain and shell_access.resolve(plain) == "sandbox"


# ── loadouts ────────────────────────────────────────────────────────────────

def test_a_loadout_carries_its_shell_to_the_worker_chat():
    from src import agent_profiles

    [p] = agent_profiles.validate_profiles([{"name": "Coder", "shell_access": "full"}])
    assert p["shell_access"] == "host"
    assert agent_profiles.session_patch(p)["shell_access"] == "host"
    [d] = agent_profiles.validate_profiles([{"name": "Plain"}])
    assert d["shell_access"] == "sandbox"
    with pytest.raises(ValueError):
        agent_profiles.validate_profiles([{"name": "Bad", "shell_access": "root"}])


def test_a_chat_cannot_hand_a_loadout_a_wider_shell_than_its_own(monkeypatch):
    import src.tool_security as tool_security
    from src import agent_loadouts

    monkeypatch.setattr(tool_security, "owner_baseline_disabled_tools", lambda owner: set())
    for own, asked, got in (("sandbox", "host", "sandbox"), ("host", "host", "host"), ("off", "sandbox", "off")):
        monkeypatch.setattr("core.database.get_session_settings",
                            lambda sid, strict=False, own=own: {"shell_access": own})
        policy = agent_loadouts.caller_policy("chat", "admin")
        prof, notes = agent_loadouts.clamp({"name": "Coder", "shell_access": asked}, policy)
        assert prof["shell_access"] == got, (own, asked)
        assert (got != asked) == any("shell_access" in n for n in notes)
