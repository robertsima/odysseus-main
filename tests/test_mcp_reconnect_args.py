"""Verify that MCP reconnect via the agent tool passes full server metadata."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from core.database import McpServer


def test_reconnect_passes_full_server_config(monkeypatch, app_db):
    """do_manage_mcp reconnect must pass name/transport/command/args/env/url
    of the stored row to the manager."""
    import core.database as cdb
    import src.agent_tools.admin_tools as admin_tools

    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    db = app_db.SessionLocal()
    db.add(McpServer(
        id="srv-123", name="test-server", transport="stdio", command="/usr/bin/test",
        args=json.dumps(["--flag"]), env=json.dumps({"KEY": "val"}), url=None,
        is_enabled=True,
    ))
    db.commit()
    db.close()

    # Reconnect goes through restart_server() so the teardown and the rebuild
    # happen under one per-server lock; the manager would spawn the process.
    fake_mcp = MagicMock()
    fake_mcp.restart_server = AsyncMock(return_value=True)
    fake_mcp.get_server_status = MagicMock(return_value={"tool_count": 3})
    monkeypatch.setattr(admin_tools, "get_mcp_manager", lambda: fake_mcp)

    result = asyncio.run(admin_tools.do_manage_mcp(
        json.dumps({"action": "reconnect", "server_id": "srv-123"})
    ))

    assert result["exit_code"] == 0
    fake_mcp.restart_server.assert_called_once_with(
        server_id="srv-123",
        name="test-server",
        transport="stdio",
        command="/usr/bin/test",
        args=["--flag"],
        env={"KEY": "val"},
        url=None,
    )
