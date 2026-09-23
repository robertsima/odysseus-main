"""Editing an MCP server's connection after it was added.

Settings could only reconnect, enable/disable, or toggle tools on an existing
server; command, args, env and URL were fixed once added. PUT
/api/mcp/servers/{id} changes them and reconnects.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from routes.mcp import mcp_routes

ROOT = Path(__file__).resolve().parent.parent


class _Query:
    def __init__(self, row):
        self.row = row

    def filter(self, *_):
        return self

    def first(self):
        return self.row


class _Db:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    def query(self, _model):
        return _Query(self.row)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _update_server(monkeypatch, row):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    db = _Db(row)
    monkeypatch.setattr(mcp_routes, "SessionLocal", lambda: db)
    manager = MagicMock()
    manager.restart_server = AsyncMock(return_value=True)
    manager.get_server_status = MagicMock(return_value={"status": "connected", "tool_count": 3})
    router = mcp_routes.setup_mcp_routes(manager)
    route = [r for r in router.routes if getattr(r, "name", None) == "update_server"][-1]
    return route.endpoint, manager, db


def _row(**kw):
    base = dict(id="abc", name="files", transport="stdio", command="npx", args='["-y", "old"]',
                env='{"TOKEN": "old"}', url=None, is_enabled=True, oauth_config=None, disabled_tools='["rm"]')
    base.update(kw)
    return SimpleNamespace(**base)


def test_update_changes_connection_and_reconnects(monkeypatch):
    row = _row()
    update, manager, db = _update_server(monkeypatch, row)
    out = asyncio.run(update(server_id="abc", request=None, name=" files ", transport="stdio",
                             command="uvx", args='["new-server"]', env='{"TOKEN": "new"}', url=None))
    assert (row.name, row.command, row.args, row.env) == ("files", "uvx", '["new-server"]', '{"TOKEN": "new"}')
    assert row.disabled_tools == '["rm"]'  # tool choices kept
    assert db.commits == 1
    manager.restart_server.assert_awaited_once()
    assert manager.restart_server.await_args.kwargs["args"] == ["new-server"]
    assert out["connected"] is True and out["tool_count"] == 3


def test_switching_to_a_remote_transport_clears_the_command(monkeypatch):
    row = _row()
    update, manager, _ = _update_server(monkeypatch, row)
    asyncio.run(update(server_id="abc", request=None, name="files", transport="http",
                       command="npx", args="[]", env="{}", url="http://mcp.local/mcp"))
    assert (row.transport, row.command, row.url) == ("http", None, "http://mcp.local/mcp")


def test_disabled_server_is_saved_but_not_started(monkeypatch):
    row = _row(is_enabled=False)
    update, manager, _ = _update_server(monkeypatch, row)
    asyncio.run(update(server_id="abc", request=None, name="files", transport="stdio",
                       command="uvx", args="[]", env="{}", url=None))
    assert row.command == "uvx"
    manager.restart_server.assert_not_called()


@pytest.mark.parametrize("field,value", [("args", "not json"), ("args", '{"a": 1}'), ("env", "nope"), ("env", "[1]")])
def test_bad_json_is_refused_without_touching_the_server(monkeypatch, field, value):
    row = _row()
    update, manager, db = _update_server(monkeypatch, row)
    kwargs = dict(server_id="abc", request=None, name="files", transport="stdio",
                  command="npx", args="[]", env="{}", url=None)
    kwargs[field] = value
    with pytest.raises(HTTPException) as exc:
        asyncio.run(update(**kwargs))
    assert exc.value.status_code == 400
    assert row.env == '{"TOKEN": "old"}' and db.commits == 0
    manager.restart_server.assert_not_called()


def test_missing_command_or_url_is_refused(monkeypatch):
    update, _, _ = _update_server(monkeypatch, _row())
    with pytest.raises(HTTPException):
        asyncio.run(update(server_id="abc", request=None, name="files", transport="stdio",
                           command="", args="[]", env="{}", url=None))
    with pytest.raises(HTTPException):
        asyncio.run(update(server_id="abc", request=None, name="files", transport="sse",
                           command=None, args="[]", env="{}", url=""))


def test_settings_editor_offers_connection_settings():
    js = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    assert "Connection settings" in js and 'id="uf-mcp-save-conn"' in js
    assert "method: 'PUT', body: fd" in js
