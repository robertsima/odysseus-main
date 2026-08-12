import asyncio
from unittest.mock import patch

from src.mcp_manager import _describe_exception, _format_mcp_connection_error, McpManager


def test_describe_exception_falls_back_to_type_name_when_message_is_empty():
    # Regression: several MCP-adjacent exceptions (anyio's
    # ClosedResourceError/BrokenResourceError when a stdio subprocess's pipe
    # closes underneath it, among others) carry no message -- str(e) is "".
    # `str(error) if error else "Unknown error"` only catches error being
    # None/falsy; an Exception instance is always truthy even with an empty
    # str(), so that produced e.g. "MCP tool call failed: mcp__x__y: " with
    # nothing after the colon -- undiagnosable from logs alone.
    class _NoMessageError(Exception):
        pass

    desc = _describe_exception(_NoMessageError())
    assert desc != ""
    assert "_NoMessageError" in desc


def test_describe_exception_preserves_real_message():
    assert _describe_exception(RuntimeError("boom")) == "boom"


def test_describe_exception_handles_none():
    assert _describe_exception(None) == "Unknown error"


def test_connection_error_with_empty_exception_message_is_not_blank():
    msg = _format_mcp_connection_error(
        "Custom MCP", "python", ["server.py"], ConnectionError(),
    )

    assert msg.strip() != ""
    assert "ConnectionError" in msg


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Odysseus" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_todoist_connection_error_explains_cli_wrapper():
    msg = _format_mcp_connection_error(
        "Todoist",
        "td",
        ["today", "--json"],
        RuntimeError("Connection closed"),
    )

    assert "Connection closed" in msg
    assert "td" in msg
    assert "not an MCP server" in msg
    assert "mcp_servers/todoist_server.py" in msg


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    async def fake_start(server_id, name, url):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()
