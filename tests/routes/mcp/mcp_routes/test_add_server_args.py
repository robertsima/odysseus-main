"""The Args field of the Add MCP Server form (issue #6211).

A malformed Args value used to fall back to an empty argv, so the server
registered as connected while its stdio process got no arguments. The route,
auth and stored row are real; the MCP manager is replaced because connecting
would spawn a process.
"""
import json

import pytest

import src.database
from src.database import McpServer


@pytest.fixture
def connections(api, monkeypatch):
    calls = []

    async def connect_server(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(api.module.mcp_manager, "connect_server", connect_server)
    monkeypatch.setattr(
        api.module.mcp_manager, "get_server_status",
        lambda server_id: {"status": "connected", "tool_count": 1})
    return calls


def _add(api, args):
    form = dict(name="filesystem", transport="stdio", command="mcp-server-filesystem")
    if args is not None:
        form["args"] = args
    return api.as_admin().post("/api/mcp/servers", data=form)


def _stored_args():
    db = src.database.SessionLocal()
    try:
        return [row.args for row in db.query(McpServer).all()]
    finally:
        db.close()


@pytest.mark.parametrize("args", ["/app/data/jarvis-files", "5", '{"a": 1}'])
def test_args_that_are_not_a_json_list_are_refused_and_nothing_is_saved(api, connections, args):
    response = _add(api, args)

    assert response.status_code == 400
    assert connections == []
    assert _stored_args() == []


def test_a_json_list_of_args_is_stored_and_passed_to_the_server(api, connections):
    response = _add(api, json.dumps(["/app/data/jarvis-files"]))

    assert response.status_code == 200, response.text
    assert response.json()["connected"] is True
    assert connections[0]["args"] == ["/app/data/jarvis-files"]
    assert _stored_args() == [json.dumps(["/app/data/jarvis-files"])]


@pytest.mark.parametrize("args", ["", None])
def test_empty_args_mean_no_arguments(api, connections, args):
    response = _add(api, args)

    assert response.status_code == 200, response.text
    assert connections[0]["args"] == []
