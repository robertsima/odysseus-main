"""Editing an MCP server's connection after it was added.

Settings could only reconnect, enable/disable, or toggle tools on an existing
server; command, args, env and URL were fixed once added. PUT
/api/mcp/servers/{id} changes them and reconnects. The route, auth and the
stored row are real; the MCP manager is replaced, because restarting a server
would spawn its process.
"""
import json

import pytest

import src.database
from src.database import McpServer


@pytest.fixture
def restarts(api, monkeypatch):
    calls = []

    async def restart_server(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(api.module.mcp_manager, "restart_server", restart_server)
    monkeypatch.setattr(
        api.module.mcp_manager, "get_server_status",
        lambda server_id: {"status": "connected", "tool_count": 3})
    return calls


def _add_server(**overrides):
    fields = dict(
        id="abc", name="files", transport="stdio", command="npx", args='["-y", "old"]',
        env='{"TOKEN": "old"}', url=None, is_enabled=True, disabled_tools='["rm"]')
    fields.update(overrides)
    db = src.database.SessionLocal()
    db.add(McpServer(**fields))
    db.commit()
    db.close()


def _stored():
    db = src.database.SessionLocal()
    try:
        row = db.query(McpServer).filter(McpServer.id == "abc").one()
        return {c: getattr(row, c) for c in
                ("name", "transport", "command", "args", "env", "url", "disabled_tools")}
    finally:
        db.close()


def _put(api, **overrides):
    form = dict(name="files", transport="stdio", command="npx", args="[]", env="{}")
    form.update(overrides)
    return api.as_admin().put("/api/mcp/servers/abc", data=form)


def test_update_changes_connection_and_reconnects(api, restarts):
    _add_server()

    response = _put(api, name=" files ", command="uvx", args='["new-server"]', env='{"TOKEN": "new"}')

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True and body["tool_count"] == 3
    stored = _stored()
    assert (stored["name"], stored["command"], stored["args"]) == ("files", "uvx", '["new-server"]')
    assert json.loads(stored["env"]) == {"TOKEN": "new"}
    assert stored["disabled_tools"] == '["rm"]'  # tool choices kept
    assert len(restarts) == 1 and restarts[0]["args"] == ["new-server"]


def test_switching_to_a_remote_transport_clears_the_command(api, restarts):
    _add_server()

    assert _put(api, transport="http", url="http://mcp.local/mcp").status_code == 200

    stored = _stored()
    assert (stored["transport"], stored["command"], stored["url"]) == ("http", None, "http://mcp.local/mcp")


def test_a_disabled_server_is_saved_but_not_started(api, restarts):
    _add_server(is_enabled=False)

    assert _put(api, command="uvx").status_code == 200

    assert _stored()["command"] == "uvx"
    assert restarts == []


@pytest.mark.parametrize("field,value", [("args", "not json"), ("args", '{"a": 1}'), ("env", "nope"), ("env", "[1]")])
def test_bad_json_is_refused_without_touching_the_server(api, restarts, field, value):
    _add_server()

    assert _put(api, **{field: value}).status_code == 400

    assert _stored()["env"] == '{"TOKEN": "old"}' and restarts == []


@pytest.mark.parametrize("overrides", [
    dict(transport="stdio", command=""),
    dict(transport="sse", command=None, url=""),
])
def test_missing_command_or_url_is_refused(api, restarts, overrides):
    _add_server()

    assert _put(api, **overrides).status_code == 400

    assert restarts == []


@pytest.mark.security
def test_a_user_who_is_not_an_admin_cannot_edit_a_server(api, restarts):
    _add_server()

    response = api.as_user("alice").put(
        "/api/mcp/servers/abc", data=dict(name="files", transport="stdio", command="sh", args="[]", env="{}"))

    assert response.status_code == 403
    assert _stored()["command"] == "npx" and restarts == []
