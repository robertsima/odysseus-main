import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.builtin_mcp import _BUILTIN_SERVERS
from src.lotus_checkins import LotusCheckinStore, owner_storage_key
from src.mcp_manager import _BUILTIN_FUNCTION_CALLING_SERVERS, McpManager

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.gpu-nvidia.yml",
    ROOT / "docker-compose.gpu-amd.yml",
    ROOT / "docker-compose.zimaos-local.yml",
)


def _compose_environment(path: Path) -> dict[str, str]:
    environment = yaml.safe_load(path.read_text(encoding="utf-8"))["services"][
        "odysseus"
    ]["environment"]
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    return {
        str(item).split("=", 1)[0]: str(item).split("=", 1)[1]
        for item in environment
        if "=" in str(item)
    }


def test_lotus_is_available_for_filtered_chat_function_calling():
    assert _BUILTIN_SERVERS["lotus"] == (
        "mcp_servers/lotus_server.py",
        "Built-in: Lotus",
    )
    assert McpManager().is_builtin("lotus")
    assert "lotus" in _BUILTIN_FUNCTION_CALLING_SERVERS


def test_compose_persists_lotus_under_odysseus_data_mount():
    for path in COMPOSE_FILES:
        env = _compose_environment(path)
        assert env["LOTUS_CONFIG"] == "/app/data/lotus/config.yaml", path.name
        assert env["LOTUS_DATA_DIR"] == "/app/data/lotus/data", path.name
        assert env["LOTUS_IMPORT_ROOT"] == "/app/data/lotus/imports", path.name
        assert env["LOTUS_TOOL_NAME_STYLE"] == "underscore", path.name


def test_builtin_lotus_completes_mcp_handshake_with_safe_defaults(tmp_path):
    async def probe():
        env = dict(os.environ)
        env.update(
            {
                "LOTUS_CONFIG": str(tmp_path / "missing-config.yaml"),
                "LOTUS_DATA_DIR": str(tmp_path / "data"),
                "LOTUS_IMPORT_ROOT": str(tmp_path / "imports"),
                "LOTUS_TOOL_NAME_STYLE": "underscore",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "mcp_servers" / "lotus_server.py")],
            env=env,
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            missing = await session.call_tool("mood_get_import_status", {})
            alice = await session.call_tool(
                "mood_get_import_status", {"_odysseus_owner": "alice"}
            )
            bob = await session.call_tool(
                "mood_get_import_status", {"_odysseus_owner": "bob"}
            )
            return (
                [tool.name for tool in result.tools],
                json.loads(missing.content[0].text),
                json.loads(alice.content[0].text),
                json.loads(bob.content[0].text),
            )

    names, missing, alice, bob = asyncio.run(probe())
    assert names == [
        "mood_get_import_status",
        "mood_import_file",
        "mood_search_entries",
        "mood_summarize_period",
        "mood_detect_low_energy_patterns",
    ]
    assert missing["error"] == "owner_required"
    assert alice == {"batches": []}
    assert bob == {"batches": []}
    assert (
        tmp_path / "data" / "users" / owner_storage_key("alice") / "mood.db"
    ).is_file()
    assert (
        tmp_path / "data" / "users" / owner_storage_key("bob") / "mood.db"
    ).is_file()


def test_lotus_private_filter_allows_local_and_blocks_remote_models():
    from src.agent_loop import _LOTUS_MCP_TOOL_NAMES, _apply_private_mcp_filter

    local_map: dict[str, set] = {}
    local_disabled: set[str] = set()
    _apply_private_mcp_filter(
        "http://127.0.0.1:11434/v1/chat/completions",
        local_map,
        local_disabled,
    )
    assert local_map == {}
    assert local_disabled == set()

    remote_map: dict[str, set] = {}
    remote_disabled: set[str] = set()
    _apply_private_mcp_filter(
        "https://api.openai.com/v1/chat/completions",
        remote_map,
        remote_disabled,
    )
    assert remote_map == {"lotus": _LOTUS_MCP_TOOL_NAMES}
    assert remote_disabled == {f"mcp__lotus__{name}" for name in _LOTUS_MCP_TOOL_NAMES}


def test_lotus_tool_execution_injects_authenticated_owner(monkeypatch):
    from src import tool_execution

    class FakeMcp:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return {"stdout": "{}", "stderr": "", "exit_code": 0}

    fake = FakeMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: fake)
    result = asyncio.run(
        tool_execution.execute_tool_block(
            SimpleNamespace(
                tool_type="mcp__lotus__mood_summarize_period",
                content=json.dumps(
                    {
                        "start": "2026-08-01T00:00:00-04:00",
                        "end": "2026-08-10T00:00:00-04:00",
                    }
                ),
            ),
            owner="alice",
        )
    )[1]
    assert result["exit_code"] == 0
    assert fake.calls[0][1]["_odysseus_owner"] == "alice"


def test_lotus_mcp_reads_ui_checkins_only_for_the_injected_owner(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    imports_dir = tmp_path / "imports"
    monkeypatch.setenv("LOTUS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LOTUS_IMPORT_ROOT", str(imports_dir))
    LotusCheckinStore("alice").create_checkin(
        {
            "occurred_at": "2026-08-05T09:30:00-04:00",
            "timezone": "America/New_York",
            "emotion_label": "calm",
            "emotion_family": "pleasant_low",
            "valence": 0.6,
            "energy": 0.25,
            "intensity": 0.5,
            "note": "owner-only note",
            "tags": ["morning"],
            "context": {"entry_method": "test"},
        }
    )

    async def probe():
        env = dict(os.environ)
        env.update(
            {
                "LOTUS_CONFIG": str(tmp_path / "missing-config.yaml"),
                "LOTUS_DATA_DIR": str(data_dir),
                "LOTUS_IMPORT_ROOT": str(imports_dir),
                "LOTUS_TOOL_NAME_STYLE": "underscore",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "mcp_servers" / "lotus_server.py")],
            env=env,
        )
        query = {
            "start": "2026-08-01T00:00:00-04:00",
            "end": "2026-08-10T23:59:59-04:00",
        }
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            alice = await session.call_tool(
                "mood_summarize_period",
                {**query, "_odysseus_owner": "alice"},
            )
            bob = await session.call_tool(
                "mood_summarize_period",
                {**query, "_odysseus_owner": "bob"},
            )
            return json.loads(alice.content[0].text), json.loads(bob.content[0].text)

    alice, bob = asyncio.run(probe())
    assert alice["total_entries"] == 1
    assert bob["total_entries"] == 0
    assert "owner-only note" not in json.dumps(alice)


def test_dispatcher_owner_argument_is_hidden_from_model_schemas():
    manager = McpManager()
    manager._connections["lotus"] = {"name": "Built-in: Lotus"}
    manager._tools["lotus"] = [
        {
            "name": "mood_summarize_period",
            "description": "Summarize moods",
            "input_schema": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "_odysseus_owner": {"type": "string"},
                },
                "required": ["start", "_odysseus_owner"],
            },
        }
    ]
    schema = manager.get_all_openai_schemas()[0]["function"]["parameters"]
    assert "_odysseus_owner" not in schema["properties"]
    assert schema["required"] == ["start"]
