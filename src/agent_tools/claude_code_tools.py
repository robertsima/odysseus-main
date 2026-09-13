"""Safe delegation to a locally installed Claude Code CLI.

Odysseus never talks to Anthropic itself on this path. It runs the unmodified
``claude`` binary in headless (``-p``) mode inside an approved Git checkout,
with the operator's own Claude login or API key doing the authentication. The
harness never reads, stores, or forwards those credentials: the only thing it
hands the child is (optionally) a scoped *Odysseus* token so Claude can call
back into this instance during the job.

Two ways in:

* the admin chat tool ``delegate_to_claude_code`` (this module's
  :class:`ClaudeCodeTool`), which the primary Odysseus agent calls, and
* ``/api/claude-code/tasks`` (routes/claude_code_routes.py) for automation.

Both share the per-repository locks, the process limit, the task runner, and
the same validation, so a job started from chat and a job started over HTTP
cannot stomp on the same working tree.

Configuration lives in Settings > Agents (``claude_code_*`` keys, admin-only)
and falls back to the ``CLAUDE_CODE_*`` environment variables, so an operator
can point the harness at a binary or repository root from the UI without
restarting the container.
"""
import asyncio
import contextvars
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.atomic_io import atomic_write_json
from src import agent_activity as activity
from src.constants import CLAUDE_CODE_TASKS_FILE

# Who a run is for. Set by the chat tool / task runner around `_run_claude`
# rather than threaded through its signature, so the many callers and test
# doubles that call `_run_claude(repo, prompt, timeout, tools)` keep working
# while the run still reports to the right chat session's activity feed.
_RUN_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar("claude_code_run_context", default={})


@contextmanager
def run_context(**fields):
    token = _RUN_CONTEXT.set({k: v for k, v in fields.items() if v is not None})
    try:
        yield
    finally:
        _RUN_CONTEXT.reset(token)

# Environment defaults. The ``claude_code_*`` settings override each of these
# (see ``_setting``); tests monkeypatch the module names directly.
DEFAULT_BINARY = os.environ.get("CLAUDE_CODE_BINARY", "/app/data/claude-code/bin/claude")
DEFAULT_ROOTS = tuple(filter(None, os.environ.get(
    "CLAUDE_CODE_REPOSITORY_ROOTS", "/app/data/development:/app/data/agent_worktrees"
).split(os.pathsep)))
DEFAULT_REPOSITORY = os.environ.get("CLAUDE_CODE_DEFAULT_REPOSITORY", "")
# Claude may inspect, edit, test, and commit — never push, touch a remote, or
# run arbitrary shell (see tool_schemas.py's delegate_to_claude_code
# description, which promises exactly this).
DEFAULT_TOOLS = (
    # Glob/Grep are read-only and confined to the checkout like Read; without
    # them Claude has to find code by reading whole files.
    "Read", "Glob", "Grep", "Edit", "Write",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git add:*)", "Bash(git commit:*)",
)
MAX_OUTPUT = 50000
MAX_RESULT_TEXT = 20000
MAX_CONCURRENT_TASKS = max(1, int(os.environ.get("CLAUDE_CODE_MAX_CONCURRENT_TASKS", "2")))
# Each git subcommand is spelled out explicitly — NOT a bare "git" prefix.
# Claude Code's --allowedTools patterns are prefix matches, so a bare
# "Bash(git:*)" would also permit "git push ..." (it starts with "git").
# Enumerating exactly the safe subcommands is what actually keeps push/pull/
# clone/remote/fetch out of reach.
_SAFE_GIT_SUBCOMMANDS = "status|diff|log|show|branch|rev-parse|add|commit"
# Verification commands, all the same trust class as `npm test`: each runs the
# checkout's own test/build config and nothing that publishes or deploys.
_SAFE_TEST_RUNNERS = (r"pytest|python -m pytest|npm test|pnpm test|yarn test"
                      r"|(?:npm run|pnpm(?: run)?|yarn) (?:test|typecheck|type-check|lint|build)"
                      r"|npx tsc --noEmit"
                      r"|(?:\./gradlew|gradle) (?:test|check|build)"
                      r"|(?:\./mvnw|mvn) (?:test|verify|compile)")
# The bundled Odysseus skill helper (integrations/claude/skills/odysseus/
# scripts/odysseus_api.py). Allowing it lets a delegated Claude session call
# back into Odysseus through the scope-gated /api/codex/* API — the only
# way the "bidirectional" half of the integration can work inside one job.
# Anchored to that exact script path so the rule cannot widen into
# arbitrary python.
_CALLBACK_HELPER = r"python3 (?:~|/)[^\s()]*/skills/odysseus/scripts/odysseus_api\.py"
SAFE_TOOL = re.compile(
    rf"^(Read|Glob|Grep|Edit|Write"
    rf"|Bash\(git (?:{_SAFE_GIT_SUBCOMMANDS})(?::\*)?\)"
    rf"|Bash\((?:{_SAFE_TEST_RUNNERS})(?::\*)?\)"
    rf"|Bash\({_CALLBACK_HELPER}(?::\*)?\))$"
)
_MODEL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")
# Flags the runner adapts to. Claude Code < 2.1.259 has no
# --permission-prompts (prompts are denied anyway in a hostless -p run) and
# older builds have no --restricted; both are detected from --help rather
# than guessed from the version string.
_OPTIONAL_FLAGS = ("--permission-prompts", "--restricted", "--bare", "--model",
                   "--tools", "--allowedTools", "--no-session-persistence",
                   "--output-format", "--verbose", "--include-partial-messages")
# Lines of the live transcript kept on the task record (the activity feed
# keeps its own bounded copy per session).
MAX_TRANSCRIPT_ENTRIES = 400
MAX_TRANSCRIPT_TEXT = 1500
# asyncio's default StreamReader limit is 64 KiB per line; one stream-json
# line carrying a Write tool call can be far longer.
_STREAM_LINE_LIMIT = 16 * 1024 * 1024
_REQUIRED_FLAGS = ("--tools", "--allowedTools", "--output-format", "--no-session-persistence")

# Per-repository serialization shared by every caller (the admin chat tool
# AND the HTTP task runner below) so two delegations never run concurrently
# against the same checkout and stomp on each other's working tree.
_REPO_LOCKS: dict[str, asyncio.Lock] = {}
_PROCESS_LIMIT = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
_PROCESS_LIMIT_SIZE = MAX_CONCURRENT_TASKS
# --version / --help of a binary, keyed by path and mtime so an upgraded
# install is re-probed without a restart.
_BINARY_INFO: dict[str, dict] = {}


# ── Configuration (settings first, then environment) ──

def _setting(key: str, default=None):
    """A ``claude_code_*`` setting, or ``default`` when unset/blank.

    Settings are read lazily so importing this module never touches the
    settings file (tests construct the tool without a data directory).
    """
    try:
        from src.settings import get_setting
        value = get_setting(key, None)
    except Exception:
        return default
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, (list, tuple, dict)) and not value:
        # An empty list is the "not overridden" sentinel for list settings
        # (e.g. claude_code_repository_roots) — never "no roots at all".
        return default
    return value


def _flag_setting(key: str, default: bool) -> bool:
    value = _setting(key, None)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def binary_path() -> Path:
    return Path(str(_setting("claude_code_binary", DEFAULT_BINARY))).expanduser()


def repository_roots() -> tuple[Path, ...]:
    raw = _setting("claude_code_repository_roots", None)
    if isinstance(raw, (list, tuple)):
        roots = [str(item) for item in raw if str(item).strip()]
    elif isinstance(raw, str):
        roots = [item for item in raw.replace("\n", os.pathsep).split(os.pathsep) if item.strip()]
    else:
        roots = list(DEFAULT_ROOTS)
    return tuple(Path(root.strip()).expanduser().resolve() for root in roots)


def _process_limit() -> asyncio.Semaphore:
    """The shared concurrency gate, resized when the setting changes.

    In-flight jobs keep the semaphore they acquired; only new jobs see the
    new size, which is the safe direction for a live change.
    """
    global _PROCESS_LIMIT, _PROCESS_LIMIT_SIZE
    try:
        # 0 (the settings default) means "use the environment/built-in value".
        size = int(_setting("claude_code_max_concurrent_tasks", 0) or 0) or MAX_CONCURRENT_TASKS
        size = max(1, min(16, size))
    except (TypeError, ValueError):
        size = MAX_CONCURRENT_TASKS
    if size != _PROCESS_LIMIT_SIZE:
        _PROCESS_LIMIT = asyncio.Semaphore(size)
        _PROCESS_LIMIT_SIZE = size
    return _PROCESS_LIMIT


def _repo_lock(repository: Path) -> asyncio.Lock:
    return _REPO_LOCKS.setdefault(str(repository), asyncio.Lock())


def _head_branch(repository: Path) -> str:
    """Branch name from .git/HEAD without spawning git (worktree-aware)."""
    try:
        git = repository / ".git"
        gitdir = git
        if git.is_file():
            # A linked worktree: ".git" is a pointer file "gitdir: <path>".
            pointer = git.read_text(encoding="utf-8", errors="replace").strip()
            if not pointer.startswith("gitdir:"):
                return ""
            gitdir = Path(pointer.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (repository / gitdir).resolve()
        head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return head[:12] if head else ""


def discover_repositories(limit: int = 25) -> list[dict]:
    """Git checkouts/worktrees at or one level below each approved root.

    The failed test in the 2026-09-10 log passed ``/app`` — not a checkout —
    because the agent had no way to learn which paths *are* approved. Listing
    the real candidates (like the worktree ``doctor`` does) is what lets the
    one permitted retry succeed.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for root in repository_roots():
        if not root.is_dir():
            continue
        candidates = [root]
        try:
            candidates.extend(sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")))
        except OSError:
            pass
        for path in candidates:
            key = str(path)
            if key in seen or not (path / ".git").exists():
                continue
            seen.add(key)
            found.append({"path": key, "root": str(root), "branch": _head_branch(path)})
            if len(found) >= limit:
                return found
    return found


def _describe_candidates() -> str:
    roots = ", ".join(str(root) for root in repository_roots()) or "(none configured)"
    repos = discover_repositories()
    if repos:
        listing = "; ".join(
            f"{repo['path']}" + (f" ({repo['branch']})" if repo["branch"] else "") for repo in repos
        )
        return (f"Approved roots: {roots}. Known repositories: {listing}. "
                "Pass one of these, or omit repository to use the default.")
    return (f"Approved roots: {roots}, but no Git checkout was found under them. "
            "Clone or mount one there, or set CLAUDE_CODE_REPOSITORY_ROOTS / "
            "Settings > Agents > Claude Code repository roots.")


def _approved_repository(value: str) -> Path:
    raw = (value or "").strip()
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except OSError:
        raise ValueError(
            f"repository {raw!r} is not an existing Git repository or worktree. {_describe_candidates()}"
        ) from None
    if not path.is_dir() or not (path / ".git").exists():
        raise ValueError(
            f"repository {raw!r} is not an existing Git repository or worktree. {_describe_candidates()}"
        )
    roots = repository_roots()
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError(
            f"repository {raw!r} is outside Claude Code approved roots. {_describe_candidates()}"
        )
    return path


def default_repository() -> tuple[Optional[Path], str]:
    """Where a delegation goes when the caller names no repository.

    Order: the ``claude_code_default_repository`` setting (or
    ``CLAUDE_CODE_DEFAULT_REPOSITORY``), the worktree tool's
    ``ODYSSEUS_AGENT_SOURCE_REPO``, the agent's active workspace, and finally
    the only discovered checkout. Returns ``(path, how)`` or ``(None, why)``.
    """
    configured = str(_setting("claude_code_default_repository", DEFAULT_REPOSITORY) or "").strip()
    if configured:
        try:
            return _approved_repository(configured), "configured default"
        except ValueError as exc:
            return None, f"configured default repository is unusable: {exc}"
    source = os.environ.get("ODYSSEUS_AGENT_SOURCE_REPO", "").strip()
    if source:
        try:
            return _approved_repository(source), "ODYSSEUS_AGENT_SOURCE_REPO"
        except ValueError:
            pass
    try:
        from src.tool_execution import get_active_workspace
        workspace = get_active_workspace()
    except Exception:
        workspace = None
    if workspace:
        try:
            return _approved_repository(workspace), "active workspace"
        except ValueError:
            pass
    repos = discover_repositories()
    if len(repos) == 1:
        return Path(repos[0]["path"]), "only approved checkout"
    if not repos:
        return None, "no repository given and none found under the approved roots"
    return None, ("no repository given and several are approved; pass one of: "
                  + ", ".join(repo["path"] for repo in repos))


def _callback_config() -> dict:
    """Odysseus callback settings for the child, without the token itself."""
    url = str(_setting("claude_code_odysseus_url", os.environ.get("CLAUDE_CODE_ODYSSEUS_URL", "")) or "").strip()
    token_file = str(_setting("claude_code_odysseus_token_file",
                              os.environ.get("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", "")) or "").strip()
    return {"url": url, "token_file": token_file, "enabled": bool(url and token_file)}


def _claude_home() -> str:
    return str(_setting("claude_code_home", os.environ.get("CLAUDE_CODE_HOME", os.environ.get("HOME", "/app/data"))))


def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(_claude_home(), ".claude")


def default_tools() -> list[str]:
    """The default allowlist, plus the Odysseus callback helper when the
    callback is configured (otherwise the helper is unreachable and Claude
    would only ever see a denial)."""
    tools = list(DEFAULT_TOOLS)
    if _callback_config()["enabled"]:
        helper = os.path.join(_claude_config_dir(), "skills", "odysseus", "scripts", "odysseus_api.py")
        tools.append(f"Bash(python3 {helper}:*)")
        # The bundled SKILL.md shows the helper as ~/.claude/…; allow that
        # spelling too so Claude does not get denied for following its own
        # instructions.
        tilde = "~/.claude/skills/odysseus/scripts/odysseus_api.py"
        if helper != os.path.expanduser(tilde):
            tools.append(f"Bash(python3 {tilde}:*)")
    return tools


def _claude_environment() -> dict[str, str]:
    """Build the child environment without persisting or exposing secrets.

    A deployment may give delegated Claude sessions scoped access back to
    Odysseus with a private token file. Keeping the token out of argv avoids
    process-list exposure; the child receives it only in its environment.
    The child's own Anthropic credentials (its OAuth login or API key) are
    never read here — the unmodified binary resolves them itself.
    """
    env = {
        **os.environ,
        "HOME": _claude_home(),
    }
    callback = _callback_config()
    if callback["url"]:
        env["ODYSSEUS_URL"] = callback["url"]
    if callback["token_file"]:
        path = Path(callback["token_file"]).expanduser()
        try:
            info = path.stat()
            if not path.is_file() or info.st_mode & 0o077:
                raise ValueError("Claude Code Odysseus token file must be a private regular file (mode 0600)")
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Claude Code Odysseus token file is unavailable: {exc}") from exc
        if not token:
            raise ValueError("Claude Code Odysseus token file is empty")
        env["ODYSSEUS_API_TOKEN"] = token
    return env


def _parse_args(args: dict) -> dict:
    """Validate a delegation request. Returns either
    {"repository": Path, "prompt": str, "timeout": int, "tools": list[str], "model": str|None}
    or {"error": str} — never both, and never raises."""
    if not isinstance(args, dict):
        return {"error": "delegate_to_claude_code: JSON object required"}
    requested = str(args.get("repository") or "").strip()
    if requested and requested.lower() != "auto":
        try:
            repository = _approved_repository(requested)
        except (ValueError, OSError) as exc:
            return {"error": f"delegate_to_claude_code: {exc}"}
    else:
        repository, why = default_repository()
        if repository is None:
            return {"error": f"delegate_to_claude_code: {why}. {_describe_candidates()}"}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt or len(prompt) > 20000:
        return {"error": "delegate_to_claude_code: prompt is required and must be <= 20000 characters"}
    try:
        timeout = max(30, min(1800, int(args.get("timeout_seconds", 900))))
    except (TypeError, ValueError):
        timeout = 900
    tools = args.get("allowed_tools") or default_tools()
    if not isinstance(tools, list) or not all(isinstance(item, str) and item for item in tools):
        return {"error": "delegate_to_claude_code: allowed_tools must be a list of strings"}
    rejected = [item for item in tools if not SAFE_TOOL.fullmatch(item)]
    accepted_hint = (
        f"Accepted: Read, Glob, Grep, Edit, Write, Bash(git <{_SAFE_GIT_SUBCOMMANDS.replace('|', '/')}>:*), "
        "Bash(<pytest/npm test/npm run typecheck|lint|build/./gradlew test|build/./mvnw test|verify>:*). "
        "Claude never pushes: publish its commits yourself afterwards."
    )
    if rejected and len(rejected) == len(tools):
        return {"error": f"delegate_to_claude_code: unsafe allowed tool(s) {rejected[:5]}. {accepted_hint} "
                         "Omit allowed_tools to use the defaults."}
    dropped: list[str] = []
    if rejected:
        # A mixed list runs with its safe entries. Refusing the whole job cost
        # the 2026-09-13 run three rounds re-sending one list with `git push`
        # and a malformed entry in it; dropping can only narrow Claude's rights.
        dropped = rejected[:10]
        tools = [item for item in tools if SAFE_TOOL.fullmatch(item)]
    model = str(args.get("model") or _setting("claude_code_model", "") or "").strip() or None
    if model and not _MODEL_RE.fullmatch(model):
        return {"error": "delegate_to_claude_code: model must be a plain model name or alias"}
    parsed = {"repository": repository, "prompt": prompt, "timeout": timeout, "tools": tools, "model": model}
    if dropped:
        parsed["dropped_tools"] = dropped
        parsed["dropped_note"] = f"Ignored unsafe allowed_tools {dropped}; ran with the rest. {accepted_hint}"
    return parsed


def _with_dropped(result: dict, parsed: dict) -> dict:
    if parsed.get("dropped_tools"):
        result["dropped_allowed_tools"] = parsed["dropped_tools"]
        result["allowed_tools_note"] = parsed["dropped_note"]
    return result


# ── Binary probing ──

async def _capture(binary: Path, *args: str, timeout: float = 30, env: Optional[dict] = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        str(binary), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env, cwd=str(binary.parent),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def binary_info(binary: Optional[Path] = None) -> dict:
    """Version and supported flags of the configured binary (cached by mtime)."""
    binary = binary or binary_path()
    key = str(binary)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return {"path": key, "available": False, "version": "", "flags": [],
                "error": f"binary unavailable at {binary}"}
    mtime = binary.stat().st_mtime
    cached = _BINARY_INFO.get(key)
    if cached and cached.get("mtime") == mtime:
        return cached
    info = {"path": key, "available": True, "mtime": mtime, "version": "", "flags": [], "error": ""}
    try:
        code, version = await _capture(binary, "--version", timeout=45)
        info["version"] = version.strip().splitlines()[0] if version.strip() else ""
        code, help_text = await _capture(binary, "--help", timeout=45)
        info["flags"] = [flag for flag in _OPTIONAL_FLAGS if flag in help_text]
        # `stream-json` prints every assistant message and tool call as it
        # happens instead of one envelope at the end — what the Workbench
        # transcript is built from.
        info["stream_json"] = "stream-json" in help_text and "--verbose" in help_text
        missing = [flag for flag in _REQUIRED_FLAGS if flag not in help_text]
        if missing:
            info["error"] = "binary does not support required flags: " + ", ".join(missing)
    except OSError as exc:
        info["error"] = f"could not run binary: {exc}"
    _BINARY_INFO[key] = info
    return info


async def auth_status(binary: Optional[Path] = None) -> dict:
    """``claude auth status`` — whether the binary is signed in, and how.

    Only the booleans/labels Claude Code prints are returned; no token is
    read or exposed. A failure here is reported, not raised, so ``status``
    still answers everything else.
    """
    binary = binary or binary_path()
    try:
        env = _claude_environment()
    except ValueError as exc:
        return {"checked": False, "error": str(exc)}
    try:
        code, out = await _capture(binary, "auth", "status", timeout=45, env=env)
    except OSError as exc:
        return {"checked": False, "error": str(exc)}
    try:
        parsed = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict) or not parsed:
        return {"checked": True, "logged_in": None, "error": out.strip()[-400:] or f"exit {code}"}
    return {
        "checked": True,
        "logged_in": bool(parsed.get("loggedIn")),
        "auth_method": parsed.get("authMethod"),
        "api_provider": parsed.get("apiProvider"),
    }


async def status_report() -> dict:
    """Everything the primary agent needs before delegating.

    This is the preflight the 2026-09-10 test lacked: it says whether Claude
    Code is installed, signed in, which repositories it may work in, and
    whether the callback into Odysseus is configured — in one call.
    """
    binary = binary_path()
    info = await binary_info(binary)
    auth = await auth_status(binary) if info["available"] else {"checked": False, "error": info["error"]}
    callback = _callback_config()
    callback_status = {"enabled": callback["enabled"], "url": callback["url"]}
    token_problem = ""
    if callback["token_file"]:
        path = Path(callback["token_file"]).expanduser()
        callback_status["token_file"] = str(path)
        try:
            st = path.stat()
        except OSError as exc:
            callback_status["token_file_ok"] = False
            token_problem = f"it cannot be read ({exc.strerror or exc})"
        else:
            readable = os.access(path, os.R_OK)
            if not path.is_file():
                token_problem = "it is not a regular file (a directory is created when the path did not exist at container start)"
            elif st.st_mode & 0o077:
                token_problem = f"its mode is {oct(st.st_mode & 0o777)}; it must be 0600"
            elif not readable:
                # Typically created with `docker exec` (root) while the app
                # runs as PUID/PGID — the classic ZimaOS case.
                token_problem = (f"it is owned by uid {st.st_uid} but Odysseus runs as uid {os.getuid()}; "
                                 f"chown it to {os.getuid()}:{os.getgid()}")
            callback_status["token_file_ok"] = not token_problem
            callback_status["token_file_owner"] = st.st_uid
            callback_status["process_uid"] = os.getuid()
    default_repo, how = default_repository()
    runner = get_task_runner()
    _process_limit()  # refresh the gate size from settings before reporting it
    active = [record for record in runner.tasks.values() if record.get("status") in _ACTIVE_STATUSES]
    ready = bool(info["available"] and not info["error"] and auth.get("logged_in"))
    hints: list[str] = []
    if not info["available"]:
        hints.append("Install Claude Code or set Settings > Agents > Claude Code binary (CLAUDE_CODE_BINARY).")
    elif info["error"]:
        hints.append(info["error"])
    if info["available"] and auth.get("logged_in") is False:
        hints.append("The binary is not signed in. Run `claude` once as the container user "
                     "(HOME=%s) and log in with your own Claude account, or provide ANTHROPIC_API_KEY." % _claude_home())
    if info["available"] and "--permission-prompts" not in info["flags"]:
        hints.append("Claude Code is older than 2.1.259; upgrade for --permission-prompts none "
                     "(prompts are still denied in headless mode, but Claude may retry them).")
    if default_repo is None:
        hints.append(how)
    if callback["token_file"] and not callback_status.get("token_file_ok"):
        # _claude_environment() refuses to run with a loose or unreadable
        # token, so this blocks every delegation, not just the callback.
        ready = False
        hints.append(
            f"The callback token file {callback_status.get('token_file')} blocks delegation: {token_problem}. "
            "It must be a regular file, mode 0600, owned by the Odysseus process user "
            "(Settings > Tools > Claude Code > Callback token file / CLAUDE_CODE_ODYSSEUS_TOKEN_FILE). "
            "Fix it, or clear the callback URL and token file to delegate without the callback."
        )
    return {
        "ready": ready,
        "binary": {k: info[k] for k in ("path", "available", "version", "flags", "error")},
        "auth": auth,
        "home": _claude_home(),
        "restricted_mode": _flag_setting("claude_code_restricted", True) and "--restricted" in info["flags"],
        "repository_roots": [str(root) for root in repository_roots()],
        "repositories": discover_repositories(),
        "default_repository": str(default_repo) if default_repo else None,
        "default_repository_source": how if default_repo else None,
        "default_tools": default_tools(),
        "default_model": str(_setting("claude_code_model", "") or "") or None,
        "callback": callback_status,
        "max_concurrent_tasks": _PROCESS_LIMIT_SIZE,
        "active_tasks": len(active),
        "hints": hints,
        "exit_code": 0 if ready else 1,
    }


def _build_argv(binary: Path, prompt: str, tools: list[str], flags: list[str], *,
                model: Optional[str] = None, stream: bool = False) -> list[str]:
    """argv for one headless run. Never shells out through a string — argv is
    built from a fixed binary path and an allowlist-checked ``--allowedTools``
    list, so nothing here is vulnerable to shell interpolation."""
    # ``--tools`` limits which built-ins exist for this run; ``--allowedTools``
    # grants only the allowlist-validated operations without an interactive
    # permission prompt. Both are required in headless mode.
    tool_names = list(dict.fromkeys(tool.split("(", 1)[0] for tool in tools))
    if prompt.startswith("-"):
        # Commander would read a leading dash as an option name.
        prompt = " " + prompt
    argv = [str(binary), "-p", prompt, "--output-format", "stream-json" if stream else "json",
            "--no-session-persistence"]
    if stream:
        # Print mode only streams with --verbose; without it the CLI refuses
        # the format.
        argv.append("--verbose")
    if "--permission-prompts" in flags:
        # Nobody is at the keyboard: anything that would prompt is denied and
        # Claude is told not to retry it.
        argv += ["--permission-prompts", "none"]
    if "--restricted" in flags and _flag_setting("claude_code_restricted", True):
        # Ignore hooks/MCP servers declared inside the (possibly untrusted)
        # checkout, confine file tools to it, and refuse bypassPermissions.
        argv.append("--restricted")
    if model and "--model" in flags:
        argv += ["--model", model]
    argv += ["--tools", *tool_names, "--allowedTools", *tools]
    return argv


def _summarize_envelope(result: dict, parsed: dict) -> None:
    """Lift the fields callers actually read out of Claude Code's JSON result."""
    result["claude"] = parsed
    # Claude Code's JSON result commonly includes these metrics;
    # surface them without making callers parse the raw envelope.
    for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "is_error",
                "subtype", "stop_reason", "session_id"):
        if key in parsed:
            result[key] = parsed[key]
    text = parsed.get("result")
    if isinstance(text, str):
        result["result"] = text[:MAX_RESULT_TEXT] + ("\n... (truncated)" if len(text) > MAX_RESULT_TEXT else "")
    denials = parsed.get("permission_denials")
    if isinstance(denials, list) and denials:
        result["permission_denials"] = [
            {"tool": str(item.get("tool_name") or item.get("tool") or "?"),
             "input": str(item.get("tool_input") or "")[:200]}
            if isinstance(item, dict) else {"tool": str(item)[:200]}
            for item in denials[:25]
        ]
    if parsed.get("is_error") and "error" not in result:
        result["error"] = (text if isinstance(text, str) else str(parsed.get("error") or "Claude Code reported an error"))[:2000]


def _tool_summary(name: str, tool_input: Any) -> str:
    """One line that says what a Claude tool call touched."""
    if not isinstance(tool_input, dict):
        return _clip(str(tool_input or ""), 160)
    for key in ("file_path", "path", "notebook_path", "command", "pattern", "query", "url", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip().splitlines()[0], 160)
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return _clip(value.strip().splitlines()[0], 160)
    return ""


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _block_text(content: Any) -> str:
    """Text of a message/tool_result content field (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


class _Transcript:
    """Turns Claude Code's ``stream-json`` lines into the live transcript.

    Each assistant text, tool call and tool result becomes one bounded entry
    on the task record and one event on the session's activity feed, so the
    chat UI and the Workbench can show Claude working while it works. The
    final ``result`` line is the same envelope the batch mode returns and is
    kept for :func:`_summarize_envelope`.
    """

    def __init__(self, session_id: Optional[str], run_id: str, owner: Optional[str]):
        self.session_id = session_id
        self.run_id = run_id
        self.owner = owner
        self.entries: list[dict] = []
        self.truncated = False
        self.envelope: Optional[dict] = None
        self.tool_names: dict[str, str] = {}

    def _add(self, entry: dict) -> None:
        if len(self.entries) >= MAX_TRANSCRIPT_ENTRIES:
            self.truncated = True
            return
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.entries.append(entry)

    def _publish(self, kind: str, title: str, *, detail: Optional[str] = None,
                 data: Optional[dict] = None, level: str = "info") -> None:
        activity.publish(self.session_id, kind, title, source="claude_code", run_id=self.run_id,
                         owner=self.owner, detail=detail, data=data, level=level)

    def feed(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("{"):
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")
        try:
            if kind == "system" and obj.get("subtype") == "init":
                tools = obj.get("tools") or []
                summary = f"session ready (model {obj.get('model') or '?'}, {len(tools)} tools)"
                self._add({"kind": "status", "text": summary})
                self._publish("status", f"Claude Code {summary}",
                              data={"model": obj.get("model"), "cwd": obj.get("cwd")})
            elif kind == "assistant":
                self._assistant(obj.get("message") or {})
            elif kind == "user":
                self._user(obj.get("message") or {})
            elif kind == "result":
                self.envelope = obj
        except Exception as exc:  # never let transcript bookkeeping kill the run
            self._add({"kind": "status", "text": f"transcript parse error: {exc}"})

    def _assistant(self, message: dict) -> None:
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and (block.get("text") or "").strip():
                text = block["text"].strip()
                self._add({"kind": "message", "text": _clip(text, MAX_TRANSCRIPT_TEXT)})
                self._publish("message", _clip(text.splitlines()[0], 200), detail=_clip(text, 2000))
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "tool")
                summary = _tool_summary(name, block.get("input"))
                tool_id = str(block.get("id") or "")
                if tool_id:
                    self.tool_names[tool_id] = name
                entry = {"kind": "tool_start", "tool": name, "summary": summary, "tool_use_id": tool_id}
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    compact = {k: (_clip(v, 400) if isinstance(v, str) else v) for k, v in tool_input.items()}
                    entry["input"] = compact
                self._add(entry)
                self._publish("tool_start", f"{name} {summary}".strip(),
                              data={"tool": name, "tool_use_id": tool_id, "input": entry.get("input")})

    def _user(self, message: dict) -> None:
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            name = self.tool_names.get(tool_id, "tool")
            is_error = bool(block.get("is_error"))
            text = _block_text(block.get("content")).strip()
            self._add({"kind": "tool_result", "tool": name, "tool_use_id": tool_id, "is_error": is_error,
                       "excerpt": _clip(text, MAX_TRANSCRIPT_TEXT)})
            self._publish("tool_result", f"{name} {'failed' if is_error else 'done'}",
                          detail=_clip(text, 2000) or None,
                          data={"tool": name, "tool_use_id": tool_id, "is_error": is_error},
                          level="error" if is_error else "info")


async def _pump_stream(proc, transcript: _Transcript) -> tuple[bytes, bytes]:
    """Read stdout line by line (feeding the transcript) and stderr whole."""
    chunks: list[bytes] = []
    kept = 0

    async def read_out():
        nonlocal kept
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            transcript.feed(line)
            if kept < MAX_OUTPUT * 2:
                chunks.append(line)
                kept += len(line)

    async def read_err():
        return await proc.stderr.read()

    _, err = await asyncio.gather(read_out(), read_err())
    await proc.wait()
    return b"".join(chunks), err


async def _git_head(repository: Path) -> Optional[str]:
    if not (Path(repository) / ".git").exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--verify", "--quiet", "HEAD", cwd=str(repository),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return None
    sha = stdout.decode("utf-8", errors="replace").strip()
    return sha or None


async def _git_changes(repository: Path, start_sha: Optional[str]) -> dict:
    """Per-file numstat and the commits Claude made, relative to where the run
    started, for the Changes/Commits views. Empty on any failure."""
    from src import repo_inspect

    out: dict = {"start_commit": start_sha}
    if not (Path(repository) / ".git").exists():
        return out
    try:
        roots = [str(repository)]
        changes = await repo_inspect.changes(str(repository), base=start_sha, roots=roots)
        out["changes"] = changes["files"]
        out["changes_truncated"] = changes["truncated"]
        out["total_additions"] = changes["total_additions"]
        out["total_deletions"] = changes["total_deletions"]
        out["commits"] = await repo_inspect.commits(str(repository), base=start_sha, roots=roots, limit=50) if start_sha else []
    except Exception as exc:
        out["changes_error"] = str(exc)[:300]
    return out


def _run_title(prompt: str, label: Optional[str]) -> str:
    text = (label or "").strip() or " ".join((prompt or "").split())
    return "Claude Code: " + _clip(text, 90)


def _finish_run(ctx: dict, run_id: str, title: str, result: dict, *, status: Optional[str] = None) -> None:
    """Close the run on the activity feed with what changed."""
    session_id, owner = ctx.get("session_id"), ctx.get("owner")
    changes = result.get("changes") or []
    for row in changes[:50]:
        activity.publish(session_id, "file_change",
                         f"{row.get('status', 'modified')} {row.get('path')} (+{row.get('additions', 0)} −{row.get('deletions', 0)})",
                         source="claude_code", run_id=run_id, owner=owner, data=row)
    for commit in (result.get("commits") or [])[:20]:
        activity.publish(session_id, "commit", f"commit {commit.get('short')}: {commit.get('subject')}",
                         source="claude_code", run_id=run_id, owner=owner, data=commit)
    if status is None:
        failed = bool(result.get("error")) or bool(result.get("is_error")) or result.get("exit_code") not in (0, None)
        status = "failed" if failed else "completed"
    data = {
        "task_id": ctx.get("task_id"),
        "repository": result.get("repository"),
        "branch": result.get("branch"),
        "commit": result.get("commit"),
        "changed_files": (result.get("changed_files") or [])[:100],
        "changes_count": len(changes),
        "commits": len(result.get("commits") or []),
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd"),
        "exit_code": result.get("exit_code"),
        "error": _clip(result.get("error") or "", 400) or None,
        "result_excerpt": _clip(result.get("result") or "", 400) or None,
    }
    suffix = {"completed": "finished", "failed": "failed", "cancelled": "cancelled"}.get(status, status)
    activity.run_finished(session_id, "claude_code", run_id, f"{title} — {suffix}", status=status,
                          owner=owner, data=data, detail=_clip(result.get("result") or result.get("error") or "", 2000) or None)


async def _run_claude(
    repository: Path,
    prompt: str,
    timeout: int,
    tools: list[str],
    on_process=None,
    model: Optional[str] = None,
) -> dict:
    """Run the Claude Code CLI once, serialized per repository.

    ``on_process`` (optional) is called with the live ``Process`` as soon as
    it starts, so a caller (the task runner) can kill it on cancellation.
    The run reports to the activity feed of the session named in
    :func:`run_context` (if any); with a ``stream-json``-capable binary the
    transcript arrives live, otherwise only the final envelope does.
    """
    binary = binary_path()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return {"error": f"delegate_to_claude_code: binary unavailable at {binary}", "exit_code": 1}
    info = await binary_info(binary)
    if info.get("error"):
        return {"error": f"delegate_to_claude_code: {info['error']}", "exit_code": 1}
    stream = bool(info.get("stream_json")) and _flag_setting("claude_code_stream_transcript", True)
    argv = _build_argv(binary, prompt, tools, info.get("flags", []), model=model, stream=stream)
    try:
        child_env = _claude_environment()
    except ValueError as exc:
        return {"error": f"delegate_to_claude_code: {exc}", "exit_code": 1}
    ctx = dict(_RUN_CONTEXT.get() or {})
    run_id = ctx.get("task_id") or activity.new_run_id("claude_code")
    title = _run_title(prompt, ctx.get("label"))
    # The repository lock prevents worktree collisions. The process limit also
    # bounds aggregate CPU/memory/API use across different repositories.
    async with _process_limit(), _repo_lock(repository):
        start_sha = await _git_head(repository)
        activity.run_started(ctx.get("session_id"), "claude_code", title, run_id=run_id, owner=ctx.get("owner"),
                             data={"repository": str(repository), "model": model, "task_id": ctx.get("task_id"),
                                   "mode": "stream" if stream else "batch"},
                             detail=_clip(prompt, 1500))
        transcript = _Transcript(ctx.get("session_id"), run_id, ctx.get("owner"))
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(repository), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env, limit=_STREAM_LINE_LIMIT,
            )
            if on_process is not None:
                on_process(proc)
            if stream:
                stdout, stderr = await asyncio.wait_for(_pump_stream(proc, transcript), timeout=timeout)
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            assert proc is not None
            proc.kill()
            await proc.wait()
            result = {"error": f"Claude Code timed out after {timeout}s", "exit_code": 124,
                      "repository": str(repository), "transcript": transcript.entries}
            result.update(await _git_changes(repository, start_sha))
            _finish_run(ctx, run_id, title, result)
            return result
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            _finish_run(ctx, run_id, title, {"error": "Task cancelled", "exit_code": 130,
                                             "repository": str(repository)}, status="cancelled")
            raise
        except OSError as exc:
            result = {"error": f"Claude Code could not start: {exc}", "exit_code": 127, "repository": str(repository)}
            _finish_run(ctx, run_id, title, result)
            return result
        full_out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")[-10000:]
        result = {
            "output": full_out[-MAX_OUTPUT:],
            "stderr": err,
            "exit_code": proc.returncode,
            "repository": str(repository),
            "model": model,
        }
        parsed = transcript.envelope if stream else None
        if parsed is None:
            try:
                parsed = json.loads(full_out)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict):
            _summarize_envelope(result, parsed)
        elif proc.returncode:
            # No envelope at all: the CLI rejected a flag, could not
            # authenticate, or crashed before the run started. stderr has it.
            result["error"] = (err.strip() or full_out.strip() or f"Claude Code exited {proc.returncode}")[-2000:]
        if stream:
            result["transcript"] = transcript.entries
            result["transcript_truncated"] = transcript.truncated
        result.update(await _git_report(repository))
        result.update(await _git_changes(repository, start_sha))
        _finish_run(ctx, run_id, title, result)
        return result


_ACTIONS = ("run", "start", "poll", "get", "cancel", "list", "status", "list_repositories", "repositories")
# Longest a single poll may block. The 2026-09-12 logs show the agent
# alternating `bash sleep 20..120` with poll for ten minutes until the
# loop-breaker ended the turn; one poll that waits on the job replaces both.
MAX_POLL_WAIT_S = 600


def _running_report(record: dict) -> dict:
    """A still-running task as the model should see it: where it is and what
    Claude did last, plus how to wait without burning rounds."""
    out = {key: record.get(key) for key in ("task_id", "status", "repository", "label", "model",
                                            "created_at", "started_at") if record.get(key) is not None}
    started = record.get("started_at") or record.get("created_at")
    try:
        out["elapsed_seconds"] = int(datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(started).timestamp())
    except (TypeError, ValueError):
        pass
    try:
        recent = activity.run_events(record.get("task_id") or "", limit=8)
    except Exception:
        recent = []
    progress = [ev.get("title") for ev in recent if ev.get("kind") in ("message", "tool_start", "tool_result", "status")]
    if progress:
        out["recent_activity"] = progress[-6:]
    out["note"] = (f"Still {record.get('status')}. To wait, call poll again with wait_seconds (up to {MAX_POLL_WAIT_S}) — "
                   "do not sleep in bash. Or end your turn and tell the user; the result is kept for 24h.")
    out["exit_code"] = 0
    return out


class ClaudeCodeTool:
    """The ``delegate_to_claude_code`` admin chat tool.

    ``action`` selects the operation (default ``run``):

    * ``status`` — preflight: binary, sign-in, approved repositories, callback.
    * ``list_repositories`` — approved checkouts/worktrees.
    * ``run`` — delegate and wait (blocks this agent turn up to ``timeout_seconds``).
    * ``start`` / ``poll`` / ``cancel`` / ``list`` — the same job as a background
      task, so the primary agent can keep working (or coordinate several
      Claude jobs across repositories) and pick the result up later.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if (content or "").strip() else {}
        except (TypeError, json.JSONDecodeError):
            return {"error": "delegate_to_claude_code: JSON object required", "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "delegate_to_claude_code: JSON object required", "exit_code": 1}
        action = str(args.get("action") or "run").strip().lower()
        if action not in _ACTIONS:
            return {"error": f"delegate_to_claude_code: unknown action {action!r}; one of {', '.join(_ACTIONS)}",
                    "exit_code": 1}
        owner = (ctx or {}).get("owner") if isinstance(ctx, dict) else None
        session_id = (ctx or {}).get("session_id") if isinstance(ctx, dict) else None
        if action == "status":
            return await status_report()
        if action in ("list_repositories", "repositories"):
            repos = discover_repositories()
            default_repo, how = default_repository()
            return {
                "repository_roots": [str(root) for root in repository_roots()],
                "repositories": repos,
                "default_repository": str(default_repo) if default_repo else None,
                "default_repository_source": how,
                "exit_code": 0,
            }
        runner = get_task_runner()
        if action == "start":
            started = await runner.start(args, owner=owner, session_id=session_id)
            if "error" in started:
                return {**started, "exit_code": 1}
            return {**started, "exit_code": 0,
                    "note": "Poll with action=poll and this task_id; cancel with action=cancel."}
        if action in ("poll", "get", "cancel"):
            task_id = str(args.get("task_id") or "").strip()
            if not task_id:
                return {"error": "delegate_to_claude_code: task_id is required", "exit_code": 1}
            if action == "cancel":
                record = await runner.cancel(task_id)
            else:
                try:
                    wait = max(0, min(MAX_POLL_WAIT_S, int(args.get("wait_seconds") or 0)))
                except (TypeError, ValueError):
                    wait = 0
                record = await runner.wait(task_id, wait) if wait else runner.get(task_id)
            if record is None:
                return {"error": f"delegate_to_claude_code: task {task_id} not found", "exit_code": 1}
            if record.get("status") in _ACTIVE_STATUSES:
                return _running_report(record)
            return {**record, "exit_code": record.get("exit_code", 1)}
        if action == "list":
            return {"tasks": runner.summaries(), "exit_code": 0}
        parsed = _parse_args(args)
        if "error" in parsed:
            return {**parsed, "exit_code": 1}
        label = str(args.get("label") or "").strip()[:120] or None
        with run_context(session_id=session_id, owner=owner, label=label):
            result = await _run_claude(parsed["repository"], parsed["prompt"], parsed["timeout"], parsed["tools"],
                                       model=parsed["model"])
        return _with_dropped(result, parsed)


async def _git_report(repository: Path) -> dict:
    """Return reviewable repository facts without granting Claude extra access."""
    async def run(*args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(repository), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return stdout.decode("utf-8", errors="replace").rstrip()
    try:
        branch = await run("branch", "--show-current")
        status = await run("status", "--short")
        commit = await run("rev-parse", "HEAD")
        changed_files = [line[3:] if len(line) > 3 else line for line in status.splitlines() if line]
        return {
            "branch": branch,
            "clean": not bool(status),
            "status": status,
            "changed_files": changed_files,
            "commit": commit,
        }
    except (OSError, asyncio.TimeoutError) as exc:
        return {"git_error": str(exc)}


_ACTIVE_STATUSES = {"queued", "running"}
# Keep finished task records around for a day so a caller can poll a result
# after the fact; old ones are pruned on every save so the store file (and
# the repo output/stderr it inlines, each capped well below MAX_OUTPUT) can't
# grow without bound.
_RETENTION_S = 86400
_SUMMARY_FIELDS = ("task_id", "status", "repository", "owner", "created_at", "started_at",
                   "finished_at", "exit_code", "branch", "commit", "changed_files", "error",
                   "num_turns", "total_cost_usd", "model", "label", "session_id", "start_commit",
                   "total_additions", "total_deletions")


def _prune(tasks: dict, now: float) -> bool:
    stale = []
    for task_id, record in tasks.items():
        if record.get("status") in _ACTIVE_STATUSES:
            continue
        finished_at = record.get("finished_at")
        if not finished_at:
            continue
        try:
            ts = datetime.fromisoformat(finished_at).timestamp()
        except ValueError:
            continue
        if now - ts > _RETENTION_S:
            stale.append(task_id)
    for task_id in stale:
        tasks.pop(task_id, None)
    return bool(stale)


class ClaudeCodeTaskRunner:
    """Bounded, restart-safe task runner used by the HTTP integration.

    Task metadata (status, owner, repository, branch/commit/output — never the
    prompt, credentials, or process environment) is written to ``CLAUDE_CODE_TASKS_FILE``
    after every state change via ``atomic_write_json``, so ``GET .../tasks/{id}``
    still reports the last known state after a process restart. The running
    subprocess and its asyncio.Task do not themselves survive a restart — a
    task that was queued/running when the process died is reconciled to
    "interrupted" the next time the store loads.
    """

    def __init__(self, store_path: Optional[str] = None):
        self.store_path = store_path or CLAUDE_CODE_TASKS_FILE
        self.tasks: dict[str, dict] = self._load()
        self._reconcile_interrupted()
        self.jobs: dict[str, asyncio.Task] = {}
        self.procs: dict[str, "asyncio.subprocess.Process"] = {}
        self._cancelling: set[str] = set()

    def _load(self) -> dict:
        try:
            path = Path(self.store_path)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    records = {str(k): v for k, v in data.items() if isinstance(v, dict)}
                    # Strip fields persisted by pre-release/older versions.
                    # Prompts and child environments must never survive on disk.
                    for record in records.values():
                        record.pop("prompt", None)
                        record.pop("env", None)
                        record.pop("environment", None)
                    return records
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save(self) -> None:
        _prune(self.tasks, datetime.now(timezone.utc).timestamp())
        # Task output can contain source snippets or user-provided instructions.
        # Set the private mode on the temporary file before its atomic replace,
        # so there is no world-readable window.
        atomic_write_json(self.store_path, self.tasks, indent=2, mode=0o600)

    def _reconcile_interrupted(self) -> None:
        changed = False
        now = datetime.now(timezone.utc).isoformat()
        for record in self.tasks.values():
            if record.get("status") in _ACTIVE_STATUSES:
                record.update({
                    "status": "interrupted",
                    "error": "Task was interrupted by a server restart",
                    "exit_code": record.get("exit_code", 1),
                    "finished_at": now,
                })
                changed = True
        if changed:
            self._save()

    async def start(self, args: dict, *, owner: Optional[str] = None,
                    session_id: Optional[str] = None) -> dict:
        parsed = _parse_args(args if isinstance(args, dict) else {})
        if "error" in parsed:
            return {**parsed, "exit_code": 1}
        task_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        label = str(args.get("label") or "").strip()[:120] if isinstance(args, dict) else ""
        self.tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "repository": str(parsed["repository"]),
            "owner": owner,
            # The chat session that delegated, so the run reports to its
            # activity feed and the UI can find the task from the chat.
            "session_id": session_id,
            "created_at": now,
            "model": parsed["model"],
            # A short, operator-chosen name; the prompt itself is never stored.
            "label": label or None,
        }
        self._save()
        job = asyncio.create_task(self._run(task_id, parsed))
        self.jobs[task_id] = job
        return _with_dropped({"task_id": task_id, "status": "queued", "repository": str(parsed["repository"])}, parsed)

    async def _run(self, task_id: str, parsed: dict):
        record = self.tasks[task_id]
        record.update({"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        self._save()

        def _register(proc: "asyncio.subprocess.Process") -> None:
            self.procs[task_id] = proc

        try:
            with run_context(session_id=record.get("session_id"), owner=record.get("owner"),
                             task_id=task_id, label=record.get("label")):
                result = await _run_claude(
                    parsed["repository"], parsed["prompt"], parsed["timeout"], parsed["tools"],
                    on_process=_register, model=parsed.get("model"),
                )
            record.update(result)
            if task_id in self._cancelling:
                record.update({"status": "cancelled", "error": "Task cancelled", "exit_code": 130})
            else:
                ok = result.get("exit_code") == 0 and not result.get("is_error")
                record["status"] = "completed" if ok else "failed"
        except asyncio.CancelledError:
            record.update({"status": "cancelled", "error": "Task cancelled", "exit_code": 130})
        except Exception as exc:
            record.update({"status": "failed", "error": str(exc), "exit_code": 1})
        finally:
            self.procs.pop(task_id, None)
            self.jobs.pop(task_id, None)
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save()
            self._cancelling.discard(task_id)

    def get(self, task_id: str, *, owner: Optional[str] = None) -> dict | None:
        record = self.tasks.get(task_id)
        if record is None or (owner is not None and record.get("owner") != owner):
            return None
        return dict(record)

    async def wait(self, task_id: str, timeout: float, *, owner: Optional[str] = None) -> dict | None:
        """``get``, after waiting up to ``timeout`` seconds for the job to end.

        ``asyncio.wait`` never cancels the job, so an abandoned wait (the chat
        turn stopped) leaves the delegation running.
        """
        job = self.jobs.get(task_id)
        if job is not None and not job.done() and timeout > 0:
            await asyncio.wait({job}, timeout=timeout)
        return self.get(task_id, owner=owner)

    def summaries(self, *, owner: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Bounded per-task rows (no output blobs) for listings and the UI."""
        rows = []
        for record in self.tasks.values():
            if owner is not None and record.get("owner") != owner:
                continue
            row = {key: record.get(key) for key in _SUMMARY_FIELDS if key in record}
            row["changes_count"] = len(record.get("changes") or [])
            row["commit_count"] = len(record.get("commits") or [])
            row["has_transcript"] = bool(record.get("transcript"))
            rows.append(row)
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows[:limit]

    async def cancel(self, task_id: str, *, owner: Optional[str] = None) -> dict | None:
        """Kill a queued/running task's subprocess and mark it cancelled.
        Idempotent: cancelling an already-finished task just returns its
        (unchanged) record."""
        record = self.tasks.get(task_id)
        if record is None or (owner is not None and record.get("owner") != owner):
            return None
        if record.get("status") not in _ACTIVE_STATUSES:
            return dict(record)
        self._cancelling.add(task_id)
        proc = self.procs.get(task_id)
        if proc is not None and proc.returncode is None:
            proc.kill()
        job = self.jobs.get(task_id)
        if job is not None and not job.done():
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
        if record.get("status") in _ACTIVE_STATUSES:
            record.update({
                "status": "cancelled",
                "error": "Task cancelled by request",
                "exit_code": 130,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
        self._save()
        self._cancelling.discard(task_id)
        return dict(record)


_task_runner: Optional[ClaudeCodeTaskRunner] = None


def get_task_runner() -> ClaudeCodeTaskRunner:
    """Process-wide singleton so the HTTP routes and any other caller share
    one task store and one set of per-repo locks/live-process handles."""
    global _task_runner
    if _task_runner is None:
        _task_runner = ClaudeCodeTaskRunner()
    return _task_runner
