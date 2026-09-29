"""Settings › Built-in: GET /api/mcp/builtin.

Built-in tool servers start in memory at boot and are not rows of the MCP
servers table, so /api/mcp/servers never listed them and no Settings page
showed whether Todoist, GitHub or Lotus were running (2026-09-28).
"""

from unittest.mock import MagicMock

from routes.mcp import mcp_routes
from src import builtin_mcp


def _builtin_endpoint(monkeypatch, statuses):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    manager = MagicMock()
    manager.get_server_status = MagicMock(side_effect=lambda sid: statuses.get(sid, {"status": "disconnected"}))
    router = mcp_routes.setup_mcp_routes(manager)
    route = [r for r in router.routes if getattr(r, "name", None) == "list_builtin"][-1]
    return route.endpoint


def test_lists_every_built_in_with_its_state(monkeypatch):
    for var in ("TODOIST_API_TOKEN", "ODYSSEUS_PI_WORKER_HOST", "GITHUB_PERSONAL_ACCESS_TOKEN", "ODYSSEUS_GITHUB_MCP_WRITE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TODOIST_API_TOKEN", "t")
    list_builtin = _builtin_endpoint(monkeypatch, {
        "lotus": {"status": "connected", "tool_count": 5},
        "todoist": {"status": "error", "error": "401 from Todoist"},
    })
    result = list_builtin(request=None)
    rows = {row["id"]: row for row in result["integrations"]}
    assert set(rows) == {entry["id"] for entry in builtin_mcp.BUILTIN_CATALOG}
    assert rows["lotus"]["status"] == "connected" and rows["lotus"]["tool_count"] == 5
    assert rows["todoist"]["status"] == "error" and rows["todoist"]["error"] == "401 from Todoist"
    # Optional ones without their variable say what to set instead of "stopped".
    assert rows["pi_worker"]["status"] == "not_configured"
    assert "ODYSSEUS_PI_WORKER_HOST" in rows["pi_worker"]["enable"]
    assert rows["github_read"]["status"] == "not_configured"
    # Always-on ones that are not running are reported as stopped, not "not set up".
    assert rows["memory"]["status"] == "disconnected" and rows["memory"]["configured"] is True
    assert result["disabled"] is builtin_mcp.MCP_DISABLED


def test_github_write_needs_both_the_flag_and_the_token(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_GITHUB_MCP_WRITE", "1")
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    assert {r["id"]: r for r in builtin_mcp.builtin_catalog()}["github_write"]["configured"] is False
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_x")
    assert {r["id"]: r for r in builtin_mcp.builtin_catalog()}["github_write"]["configured"] is True
