import asyncio


from mcp_servers import penpot_studio_server as server
from src import builtin_mcp, mcp_manager


def test_server_is_a_registered_function_calling_builtin():
    assert builtin_mcp._BUILTIN_SERVERS["penpot_studio"][0] == "mcp_servers/penpot_studio_server.py"
    assert "penpot_studio" in mcp_manager._BUILTIN_FUNCTION_CALLING_SERVERS
    assert mcp_manager.McpManager().is_builtin("penpot_studio")
    assert any(row["id"] == "penpot_studio" for row in builtin_mcp.BUILTIN_CATALOG)


def test_tools_declare_what_they_do_to_penpot():
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"search_icons", "build_design", "move_shapes", "inspect_design", "render_preview"}
    assert tools["inspect_design"].annotations.readOnlyHint is True
    assert tools["render_preview"].annotations.readOnlyHint is True
    assert tools["build_design"].annotations.readOnlyHint is False
    assert tools["build_design"].annotations.destructiveHint is False
    for tool in tools.values():
        assert tool.inputSchema["required"]


def test_errors_come_back_as_instructions_not_exceptions(monkeypatch):
    def unconfigured():
        raise server.ps.PenpotError("Penpot is not configured for the studio tools.")

    monkeypatch.setattr(server.ps, "load_config", unconfigured)
    (out,) = asyncio.run(server.call_tool("inspect_design", {"file_id": "f"}))
    assert out.text.startswith("Error: Penpot is not configured")
    (out,) = asyncio.run(server.call_tool("build_design", {"file_id": "f"}))
    assert "missing required argument(s): page_id, nodes" in out.text
    (out,) = asyncio.run(server.call_tool("nope", {}))
    assert "Unknown tool" in out.text
