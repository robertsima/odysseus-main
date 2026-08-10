"""Owner-scoped built-in stdio entry point for the bundled Lotus MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
LOTUS_SOURCE = APP_ROOT / "lotus-mcp" / "src"
LOTUS_ROOT = Path(os.environ.get("LOTUS_ROOT", APP_ROOT / "data" / "lotus"))

sys.path.insert(0, str(LOTUS_SOURCE))

os.environ.setdefault("LOTUS_CONFIG", str(LOTUS_ROOT / "config.yaml"))
os.environ.setdefault("LOTUS_DATA_DIR", str(LOTUS_ROOT / "data"))
os.environ.setdefault("LOTUS_IMPORT_ROOT", str(LOTUS_ROOT / "imports"))
os.environ.setdefault("LOTUS_TOOL_NAME_STYLE", "underscore")

_OWNER_ARG = "_odysseus_owner"
_SINGLE_USER = "__single_user__"


def _owner_key(owner: str) -> str:
    """Match ``src.lotus_checkins.owner_storage_key`` without importing the app."""
    return hashlib.sha256((owner or _SINGLE_USER).encode("utf-8")).hexdigest()[:32]


def _owner_config(base_config, owner: str):
    """Clone the conservative Lotus config with paths confined to one owner."""
    from lotus_mcp.config import Paths

    key = _owner_key(owner)
    data_root = Path(os.environ["LOTUS_DATA_DIR"]) / "users" / key
    import_root = Path(os.environ["LOTUS_IMPORT_ROOT"]) / "users" / key
    paths = Paths(
        database_path=data_root / "mood.db",
        state_dir=data_root,
        import_root=import_root / "incoming",
        processed_dir=import_root / "processed",
        failed_dir=import_root / "failed",
    )
    return base_config.model_copy(update={"paths": paths})


async def _run() -> None:
    from lotus_mcp.config import load_config
    from lotus_mcp.server import ServerContext, build_tools, call_tool_sync
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent

    base_config = load_config()
    contexts: dict[str, ServerContext] = {}
    server = Server("lotus-mcp")

    @server.list_tools()
    async def list_tools():
        tools = build_tools(base_config.tool_name_style)
        # The Odysseus dispatcher supplies this value. It must exist in the
        # wire schema for MCP validation, but mcp_manager removes it before
        # presenting schemas or prompt hints to a model.
        for tool in tools:
            tool.inputSchema.setdefault("properties", {})[_OWNER_ARG] = {
                "type": "string"
            }
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        args = dict(arguments or {})
        owner = str(args.pop(_OWNER_ARG, "") or "").strip()
        if not owner:
            payload = {
                "error": "owner_required",
                "message": "Lotus requires an authenticated Odysseus owner.",
            }
        else:
            context = contexts.get(owner)
            if context is None:
                context = ServerContext(_owner_config(base_config, owner))
                contexts[owner] = context
            payload = call_tool_sync(context, name, args)
        return [
            TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)
            )
        ]

    logging.basicConfig(
        level=getattr(logging, base_config.log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def run() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    run()
