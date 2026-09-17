"""Private vault reads require an explicit execution-context grant."""

import json

import pytest

from src.agent_tools import ToolBlock, TOOL_HANDLERS
from src.agent_tools.filesystem_tools import ReadFileTool
from src.agent_tools.rag_tools import SearchDocumentsTool
from src.tool_execution import execute_tool_block


def test_private_vault_setting_is_explicit_boolean():
    from src.session_settings import validate_patch

    assert validate_patch({"private_vault_access": True}) == {"private_vault_access": True}
    with pytest.raises(ValueError):
        validate_patch({"private_vault_access": "true"})


@pytest.mark.asyncio
async def test_read_file_denies_private_path_without_context_grant(monkeypatch, tmp_path):
    secret = tmp_path / "private.md"
    secret.write_text("private body", encoding="utf-8")

    def resolve(_raw, *, allow_private=False):
        if not allow_private:
            raise ValueError("private vault read requires an explicit grant")
        return str(secret)

    import src.tool_execution as execution

    monkeypatch.setattr(execution, "_resolve_tool_path", resolve)
    denied = await ReadFileTool().execute("private.md", {})
    allowed = await ReadFileTool().execute("private.md", {"allow_private": True})
    assert denied["exit_code"] == 1
    assert "explicit grant" in denied["error"]
    assert allowed == {"output": "private body", "exit_code": 0}


def test_file_frontmatter_private_is_denied_without_grant(monkeypatch, tmp_path):
    import src.rag_sensitivity as sensitivity
    import src.tool_execution as execution

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("---\nsensitivity: private\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(vault))
    assert execution._is_sensitive_path(str(note)) is True
    assert execution._is_sensitive_path(str(note), allow_private=True) is False


@pytest.mark.asyncio
async def test_search_documents_uses_context_grant(monkeypatch):
    import src.rag_singleton as singleton

    class FakeRag:
        def __init__(self):
            self.allow_private = []

        def search(self, query, k, owner=None, allow_private=False):
            self.allow_private.append(allow_private)
            return []

    rag = FakeRag()
    monkeypatch.setattr(singleton, "get_rag_manager", lambda: rag)
    await SearchDocumentsTool().execute("private", {})
    await SearchDocumentsTool().execute("private", {"allow_private": True})
    assert rag.allow_private == [False, True]


@pytest.mark.asyncio
async def test_dispatcher_carries_private_grant_into_dynamic_tools(monkeypatch):
    seen = []
    import src.tool_execution as execution

    monkeypatch.setattr(execution, "is_public_blocked_tool", lambda _tool: False)

    async def handler(_content, ctx):
        seen.append(ctx)
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setitem(TOOL_HANDLERS, "read_file", handler)
    _desc, result = await execute_tool_block(
        ToolBlock("read_file", "private.md"),
        session_id="sid",
        owner="alice",
        allow_private=True,
    )
    assert result["exit_code"] == 0, result
    assert seen and seen[0]["session_id"] == "sid"
    assert seen[0]["owner"] == "alice"
    assert seen[0]["allow_private"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["bash", "python"])
async def test_unrestricted_subprocesses_require_private_grant(monkeypatch, tool):
    import src.tool_execution as execution

    monkeypatch.setattr(execution, "is_public_blocked_tool", lambda _tool: False)
    monkeypatch.setattr(execution, "_owner_is_admin", lambda _owner: True)

    _desc, result = await execute_tool_block(
        ToolBlock(tool, "print('must not execute')" if tool == "python" else "echo must-not-execute"),
        session_id="sid",
        owner="admin",
        allow_private=False,
    )
    assert result["exit_code"] == 1
    assert "private vault access" in result["error"]


@pytest.mark.asyncio
async def test_qualified_mcp_filesystem_read_requires_private_grant(monkeypatch):
    import src.tool_execution as execution

    monkeypatch.setattr(execution, "is_public_blocked_tool", lambda _tool: False)
    monkeypatch.setattr(execution, "_owner_is_admin", lambda _owner: True)

    def should_not_connect():
        raise AssertionError("blocked private MCP read reached the MCP manager")

    monkeypatch.setattr(execution, "get_mcp_manager", should_not_connect)
    _desc, result = await execute_tool_block(
        ToolBlock("mcp__filesystem__read_file", '{"path":"private.md"}'),
        session_id="sid",
        owner="admin",
        allow_private=False,
    )
    assert result["exit_code"] == 1
    assert "private vault access" in result["error"]


@pytest.mark.asyncio
async def test_background_bash_requires_private_grant(monkeypatch):
    import src.tool_execution as execution

    monkeypatch.setattr(execution, "is_public_blocked_tool", lambda _tool: False)
    monkeypatch.setattr(execution, "_owner_is_admin", lambda _owner: True)

    import src.bg_jobs as bg_jobs
    monkeypatch.setattr(bg_jobs, "launch", lambda *_args, **_kwargs: pytest.fail("background shell launched"))
    _desc, result = await execute_tool_block(
        ToolBlock("bash", "#!bg\necho must-not-launch"),
        session_id="sid",
        owner="admin",
        allow_private=False,
    )
    assert result["exit_code"] == 1
    assert "private vault access" in result["error"]


@pytest.mark.asyncio
async def test_direct_bash_handler_honors_resolved_context_grant():
    result = await TOOL_HANDLERS["bash"]("cat private.md", {"allow_private": False})
    assert result["exit_code"] == 1
    assert "private vault access" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/notes"),
        ("GET", "/api/notes/abc"),
        ("GET", "/api/personal"),
        ("POST", "/api/personal/directory_sensitivity"),
        ("POST", "/api/settings/schema"),
        ("PATCH", "/api/session/chat-1/settings"),
    ],
)
async def test_app_api_blocks_vault_reads_and_self_grants_without_grant(
    monkeypatch, method, path
):
    from src.tools.system import do_app_api

    class ShouldNotOpenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("blocked private API read reached loopback HTTP")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", ShouldNotOpenClient)
    result = await do_app_api(
        json.dumps({"method": method, "path": path}),
        owner="alice",
        allow_private=False,
    )
    assert result["exit_code"] == 1
    assert "private-vault read access" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "reset"])
async def test_manage_settings_cannot_declassify_vault_policy(monkeypatch, action):
    import core.database as database
    import src.settings as settings
    from src.agent_tools.admin_tools import do_manage_settings

    class FakeDb:
        def close(self):
            pass

    saved = []
    monkeypatch.setattr(database, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(settings, "save_settings", lambda value: saved.append(value))
    result = await do_manage_settings(
        json.dumps(
            {
                "action": action,
                "key": "vault_default_sensitivity",
                "value": "public",
            }
        ),
        owner="admin",
    )
    assert result["exit_code"] == 0
    assert "only be changed by the user" in result["response"]
    assert saved == []
