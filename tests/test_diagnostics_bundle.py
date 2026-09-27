"""Diagnostics bundle: discovery, redaction, privacy defaults, failure
isolation, and the admin / ``diagnostics:read`` gates on its routes."""

import asyncio
import io
import json
import uuid
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from src import agent_logs  # noqa: E402
from src import diagnostics_bundle as bundle_mod  # noqa: E402

SID = "0a1b2c3d-1111-2222-3333-444455556666"
PLANTED_KEY = "sk-PLANTEDsecretVALUE1234567890abcdef"
PLANTED_PASSWORD = "hunter2-planted-password"
PLANTED_OPAQUE = "AbCdEf0123456789GhIjKl0123456789MnOpQr0123"
PRIVATE_TEXT = "PRIVATE-VAULT-JOURNAL-ENTRY"


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,000")


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _all_text(zf: zipfile.ZipFile) -> str:
    return "\n".join(zf.read(name).decode("utf-8", "replace") for name in zf.namelist())


# ── discovery ────────────────────────────────────────────────────────────── #

def test_scan_references_finds_sessions_runs_and_loadouts():
    other = "0a1b2c3d-1111-2222-3333-44445555aaaa"
    child = "0a1b2c3d-1111-2222-3333-44445555bbbb"
    lines = [  # newest first
        f"{_stamp()} - src.tool_execution - WARNING - Tool blocked: session={SID} tool=bash",
        f"{_stamp()} - routes.workbench_routes - INFO - [workbench] runs for chat {other.upper()}: 1 row(s), 1 running",
        f'{_stamp()} - x - INFO - payload {{"session_id": "{SID}", "worker_session": "{child}"}}',
        f"{_stamp()} - x - INFO - [agent-loadout] start loadout=Lead Engineer run=subagent-0123456789 "
        f"child={child} model=m rounds=5 tools=3",
        f'{_stamp()} - x - INFO - tool manage_agent_loadout {{"action": "create", "name": "Research Helper"}}',
        f"{_stamp()} - x - INFO - bg-followup: session session-abc123 gone",
    ]
    refs = bundle_mod.scan_references(lines)
    assert refs["sessions"][:2] == [SID, other]  # most recent first, lower-cased
    assert child in refs["sessions"]
    assert "session-abc123" in refs["sessions"]
    assert refs["runs"] == ["subagent-0123456789"]
    assert "Lead Engineer" in refs["loadouts"]
    assert "Research Helper" in refs["loadouts"]
    assert "Lead" not in refs["loadouts"]  # the start line's name is not also split at the space


def test_discover_resolves_against_stores(monkeypatch):
    gone = "0a1b2c3d-1111-2222-3333-44445555dead"
    worker = "0a1b2c3d-1111-2222-3333-44445555cafe"
    logs = [{"name": "app.log", "lines": [
        f"{_stamp()} - x - INFO - session={gone} tool=x",
        f"{_stamp()} - x - INFO - [agent] run odysseus-00000000ff finished",
        f"{_stamp()} - x - INFO - for chat {SID}",
    ]}]
    monkeypatch.setattr(bundle_mod, "_existing_sessions",
                        lambda ids: {i: "alice" for i in ids if i in (SID, worker)})
    from src import agent_activity

    monkeypatch.setattr(agent_activity, "get_run", lambda rid: {
        "run_id": rid, "session_id": worker, "source": "odysseus", "status": "failed",
        "summary": {"parent_session": SID, "target_session": worker, "profile": "Lead Engineer"},
    } if rid == "odysseus-00000000ff" else None)

    found = bundle_mod.discover(logs, session_ids=None)
    assert found["sessions"][0] == SID  # newest mention first
    assert worker in found["sessions"]
    assert found["sessions_not_found"] == [gone]
    assert found["runs"][0]["run_id"] == "odysseus-00000000ff"
    assert "Lead Engineer" in found["loadout_mentions"]


def test_mask_keeps_shape_and_numbers():
    masked = bundle_mod.mask({
        "openai_api_key": PLANTED_KEY, "brave_api_key": "", "max_tokens": 4096,
        "email": {"smtp_password": PLANTED_PASSWORD}, "keybinds": {"search": "ctrl+k"},
        "claude_code_odysseus_token_file": "/app/data/secrets/token",
        "blob": PLANTED_OPAQUE, "model": "accounts/fireworks/models/llama-v3p1-405b-instruct",
        "endpoint": "https://user:pw@example.com/v1?api_key=abc",
        "auth": {"logged_in": True, "auth_method": "oauth_token", "auth_token": "oauth_token"},
    })
    assert masked["auth"] == {"logged_in": True, "auth_method": "oauth_token", "auth_token": "***"}
    assert masked["openai_api_key"] == "***"
    assert masked["brave_api_key"] == ""
    assert masked["max_tokens"] == 4096
    assert masked["email"]["smtp_password"] == "***"
    assert masked["keybinds"]["search"] == "ctrl+k"
    assert masked["claude_code_odysseus_token_file"] == "/app/data/secrets/token"
    assert masked["blob"] == "***"
    assert masked["model"].startswith("accounts/fireworks")
    assert masked["endpoint"] == "https://example.com/v1"


# ── building the bundle ──────────────────────────────────────────────────── #

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp log dir with planted secrets, planted secret settings, a real chat
    row with private message text, and no slow external probes."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.log").write_text("\n".join([
        f"{_stamp()} - src.agent - INFO - turn start session={SID} model=gpt",
        f"{_stamp()} - src.llm - WARNING - retry Authorization: Bearer {PLANTED_KEY}",
        f"{_stamp()} - src.llm - INFO - api_key={PLANTED_KEY} endpoint=https://u:{PLANTED_PASSWORD}@host/v1",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(agent_logs, "log_roots", lambda: [str(logs)])

    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {
        "openai_api_key": PLANTED_KEY, "nested": {"smtp_password": PLANTED_PASSWORD},
        "opaque_value": PLANTED_OPAQUE, "agent_max_tokens": 4096,
    })
    monkeypatch.setattr(settings_mod, "load_features", lambda: {"feature_x": True})

    import src.agent_tools.claude_code_tools as cc

    async def fake_status():
        return {"ready": True, "auth": {"logged_in": True, "api_key": PLANTED_KEY}}

    monkeypatch.setattr(cc, "status_report", fake_status)

    # The in-memory test DB is per-thread; build in this one.
    async def same_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(bundle_mod.asyncio, "to_thread", same_thread)

    from core.database import ChatMessage, Session, get_db_session

    with get_db_session() as db:
        db.query(ChatMessage).filter(ChatMessage.session_id == SID).delete()
        db.query(Session).filter(Session.id == SID).delete()
    with get_db_session() as db:
        db.add(Session(id=SID, name="Debug me", endpoint_url=f"https://u:{PLANTED_PASSWORD}@llm.local/v1?key=zz",
                       model="gpt-test", owner="alice", mode="agent",
                       settings_json=json.dumps({"private_vault_access": True, "approval_mode": "auto",
                                                 "agent_profile": "Nope"})))
        db.add(ChatMessage(id=str(uuid.uuid4()), session_id=SID, role="user",
                           content=f"{PRIVATE_TEXT} please fix"))
        db.add(ChatMessage(id=str(uuid.uuid4()), session_id=SID, role="assistant",
                           content=f"answer about {PRIVATE_TEXT}",
                           meta_data=json.dumps({"model": "gpt-test", "tool_events": [
                               {"tool": "bash", "exit_code": 1, "round": 2, "error": "denied"}]})))
    yield tmp_path
    with get_db_session() as db:
        db.query(ChatMessage).filter(ChatMessage.session_id == SID).delete()
        db.query(Session).filter(Session.id == SID).delete()


def _build(**kwargs):
    kwargs.setdefault("include_health", False)
    kwargs.setdefault("owner", "alice")
    return asyncio.run(bundle_mod.build_bundle(**kwargs))


def test_bundle_layout_and_redaction(env):
    result = _build(minutes=60)
    zf = _open(result.data)
    names = zf.namelist()
    assert "manifest.json" in names and "README.txt" in names
    assert "logs/app.log" in names
    assert f"sessions/{SID}.json" in names  # discovered from "session=<id>" in the log
    assert "system/settings.json" in names and "system/claude_code.json" in names
    assert result.filename.startswith("odysseus-diagnostics-") and result.filename.endswith(".zip")

    text = _all_text(zf)
    for secret in (PLANTED_KEY, PLANTED_PASSWORD, PLANTED_OPAQUE):
        assert secret not in text

    log = zf.read("logs/app.log").decode()
    assert f"session={SID}" in log and "***" in log

    settings = json.loads(zf.read("system/settings.json"))["settings"]
    assert settings["openai_api_key"] == "***"  # masked, not dropped
    assert settings["nested"]["smtp_password"] == "***"
    assert settings["agent_max_tokens"] == 4096

    session = json.loads(zf.read(f"sessions/{SID}.json"))
    assert session["title"] == "Debug me"
    assert session["endpoint"] == "https://llm.local/v1"
    assert session["settings"]["private_vault_access"] is True
    assert session["last_turn"]["tool_calls"][0] == {"tool": "bash", "exit_code": 1, "round": 2, "error": "denied"}
    assert "lineage" in session and "effective_policy" in session

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "odysseus-diagnostics-bundle"
    assert manifest["window"]["minutes"] == 60
    assert manifest["privacy"]["include_messages"] is False
    assert "Nope" in manifest["discovery"]["loadouts_not_found"]
    assert {f["path"] for f in manifest["files"]} >= {"logs/app.log", f"sessions/{SID}.json"}


def test_no_message_content_by_default(env):
    zf = _open(_build(minutes=60).data)
    assert PRIVATE_TEXT not in _all_text(zf)
    session = json.loads(zf.read(f"sessions/{SID}.json"))
    assert "messages" not in session


def test_include_messages_is_explicit_and_warned(env):
    zf = _open(_build(minutes=60, include_messages=True).data)
    session = json.loads(zf.read(f"sessions/{SID}.json"))
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
    assert PRIVATE_TEXT in session["messages"][0]["content"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["privacy"]["include_messages"] is True
    assert "private vault" in manifest["privacy"]["message_content"]


def test_include_messages_withholds_another_users_chat(env):
    zf = _open(_build(minutes=60, include_messages=True, owner="bob").data)
    session = json.loads(zf.read(f"sessions/{SID}.json"))
    assert "messages" not in session and session["messages_withheld"]


def test_explicit_session_is_included_without_log_mentions(env, monkeypatch):
    (env / "logs" / "app.log").write_text(f"{_stamp()} - x - INFO - nothing here\n", encoding="utf-8")
    zf = _open(_build(minutes=60, session_ids=[SID.upper()]).data)
    assert f"sessions/{SID}.json" in zf.namelist()


def test_component_failure_is_recorded_not_fatal(env, monkeypatch):
    def boom():
        raise RuntimeError(f"mcp exploded token={PLANTED_KEY}")

    monkeypatch.setattr(bundle_mod, "mcp_servers", boom)
    result = _build(minutes=60)
    zf = _open(result.data)
    manifest = json.loads(zf.read("manifest.json"))
    failed = {e["component"]: e["error"] for e in manifest["errors"]}
    assert "system/mcp_servers.json" in failed
    assert PLANTED_KEY not in failed["system/mcp_servers.json"]
    assert "system/mcp_servers.json" not in zf.namelist()
    assert "logs/app.log" in zf.namelist()  # the rest of the bundle still built


def test_size_cap_truncates_oldest_log_lines(env):
    big = "\n".join(f"{_stamp()} - x - INFO - line {i} " + "x" * 200 for i in range(20000))
    (env / "logs" / "app.log").write_text(big + "\n", encoding="utf-8")
    result = _build(minutes=60, max_bytes=2 * 1024 * 1024)
    zf = _open(result.data)
    manifest = json.loads(zf.read("manifest.json"))
    assert any(t["path"] == "logs/app.log" for t in manifest["truncated"])
    log = zf.read("logs/app.log").decode()
    assert "line 19999 " in log and "line 0 " not in log
    assert sum(len(zf.read(n)) for n in zf.namelist()) <= 2 * 1024 * 1024


def test_window_excludes_old_entries(env):
    (env / "logs" / "app.log").write_text(
        "2020-01-01 00:00:00,000 - x - INFO - ancient\n"
        f"{_stamp()} - x - INFO - recent\n", encoding="utf-8")
    zf = _open(_build(minutes=15).data)
    log = zf.read("logs/app.log").decode()
    assert "recent" in log and "ancient" not in log


# ── routes ───────────────────────────────────────────────────────────────── #

diag = pytest.importorskip("routes.diagnostics_routes")


def _fake_build(calls):
    async def fake_build_bundle(**kwargs):
        calls.append(kwargs)
        return bundle_mod.BundleResult(data=b"PK-fake", filename="odysseus-diagnostics-20260101-000000.zip",
                                       summary={"files": 1, "log_lines": 3, "sessions": [], "loadouts": [],
                                                "errors": [], "truncated": []})
    return fake_build_bundle


def _client(monkeypatch, gate=None, token=None, admins=("root",)):
    calls = []
    monkeypatch.setattr(bundle_mod, "build_bundle", _fake_build(calls))
    monkeypatch.setattr(bundle_mod, "summarize", lambda **kw: {"sessions": [], "kw": kw})
    if gate is not None:
        monkeypatch.setattr(diag, "require_admin", gate)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u in admins)

    if token is not None:
        @app.middleware("http")
        async def fake_token_auth(request, call_next):
            request.state.current_user = "api"
            request.state.api_token = True
            request.state.api_token_owner = token["owner"]
            request.state.api_token_scopes = token["scopes"]
            return await call_next(request)

    app.include_router(diag.setup_diagnostics_routes(
        rag_manager=None, rag_available=False, research_handler=None, memory_vector=None))
    return TestClient(app, raise_server_exceptions=False), calls


def _deny(_request: Request):
    raise HTTPException(403, "Admin only")


def _allow(_request: Request):
    return None


def test_bundle_route_requires_admin(monkeypatch):
    client, calls = _client(monkeypatch, gate=_deny)
    assert client.get("/api/diagnostics/bundle").status_code == 403
    assert client.get("/api/diagnostics/bundle/summary").status_code == 403
    assert calls == []


def test_bundle_route_returns_zip_for_admin(monkeypatch):
    client, calls = _client(monkeypatch, gate=_allow)
    r = client.get(f"/api/diagnostics/bundle?minutes=15&session={SID}&session=other1&include_messages=true")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert 'filename="odysseus-diagnostics-20260101-000000.zip"' in r.headers["content-disposition"]
    assert r.content == b"PK-fake"
    assert calls[0]["minutes"] == 15
    assert calls[0]["session_ids"] == [SID, "other1"]
    assert calls[0]["include_messages"] is True
    summary = client.get("/api/diagnostics/bundle/summary?minutes=240")
    assert summary.status_code == 200 and summary.json()["kw"]["minutes"] == 240


def test_token_without_scope_is_refused(monkeypatch):
    client, calls = _client(monkeypatch, token={"owner": "root", "scopes": ["chat", "claude_code:read"]})
    r = client.get("/api/diagnostics/bundle")
    assert r.status_code == 403 and "diagnostics:read" in r.json()["detail"]
    assert client.get("/api/diagnostics/bundle/summary").status_code == 403
    assert calls == []


def test_token_with_scope_gets_bundle_but_never_messages(monkeypatch):
    client, calls = _client(monkeypatch, token={"owner": "root", "scopes": ["diagnostics:read"]})
    assert client.get("/api/diagnostics/bundle?minutes=60").status_code == 200
    assert calls[0]["owner"] == "root" and calls[0]["include_messages"] is False
    assert client.get("/api/diagnostics/bundle?include_messages=true").status_code == 403
    assert client.get("/api/diagnostics/bundle/summary").status_code == 200


def test_token_scope_needs_an_admin_owner(monkeypatch):
    client, calls = _client(monkeypatch, token={"owner": "bob", "scopes": ["diagnostics:read"]})
    assert client.get("/api/diagnostics/bundle").status_code == 403
    assert calls == []


def test_diagnostics_scope_is_opt_in():
    from routes.api_token_routes import ALLOWED_SCOPES, DEFAULT_SCOPES, TOKEN_PROFILES, _normalize_scopes

    assert "diagnostics:read" in ALLOWED_SCOPES
    assert all("diagnostics:read" not in scopes for scopes in TOKEN_PROFILES.values())
    assert "diagnostics:read" not in _normalize_scopes(None)
    assert DEFAULT_SCOPES == "chat"
    assert _normalize_scopes(["diagnostics:read"]) == ["diagnostics:read"]


# ── agent tool ───────────────────────────────────────────────────────────── #

def test_read_app_logs_bundle_action_writes_zip(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bundle_mod, "build_bundle", _fake_build(calls))
    monkeypatch.setattr(bundle_mod, "exports_dir", lambda: str(tmp_path / "exports"))
    from src.agent_tools.worktree_tools import ReadAppLogsTool

    out = asyncio.run(ReadAppLogsTool().execute(
        json.dumps({"action": "bundle", "since_minutes": 15}), {"owner": "root", "session_id": SID}))
    assert out["exit_code"] == 0
    assert out["path"].endswith(".zip")
    with open(out["path"], "rb") as fh:
        assert fh.read() == b"PK-fake"
    assert calls[0]["include_messages"] is False
    assert calls[0]["session_ids"] == [SID] and calls[0]["minutes"] == 15
