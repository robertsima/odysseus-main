"""The MCP server and tool listings are for admins.

They show every configured server's command, URL and environment, which can
carry credentials, and every tool those servers expose.
"""
import pytest

pytestmark = pytest.mark.security

LISTINGS = ["/api/mcp/servers", "/api/mcp/tools", "/api/mcp/servers/some-server/tools"]


@pytest.mark.parametrize("path", LISTINGS)
def test_a_user_who_is_not_an_admin_cannot_list_mcp_config(api, path):
    assert api.as_user("alice").get(path).status_code == 403


def test_an_admin_can_list_the_servers(api):
    assert api.as_admin().get("/api/mcp/servers").status_code == 200
