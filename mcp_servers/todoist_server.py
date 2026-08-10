"""
todoist_server.py

Thin MCP stdio wrapper around the official Todoist CLI.
"""

import asyncio
import os
import shutil

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("todoist")

TIMEOUT_SECONDS = 60
TOKEN_ENV_VAR = "TODOIST_API_TOKEN"


def _text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


def _redact_secrets(text: str) -> str:
    token = os.environ.get(TOKEN_ENV_VAR, "")
    if token:
        text = text.replace(token, "[REDACTED]")
    return text


def _validate_args(arguments: dict) -> list[str] | str:
    args = arguments.get("args") if isinstance(arguments, dict) else None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return 'Error: "args" must be an array of strings.'
    return args


async def _run_td(args: list[str], timeout: int = TIMEOUT_SECONDS) -> tuple[int, str, str]:
    if not os.environ.get(TOKEN_ENV_VAR):
        return 1, "", f"Error: {TOKEN_ENV_VAR} is not configured."

    td_path = shutil.which("td")
    if not td_path:
        return 1, "", "Error: Todoist CLI executable 'td' was not found in PATH."

    try:
        proc = await asyncio.create_subprocess_exec(
            td_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 1, "", f"Error: failed to start Todoist CLI: {_redact_secrets(str(exc))}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return 1, "", f"Error: Todoist CLI timed out after {timeout} seconds."

    out_text = _redact_secrets(stdout.decode(errors="replace").strip())
    err_text = _redact_secrets(stderr.decode(errors="replace").strip())
    return proc.returncode or 0, out_text, err_text


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="todoist",
            description=(
                "Run the official Todoist CLI to list Todoist tasks, inspect today, inbox, "
                "and upcoming views, create tasks, update tasks, complete tasks, and manage "
                "Todoist projects and organization. Pass CLI arguments as an array of strings, "
                'for example {"args":["today","--json"]} or '
                '{"args":["task","quickadd","Finish report tomorrow p1","--json"]}.'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments passed directly to td, without a shell.",
                    },
                },
                "required": ["args"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "todoist":
        return _text(f"Unknown tool: {name}")

    args = _validate_args(arguments)
    if isinstance(args, str):
        return _text(args)

    returncode, stdout, stderr = await _run_td(args)
    if returncode != 0:
        detail = stderr or stdout or "Todoist CLI failed without output."
        return _text(f"Error: td exited with status {returncode}\n{detail}")

    return _text(stdout or stderr or "Todoist CLI completed successfully.")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
