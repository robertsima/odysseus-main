import asyncio
from unittest.mock import patch

from src.mcp_manager import (
    _describe_exception,
    _format_mcp_connection_error,
    _is_dead_transport_error,
    McpManager,
)


class _ClosedResourceError(Exception):
    """Stand-in for anyio's ClosedResourceError: no message, matched by name."""


_ClosedResourceError.__name__ = "ClosedResourceError"


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


def test_dead_transport_errors_are_distinguished_from_tool_errors():
    assert _is_dead_transport_error(_ClosedResourceError())
    assert _is_dead_transport_error(BrokenPipeError())
    assert not _is_dead_transport_error(RuntimeError("tool said no"))
    assert not _is_dead_transport_error(None)


def _connected_stdio_manager(server_id="ntfy1"):
    mgr = McpManager()
    mgr._sessions[server_id] = object()
    mgr._connections[server_id] = {"status": "connected", "name": "ntfy"}
    mgr._configs[server_id] = {
        "server_id": server_id,
        "name": "ntfy",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "ntfy-me-mcp"],
        "env": {},
        "url": None,
    }
    return mgr


def test_user_added_stdio_server_reconnects_after_its_subprocess_dies():
    # Regression: reconnect used to be gated on is_builtin(), so a user-added
    # stdio server whose subprocess had exited stayed dead until Odysseus
    # restarted -- every call failing in ~1ms with a messageless anyio error.
    mgr = _connected_stdio_manager()
    calls = []

    async def fake_do_call(session, tool_name, arguments):
        calls.append(session)
        if len(calls) == 1:
            raise _ClosedResourceError()
        return {"stdout": "sent", "stderr": "", "exit_code": 0}

    async def fake_connect(**config):
        mgr._sessions[config["server_id"]] = object()
        return True

    with patch.object(McpManager, "_do_call", side_effect=fake_do_call), \
         patch.object(McpManager, "disconnect_server", side_effect=_noop), \
         patch.object(McpManager, "connect_server", side_effect=fake_connect):
        result = asyncio.run(mgr.call_tool("mcp__ntfy1__notify_me", {"message": "hi"}))

    assert result["exit_code"] == 0
    assert result["stdout"] == "sent"
    assert len(calls) == 2


async def _noop(*args, **kwargs):
    return None


def test_ordinary_tool_exception_is_not_retried_on_a_user_added_server():
    # A tool that actually ran and raised must not be replayed: the first attempt
    # may already have had a side effect (e.g. a notification was delivered).
    mgr = _connected_stdio_manager()
    calls = []

    async def fake_do_call(session, tool_name, arguments):
        calls.append(session)
        raise RuntimeError("topic not found")

    with patch.object(McpManager, "_do_call", side_effect=fake_do_call):
        result = asyncio.run(mgr.call_tool("mcp__ntfy1__notify_me", {"message": "hi"}))

    assert result["exit_code"] == 1
    assert "topic not found" in result["error"]
    assert len(calls) == 1


def test_dead_transport_error_reported_to_the_agent_is_actionable():
    # The whole symptom was exit_code=1 with an empty error message. Whatever
    # else changes, the agent must get the server name and its command back.
    mgr = _connected_stdio_manager()

    async def fake_do_call(session, tool_name, arguments):
        raise _ClosedResourceError()

    async def failed_reconnect(server_id):
        return False

    with patch.object(McpManager, "_do_call", side_effect=fake_do_call), \
         patch.object(McpManager, "_reconnect_server", side_effect=failed_reconnect):
        result = asyncio.run(mgr.call_tool("mcp__ntfy1__notify_me", {"message": "hi"}))

    assert result["exit_code"] == 1
    error = result["error"]
    assert error.strip() != ""
    assert "ntfy" in error
    assert "npx -y ntfy-me-mcp" in error
    assert "not running" in error


def test_stderr_capture_tail_is_quoted_in_the_failure_message():
    mgr = _connected_stdio_manager()
    handle = mgr._open_stderr_log("ntfy1")
    assert handle is not None
    handle.write("Error: NTFY_TOPIC is required\n")
    handle.flush()

    message = mgr._describe_call_failure("ntfy1", _ClosedResourceError())
    assert "NTFY_TOPIC is required" in message

    # Cleanup removes the capture file.
    path = mgr._stderr_logs["ntfy1"]["path"]
    mgr._close_stderr_log("ntfy1")
    import os

    assert not os.path.exists(path)


def test_http_server_is_not_auto_reconnected_mid_call():
    # Restarting an HTTP/OAuth server inside a tool call could re-enter the
    # browser authorization flow, so it must be left alone.
    mgr = _connected_stdio_manager()
    mgr._configs["ntfy1"]["transport"] = "http"

    with patch.object(McpManager, "connect_server", side_effect=_noop) as connect:
        reconnected = asyncio.run(mgr._reconnect_server("ntfy1"))

    assert reconnected is False
    connect.assert_not_called()


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    async def fake_start(server_id, name, url):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()
