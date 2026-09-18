"""Global tool revocations are fresh execution-time authorization."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import settings
from src.tool_execution import execute_tool_block


def _block(name, content="{}"):
    return SimpleNamespace(tool_type=name, content=content)


def test_strict_disabled_reader_missing_file_is_fresh_install_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "missing.json"))
    assert settings.load_disabled_tools_strict() == frozenset()


@pytest.mark.parametrize("payload", [
    "not json",
    "[]",
    '{"disabled_tools":"bash"}',
    '{"disabled_tools":["bash",7]}',
])
def test_strict_disabled_reader_rejects_unreadable_policy_shapes(tmp_path, monkeypatch, payload):
    path = tmp_path / "settings.json"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(path))
    with pytest.raises((ValueError, json.JSONDecodeError)):
        settings.load_disabled_tools_strict()


def test_strict_disabled_reader_bypasses_failsoft_ttl_cache(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"disabled_tools":[]}', encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(path))
    settings._settings_cache = (float("inf"), {"disabled_tools": []})
    path.write_text('{"disabled_tools":["bash"]}', encoding="utf-8")
    assert settings.load_disabled_tools_strict() == frozenset({"bash"})


def test_dispatcher_blocks_fresh_global_revocation_and_email_alias(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"disabled_tools":["mcp__email__send_email"]}', encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(path))
    desc, result = asyncio.run(execute_tool_block(_block("send_email")))
    assert desc == "send_email: BLOCKED"
    assert result["blocked_reason"] == "fresh_global_disabled"


def test_dispatcher_fails_closed_when_global_policy_read_fails(monkeypatch):
    monkeypatch.setattr(
        settings, "load_disabled_tools_strict",
        lambda: (_ for _ in ()).throw(PermissionError("denied")),
    )
    _, result = asyncio.run(execute_tool_block(_block("read_file", '{"path":"x"}')))
    assert result["blocked_reason"] == "global_policy_unavailable"


def test_discovery_receives_fresh_global_revocations(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"disabled_tools":["web_search"]}', encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(path))

    class Discovery:
        async def discover(self, query, max_results, *, settings):
            assert "web_search" in settings["_runtime_disabled_tools"]
            return {"loaded_names": []}

    content = json.dumps({"query": "web search", "max_results": 3})
    desc, result = asyncio.run(execute_tool_block(
        _block("discover_tools", content), tool_discovery=Discovery(),
    ))
    assert desc.startswith("discover_tools:")
    assert result["loaded_names"] == []
