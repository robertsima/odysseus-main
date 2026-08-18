"""MCP bridge to a Pi coding-agent worker running on a Windows host.

Odysseus runs this server locally over stdio. Each tool call opens a
non-interactive SSH connection to the Windows host, starts Pi in RPC mode for
the requested project, submits one task, and returns Pi's final response.
Pi's file and shell tools therefore execute on Windows, not in the Odysseus
container.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import PureWindowsPath
import re
import shutil
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


server = Server("pi-worker")

HOST_ENV = "ODYSSEUS_PI_WORKER_HOST"
SCRIPT_ENV = "ODYSSEUS_PI_WORKER_SCRIPT"
ROOT_ENV = "ODYSSEUS_PI_WORKER_ROOT"
IDENTITY_ENV = "ODYSSEUS_PI_WORKER_IDENTITY_FILE"
DOC_ROOT_ENV = "ODYSSEUS_PI_DOCUMENTATION_ROOT"

DEFAULT_SCRIPT = "D:/Development/Start-Pi-Worker.ps1"
DEFAULT_ROOT = "D:/Development"
DEFAULT_DOC_ROOT = "/app/data/personal_docs/AI Mind"
DEFAULT_DOC_NAME = "Local Model Delegation.md"
DEFAULT_TIMEOUT_SECONDS = 900
MAX_TIMEOUT_SECONDS = 1800
MAX_TASK_CHARS = 40_000
MAX_RESULT_CHARS = 200_000

_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$")
_SAFE_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[A-Za-z0-9 _./\\-]*$")
_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


def _text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


def _normalize_windows_path(value: str, *, field: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or not _SAFE_WINDOWS_PATH_RE.fullmatch(raw):
        raise ValueError(f"{field} must be an absolute Windows path without shell metacharacters")
    path = PureWindowsPath(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be an absolute Windows path without '..'")
    return str(path).replace("\\", "/").rstrip("/")


def _validate_project_path(project_path: str) -> str:
    project = _normalize_windows_path(project_path, field="project_path")
    root = _normalize_windows_path(os.environ.get(ROOT_ENV, DEFAULT_ROOT), field=ROOT_ENV)
    project_lower = project.casefold()
    root_lower = root.casefold()
    if project_lower != root_lower and not project_lower.startswith(root_lower + "/"):
        raise ValueError(f"project_path must stay under {root}")
    return project


def _worker_host() -> str:
    host = os.environ.get(HOST_ENV, "").strip()
    if not host:
        raise ValueError(f"{HOST_ENV} is not configured")
    if not _HOST_RE.fullmatch(host):
        raise ValueError(f"{HOST_ENV} must look like user@hostname")
    return host


def _remote_command(project_path: str, thinking: str, no_session: bool) -> str:
    script = _normalize_windows_path(os.environ.get(SCRIPT_ENV, DEFAULT_SCRIPT), field=SCRIPT_ENV)
    session_arg = " -NoSession" if no_session else ""
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f'-File "{script}" -ProjectPath "{project_path}" -Rpc '
        f"-Thinking {thinking}{session_arg}"
    )


def _ssh_argv(project_path: str, thinking: str, no_session: bool) -> list[str]:
    ssh = shutil.which("ssh")
    if not ssh:
        raise ValueError("ssh executable was not found in the Odysseus runtime")
    args = [
        ssh,
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
    ]
    identity = os.environ.get(IDENTITY_ENV, "").strip()
    if identity:
        args.extend(["-i", identity])
    args.extend([_worker_host(), _remote_command(project_path, thinking, no_session)])
    return args


def _documentation_path() -> str:
    root = os.path.realpath(os.environ.get(DOC_ROOT_ENV, DEFAULT_DOC_ROOT))
    if not root:
        raise ValueError(f"{DOC_ROOT_ENV} is not configured")
    if os.path.basename(root).casefold() != "ai mind":
        raise ValueError(f"{DOC_ROOT_ENV} must point to the AI Mind directory")
    path = os.path.realpath(os.path.join(root, DEFAULT_DOC_NAME))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("documentation path must stay inside AI Mind")
    return path


def _clean_doc_field(value, *, field: str, required: bool = True, limit: int = 8_000) -> str:
    text = str(value or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return " ".join(text.split()).replace("`", "'")


def _append_acceptance_record(arguments: dict) -> str:
    project = _validate_project_path(arguments.get("project_path", ""))
    task = _clean_doc_field(arguments.get("task"), field="task", limit=2_000)
    outcome = _clean_doc_field(arguments.get("outcome"), field="outcome")
    verification = _clean_doc_field(arguments.get("verification"), field="verification")
    limitations = _clean_doc_field(
        arguments.get("limitations"), field="limitations", required=False, limit=4_000
    )
    files = arguments.get("files_changed", [])
    if not isinstance(files, list) or len(files) > 50:
        raise ValueError("files_changed must be an array with at most 50 entries")
    cleaned_files = [
        _clean_doc_field(item, field="files_changed item", limit=500)
        for item in files
    ]

    path = _documentation_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    is_new = not os.path.exists(path)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"## {timestamp} — {task}",
        "",
        f"- **Project:** `{project}`",
        f"- **Outcome:** {outcome}",
        f"- **Verification:** {verification}",
    ]
    if cleaned_files:
        lines.append("- **Files changed:** " + ", ".join(f"`{item}`" for item in cleaned_files))
    if limitations:
        lines.append(f"- **Limitations/follow-up:** {limitations}")
    lines.extend(["", ""])

    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        if is_new:
            handle.write(
                "# Local Model Delegation\n\n"
                "Accepted changes delegated to the Windows Pi/Qwen worker.\n\n"
            )
        handle.write("\n".join(lines))
    return path


async def _terminate(proc) -> None:
    if getattr(proc, "returncode", None) is not None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, AttributeError):
        return
    try:
        await proc.wait()
    except Exception:
        pass


async def _run_pi_task(
    project_path: str,
    task: str,
    *,
    thinking: str = "medium",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    no_session: bool = False,
) -> str:
    project = _validate_project_path(project_path)
    task = str(task or "").strip()
    if not task:
        raise ValueError("task is required")
    if len(task) > MAX_TASK_CHARS:
        raise ValueError(f"task exceeds {MAX_TASK_CHARS} characters")
    if thinking not in _THINKING_LEVELS:
        raise ValueError(f"thinking must be one of: {', '.join(sorted(_THINKING_LEVELS))}")
    try:
        timeout = max(10, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be an integer") from exc

    argv = _ssh_argv(project, thinking, no_session)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    stderr_task = asyncio.create_task(proc.stderr.read())
    deadline = time.monotonic() + timeout

    async def send(payload: dict) -> None:
        proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def read_event() -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not raw:
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            detail = stderr[-4000:] if stderr else f"SSH worker exited with status {proc.returncode}"
            raise RuntimeError(detail)
        try:
            event = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw.decode("utf-8", errors="replace").strip()[:500]
            raise RuntimeError(f"Pi worker emitted non-JSON stdout: {preview}") from exc
        if not isinstance(event, dict):
            raise RuntimeError("Pi worker emitted a non-object JSON event")
        return event

    try:
        await send({"type": "prompt", "id": "odysseus-prompt", "message": task})
        while True:
            event = await read_event()
            if event.get("type") == "response" and event.get("id") == "odysseus-prompt":
                if event.get("success") is not True:
                    raise RuntimeError(str(event.get("error") or "Pi rejected the task"))
            if event.get("type") == "agent_settled":
                break

        await send({"type": "get_last_assistant_text", "id": "odysseus-result"})
        while True:
            event = await read_event()
            if event.get("type") != "response" or event.get("id") != "odysseus-result":
                continue
            if event.get("success") is not True:
                raise RuntimeError(str(event.get("error") or "Unable to read Pi result"))
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            result = str(data.get("text") or "").strip()
            if not result:
                raise RuntimeError("Pi completed without a final text response")
            return result[:MAX_RESULT_CHARS]
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Pi worker timed out after {timeout} seconds") from exc
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        await _terminate(proc)
        if not stderr_task.done():
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_pi_task",
            description=(
                "Delegate a coding or repository task to the Windows Pi agent. Pi can inspect, "
                "edit, and run commands inside the selected project under D:/Development. "
                "Use for bounded, low-to-medium complexity work that does not need a large context "
                "window. Pass paths, symbols, constraints, and acceptance tests instead of pasted "
                "files or conversation history. The call is non-interactive."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Absolute Windows project path under D:/Development.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Complete task and acceptance criteria for the Pi agent.",
                    },
                    "thinking": {
                        "type": "string",
                        "enum": sorted(_THINKING_LEVELS),
                        "default": "medium",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "default": DEFAULT_TIMEOUT_SECONDS,
                    },
                    "no_session": {
                        "type": "boolean",
                        "default": False,
                        "description": "Do not persist the delegated Pi session.",
                    },
                },
                "required": ["project_path", "task"],
            },
        ),
        Tool(
            name="record_pi_task",
            description=(
                "Append a concise acceptance record to the AI Mind Local Model Delegation note. "
                "Call only after the primary harness independently judges the delegated change "
                "sufficient and verifies its tests. This tool never writes to Vault Mind."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Absolute Windows project path under D:/Development.",
                    },
                    "task": {"type": "string", "description": "Short accepted-task title."},
                    "outcome": {"type": "string", "description": "Concise result summary."},
                    "verification": {
                        "type": "string",
                        "description": "Tests or checks independently reviewed by the harness.",
                    },
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 50,
                    },
                    "limitations": {
                        "type": "string",
                        "description": "Known limitations or follow-up work, if any.",
                    },
                },
                "required": ["project_path", "task", "outcome", "verification"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "record_pi_task":
        try:
            path = await asyncio.to_thread(_append_acceptance_record, arguments)
        except Exception as exc:
            return _text(f"Error: {exc}")
        return _text(f"Recorded accepted Pi task in {path}")
    if name != "run_pi_task":
        return _text(f"Unknown tool: {name}")
    try:
        result = await _run_pi_task(
            arguments.get("project_path", ""),
            arguments.get("task", ""),
            thinking=str(arguments.get("thinking") or "medium"),
            timeout_seconds=arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            no_session=bool(arguments.get("no_session", False)),
        )
    except Exception as exc:
        return _text(f"Error: {exc}")
    return _text(result)


async def run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
