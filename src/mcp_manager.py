"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import json
import logging
import os
import re
import asyncio 
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from src.database import McpServer, SessionLocal

from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)

# Lotus is filtered per request in agent_loop according to the owner's
# local/LAN/API access policy. Approved endpoints may use it; denied endpoints
# have every Lotus tool hidden and runtime-blocked.
_BUILTIN_FUNCTION_CALLING_SERVERS = {
    "builtin_browser",
    "todoist",
    "lotus",
    "pi_worker",
    "github_read",
    "github_write",
}


# ── How much MCP may stay bound on every turn ──
#
# `gated_tool_names` below decides which MCP tools have to WIN retrieval to be
# sent and which are attached unconditionally. The original split was "builtin
# catalog = gated, anything the user added = always bound", resting on the
# stated assumption that a user-added server "is typically a handful of tools".
# That assumption is what broke. On 2026-09-16 a turn that had selected 11
# tools was sent 48: five connected servers contributed 37 unselected schemas,
# 11,146 schema tokens, one firecrawl server accounting for 27 of them.
#
# The cost was not only tokens. The turn's own tools (`read_app_logs`, the file
# tools) had not been selected, so the only callable things in its schema list
# were those 37 always-bound strangers, and it spent six rounds steering a
# design tool and a docs lookup at a "read the logs and fix the bug" request
# before giving up in prose. An always-bound tool is not neutral ballast: it is
# an active suggestion about what this turn is for.
#
# The guarantee itself is still right for a SMALL server -- it is what stops a
# real, connected tool from vanishing the moment a follow-up message
# ("continue", "now run it") fails to resemble its description. So keep it and
# bound it, with two limits expressed in tools rather than tokens. Tools are
# the unit the operator actually sees (the MCP settings list, the tool-index
# count); token cost per schema varies several-fold with how verbose a server's
# parameter prose is, so a token cap would demote different servers on
# different days for reasons no one can see in the UI.
#
#   * Per server. A server larger than _MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS is
#     not "a handful the user wired up for this job"; it is an ambient catalog
#     like the browser or GitHub ones, and is gated exactly like those.
#   * In total. Ten six-tool servers flood a turn just as thoroughly as one
#     sixty-tool server, so whatever survives the per-server rule is capped at
#     _MCP_ALWAYS_BOUND_TOTAL_MAX_TOOLS and trimmed LARGEST SERVER FIRST.
#     Largest-first frees the most schema budget per server that loses the
#     guarantee, so the fewest servers lose it -- demoting three two-tool
#     servers to save six schemas would break the guarantee three times over
#     for a sixth of the benefit.
#
# The numbers, from the 2026-09-16 measurement of ~300 schema tokens per MCP
# tool (11,146 / 37):
#   8 tools  ~= 2.4k tokens. Comparable to one builtin tool group, and still
#              affordable to waste on an 8k-context local model when the server
#              turns out to be irrelevant to the turn. It also matches the
#              observed shape of purpose-built servers (ntfy 2, context7 2,
#              penpot 5, sequentialthinking 1) while excluding scraped-API
#              catalogs (firecrawl 27).
#   24 tools ~= 7k tokens, i.e. at most three max-size servers. Past this point
#              MCP stops being an accessory to the builtin toolset and becomes
#              the bulk of what the model is asked to choose from, which is the
#              misrouting failure above.
#
# Demotion is deliberately the safe direction to be wrong in: a demoted tool is
# still indexed and still retrievable (see `get_tool_descriptions_for_prompt`,
# which feeds ToolIndex.index_mcp_tools for every connected server, gated or
# not), so over-demoting costs a round of retrieval, while under-demoting costs
# the whole turn. That asymmetry is also why the counts below are of DISCOVERED
# tools: a server whose tools are mostly disabled counts small once the caller
# passes its disabled map, and counts large otherwise, which is the cautious
# reading.
_MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS = 8
_MCP_ALWAYS_BOUND_TOTAL_MAX_TOOLS = 24


def _always_bound_limits() -> Tuple[int, int]:
    """(per-server cap, total cap) for unconditionally bound MCP tools.

    Settings-overridable like the other agent budgets (cf. agent_loop's
    `agent_input_token_budget`), for a host with a very large context window or
    a deliberately MCP-centric setup. A missing or non-numeric value falls back
    to the constant; a value <= 0 means "no cap for this dimension", which
    restores the pre-2026-09-16 always-bind-everything behaviour for an
    operator who knowingly wants it.
    """
    per_server = _MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS
    total = _MCP_ALWAYS_BOUND_TOTAL_MAX_TOOLS
    try:
        from src.settings import get_setting

        per_server = int(get_setting(
            "mcp_always_bound_server_max_tools", per_server,
        ))
        total = int(get_setting(
            "mcp_always_bound_total_max_tools", total,
        ))
    except (ImportError, TypeError, ValueError):
        # Settings unreadable (import cycle during early boot) or a hand-edited
        # non-numeric value: the defaults are a working policy, an exception
        # here would take the whole turn down.
        return _MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS, _MCP_ALWAYS_BOUND_TOTAL_MAX_TOOLS
    return per_server, total


def _model_visible_schema(schema: Any) -> Dict:
    """Remove dispatcher-injected arguments from a model-facing MCP schema."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    visible = dict(schema)
    properties = dict(visible.get("properties") or {})
    hidden = {name for name in properties if name.startswith("_odysseus_")}
    for name in hidden:
        properties.pop(name, None)
    visible["properties"] = properties
    if isinstance(visible.get("required"), list):
        visible["required"] = [
            name for name in visible["required"] if name not in hidden
        ]
    return visible

def _describe_exception(error: Optional[BaseException]) -> str:
    """Render an exception for logs/error payloads, never as an empty string.

    Several MCP-adjacent exceptions (anyio's ClosedResourceError/
    BrokenResourceError when a stdio subprocess's pipe closes underneath it,
    among others) carry no message -- str(e) is "". `str(error) if error
    else "Unknown error"` only catches error being None/falsy, not an
    Exception instance whose str() is empty (Exception instances are always
    truthy), so that fallback silently produced e.g. "MCP tool call failed:
    mcp__xyz__tool: " with nothing after the colon -- undiagnosable from
    logs alone. Fall back to the exception's type name so there's always
    something to search for/report.
    """
    if error is None:
        return "Unknown error"
    text = str(error).strip()
    return text if text else f"{type(error).__name__} (no error message — check the MCP server's own logs/stderr)"


# Exceptions that mean "the pipe to the MCP server is gone", as opposed to "the
# tool ran and failed". A stdio server whose subprocess has exited fails the very
# next call in ~1ms, before any bytes are written, so such a call provably had no
# side effects and is safe to replay on a fresh connection.
_DEAD_TRANSPORT_ERRORS = {
    "ClosedResourceError",   # anyio: stream closed underneath us
    "BrokenResourceError",   # anyio: peer went away mid-write
    "EndOfStream",           # anyio: reader hit EOF (subprocess exited)
    "BrokenPipeError",
    "ConnectionResetError",
    "ProcessLookupError",
}


def _is_dead_transport_error(error: Optional[BaseException]) -> bool:
    """True when an exception means the transport died, not that the tool failed."""
    if error is None:
        return False
    if isinstance(error, (BrokenPipeError, ConnectionResetError, ProcessLookupError)):
        return True
    return type(error).__name__ in _DEAD_TRANSPORT_ERRORS


# How much of a dead server's stderr to quote back to the agent/logs.
_STDERR_TAIL_BYTES = 2000


def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = _describe_exception(error)
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart Odysseus and reconnect the Browser MCP server."
        )

    if name.lower() == "todoist" or lower_command.startswith("td "):
        return (
            f"{raw_error}\n\n"
            "Todoist MCP could not start. The `td` binary is the Todoist CLI, not an MCP server, "
            "so adding `td` directly as a custom MCP server will close the connection during handshake.\n\n"
            "Use the built-in Todoist MCP server, or run the wrapper as a stdio server:\n\n"
            "python /app/mcp_servers/todoist_server.py"
        )

    return raw_error


# Caps for rendering untrusted MCP tool schemas into the agent prompt (issue #2660).
# MCP servers are third-party/user-added, so field names and parameter counts are
# untrusted input — bound them so an odd or hostile schema cannot distort the prompt.
_MCP_PARAM_MAX = 12   # max params rendered per tool
_MCP_TOKEN_MAX = 40   # max chars per rendered name / type token
_MCP_HINT_MAX = 300   # total-length backstop for the whole hint


def _sanitize_schema_token(value: Any, limit: int = _MCP_TOKEN_MAX) -> str:
    """Make an untrusted JSON-Schema token safe to splice into the prompt.

    Replaces control chars / newlines with a space, collapses whitespace, and
    length-caps the result, so a weird field name or type cannot inject newlines
    or run on. Normal short identifiers pass through unchanged.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _format_mcp_params(input_schema: Any) -> str:
    """Render an MCP tool's JSON-Schema inputs as a compact prompt hint.

    Without this the agent only sees a tool's name + description and has to
    guess its arguments (issue #2509). Produces e.g.
    ` Args (JSON): {"path": string (required), "limit": integer}` — names,
    coarse types, and required-ness, kept short so it stays prompt-friendly.
    Returns "" when there are no parameters.

    MCP servers are third-party, so names/types are sanitized and the parameter
    count + total length are capped (issue #2660); normal schemas are unaffected.
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(input_schema.get("required") or [])
    parts = []
    for pname, pinfo in list(props.items())[:_MCP_PARAM_MAX]:
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        ptype = pinfo.get("type") or "any"
        if isinstance(ptype, list):
            ptype = "|".join(str(x) for x in ptype)
        tag = f'"{_sanitize_schema_token(pname)}": {_sanitize_schema_token(ptype)}'
        if pname in required:
            tag += " (required)"
        parts.append(tag)
    extra = len(props) - len(parts)
    if extra > 0:
        parts.append(f"…+{extra} more")
    hint = " Args (JSON): {" + ", ".join(parts) + "}"
    if len(hint) > _MCP_HINT_MAX:
        hint = hint[:_MCP_HINT_MAX - 1].rstrip() + "…"
    return hint


# Tool-name prefixes that denote a read-only/inspection operation. Used to
# classify MCP tools for plan mode when the server provides no readOnlyHint.
# These are PREFIXES, not whole words (matched via str.startswith below), so a
# stem like "summar" intentionally covers "summarise"/"summarize"/"summary".
_MCP_READONLY_VERBS = (
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summar",
)


def mcp_tool_is_readonly(tool: Dict) -> bool:
    """Classify an MCP tool as safe (non-mutating) for plan mode.

    Prefer the server's own annotations (readOnlyHint / destructiveHint). When
    absent, fall back to a tool-name verb heuristic, and FAIL CLOSED (treat as
    write) for anything that doesn't clearly read — plan mode must not run a
    write tool just because its intent is ambiguous.
    """
    ann = tool.get("annotations")
    # annotations may be a dict or a pydantic model
    read_hint = None
    destructive = None
    if ann is not None:
        if isinstance(ann, dict):
            read_hint = ann.get("readOnlyHint")
            destructive = ann.get("destructiveHint")
        else:
            read_hint = getattr(ann, "readOnlyHint", None)
            destructive = getattr(ann, "destructiveHint", None)
    if read_hint is False or destructive is True:
        return False
    if read_hint is True:
        return True
    # No usable hint — heuristic on the tool name's leading verb.
    name = (tool.get("name") or "").lower()
    return name.startswith(_MCP_READONLY_VERBS)


class _ServerLock:
    """A per-server mutex that a single task may re-enter.

    Every mutation of McpManager's state dicts for one server_id runs under
    this lock so overlapping connect/disconnect cycles serialize instead of
    interleaving (see McpManager.restart_server for the bug this fixes).

    It has to be re-entrant *within one task* because the public entry points
    nest: _reconnect_server() takes the lock and then calls the public
    disconnect_server()/connect_server(), which take it again. A plain
    asyncio.Lock would self-deadlock there. Re-entry is keyed on the owning
    asyncio.Task, so a *different* task still blocks — which is exactly the
    serialization we need between concurrent HTTP requests.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._depth -= 1
        if self._depth <= 0:
            self._owner = None
            self._depth = 0
            self._lock.release()
        return False


class _OwnedStack:
    """An AsyncExitStack that one dedicated task both enters and exits.

    The MCP clients open anyio task groups, and an anyio cancel scope may only
    be exited by the task that entered it. The stack used to be entered by
    whichever task ran the connect (a request, the startup task, a restart)
    and closed by whichever task asked for the disconnect, which raised
    "Attempted to exit cancel scope in a different task than it was entered
    in" for every server at shutdown. Here the owner task runs ``setup``,
    hands its result back, then waits for ``aclose`` and unwinds the stack
    itself.
    """

    def __init__(self, label: str):
        self._label = label
        self._stop: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    async def open(self, setup):
        """Run ``setup(stack)`` in the owner task and return its result.

        If ``setup`` fails, the owner unwinds what it entered and the error is
        raised here. If the caller is cancelled (a connect timeout), the owner
        is cancelled too, so nothing is left half-open.
        """
        from contextlib import AsyncExitStack

        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self._stop = asyncio.Event()
        stop = self._stop

        async def owner():
            try:
                setup_error: Optional[BaseException] = None
                async with AsyncExitStack() as stack:
                    try:
                        result = await setup(stack)
                    except BaseException as exc:  # noqa: BLE001 - re-raised to the caller below
                        # Unwind without handing the error to the stack: a task
                        # group would wrap it in an ExceptionGroup, and the
                        # caller should see the handshake's own error.
                        setup_error = exc
                    else:
                        if ready.done():
                            return  # the caller gave up; unwind now
                        ready.set_result(result)
                        await stop.wait()
                if setup_error is not None and not ready.done():
                    if isinstance(setup_error, asyncio.CancelledError):
                        ready.cancel()
                    else:
                        ready.set_exception(setup_error)
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
            except BaseException as exc:  # noqa: BLE001 - reported to the caller or logged
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    logger.warning("MCP server %s connection ended: %s", self._label, _describe_exception(exc))

        self._task = loop.create_task(owner(), name=f"mcp-owner-{self._label}")
        try:
            return await ready
        except asyncio.CancelledError:
            self._task.cancel()
            raise

    async def aclose(self, timeout: float = 10.0) -> None:
        if self._stop is not None:
            self._stop.set()
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning("MCP server %s did not close within %.0fs; cancelling it", self._label, timeout)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class McpManager:
    """Manages MCP server connections and tool routing."""

    def __init__(self):
        # server_id -> connection state
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas
        self._tools: Dict[str, List[Dict]] = {}
        # server_id -> MCP ClientSession
        self._sessions: Dict[str, Any] = {}
        # server_id -> exit stack (for cleanup)
        self._stacks: Dict[str, Any] = {}
        # server_id -> background connect task (HTTP transport / OAuth)
        self._connect_tasks: Dict[str, Any] = {}
        # server_id -> connect_server() kwargs, so a crashed server can be
        # restarted on demand without a DB round-trip (see _reconnect_server).
        self._configs: Dict[str, Dict[str, Any]] = {}
        # server_id -> open stderr capture file {"path": str, "handle": file}
        self._stderr_logs: Dict[str, Dict[str, Any]] = {}
        # server_id -> (event loop, _ServerLock) serializing every mutation of
        # the dicts above for that server. Created lazily so McpManager can be
        # constructed at import time, before any loop exists.
        self._locks: Dict[str, Tuple[Any, Any]] = {}
        # server_id -> in-flight restart_server() task, so concurrent reconnect
        # requests for one server join the running restart instead of stacking
        # up another disconnect/connect cycle each.
        self._restart_tasks: Dict[str, Any] = {}
        # Tracking updates to tools/connections for RAG indexing / prompt cache
        self._generation = 0

    def _get_lock(self, server_id: str) -> "_ServerLock":
        """Return this server's mutex, creating it on first use.

        Must be called from inside a running loop. The loop is remembered
        because asyncio primitives bind to the loop that first awaits them:
        tests (and anything else) that drive the same manager through several
        asyncio.run() calls would otherwise hit "bound to a different event
        loop". A loop change means the previous loop is gone, so the old lock
        cannot still be held and is safe to replace.
        """
        loop = asyncio.get_running_loop()
        entry = self._locks.get(server_id)
        if entry is None or entry[0] is not loop:
            entry = (loop, _ServerLock())
            self._locks[server_id] = entry
        return entry[1]

    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """Connect to an MCP server via stdio, SSE, or Streamable HTTP transport."""
        async with self._get_lock(server_id):
            return await self._connect_server_unlocked(
                server_id=server_id,
                name=name,
                transport=transport,
                command=command,
                args=args,
                env=env,
                url=url,
            )

    async def _connect_server_unlocked(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """connect_server() body. Callers must hold this server's lock."""
        # Never let a second connect orphan a live transport: overwriting
        # self._stacks[server_id] used to leave the previous stdio subprocess
        # running with nothing referencing its AsyncExitStack, so it was torn
        # down later by the garbage collector from an unrelated task -- the
        # "Attempted to exit cancel scope in a different task" RuntimeError
        # that showed up as an unretrieved task exception in the logs.
        if server_id in self._stacks or server_id in self._sessions:
            await self._disconnect_server_unlocked(server_id, log=False)

        self._configs[server_id] = {
            "server_id": server_id,
            "name": name,
            "transport": transport,
            "command": command,
            "args": list(args or []),
            "env": dict(env or {}),
            "url": url,
        }
        try:
            if transport == "stdio":
                res = await self._connect_stdio(server_id, name, command, args or [], env or {})
            elif transport == "sse":
                res = await self._connect_sse(server_id, name, url)
            elif transport == "http":
                res = await self._start_http_connect(server_id, name, url)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                res = False
            if res:
                self._generation += 1
            return res
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {_describe_exception(e)}")
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            # A server that dies during the handshake usually explains itself on
            # stderr while the client-side exception says nothing useful.
            tail = self._read_stderr_tail(server_id)
            if tail:
                error_message = f"{error_message}\n\nLast output from the server:\n{tail}"
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name}
            self._generation += 1
            return False

    async def _connect_stdio(self, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> bool:
        """Connect to an MCP server via stdio transport.

        stdio_client() opens an anyio task group, whose cancel scope may only
        be exited by the task that entered it, so the stack lives in its own
        owner task (_OwnedStack) and connect and disconnect can come from any
        task.
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env={**os.environ, **env} if env else None,
            )

            # Capture the subprocess's stderr to a file instead of letting it
            # vanish into the app's own stderr. When the process dies, the
            # client-side exception is often anyio's ClosedResourceError with
            # no message at all; this tail is the only thing that says *why*.
            errlog = self._open_stderr_log(server_id)

            async def setup(stack):
                if errlog is not None:
                    transport = await stack.enter_async_context(stdio_client(server_params, errlog=errlog))
                else:
                    transport = await stack.enter_async_context(stdio_client(server_params))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                return session, await session.list_tools()

            stack = _OwnedStack(server_id)
            session, tools_result = await stack.open(setup)
            registered = False

            try:

                tools = []
                for tool in tools_result.tools:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                        # MCP tool annotations (readOnlyHint / destructiveHint) drive
                        # plan-mode read-only gating. Absent on many servers, so we
                        # fall back to a name heuristic in mcp_tool_is_readonly().
                        "annotations": getattr(tool, "annotations", None),
                    })

                # Extract identity hints from env vars (e.g. email address, API name)
                # so tool descriptions can distinguish between multiple instances of
                # the same MCP server (e.g. two email accounts).
                identity_hints = []
                for k, v in (env or {}).items():
                    k_lower = k.lower()
                    if any(x in k_lower for x in ["email_address", "account", "user", "username"]):
                        identity_hints.append(v)
                identity = ", ".join(identity_hints) if identity_hints else ""

                self._sessions[server_id] = session
                self._stacks[server_id] = stack
                self._tools[server_id] = tools
                self._connections[server_id] = {
                    "status": "connected",
                    "name": name,
                    "transport": "stdio",
                    "tool_count": len(tools),
                    "identity": identity,
                }

                registered = True

            finally:
                if not registered:
                    await stack.aclose()

            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via stdio")
            return True

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {
                "status": "error",
                "error": "mcp package not installed",
                "name": name,
            }
            return False

    async def _connect_sse(self, server_id: str, name: str, url: str) -> bool:
        """Connect to an MCP server via SSE transport."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            async def setup(stack):
                read_stream, write_stream = await stack.enter_async_context(sse_client(url))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                return session, await session.list_tools()

            stack = _OwnedStack(server_id)
            session, tools_result = await stack.open(setup)
            registered = False

            try:

                tools = []
                for tool in tools_result.tools:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                        # MCP tool annotations (readOnlyHint / destructiveHint) drive
                        # plan-mode read-only gating. Absent on many servers, so we
                        # fall back to a name heuristic in mcp_tool_is_readonly().
                        "annotations": getattr(tool, 'annotations', None),
                    })

                self._sessions[server_id] = session
                self._stacks[server_id] = stack
                self._tools[server_id] = tools
                self._connections[server_id] = {
                    "status": "connected",
                    "name": name,
                    "transport": "sse",
                    "tool_count": len(tools),
                }

                registered = True

                logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via SSE")
                return True

            finally:
                if not registered:
                    await stack.aclose()

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    async def _start_http_connect(self, server_id: str, name: str, url: str, wait: float = 8.0) -> bool:
        """Begin a Streamable HTTP connect in the background. Returns within
        `wait` seconds: True if it connected (cached-token path), otherwise the
        flow is awaiting browser authorization and status becomes 'needs_auth'."""
        import asyncio
        # The background connect deliberately runs outside this server's lock:
        # an OAuth browser flow can take minutes, and holding the lock for that
        # long would block disconnects. disconnect_server() cancels the task
        # instead (see self._connect_tasks).
        self._connections[server_id] = {"status": "connecting", "name": name, "transport": "http"}
        task = asyncio.create_task(self._connect_http(server_id, name, url))
        self._connect_tasks[server_id] = task
        done, _ = await asyncio.wait({task}, timeout=wait)
        if task in done:
            try:
                return task.result()
            except Exception as e:
                self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
                return False
        # Still running → either awaiting authorization, or discovery/DCR is
        # still in flight. If _on_redirect already published needs_auth+auth_url,
        # leave it; otherwise mark needs_auth (auth_url filled in once it fires).
        from src.mcp_oauth import pop_auth_url
        cur = self._connections.get(server_id, {})
        if cur.get("status") != "needs_auth":
            self._connections[server_id] = {
                "status": "needs_auth", "name": name, "transport": "http",
                "auth_url": pop_auth_url(server_id),
            }
        return False

    async def _connect_http(self, server_id: str, name: str, url: str) -> bool:
        """Connect to a Streamable HTTP MCP server (with automatic OAuth)."""
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            from src.mcp_oauth import build_provider, clear_auth_url

            def _on_redirect(auth_url):
                # Publish needs_auth the moment the URL is known, independent of
                # how long discovery/DCR took (may exceed the bounded start wait).
                self._connections[server_id] = {
                    "status": "needs_auth", "name": name, "transport": "http",
                    "auth_url": auth_url,
                }

            provider = build_provider(server_id, url, on_redirect=_on_redirect)

            async def setup(stack):
                transport = await stack.enter_async_context(streamablehttp_client(url, auth=provider))
                read_stream, write_stream, _get_session_id = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                return session, await session.list_tools()

            stack = _OwnedStack(server_id)
            session, tools_result = await stack.open(setup)
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            self._connections[server_id] = {
                "status": "connected", "name": name, "transport": "http",
                "tool_count": len(tools),
            }
            clear_auth_url(server_id)
            # Tools changed (this can complete after connect_server already
            # returned, via the background OAuth flow), so bump the generation
            # to invalidate the tool-prompt cache.
            self._generation += 1
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via http")
            return True
        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False
        except Exception as e:
            desc = _describe_exception(e)
            logger.error(f"Failed to connect HTTP MCP server {name} ({server_id}): {desc}")
            self._connections[server_id] = {"status": "error", "error": desc, "name": name}
            return False

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        async with self._get_lock(server_id):
            await self._disconnect_server_unlocked(server_id)

    async def _disconnect_server_unlocked(self, server_id: str, log: bool = True):
        """disconnect_server() body. Callers must hold this server's lock.

        The lock matters here specifically because `await stack.aclose()`
        below is a suspension point that sits *between* popping _stacks and
        popping _sessions/_tools/_connections. Unlocked, a connect that
        completed during that await would publish a fresh "connected" entry
        which this call then wiped on the way out, leaving the UI showing a
        disconnected server with 0 tools.
        """
        # Cancel any in-flight HTTP/OAuth background connect so it stops
        # publishing status for a server that may be getting deleted.
        task = self._connect_tasks.pop(server_id, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
        except Exception:
            pass

        stack = self._stacks.pop(server_id, None)
        if stack:
            try:
                await stack.aclose()
            except Exception as e:
                logger.warning(f"Error closing MCP server {server_id}: {e}")

        self._sessions.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._configs.pop(server_id, None)
        self._close_stderr_log(server_id)
        self._generation += 1
        if log:
            logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.disconnect_server(sid)

    async def restart_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """Disconnect then reconnect one server, coalescing concurrent calls.

        Both the Settings and Admin MCP panels expose a Reconnect button, and
        a reconnect takes a couple of seconds, so a single user could easily
        have several overlapping POSTs to
        /api/mcp/servers/{id}/reconnect in flight for the same server. Each
        one used to run its own unsynchronized disconnect+connect pair, which
        went wrong two ways:

          * every attempt spawned its own stdio subprocess, and all but the
            last had its AsyncExitStack dropped on the floor when the next
            connect overwrote _stacks[server_id]; the orphans were closed
            later by the GC from an unrelated task, raising anyio's
            "Attempted to exit cancel scope in a different task" RuntimeError.
          * a disconnect that resumed after a *newer* connect had published
            state popped _sessions/_tools/_connections for a connection that
            was actually live, so /api/mcp/servers reported the server as
            disconnected with 0 tools while the agent could still call its
            tools through the session it had already been handed.

        A caller arriving while a restart is running joins that restart and
        gets its result, instead of queueing another teardown of a server
        that is being rebuilt right now.
        """
        loop = asyncio.get_running_loop()
        task = self._restart_tasks.get(server_id)
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(
                self._restart_once(
                    server_id=server_id,
                    name=name,
                    transport=transport,
                    command=command,
                    args=args,
                    env=env,
                    url=url,
                )
            )
            self._restart_tasks[server_id] = task
            task.add_done_callback(
                lambda finished, sid=server_id: self._restart_tasks.pop(sid, None)
                if self._restart_tasks.get(sid) is finished
                else None
            )
        # Shielded: a browser that navigates away mid-reconnect cancels its
        # request task, and that must not cancel a restart other callers (or
        # the server's own connection state) depend on.
        return await asyncio.shield(task)

    async def _restart_once(self, server_id: str, **config) -> bool:
        # Runs in its own task, so it must NOT be called from a caller that
        # already holds this server's lock: the lock is re-entrant per task,
        # and this task is not that task.
        async with self._get_lock(server_id):
            await self._disconnect_server_unlocked(server_id)
            return await self._connect_server_unlocked(server_id=server_id, **config)


    async def connect_all_enabled(self):
        # Read the rows and give the connection back before connecting: the
        # connects take up to 20s each, and holding a pooled connection that
        # long starved everything else (the 15-16s "long checkout" warnings
        # at mcp_manager connect_all_enabled).
        db = SessionLocal()
        try:
            servers = [
                SimpleNamespace(id=srv.id, name=srv.name, transport=srv.transport, command=srv.command,
                                args=srv.args, env=srv.env, url=srv.url)
                for srv in db.query(McpServer).filter(McpServer.is_enabled == True).all()
            ]
        finally:
            db.close()

        await asyncio.gather(*(self._connect_with_timeout(srv) for srv in servers))
        # Refresh the agent's tool index now, off the request path, so the
        # next turn doesn't spend its tool-selection budget on it.
        try:
            from src.tool_index import refresh_mcp_index_if_loaded
            await asyncio.to_thread(refresh_mcp_index_if_loaded, self)
        except Exception as e:
            logger.debug("MCP index refresh after connect skipped: %s", e)


    async def _connect_with_timeout(self, srv):
        args = json.loads(srv.args) if srv.args else []
        env = json.loads(srv.env) if srv.env else {}

        try:
            await asyncio.wait_for(
                self.connect_server(
                    server_id=srv.id,
                    name=srv.name,
                    transport=srv.transport,
                    command=srv.command,
                    args=args,
                    env=env,
                    url=srv.url,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out connecting to %s", srv.name)
            self._connections[srv.id] = {
                "status": "timeout",
                "error": f"Timed out after 20 seconds",
                "name": srv.name,
            }

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        session = self._sessions.get(server_id)
        if not session:
            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}

        try:
            result = await self._do_call(session, tool_name, arguments)
        except Exception as e:
            # Describe the failure *before* reconnecting: restarting the server
            # deletes the dead subprocess's stderr capture, which is usually the
            # only record of why it died.
            detail = self._describe_call_failure(server_id, e)
            # Auto-reconnect servers whose subprocess may have died. Builtins keep
            # their historical blanket retry; user-added servers retry only when
            # the transport is provably gone. Such a call fails in ~1ms before any
            # bytes are written, so replaying it cannot duplicate a side effect.
            # Without this, a user-added stdio server stayed dead until Odysseus
            # restarted -- every call returning exit_code=1 with a messageless
            # anyio error (the ntfy-me-mcp "empty error" symptom).
            if self.is_builtin(server_id) or _is_dead_transport_error(e):
                logger.warning(f"MCP call failed for {qualified_name}, attempting reconnect: {detail}")
                reconnected = await self._reconnect_server(server_id)
                if reconnected:
                    # Reconnecting and replaying are separate decisions. A dead
                    # transport proves the call did not complete; otherwise only
                    # read-only tools are safe to replay automatically.
                    tool_meta = next(
                        (t for t in self._tools.get(server_id, []) if t.get("name") == tool_name),
                        None,
                    )
                    safe_to_replay = _is_dead_transport_error(e) or (
                        tool_meta is not None and mcp_tool_is_readonly(tool_meta)
                    )
                    if not safe_to_replay:
                        logger.error(
                            f"MCP server {server_id} reconnected but '{tool_name}' was not "
                            f"replayed (outcome of the failed call is unknown): {detail}"
                        )
                        return {
                            "error": (
                                f"MCP server '{server_id}' dropped the connection while running "
                                f"'{tool_name}' ({detail}). The server has been reconnected, but this "
                                "call was NOT retried automatically because it may have already taken "
                                "effect. Check whether it already happened before calling it again."
                            ),
                            "exit_code": 1,
                        }
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await self._do_call(session, tool_name, arguments)
                        except Exception as e2:
                            desc2 = self._describe_call_failure(server_id, e2)
                            logger.error(f"MCP tool call failed after reconnect: {qualified_name}: {desc2}")
                            return {"error": desc2, "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}: {detail}")
                    return {
                        "error": f"MCP server crashed and could not be restarted: {server_id}\n\n{detail}",
                        "exit_code": 1,
                    }
            else:
                logger.error(f"MCP tool call failed: {qualified_name}: {detail}")
                return {"error": detail, "exit_code": 1}

        return result

    async def _do_call(self, session, tool_name: str, arguments: Dict) -> Dict:
        """Execute a single MCP tool call and return result dict."""
        result = await session.call_tool(tool_name, arguments)
        output_parts = []
        images = []
        for content in result.content:
            if hasattr(content, 'text'):
                output_parts.append(content.text)
            elif getattr(content, 'type', '') == 'image' and hasattr(content, 'data'):
                # Image content (e.g. Playwright screenshots)
                mime = getattr(content, 'mimeType', 'image/png')
                images.append({"data": content.data, "mimeType": mime})
                output_parts.append(f"[Screenshot captured ({mime})]")
            elif hasattr(content, 'data'):
                output_parts.append(str(content.data))

        output = "\n".join(output_parts)
        is_error = getattr(result, 'isError', False)

        result_dict = {
            "stdout": output if not is_error else "",
            "stderr": output if is_error else "",
            "exit_code": 1 if is_error else 0,
        }
        if is_error and output:
            result_dict["untrusted_content"] = True
        if images:
            result_dict["images"] = images
        return result_dict

    def _open_stderr_log(self, server_id: str):
        """Open a fresh stderr capture file for a stdio server's subprocess.

        `stdio_client(errlog=...)` is handed to the child process as its stderr
        file descriptor, so this must be a real file, not an in-memory buffer.
        Returns None (and falls back to inherited stderr) if the file can't be
        created -- capturing diagnostics must never block a connection.

        Builtin servers are deliberately excluded: some of them (e.g. lotus)
        stream INFO logs to stderr so they show up in the app's own container
        logs, and redirecting that into a temp file would hide it. Their code is
        ours to debug, whereas a third-party npx server's dying words are often
        the only clue available.
        """
        import tempfile

        self._close_stderr_log(server_id)
        if self.is_builtin(server_id):
            return None
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w+", prefix=f"mcp-{server_id}-", suffix=".stderr.log", delete=False
            )
            self._stderr_logs[server_id] = {"path": handle.name, "handle": handle}
            return handle
        except Exception as e:
            logger.debug(f"Could not capture stderr for MCP server {server_id}: {e}")
            return None

    def _read_stderr_tail(self, server_id: str) -> str:
        """Return the tail of a stdio server's captured stderr, or ""."""
        entry = self._stderr_logs.get(server_id)
        if not entry:
            return ""
        try:
            size = os.path.getsize(entry["path"])
            with open(entry["path"], "r", encoding="utf-8", errors="replace") as fh:
                if size > _STDERR_TAIL_BYTES:
                    fh.seek(size - _STDERR_TAIL_BYTES)
                return fh.read().strip()
        except Exception:
            return ""

    def _close_stderr_log(self, server_id: str):
        """Close and remove a stdio server's stderr capture file."""
        entry = self._stderr_logs.pop(server_id, None)
        if not entry:
            return
        try:
            entry["handle"].close()
        except Exception:
            pass
        try:
            os.unlink(entry["path"])
        except Exception:
            pass

    def _describe_call_failure(self, server_id: str, error: BaseException) -> str:
        """Turn a tool-call exception into something a user can act on.

        The bare exception is frequently useless -- anyio raises a messageless
        ClosedResourceError when the server's pipe is gone -- so name the server,
        say the subprocess exited, and quote whatever it printed on the way out.
        """
        desc = _describe_exception(error)
        if not _is_dead_transport_error(error):
            return desc

        config = self._configs.get(server_id, {})
        conn = self._connections.get(server_id, {})
        name = config.get("name") or conn.get("name") or server_id
        command_line = " ".join(
            [config.get("command") or "", *(config.get("args") or [])]
        ).strip()

        lines = [
            f"MCP server '{name}' ({server_id}) is not running — its connection is closed, "
            f"so the tool call never reached it ({desc})."
        ]
        if command_line:
            lines.append(f"Command: {command_line}")
        tail = self._read_stderr_tail(server_id)
        if tail:
            lines.append(f"Last output from the server:\n{tail}")
        else:
            lines.append(
                "The server produced no output before exiting — check its configuration "
                "(command, args, and env such as URL/token/topic values)."
            )
        return "\n\n".join(lines)

    async def _reconnect_server(self, server_id: str) -> bool:
        """Restart a crashed MCP server using the config it was connected with.

        Builtins keep their dedicated path (their command is derived, not stored).
        Only stdio servers are restarted automatically: HTTP/SSE reconnects can
        re-enter the OAuth browser flow, which must not happen inside a tool call.

        Holds the server's lock across the whole teardown+rebuild so a
        crash-recovery restart and a user-triggered reconnect cannot run at
        the same time. The lock is re-entrant within this task, which is why
        the public disconnect_server()/connect_server() can still be used
        below.
        """
        async with self._get_lock(server_id):
            if self.is_builtin(server_id):
                return await self._reconnect_builtin(server_id)

            # Snapshot before disconnect_server() clears it.
            config = dict(self._configs.get(server_id) or {})
            if not config:
                logger.warning(f"No stored config to reconnect MCP server: {server_id}")
                return False
            if config.get("transport") != "stdio":
                return False

            await self.disconnect_server(server_id)
            try:
                ok = await self.connect_server(**config)
                if ok:
                    logger.info(f"Reconnected MCP server: {config.get('name', server_id)} ({server_id})")
                return ok
            except Exception as e:
                logger.error(f"Failed to reconnect MCP server {server_id}: {_describe_exception(e)}")
                return False

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server."""
        import sys
        from src.builtin_mcp import _BUILTIN_SERVERS, builtin_python_env

        if server_id not in _BUILTIN_SERVERS:
            return False

        script_rel, name = _BUILTIN_SERVERS[server_id]
        base_dir = get_app_root()
        script_path = os.path.join(base_dir, script_rel)

        async with self._get_lock(server_id):
            # Clean up old connection
            await self.disconnect_server(server_id)

            try:
                ok = await self.connect_server(
                    server_id=server_id,
                    name=name,
                    transport="stdio",
                    command=sys.executable,
                    args=[script_path],
                    env=builtin_python_env(base_dir),
                )
                if ok:
                    logger.info(f"Reconnected builtin MCP server: {name}")
                return ok
            except Exception as e:
                logger.error(f"Failed to reconnect builtin MCP server {name}: {_describe_exception(e)}")
                return False

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        for server_id, tools in self._tools.items():
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id not in _BUILTIN_FUNCTION_CALLING_SERVERS:
                continue
            conn = self._connections.get(server_id, {})
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": f"[MCP:{label}] {tool['description']}",
                        "parameters": _model_visible_schema(tool.get("input_schema")),
                    },
                }
                schemas.append(schema)

        return schemas

    def _schema_bearing_tool_counts(
        self, disabled_map: Optional[Dict[str, set]] = None
    ) -> Dict[str, int]:
        """{server_id: enabled tool count} for servers whose tools reach the
        model as function schemas.

        Mirrors the skip in `get_all_openai_schemas`: builtin PYTHON servers
        speak the code-block format instead, never appear in the schema list,
        and so cannot flood it -- they must not be counted against the
        always-bound budget either, or a host with several of them would push
        real MCP servers out for tokens nobody is spending.
        """
        counts: Dict[str, int] = {}
        for server_id, tools in self._tools.items():
            if self.is_builtin(server_id) and server_id not in _BUILTIN_FUNCTION_CALLING_SERVERS:
                continue
            disabled = (disabled_map or {}).get(server_id, set())
            counts[server_id] = sum(1 for t in tools if t["name"] not in disabled)
        return counts

    def demoted_servers(
        self, disabled_map: Optional[Dict[str, set]] = None
    ) -> List[Tuple[str, str, int]]:
        """[(server_id, display name, tool count)] for user-added servers that
        exceeded the always-bound budget and are now gated behind retrieval.

        Ordered largest first -- the order they were demoted in. Separate from
        `gated_tool_names` so the agent loop can name the decision in its
        existing [agent-debug] line without re-deriving the rule; the two share
        `_resolve_gating` so they can never disagree about who was demoted.
        """
        return self._resolve_gating(disabled_map)[1]

    def _resolve_gating(
        self, disabled_map: Optional[Dict[str, set]] = None
    ) -> Tuple[Set[str], List[Tuple[str, str, int]]]:
        """(gated qualified names, demoted server rows). See the budget notes
        at the top of this module for the rule and the numbers behind it."""
        per_server_cap, total_cap = _always_bound_limits()
        counts = self._schema_bearing_tool_counts(disabled_map)

        # The embedded catalogs are gated by identity, not by size: they ship
        # with Odysseus, are relevant to a minority of turns, and were already
        # behind retrieval before any budget existed. They are not "demoted" --
        # nothing changed for them -- so they stay out of the demoted list and
        # out of the budget arithmetic below.
        candidates = {
            sid: n for sid, n in counts.items()
            if sid not in _BUILTIN_FUNCTION_CALLING_SERVERS
        }

        demoted_ids: List[str] = []
        if per_server_cap > 0:
            demoted_ids = [
                sid for sid, n in sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
                if n > per_server_cap
            ]

        if total_cap > 0:
            remaining = {sid: n for sid, n in candidates.items() if sid not in demoted_ids}
            bound = sum(remaining.values())
            # Largest first; server_id breaks ties so two equally sized servers
            # cannot swap places between rounds. The schema list is part of the
            # cached prompt prefix, and a set-iteration-order tie-break would
            # invalidate that cache at random.
            for sid, n in sorted(remaining.items(), key=lambda kv: (-kv[1], kv[0])):
                if bound <= total_cap:
                    break
                demoted_ids.append(sid)
                bound -= n

        gated: Set[str] = set()
        for server_id, tools in self._tools.items():
            if server_id not in _BUILTIN_FUNCTION_CALLING_SERVERS and server_id not in demoted_ids:
                continue
            for tool in tools:
                gated.add(f"mcp__{server_id}__{tool['name']}")

        demoted = [
            (sid, self._connections.get(sid, {}).get("name", sid), candidates.get(sid, 0))
            for sid in demoted_ids
        ]
        return gated, demoted

    def gated_tool_names(self, disabled_map: Optional[Dict[str, set]] = None) -> Set[str]:
        """Qualified names of MCP tools that must still win RAG/intent-based
        tool selection to appear in a turn's schema list.

        Two groups land here.

        The large embedded catalogs (browser, GitHub, Todoist, Lotus,
        pi_worker) where showing every tool on every turn would flood small
        models with ~30 irrelevant schemas (issue tracked alongside the
        Terminus toolset swap).

        And any user-added server that has outgrown the always-bound budget --
        by itself or together with its peers. Everything under that budget
        still binds unconditionally once connected, regardless of how the RAG
        tool-selection heuristic scores the turn's wording, because that is
        what stops a real, connected tool from silently disappearing the moment
        a follow-up message ("continue", "now run it") does not semantically
        resemble its description. See the budget notes at the top of this
        module, and the root-cause note in
        agent_loop._tool_schemas_for_round.

        Gating is not hiding: a gated tool stays indexed (every connected
        server is fed to ToolIndex via `get_tool_descriptions_for_prompt`), so
        retrieval can still surface it, and that same prompt text tells the
        model which servers are connected but not attached this turn.

        ``disabled_map`` is optional and only sharpens the counts: a server
        whose tools are mostly switched off is small in practice, and passing
        the map lets it keep the always-bound guarantee it would otherwise
        lose on its raw tool count.
        """
        return self._resolve_gating(disabled_map)[0]

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": _model_visible_schema(tool.get("input_schema")),
                    # Preserve authoritative hints for downstream selection and
                    # execution guards; names alone can misclassify MCP tools.
                    "annotations": tool.get("annotations"),
                    "is_disabled": tool["name"] in disabled,
                })
        return result

    def discover_requested_tools(
        self,
        query: str,
        *,
        disabled_map: Optional[Dict[str, set]] = None,
        disabled_tools: Optional[Set[str]] = None,
        allowed_servers: Optional[List[str]] = None,
        enabled_tools: Optional[Set[str]] = None,
        readonly: bool = False,
        max_tools: int = 8,
    ) -> Set[str]:
        """Deterministically attach a small set from explicitly requested servers.

        Large catalogs still use retrieval gating; mentioning a connected server
        must not leave its useful tools dependent on embedding similarity alone.
        This is a selection hint, NOT an authorization grant. Callers must pass
        the same disabled/private-policy maps used by execution and schemas.

        ``enabled_tools`` means deliberate per-agent selected bindings, not all
        permitted tools. When supplied it is also an allowlist (including an
        empty set), and those bindings have priority over query matches. A mere
        server or tool mention only promotes read-only tools. Writes can only
        enter here through explicit bindings, and ``readonly`` blocks those too.
        Normal write-intent retrieval is unchanged elsewhere in the router.
        """
        if max_tools <= 0:
            return set()

        def normalize(value: str) -> str:
            return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))

        normalized_query = f" {normalize(query)} "
        # A qualified tool's server id is not a request for its whole catalog.
        server_text = re.sub(r"\bmcp__[a-zA-Z0-9_-]+__[a-zA-Z0-9_-]+\b", " ", str(query))
        server_query = f" {normalize(server_text)} "
        query_words = set(normalized_query.split())

        def mentioned(value: str, text: str = normalized_query) -> bool:
            phrase = normalize(value)
            return bool(phrase and f" {phrase} " in text)

        generic = {"mcp", "server", "servers", "tool", "tools", "builtin"}

        def server_mentioned(server_id: str, display_name: str) -> bool:
            for label in (server_id, display_name):
                words = normalize(label).split()
                if not set(words) - generic:
                    continue
                # "bluesky-mcp" and "Bluesky MCP server" both match Bluesky,
                # but a request merely mentioning "MCP" matches no catalog.
                while words and words[0] in generic:
                    words.pop(0)
                while words and words[-1] in generic:
                    words.pop()
                if mentioned(label, server_query) or mentioned(" ".join(words), server_query):
                    return True
            return False

        blocked = set(disabled_tools or ())
        selected = set(enabled_tools) if enabled_tools is not None else None
        allowed = set(allowed_servers) if allowed_servers is not None else None
        candidates = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            if conn.get("status") != "connected":
                continue
            if allowed is not None and "*" not in allowed and server_id not in allowed:
                continue
            if self.is_builtin(server_id) and server_id not in _BUILTIN_FUNCTION_CALLING_SERVERS:
                continue
            requested_server = server_mentioned(server_id, conn.get("name", server_id))
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                name = tool["name"]
                qualified = f"mcp__{server_id}__{name}"
                if name in disabled or qualified in disabled or qualified in blocked or name in blocked:
                    continue
                if selected is not None and qualified not in selected:
                    continue
                bound = selected is not None and qualified in selected
                read_only = mcp_tool_is_readonly(tool)
                if not read_only and (readonly or not bound):
                    continue
                exact_qualified = re.search(
                    r"(?<![\w-])" + re.escape(qualified) + r"(?![\w-])", str(query), re.IGNORECASE,
                ) is not None
                exact = exact_qualified or (requested_server and mentioned(name))
                if not (bound or exact or requested_server):
                    continue
                # Selected bindings, then exact tool names, then matching nouns
                # from names. No description matching: untrusted marketing prose
                # must not make an unrelated schema win this deterministic path.
                overlap = len(set(normalize(name).split()) & query_words)
                candidates.append((not bound, not exact, -overlap, qualified))
        return {row[3] for row in sorted(candidates)[:max_tools]}

    def plan_mode_blocked_mcp(self) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """Plan mode: block every MCP tool that isn't clearly read-only.

        Returns (disabled_map, qualified_names):
          - disabled_map: {server_id: {tool_name, ...}} to hide write tools from
            the prompt/schemas (merged into the existing mcp_disabled_map).
          - qualified_names: {"mcp__<server>__<tool>", ...} for runtime rejection
            in execute_tool_block (which matches the qualified name).
        """
        disabled_map: Dict[str, Set[str]] = {}
        qualified: Set[str] = set()
        for server_id, tools in self._tools.items():
            for tool in tools:
                if not mcp_tool_is_readonly(tool):
                    disabled_map.setdefault(server_id, set()).add(tool["name"])
                    qualified.add(f"mcp__{server_id}__{tool['name']}")
        return disabled_map, qualified

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
            "todoist",
            "lotus",
            "pi_worker",
            "github_read",
            "github_write",
        }

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        return self._connections.get(server_id, {"status": "disconnected"})

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return dict(self._connections)

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        # Which servers are connected but not attached this turn is part of the
        # text (see the note emitted per server below), and it moves with the
        # always-bound settings as well as with the tool inventory -- so it goes
        # in the cache key, or a settings change would keep serving a prompt
        # that contradicts the schemas actually being sent.
        _demoted_ids = tuple(sid for sid, _name, _n in self.demoted_servers(disabled_map))
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
            _demoted_ids,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = ["\n\nYou also have access to external MCP tool servers. These tools are called via native function calling:"]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] not in _BUILTIN_FUNCTION_CALLING_SERVERS:
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            # Builtin catalogs are gated by identity on every turn, so they have
            # exactly the same honesty problem a demoted server has: listed here
            # under "you also have access to these", with no attached schema
            # until retrieval surfaces one. Telling the model a tool is callable
            # when it is not is what produces the "I do not have X" refusals this
            # note exists to prevent, so both cases get it -- only the reason
            # differs.
            if sid in _demoted_ids or sid in _BUILTIN_FUNCTION_CALLING_SERVERS:
                # A demoted server keeps its full listing here -- the model must
                # be able to find out these tools EXIST, or the always-bound
                # budget just reintroduces the vanishing bug one level down
                # (and this same text is what ToolIndex embeds, so dropping it
                # would also make the tools unretrievable). What it loses is the
                # attached call schema, so say that plainly and say how to get
                # it back. The phrasing is deliberate: naming the qualified tool
                # and the words "do not have ... available" is exactly what the
                # agent loop's missing-tool self-unblock listens for, and the
                # targeted re-arm then matches the tool name verbatim. Reusing
                # that existing path beats inventing a second one -- it already
                # handles the identical case for the builtin catalogs.
                _why = ("it is too large to attach to every turn"
                        if sid in _demoted_ids
                        else "this catalog is attached on demand")
                lines.append(
                    f"  (CONNECTED AND WORKING, but this server's {len(server_tools)} call "
                    f"schemas are not attached this turn -- {_why}. Do NOT report these tools "
                    "as unavailable to the user, and do NOT substitute an unrelated tool. To "
                    "get one attached, state that you do not have the exact tool available, by "
                    "its full mcp__ name, and it will be attached for the next round.)"
                )
            for t in server_tools:
                # One line per tool, truncated. A multi-line description
                # ("Actions:\n- create: ...") otherwise reads as extra
                # "- name: desc" rows; the tool index registered a phantom
                # `create` tool from one (2026-09-13 logs).
                flat = " ".join(str(t.get('description') or '').split())
                desc = flat[:120] + '...' if len(flat) > 120 else flat
                # Include the tool's declared inputs so the model calls it with
                # real argument names instead of guessing from the description
                # alone (issue #2509).
                args_hint = _format_mcp_params(t.get("input_schema"))
                lines.append(f"  - {t['qualified_name']}: {desc}{args_hint}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
