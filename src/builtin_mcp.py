"""
builtin_mcp.py

Auto-registration of built-in MCP servers on startup.
Each server runs as a stdio subprocess managed by McpManager.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from core.platform_compat import IS_WINDOWS, which_tool
from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)


def _find_npx() -> str:
    """Find the npx binary, checking common locations if not on PATH.

    On Windows the shim is `npx.cmd`, which `which_tool` resolves via PATHEXT.
    """
    npx = which_tool("npx")
    if npx:
        return npx
    if IS_WINDOWS:
        # Minimal-PATH fallbacks: npm's global bin lives under %APPDATA%\npm,
        # and node's installer dir carries npx.cmd alongside node.exe.
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        for candidate in (
            os.path.join(appdata, "npm", "npx.cmd"),
            r"C:\Program Files\nodejs\npx.cmd",
        ):
            if os.path.isfile(candidate):
                return candidate
        node = which_tool("node")
        if node:
            cand = os.path.join(os.path.dirname(node), "npx.cmd")
            if os.path.isfile(cand):
                return cand
        return "npx.cmd"  # fallback, will fail with a clear error
    # Common POSIX locations when PATH is minimal (e.g. systemd)
    for candidate in [
        os.path.expanduser("~/.npm-global/bin/npx"),
        os.path.expanduser("~/.local/bin/npx"),
        "/usr/local/bin/npx",
        "/usr/bin/npx",
    ]:
        if os.path.isfile(candidate):
            return candidate
    # Try to find node and use npx from same dir
    node = shutil.which("node")
    if node:
        npx_candidate = os.path.join(os.path.dirname(node), "npx")
        if os.path.isfile(npx_candidate):
            return npx_candidate
    return "npx"  # fallback, will fail with a clear error

# Server definitions: id -> (script path relative to project root, display name)
#
# bash / python / filesystem / web_search were folded into native in-process
# execution (src/tool_execution.py:_direct_fallback). Those trivial subprocess
# wrappers are gone.
#
# image_gen / memory / rag / email still run as stdio MCP servers — each
# carries hundreds of LOC of unique IMAP / HTTP / manager logic not worth
# duplicating into the native path right now.
_BUILTIN_SERVERS = {
    "image_gen":  ("mcp_servers/image_gen_server.py",  "Built-in: Image Generation"),
    "memory":     ("mcp_servers/memory_server.py",     "Built-in: Memory"),
    "rag":        ("mcp_servers/rag_server.py",        "Built-in: RAG"),
    "email":      ("mcp_servers/email_server.py",      "Built-in: Email"),
    "todoist":    ("mcp_servers/todoist_server.py",    "Built-in: Todoist"),
    "lotus":      ("mcp_servers/lotus_server.py",      "Built-in: Lotus"),
    "pi_worker":  ("mcp_servers/pi_worker_server.py",  "Built-in: Windows Pi Worker"),
}

_OPTIONAL_BUILTIN_ENV = {
    "pi_worker": "ODYSSEUS_PI_WORKER_HOST",
}

# NPX-based built-in servers (run via npx, not Python)
_BUILTIN_NPX_SERVERS = {
    "builtin_browser": {
        "name": "Built-in: Browser",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--headless", "--caps", "vision"],
    }
}

# Native-binary built-in servers.
#
# The official GitHub MCP server ships as a single Go executable, baked into
# the image by the Dockerfile. It runs as a stdio child process of this
# container rather than as a sibling container: starting GitHub's published
# image would mean mounting the host's Docker socket into Odysseus, which
# hands the whole Docker host to whatever the model can reach. One small child
# process is the cheaper trade.
#
# Read/write separation. "GitHub Read" carries a fixed, read-only tool list and
# comes up whenever a token is configured. Writes live in a second, opt-in
# server (ODYSSEUS_GITHUB_MCP_WRITE=1) scoped to the collaboration layer.
# Neither exposes repository-mutating tools -- no push_files,
# create_or_update_file, delete_repository, and no open-ended --toolsets. The
# working tree belongs to the Pi worker (it edits, runs tests, and drives git);
# GitHub MCP picks up afterwards for issues, PRs, reviews and Actions. The
# split also keeps a dozen schemas in front of the local model instead of a
# hundred.
GITHUB_MCP_TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"
# Classic (ghp_), fine-grained (github_pat_), OAuth/app (gho_/ghu_/ghs_/ghr_),
# or a pre-2021 40-hex classic token. Anything else — an Odysseus `ody_` token
# pasted into the wrong field, say — starts the server fine and then fails
# every call with 401, which reads as "GitHub stopped working".
_GITHUB_TOKEN_SHAPE = re.compile(r"^(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+$|^[0-9a-f]{40}$")

GITHUB_MCP_READ_TOOLS = (
    "get_file_contents",
    "get_commit",
    "list_branches",
    "list_commits",
    "search_code",
    "issue_read",
    "search_issues",
    "list_pull_requests",
    "pull_request_read",
    "actions_list",
    "actions_get",
    "get_job_logs",
)

# add_comment_to_pending_review is the other half of pull_request_review_write:
# without it a review can be created and submitted but can carry no line
# comments, which is most of the point of reviewing from here.
GITHUB_MCP_WRITE_TOOLS = (
    "create_pull_request",
    "add_issue_comment",
    "pull_request_review_write",
    "add_comment_to_pending_review",
)

GITHUB_MCP_WRITE_ENV = "ODYSSEUS_GITHUB_MCP_WRITE"


def find_github_mcp_binary() -> str:
    """Locate the github-mcp-server executable, or "" when it isn't installed.

    The Docker image installs it at /usr/local/bin; native installs may have it
    anywhere on PATH (which_tool picks up github-mcp-server.exe on Windows), so
    no launcher script is needed on either platform.
    """
    configured = os.environ.get("ODYSSEUS_GITHUB_MCP_BINARY", "").strip()
    if configured:
        return configured
    found = which_tool("github-mcp-server")
    if found:
        return found
    # PATH is minimal in some launches (systemd, entrypoint drops privileges),
    # so fall back to the image's install path directly.
    if os.path.isfile("/usr/local/bin/github-mcp-server"):
        return "/usr/local/bin/github-mcp-server"
    return ""


def github_mcp_env() -> dict[str, str]:
    """Environment handed to the github-mcp-server subprocesses.

    This has to be explicit. Passing env=None does *not* inherit the container
    environment: the MCP SDK falls back to get_default_environment(), which
    forwards only PATH/HOME/SHELL/TERM/USER, and the server would come up
    unauthenticated. McpManager merges os.environ in only when env is non-empty.

    The token is held in memory and passed to the child process; built-in
    servers are never written to the mcp_servers table, so it stays out of the
    database. Key names are chosen to avoid McpManager's identity-hint scan
    (keys containing user/account/email), which would echo values into tool
    descriptions.
    """
    env: dict[str, str] = {}
    token = os.environ.get(GITHUB_MCP_TOKEN_ENV, "").strip()
    if token:
        env[GITHUB_MCP_TOKEN_ENV] = token
    # GitHub Enterprise Server / ghe.com installs.
    host = os.environ.get("GITHUB_HOST", "").strip()
    if host:
        env["GITHUB_HOST"] = host
    return env


def github_mcp_servers() -> dict[str, dict]:
    """Built-in GitHub server definitions for the current configuration.

    Empty when no token is configured or the binary isn't installed -- the
    server exits immediately without credentials, and a built-in that fails on
    every startup is just noise in the logs.
    """
    token = os.environ.get(GITHUB_MCP_TOKEN_ENV, "").strip()
    if not token:
        return {}
    if not _GITHUB_TOKEN_SHAPE.match(token):
        logger.warning(
            "%s does not look like a GitHub token (it starts with %r): the GitHub MCP server "
            "will start, but every call will fail with 401. A classic token starts with ghp_, "
            "a fine-grained one with github_pat_.",
            GITHUB_MCP_TOKEN_ENV, token[:4],
        )
    binary = find_github_mcp_binary()
    if not binary:
        return {}

    servers = {
        "github_read": {
            "name": "Built-in: GitHub Read",
            "command": binary,
            "args": ["stdio", "--read-only", "--tools=" + ",".join(GITHUB_MCP_READ_TOOLS)],
        }
    }
    if os.environ.get(GITHUB_MCP_WRITE_ENV, "").lower() in ("1", "true", "yes"):
        servers["github_write"] = {
            "name": "Built-in: GitHub Write",
            "command": binary,
            # No --read-only here (it would drop every tool in the list), and
            # still no --toolsets: the explicit --tools list is the whole
            # surface this server can reach.
            "args": ["stdio", "--tools=" + ",".join(GITHUB_MCP_WRITE_TOOLS)],
        }
    return servers


# Global flag to disable MCP if there are compatibility issues
MCP_DISABLED = os.environ.get("ODYSSEUS_DISABLE_MCP", "").lower() in ("1", "true", "yes")
BROWSER_MCP_REQUIRE_CACHE = os.environ.get("ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE", "").lower() in ("1", "true", "yes")


# Strong references to the fire-and-forget startup tasks scheduled below.
# asyncio only keeps weak references to tasks created via create_task, so
# without this the GC can collect a task mid-execution and the server
# registration silently never runs. Mirrors _spawn_bg in routes/chat_helpers.py.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    """Schedule a background task and hold a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task

def _find_browser_executable() -> str:
    """Find a browser binary for the built-in Playwright MCP server.

    Docker images ship Debian's `chromium`; desktop installs may already have
    Chrome/Chromium in a conventional location. If nothing is found, return an
    empty string and let Playwright MCP use its own default browser/channel.
    """
    configured = os.environ.get("ODYSSEUS_BROWSER_EXECUTABLE", "").strip()
    if configured:
        return configured
    for name in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in (
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _browser_mcp_args(args: list[str]) -> list[str]:
    """Return Playwright MCP args with a concrete browser executable when found."""
    out = list(args or [])
    if "--executable-path" not in out:
        browser = _find_browser_executable()
        if browser:
            out.extend(["--executable-path", browser])
    if os.environ.get("ODYSSEUS_BROWSER_ISOLATED", "1").lower() not in ("0", "false", "no"):
        if "--isolated" not in out and "--user-data-dir" not in out:
            out.append("--isolated")
    if os.environ.get("ODYSSEUS_BROWSER_NO_SANDBOX", "1").lower() not in ("0", "false", "no"):
        if "--no-sandbox" not in out and "--sandbox" not in out:
            out.append("--no-sandbox")
    return out


def builtin_python_env(base_dir: str) -> dict[str, str]:
    """Environment for built-in Python MCP subprocesses.

    The app root must be importable so mcp_servers can import local modules, but
    replacing PYTHONPATH entirely hides site-packages in container/dev launches
    that rely on PYTHONPATH for their active environment.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [base_dir]
    for item in existing.split(os.pathsep):
        if item and item not in parts:
            parts.append(item)
    return {"PYTHONPATH": os.pathsep.join(parts)}


async def register_builtin_servers(mcp_manager):
    """Connect all built-in MCP servers to the manager."""
    if MCP_DISABLED:
        logger.info("Built-in MCP servers disabled via ODYSSEUS_DISABLE_MCP")
        return

    base_dir = get_app_root()
    python = sys.executable

    async def _connect_python_server(server_id: str, script_path: str, name: str):
        try:
            ok = await mcp_manager.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=python,
                args=[script_path],
                env=builtin_python_env(base_dir),
            )
            if ok:
                logger.info(f"Built-in MCP server registered: {name}")
            else:
                logger.warning(f"Built-in MCP server failed to connect: {name}")
        except asyncio.CancelledError:
            logger.warning(f"Built-in MCP server {name} cancelled")
            raise
        except BaseException as e:
            logger.warning(f"Built-in MCP server {name} error: {type(e).__name__}: {e}")

    for server_id, (script, name) in _BUILTIN_SERVERS.items():
        required_env = _OPTIONAL_BUILTIN_ENV.get(server_id)
        if required_env and not os.environ.get(required_env, "").strip():
            logger.info("Optional built-in MCP server %s disabled: %s is not configured", name, required_env)
            continue
        script_path = os.path.join(base_dir, script)
        if not os.path.exists(script_path):
            logger.warning(f"Built-in MCP server script not found: {script_path}")
            continue
        _spawn_bg(_connect_python_server(server_id, script_path, name))

    async def _connect_binary_server(server_id: str, cfg: dict):
        """Connect a built-in server that is a native executable, not a script."""
        try:
            ok = await mcp_manager.connect_server(
                server_id=server_id,
                name=cfg["name"],
                transport="stdio",
                command=cfg["command"],
                args=list(cfg["args"]),
                env=cfg.get("env") or None,
            )
            if ok:
                logger.info(f"Built-in MCP server registered: {cfg['name']}")
            else:
                logger.warning(f"Built-in MCP server failed to connect: {cfg['name']}")
        except asyncio.CancelledError:
            logger.warning(f"Built-in MCP server {cfg['name']} cancelled")
            raise
        except BaseException as e:
            logger.warning(f"Built-in MCP server {cfg['name']} error: {type(e).__name__}: {e}")

    github_servers = github_mcp_servers()
    if not github_servers:
        if not os.environ.get(GITHUB_MCP_TOKEN_ENV, "").strip():
            logger.info(
                "Built-in GitHub MCP servers disabled: %s is not configured", GITHUB_MCP_TOKEN_ENV
            )
        else:
            logger.warning(
                "Built-in GitHub MCP servers unavailable: the github-mcp-server binary was not "
                "found on PATH or at /usr/local/bin (set ODYSSEUS_GITHUB_MCP_BINARY to override)"
            )
    for server_id, cfg in github_servers.items():
        _spawn_bg(_connect_binary_server(server_id, {**cfg, "env": github_mcp_env()}))

    # Register NPX-based servers in the background (they take longer to start)
    npx_path = _find_npx()
    logger.info(f"NPX binary resolved to: {npx_path}")

    async def _start_npx_servers():
        await asyncio.sleep(3)  # let Python servers finish first
        for server_id, cfg in _BUILTIN_NPX_SERVERS.items():
            # Browser automation is a shipped built-in, so the default path
            # lets `npx -y` install @playwright/mcp on first start. Locked-down
            # installs can opt back into the old no-network startup behavior
            # with ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE=1.
            args = _browser_mcp_args(cfg["args"]) if server_id == "builtin_browser" else list(cfg["args"])
            pkg_spec = _npx_package_from_args(args)
            if BROWSER_MCP_REQUIRE_CACHE and pkg_spec and not await _is_npx_package_cached(npx_path, pkg_spec):
                logger.warning(
                    f"{cfg['name']} is not available.\n"
                    f"  Reason: npm package {pkg_spec!r} is not installed in the npx cache.\n"
                    f"  Impact: tools provided by this MCP server will be unavailable.\n"
                    f"  Fix:    {os.path.basename(npx_path)} -y {pkg_spec} --version\n"
                    f"          (run once, then restart Odysseus)\n"
                    f"  Notes:  ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE=1 is set, "
                    f"so Odysseus will not install browser automation on startup."
                )
                continue

            logger.info(f"Starting NPX server: {cfg['name']} ({npx_path} {' '.join(args)})")
            try:
                env = None
                if server_id == "builtin_browser":
                    cache_home = os.environ.get(
                        "ODYSSEUS_BROWSER_MCP_CACHE",
                        os.path.join(base_dir, "data", "local", "playwright-mcp-cache"),
                    )
                    os.makedirs(cache_home, exist_ok=True)
                    env = {
                        "XDG_CACHE_HOME": cache_home,
                        "PLAYWRIGHT_BROWSERS_PATH": os.path.join(cache_home, "browsers"),
                    }
                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=cfg["name"],
                    transport="stdio",
                    command=npx_path,
                    args=args,
                    env=env,
                )
                if ok:
                    logger.info(f"Built-in NPX server registered: {cfg['name']}")
                else:
                    logger.warning(f"Built-in NPX server failed to connect: {cfg['name']}")
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                logger.warning(f"Built-in NPX server {cfg['name']} error: {type(e).__name__}: {e}")

    _spawn_bg(_start_npx_servers())


def _npx_package_from_args(args):
    """Pick the package spec out of an npx args list shaped like
    ['-y', '<package@version>', ...flags]. Returns None if the
    convention doesn't match (we then skip the cache check and just
    try the connect)."""
    if not args:
        return None
    if "-y" in args:
        idx = args.index("-y") + 1
        if idx < len(args) and not args[idx].startswith("-"):
            return args[idx]
    # No -y prefix: first non-flag arg is the package
    for a in args:
        if not a.startswith("-"):
            return a
    return None


async def _is_npx_package_cached(npx_path, package_spec, timeout_s=5):
    """Probe whether an npx package is already in the local cache.

    First checks the local `_npx` cache for an installed package. If the
    package is not found there, falls back to `npx --no-install <pkg>
    --version` so older npm layouts still work without downloading.
    """
    if _is_package_in_npx_cache(package_spec):
        return True

    try:
        proc = await asyncio.create_subprocess_exec(
            npx_path, "--no-install", package_spec, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except NotImplementedError:
        try:
            result = subprocess.run(
                [npx_path, "--no-install", package_spec, "--version"],
                capture_output=True,
                timeout=timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, ValueError):
        return False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return False
    except asyncio.CancelledError:
        # The probe was cancelled (e.g. app shutdown). Reap the child so it
        # isn't orphaned, then propagate the cancellation.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode == 0 and bool(stdout.strip())


def _is_package_in_npx_cache(package_spec):
    """Return True when npm's `_npx` cache already contains package_spec."""
    package_name = _npx_package_name(package_spec)
    if not package_name:
        return False

    for cache_root in _npm_cache_roots():
        npx_root = os.path.join(cache_root, "_npx")
        if _npx_cache_contains_package(npx_root, package_name):
            return True
    return False


def _npx_package_name(package_spec):
    """Strip a version/range suffix from an npm package spec."""
    if not package_spec:
        return ""
    if package_spec.startswith("@"):
        parts = package_spec.split("@", 2)
        if len(parts) >= 3:
            return f"@{parts[1]}"
        return package_spec
    return package_spec.split("@", 1)[0]


def _npm_cache_roots():
    roots = []
    configured = os.environ.get("npm_config_cache")
    if configured:
        roots.append(os.path.expanduser(configured))
    roots.append(os.path.join(os.path.expanduser("~"), ".npm"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(os.path.join(local_app_data, "npm-cache"))
    return list(dict.fromkeys(roots))


def _npx_cache_contains_package(npx_root, package_name):
    if not os.path.isdir(npx_root):
        return False
    package_path = os.path.join("node_modules", *package_name.split("/"), "package.json")
    try:
        entries = list(os.scandir(npx_root))
    except OSError:
        return False
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        cached_name = _cached_package_name(os.path.join(entry.path, package_path))
        if is_dir and cached_name == package_name:
            return True
    return False


def _cached_package_name(package_json_path):
    try:
        with open(package_json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return str(data.get("name", "")).strip()
