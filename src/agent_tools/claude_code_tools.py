"""Safe delegation to a locally installed Claude Code CLI.

Agamemnon never talks to Anthropic itself on this path. It runs the unmodified
``claude`` binary in headless (``-p``) mode inside an approved Git checkout,
with the operator's own Claude login or API key doing the authentication. The
harness never reads, stores, or forwards those credentials: the only thing it
hands the child is (optionally) a scoped *Agamemnon* token so Claude can call
back into this instance during the job. The child's environment is an
allowlist (``_base_child_environment``), not the server's: no GitHub or other
server credentials reach it, deletes/force/remote commands are denied with
``--disallowedTools``, and its output is redacted before the model sees it.

Two ways in:

* the admin chat tool ``delegate_to_claude_code`` (this module's
  :class:`ClaudeCodeTool`), which the primary Agamemnon agent calls, and
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
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.atomic_io import atomic_write_json
from src import agent_activity as activity
from src.constants import CLAUDE_CODE_TASKS_FILE

logger = logging.getLogger(__name__)

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
# How much raw stream-json to keep once a parsed transcript already carries the
# run. Enough to show the tail of a broken stream, small enough not to duplicate
# the transcript. See the note in _run_claude.
RAW_TAIL_ON_ENVELOPE = 2000
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
# The bundled Agamemnon skill helper (clients/claude/skills/odysseus/
# scripts/odysseus_api.py). Allowing it lets a delegated Claude session call
# back into Agamemnon through the scope-gated /api/codex/* API — the only
# way the "bidirectional" half of the integration can work inside one job.
# Anchored to that exact script path so the rule cannot widen into
# arbitrary python.
_CALLBACK_HELPER = r"python3 (?:~|/)[^\s()]*/skills/odysseus/scripts/odysseus_api\.py"
# A folder inside the checkout as a test command names it: relative, no
# `..`, nothing a shell treats specially.
_REL_DIR = r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,120}"
# The same runners pointed at a project in a subfolder. Claude Code starts at
# the checkout's root and may not `cd`, so a monorepo's `backend/` or
# `mobile/` tests could not run at all: on 2026-09-30 Opus reported "the
# wrapper lives only in backend/ ... and changing directory isn't permitted".
_SUBDIR_TEST_RUNNERS = (
    rf"npm --prefix {_REL_DIR} (?:test|run (?:test|typecheck|type-check|lint|build))"
    rf"|pnpm (?:-C|--dir) {_REL_DIR} (?:run )?(?:test|typecheck|type-check|lint|build)"
    rf"|yarn --cwd {_REL_DIR} (?:test|typecheck|type-check|lint|build)"
    rf"|(?:\./)?{_REL_DIR}/mvnw -f {_REL_DIR}/pom\.xml (?:test|verify|compile)"
    rf"|mvn -f {_REL_DIR}/pom\.xml (?:test|verify|compile)"
    rf"|(?:\./)?{_REL_DIR}/gradlew -p {_REL_DIR} (?:test|check|build)"
    rf"|gradle -p {_REL_DIR} (?:test|check|build)"
)
SAFE_TOOL = re.compile(
    rf"^(Read|Glob|Grep|Edit|Write"
    rf"|Bash\(git (?:{_SAFE_GIT_SUBCOMMANDS})(?::\*)?\)"
    rf"|Bash\((?:{_SAFE_TEST_RUNNERS}|{_SUBDIR_TEST_RUNNERS})(?::\*)?\)"
    # Exactly `cd <folder>`, so `cd backend && ./mvnw test` passes as two
    # allowed steps; the command after it must be allowed on its own.
    rf"|Bash\(cd {_REL_DIR}\)"
    rf"|Bash\({_CALLBACK_HELPER}(?::\*)?\))$"
)
# Deny rules passed as ``--disallowedTools`` on every run, whatever the
# allowlist says. Claude Code evaluates deny before allow, so these close the
# gaps a prefix-matched allow rule leaves open — ``Bash(git branch:*)`` would
# otherwise also permit ``git branch -D main`` — and keep deleting or
# rewriting history, touching a remote, and GitHub CLI access out of reach
# even if a future allowlist entry is too broad. Delegated Claude may edit
# and commit; it may not delete, force, or publish.
DISALLOWED_TOOLS = (
    # Filesystem deletion.
    "Bash(rm:*)", "Bash(rmdir:*)", "Bash(unlink:*)", "Bash(shred:*)",
    "Bash(find:*)", "Bash(xargs:*)", "Bash(sudo:*)",
    # Branch/tag/ref deletion and force-moves.
    "Bash(git branch -d:*)", "Bash(git branch -D:*)", "Bash(git branch --delete:*)",
    "Bash(git branch -f:*)", "Bash(git branch --force:*)", "Bash(git branch -M:*)",
    "Bash(git tag -d:*)", "Bash(git tag --delete:*)", "Bash(git update-ref:*)",
    # History and working-tree destruction.
    "Bash(git reset:*)", "Bash(git clean:*)", "Bash(git rm:*)", "Bash(git checkout:*)",
    "Bash(git restore:*)", "Bash(git stash:*)", "Bash(git rebase:*)",
    "Bash(git filter-branch:*)", "Bash(git reflog:*)", "Bash(git gc:*)", "Bash(git prune:*)",
    "Bash(git worktree:*)", "Bash(git commit --amend:*)",
    # Remotes and credentials: Claude never publishes (see _base_child_environment).
    "Bash(git push:*)", "Bash(git pull:*)", "Bash(git fetch:*)", "Bash(git clone:*)",
    "Bash(git remote:*)", "Bash(git config:*)", "Bash(git credential:*)",
    "Bash(gh:*)", "Bash(curl:*)", "Bash(wget:*)",
    # Reading the environment back.
    "Bash(env:*)", "Bash(printenv:*)", "Bash(set:*)", "Bash(export:*)",
)
_MODEL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")
# What the local Claude Code CLI's --model accepts: its aliases
# (code.claude.com/docs/en/model-config) and Claude model IDs, either with an
# optional "[1m]" (1M-token context) suffix. 2026-09-27: `opus-5.5` reached the
# CLI and was refused only after a process had started, and `gpt-6-sol` was
# sent to it through delegate_to_agent; both are now caught in _parse_args.
_CLAUDE_MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable", "best", "opusplan", "default")
_CLAUDE_MODEL_EXAMPLES = ("claude-opus-5-5", "claude-sonnet-5", "claude-fable-5-1", "claude-haiku-4-5-20251001")
_CLAUDE_FAMILIES = "fable|opus|sonnet|haiku"
# Families whose short ID is not the API's canonical one.
_CLAUDE_MODEL_IDS = {("haiku", "4-5"): "claude-haiku-4-5-20251001"}
# "opus-5.5", "Opus 5.5", "claude opus 5.5", "sonnet-5", "claude-opus-5-5".
_HUMAN_CLAUDE_MODEL = re.compile(rf"^(?:claude[\s_\-]*)?({_CLAUDE_FAMILIES})(?:[\s_\-]*v?(\d+(?:[\s._\-]+\d+)*))?$")
# Any other claude-* ID (older "claude-3-5-sonnet-20241022", Vertex "...@20251001"),
# and Bedrock IDs ("us.anthropic.claude-sonnet-5-v1:0") for a CLI on that provider.
_CLAUDE_MODEL_ID = re.compile(r"^(?:(?:[a-z]{2,6}\.)?anthropic\.)?claude-[a-z0-9][a-z0-9.\-]*(?:@\d{8})?(?::\d+)?$")
_FOREIGN_MODEL = re.compile(r"^(?:gpt|chatgpt|o\d|gemini|gemma|llama|mistral|mixtral|codestral|devstral|deepseek|"
                            r"qwen|grok|command|phi|kimi|glm|minimax|nova)", re.IGNORECASE)
# Flags the runner adapts to. Claude Code < 2.1.259 has no
# --permission-prompts (prompts are denied anyway in a hostless -p run) and
# older builds have no --restricted; both are detected from --help rather
# than guessed from the version string.
_OPTIONAL_FLAGS = ("--permission-prompts", "--restricted", "--bare", "--model",
                   "--tools", "--allowedTools", "--disallowedTools", "--no-session-persistence",
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

# ``delegate_to_agent`` can use this implementation through the local CLI
# provider.  Keep diagnostics tied to the spelling the model actually called;
# otherwise a provider-neutral call receives a misleading Claude-specific
# correction message.
_TOOL_NAMES = {"delegate_to_agent", "delegate_to_claude_code"}
_DEFAULT_TOOL_NAME = "delegate_to_claude_code"


def _tool_name(value: Optional[str] = None) -> str:
    value = str(value or "").strip()
    return value if value in _TOOL_NAMES else _DEFAULT_TOOL_NAME


def _tool_error(message: str, value: Optional[str] = None) -> str:
    return f"{_tool_name(value)}: {message}"


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
    resolved = [Path(root.strip()).expanduser().resolve() for root in roots]
    # The harness's own worktree root is always approved. Saving
    # claude_code_repository_roots (e.g. ["/app/data/development"]) replaces
    # the default that listed /app/data/agent_worktrees, and 2026-09-27 runs on
    # a worktree manage_agent_worktree had just created were refused as
    # "outside Claude Code approved roots". manage_git's git_repository_roots
    # admits the same root for the same reason; the harness creates and writes
    # it, so approving it widens nothing.
    worktree_root = _managed_worktree_root()
    if worktree_root is not None and worktree_root not in resolved:
        resolved.append(worktree_root)
    return tuple(resolved)


def _managed_worktree_root() -> Optional[Path]:
    """The agent worktree root (``agent_worktree.config``), when it exists."""
    from src.agent_worktree.ownership import managed_worktree_root
    return managed_worktree_root()


def configured_concurrency() -> int:
    """Read the provider-wide ceiling without creating or resizing a gate."""
    try:
        # 0 (the settings default) means "use the environment/built-in value".
        size = int(_setting("claude_code_max_concurrent_tasks", 0) or 0) or MAX_CONCURRENT_TASKS
        size = max(1, min(16, size))
    except (TypeError, ValueError):
        size = MAX_CONCURRENT_TASKS
    return size


def _process_limit() -> asyncio.Semaphore:
    """The shared concurrency gate, resized when the setting changes.

    In-flight jobs keep the semaphore they acquired; only new jobs see the
    new size, which is the safe direction for a live change.
    """
    global _PROCESS_LIMIT, _PROCESS_LIMIT_SIZE
    size = configured_concurrency()
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
            # A linked worktree's ".git" is a pointer file; say which it is so
            # the agent can tell a harness worktree from the main checkout.
            kind = "worktree" if (path / ".git").is_file() else "checkout"
            found.append({"path": key, "root": str(root), "branch": _head_branch(path), "kind": kind})
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
            "Settings › Agents › Development folders.")


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


# ── Runs started by a worker ──
#
# A worker (a chat with a `parent_session`) gets this tool only when its
# loadout names it (headless_agent.EXPLICIT_GRANT_LAUNCH_TOOLS). Its file tools
# are confined to its workspace, and a Claude Code run it starts is held to the
# same place: its workspace, or a managed worktree of the same repository
# (manage_agent_worktree creates those under the harness's worktree root, and
# a lead engineer implements there rather than in the main checkout). It never
# falls back to the configured default repository, never uses the cloud
# runner, and may not update the shared binary.

def _caller_worker_settings(session_id: Optional[str]) -> Optional[dict]:
    """The calling chat's stored settings when it is a worker, else None."""
    if not session_id:
        return None
    try:
        from core.database import get_session_settings
        settings = get_session_settings(str(session_id)) or {}
    except Exception:
        return None
    return settings if settings.get("parent_session") else None


def _git_common_dir(path: Path) -> Optional[Path]:
    """The shared ``.git`` directory of a checkout or linked worktree."""
    from src.agent_worktree.ownership import git_common_dir
    return git_common_dir(path)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _worker_repository(requested: str, workspace: Optional[str], tool_name: str) -> "Path | dict":
    """The checkout a worker's Claude Code run may use, or an error result."""
    if not workspace:
        return {"error": _tool_error(
            "this worker has no workspace, and a worker's Claude Code run goes only to its own checkout. "
            "Report that to the chat that started you; it can start you again with workspace set to the "
            "repository (or a managed worktree of it).", tool_name), "blocked_reason": "worker_no_workspace",
            "exit_code": 1}
    base = Path(workspace).expanduser().resolve()
    raw = (requested or "").strip()
    if not raw or raw.lower() == "auto":
        try:
            return _approved_repository(str(base))
        except ValueError as exc:
            return {"error": _tool_error(
                f"this worker's workspace {str(base)!r} is not a Git checkout Claude Code may use ({exc}). "
                "Pass repository: a checkout inside the workspace, or a managed worktree of it.", tool_name),
                "exit_code": 1}
    try:
        repository = _approved_repository(raw)
    except ValueError as exc:
        return {"error": _tool_error(str(exc), tool_name), "exit_code": 1}
    if _within(repository, base):
        return repository
    worktree_root = _managed_worktree_root()
    common = _git_common_dir(repository)
    if worktree_root is not None and _within(repository, worktree_root) and common is not None:
        # A managed worktree belongs to the workspace when its repository does:
        # the shared .git lives in (or is) the workspace's checkout.
        owner_repo = common.parent if common.name == ".git" else common
        base_common = _git_common_dir(base)
        if _within(owner_repo, base) or (base_common is not None and base_common == common):
            return repository
    return {"error": _tool_error(
        f"repository {raw!r} is outside this worker's workspace {str(base)!r}. A worker's Claude Code run "
        "goes to its own workspace, or to a managed worktree of that repository (manage_agent_worktree); "
        "omit repository to use the workspace.", tool_name),
        "blocked_reason": "worker_repository_outside_workspace", "workspace": str(base), "exit_code": 1}


def _worker_scope(action: str, args: dict, session_id: Optional[str], tool_name: str) -> Optional[dict]:
    """``None`` for a person's chat; for a worker, its scoped arguments or an error."""
    worker = _caller_worker_settings(session_id)
    if worker is None:
        return None
    if action in ("update", "upgrade"):
        return {"error": _tool_error(
            "a worker may not update the Claude Code binary every delegation shares; ask the chat that "
            "started you (or the user) to run action='update'.", tool_name), "exit_code": 1}
    via = str(args.get("via") or "").strip().lower()
    requested = str(args.get("repository") or "").strip()
    if via in ("cloud", "github", "actions") or (requested and _is_cloud_repository(requested)):
        return {"error": _tool_error(
            "a worker runs Claude Code locally, in its own workspace; the cloud runner is for the chat "
            "that started it.", tool_name), "exit_code": 1}
    try:
        from src.tool_execution import get_active_workspace
        workspace = get_active_workspace()
    except Exception:
        workspace = None
    if not workspace:
        workspace = str(worker.get("workspace") or "").strip() or None
    repository = _worker_repository(requested, workspace, tool_name)
    if isinstance(repository, dict):
        return repository
    return {**args, "repository": str(repository), "via": "local"}


def _callback_config() -> dict:
    """Callback settings for the child, without the token itself.

    The token file is the opt-in. A local child runs beside the app, so the
    URL defaults to this instance's own loopback address; set one only when
    the child reaches the app some other way.
    """
    token_file = str(_setting("claude_code_odysseus_token_file",
                              os.environ.get("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", "")) or "").strip()
    url = str(_setting("claude_code_odysseus_url", os.environ.get("CLAUDE_CODE_ODYSSEUS_URL", "")) or "").strip()
    if token_file and not url:
        from src.constants import internal_api_base
        url = internal_api_base()
    return {"url": url, "token_file": token_file, "enabled": bool(url and token_file)}


def _claude_home() -> str:
    return str(_setting("claude_code_home", os.environ.get("CLAUDE_CODE_HOME", os.environ.get("HOME", "/app/data"))))


def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(_claude_home(), ".claude")


_TEST_SCAN_SKIP = frozenset({"node_modules", "target", "build", "dist", "out", "vendor", "venv",
                             "__pycache__", "coverage"})
_REL_DIR_RE = re.compile(rf"^{_REL_DIR}$")


def project_test_rules(repository: Any) -> tuple[list[str], list[str]]:
    """``(allow rules, commands)`` for the checkout's own tests and builds.

    Delegation promises Claude Code may test its work, but the default
    allowlist named no test runner, so a run that was not handed one could
    execute nothing ("every command that runs something was denied
    automatically", 2026-09-30). These are the projects at the checkout's
    root and one folder down: npm scripts (test, typecheck, lint, build),
    Maven, Gradle and pytest, each in the form that works from the root and
    as `cd <folder>` plus the plain runner. ``commands`` are the ones to name
    to Claude.
    """
    root = Path(repository)
    rules: list[str] = []
    commands: list[str] = []

    def allow(rule: str, command: Optional[str] = None) -> None:
        if SAFE_TOOL.fullmatch(rule) and rule not in rules:
            rules.append(rule)
        if command and command not in commands:
            commands.append(command)

    try:
        subdirs = sorted(e.name for e in root.iterdir()
                         if e.is_dir() and not e.name.startswith(".") and e.name not in _TEST_SCAN_SKIP
                         and _REL_DIR_RE.match(e.name))[:40]
    except OSError:
        return [], []
    for rel in ["", *subdirs]:
        path = root / rel if rel else root
        runners: list[str] = []
        try:
            scripts = json.loads((path / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        for name in ("test", "typecheck", "type-check", "lint", "build"):
            if isinstance(scripts, dict) and name in scripts:
                run = "test" if name == "test" else f"run {name}"
                runners.append(f"npm {run}")
                if rel:
                    allow(f"Bash(npm --prefix {rel} {run}:*)", f"npm --prefix {rel} {run}")
        if (path / "pom.xml").is_file():
            wrapper = (path / "mvnw").is_file()
            for goal in ("test", "verify", "compile"):
                if wrapper:
                    runners.append(f"./mvnw {goal}")
                runners.append(f"mvn {goal}")
                if rel:
                    if wrapper:
                        allow(f"Bash(./{rel}/mvnw -f {rel}/pom.xml {goal}:*)")
                    allow(f"Bash(mvn -f {rel}/pom.xml {goal}:*)")
            if rel:
                commands.append(f"cd {rel} && {'./mvnw' if wrapper else 'mvn'} test")
        if (path / "build.gradle").is_file() or (path / "build.gradle.kts").is_file():
            wrapper = (path / "gradlew").is_file()
            for task in ("test", "check", "build"):
                runners.append(f"{'./gradlew' if wrapper else 'gradle'} {task}")
                if rel:
                    allow(f"Bash({f'./{rel}/gradlew' if wrapper else 'gradle'} -p {rel} {task}:*)")
            if rel:
                commands.append(f"cd {rel} && {'./gradlew' if wrapper else 'gradle'} test")
        if any((path / f).is_file() for f in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")):
            runners += ["pytest", "python -m pytest"]
            if rel:
                commands.append(f"pytest {rel}")
        if not runners:
            continue
        if rel:
            allow(f"Bash(cd {rel})")
        for runner in runners:
            allow(f"Bash({runner}:*)", None if rel else runner)
    return rules, commands


def default_tools() -> list[str]:
    """The default allowlist, plus the Agamemnon callback helper when the
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


# ── Child environment ──
#
# The child gets an allowlisted environment, never ``os.environ``. The server
# process holds GitHub tokens (GITHUB_PERSONAL_ACCESS_TOKEN,
# ODYSSEUS_AGENT_GITHUB_TOKEN, GitHub App keys), provider API keys, OAuth
# client secrets, database URLs and SMTP passwords; anything placed in the
# child's environment can be read back with `env` or `echo $X` by the
# delegated session, whatever the calling session was allowed to see. Only
# what the CLI needs to start, reach the network and authenticate is passed.
_CHILD_ENV_EXACT = frozenset({
    # Process basics: find binaries (git, node, the test runners), know who
    # we are, pick a shell for Bash tool calls, and a temp dir. HOME is set
    # separately to the Claude home.
    "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP", "TZ",
    # Terminal / locale, so output encoding and colour handling are sane.
    "TERM", "COLORTERM", "NO_COLOR", "LANG", "LANGUAGE",
    # Outbound network: containers commonly reach Anthropic through a proxy
    # that re-signs TLS, so the proxy and CA bundle variables must survive or
    # the CLI cannot connect at all.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO",
    # Where Claude Code keeps its own login and settings (_claude_config_dir).
    "CLAUDE_CONFIG_DIR",
    # Claude Code's own authentication and endpoint. ANTHROPIC_API_KEY /
    # ANTHROPIC_AUTH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN are the *only* secrets
    # the child receives, and it can read whichever one is set: the CLI
    # cannot authenticate otherwise. A subscription login stored under
    # CLAUDE_CONFIG_DIR needs none of them.
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    # Claude Code behaviour switches (no secrets).
    "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    "DISABLE_COST_WARNINGS", "DISABLE_NON_ESSENTIAL_MODEL_CALLS",
})
# Locale categories (LC_ALL, LC_CTYPE, ...) and Claude Code's own
# CLAUDE_CODE_* switches (CLAUDE_CODE_MAX_OUTPUT_TOKENS, ...).
_CHILD_ENV_PREFIXES = ("LC_", "CLAUDE_CODE_")
# CLAUDE_CODE_* names that are *Agamemnon* configuration, not Claude Code's:
# the child has no use for them, and the token-file path points at the
# callback credential.
_ODYSSEUS_CLAUDE_CODE_VARS = frozenset({
    "CLAUDE_CODE_BINARY", "CLAUDE_CODE_REPOSITORY_ROOTS", "CLAUDE_CODE_DEFAULT_REPOSITORY",
    "CLAUDE_CODE_MAX_CONCURRENT_TASKS", "CLAUDE_CODE_HOME", "CLAUDE_CODE_ODYSSEUS_URL",
    "CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", "CLAUDE_CODE_AUTO_UPDATE",
})
# A prefix-allowed name that still looks like a credential is dropped unless
# it is listed explicitly above (CLAUDE_CODE_OAUTH_TOKEN is). Also used to
# pick the server-side values masked out of child output.
_SECRET_NAME = re.compile(r"(?i)(TOKEN(?!S)|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL|AUTH(?!OR)|COOKIE|SESSION|PRIVATE|DSN|DATABASE_URL)")


def _child_env_allowed(name: str) -> bool:
    if name in _CHILD_ENV_EXACT:
        return True
    if name in _ODYSSEUS_CLAUDE_CODE_VARS:
        return False
    return name.startswith(_CHILD_ENV_PREFIXES) and not _SECRET_NAME.search(name)


def _base_child_environment() -> dict[str, str]:
    """The allowlisted environment every ``claude`` process gets (probes too).

    Deliberately absent: every GitHub credential (GITHUB_*, GH_TOKEN,
    ODYSSEUS_AGENT_GITHUB_TOKEN, GIT_ASKPASS, SSH_AUTH_SOCK, GIT_CONFIG_*
    credential helpers). Claude commits in the local checkout only;
    publishing goes through Agamemnon' own human-gated path
    (manage_agent_worktree request_publish/publish), so the child never
    authenticates to GitHub. :data:`DISALLOWED_TOOLS` denies the matching
    commands.
    """
    env = {name: value for name, value in os.environ.items() if _child_env_allowed(name)}
    env["HOME"] = _claude_home()
    # No TTY in a headless run: fail fast instead of hanging on a git
    # credential prompt until the timeout.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _claude_environment() -> dict[str, str]:
    """Build the child environment without persisting or exposing secrets.

    Starts from :func:`_base_child_environment` — an allowlist, never
    ``os.environ``. A deployment may give delegated Claude sessions scoped
    access back to Agamemnon with a private token file. Keeping the token out
    of argv avoids process-list exposure; the child receives it only in its
    environment (it is scoped to the /api/codex/* callback, which is the
    point of handing it over).
    """
    env = _base_child_environment()
    callback = _callback_config()
    if callback["url"]:
        env["ODYSSEUS_URL"] = callback["url"]
    if callback["token_file"]:
        path = Path(callback["token_file"]).expanduser()
        try:
            info = path.stat()
            if not path.is_file() or (os.name != "nt" and info.st_mode & 0o077):
                raise ValueError(
                    "Claude Code Agamemnon token file must be a private regular "
                    "file (mode 0600 on POSIX; restricted ACL on Windows)"
                )
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Claude Code Agamemnon token file is unavailable: {exc}") from exc
        if not token:
            raise ValueError("Claude Code Agamemnon token file is empty")
        env["ODYSSEUS_API_TOKEN"] = token
    return env


# ── Output redaction ──

def _secret_values() -> list[str]:
    """Server-side secret values, longest first, for exact-match masking of
    anything the child prints back (a checkout's .env, a test that echoes a
    key, Claude's own API key in an error)."""
    values = {value for name, value in os.environ.items()
              if value and len(value) >= 8 and _SECRET_NAME.search(name)}
    token_file = _callback_config()["token_file"]
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            token = ""
        if len(token) >= 8:
            values.add(token)
    return sorted(values, key=len, reverse=True)


def _redact(value: Any, secrets: list[str]) -> Any:
    """Scrub child-produced text (recursing into lists/dicts) before it reaches
    the model or the activity feed: exact server secret values first, then
    the credential shapes :func:`src.agent_logs.redact_text` knows."""
    if isinstance(value, str):
        if not value:
            return value
        for secret in secrets:
            value = value.replace(secret, "***")
        from src.agent_logs import redact_text
        return redact_text(value)
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


# Fields of a run result that carry child-produced text.
_CHILD_TEXT_FIELDS = ("output", "stderr", "error", "result", "transcript", "permission_denials", "claude",
                      "commits")


def _redact_result(result: dict, secrets: Optional[list[str]] = None) -> dict:
    secrets = _secret_values() if secrets is None else secrets
    for key in _CHILD_TEXT_FIELDS:
        if key in result:
            result[key] = _redact(result[key], secrets)
    return result


def _canonical_allowed_tool(value: str) -> str:
    """Canonicalize tool-name casing without broadening the safe grammar.

    Only the fixed built-in names (and the ``Bash`` wrapper spelling) are
    case-normalized.  The command inside ``Bash(...)`` remains byte-for-byte
    unchanged and is still checked by ``SAFE_TOOL``; e.g. ``git PUSH`` remains
    rejected.
    """
    builtins = {name.lower(): name for name in ("Read", "Glob", "Grep", "Edit", "Write")}
    if value.lower() in builtins:
        return builtins[value.lower()]
    if len(value) >= 5 and value[:4].lower() == "bash" and value[4] == "(":
        return "Bash" + value[4:]
    return value


def _cloud_repositories() -> list[str]:
    try:
        from src import claude_cloud
        return claude_cloud.repositories()
    except Exception:
        return []


def _cloud_configured() -> bool:
    try:
        from src import claude_cloud
        return claude_cloud.configured()
    except Exception:
        return False


def _is_cloud_repository(value: str) -> bool:
    """An allowlisted ``owner/repo`` (or any slug, with ``*``). A local
    checkout path never matches: slugs have exactly one slash and no root."""
    try:
        from src import claude_cloud
        return claude_cloud.is_cloud_repository(value)
    except Exception:
        return False


def _workspace_cloud_repository() -> str:
    """The github.com ``origin`` of this chat's workspace, when the cloud
    runner accepts it; "" otherwise. Lets "run it in the cloud" follow the
    repository the chat is already working in."""
    try:
        from src.tool_execution import get_active_workspace
        workspace = get_active_workspace()
    except Exception:
        workspace = None
    if not workspace:
        return ""
    try:
        from src.agent_worktree.service import _origin_slug
        slug = _origin_slug(str(workspace))
    except Exception:
        return ""
    return slug if slug and _is_cloud_repository(slug) else ""


def _wants_cloud(args: dict) -> bool:
    """Whether this delegation goes to the GitHub Actions cloud runner.

    ``via: "cloud"`` (or ``"github"``) asks for it; ``via: "local"`` never.
    Otherwise an allowlisted ``owner/repo`` repository means cloud, and the
    ``claude_code_backend`` setting ("local" default, or "cloud") decides the
    rest.
    """
    via = str(args.get("via") or "").strip().lower()
    if via in ("cloud", "github", "actions"):
        return True
    if via == "local":
        return False
    requested = str(args.get("repository") or "").strip()
    if requested and _is_cloud_repository(requested):
        return True
    return _cloud_configured() and str(_setting("claude_code_backend", "local") or "local").lower() == "cloud"


def backend_warning() -> Optional[str]:
    """Why the ``claude_code_backend`` setting is not being followed, or None.

    "cloud" with no cloud repository configured falls through to the local
    runner in ``_wants_cloud`` without a word. On 2026-09-28 that looked like
    the setting did nothing, so the status report now says it out loud.
    """
    if str(_setting("claude_code_backend", "local") or "local").strip().lower() != "cloud":
        return None
    if _cloud_configured():
        return None
    return ("Claude Code is set to run on the cloud runner, but the cloud runner has no repository "
            "configured, so delegations run locally on this machine. Add the repositories Claude may "
            "work on (or * with a hub repository) under Settings › Claude Code › Cloud runner, "
            "or set it back to run on this machine.")


def _with_steer_note(report: dict, steered: list) -> dict:
    """Say why a wait came back early, when a user message ended it."""
    if steered and isinstance(report, dict):
        from src import agent_control
        report = {**report, "steer_note": agent_control.STEER_WAIT_NOTE}
    return report


async def _cloud_action(action: str, args: dict, *, owner, session_id, tool_name: str) -> Optional[dict]:
    """Handle the action on the cloud runner, or return None for the local one."""
    task_id = str(args.get("task_id") or "").strip()
    steered: list = []
    if action in ("poll", "get", "cancel"):
        from src import claude_cloud
        if not claude_cloud.is_cloud_task(task_id):
            return None
        try:
            if action == "cancel":
                record = await claude_cloud.cancel(task_id, owner=owner)
            else:
                try:
                    wait = max(0, min(MAX_POLL_WAIT_S, int(args.get("wait_seconds") or 0)))
                except (TypeError, ValueError):
                    wait = 0
                if claude_cloud.get(task_id, owner) is None:
                    record = None
                else:
                    record = (await claude_cloud.wait(task_id, wait, session_id=session_id, steered=steered)
                              if wait else await claude_cloud.refresh(task_id))
        except Exception as exc:
            return {"error": _tool_error(str(exc), tool_name), "exit_code": 1}
        if record is None:
            return {"error": _tool_error(f"task {task_id} not found", tool_name), "exit_code": 1}
        return _with_steer_note(claude_cloud.report(record), steered)
    if action not in ("run", "start") or not _wants_cloud(args):
        return None
    from src import claude_cloud
    repos = claude_cloud.repositories()
    repository = str(args.get("repository") or "").strip()
    if not repository or repository.lower() == "auto":
        if len(repos) == 1 and not claude_cloud.allows_any_repository():
            repository = repos[0]
        else:
            repository = _workspace_cloud_repository()
        if not repository:
            allowed = ", ".join(repos + (["* (any owner/repo)"] if claude_cloud.allows_any_repository() else []))
            return {"error": _tool_error("repository (owner/repo) is required for the cloud runner; one of: "
                                         + (allowed or "none configured"), tool_name), "exit_code": 1}
    label = str(args.get("label") or "").strip()[:120] or None
    try:
        record = await claude_cloud.dispatch(repository, str(args.get("prompt") or ""),
                                             base_branch=str(args.get("base_branch") or ""),
                                             owner=owner, session_id=session_id, label=label)
        if action == "run":
            try:
                timeout = max(30, min(1800, int(args.get("timeout_seconds", 900))))
            except (TypeError, ValueError):
                timeout = 900
            record = await claude_cloud.wait(record["task_id"], timeout, session_id=session_id, steered=steered)
    except Exception as exc:
        return {"error": _tool_error(str(exc), tool_name), "exit_code": 1}
    return _with_steer_note(claude_cloud.report(record), steered)


def _parse_args(args: dict, tool_name: str = _DEFAULT_TOOL_NAME) -> dict:
    """Validate a delegation request. Returns either
    {"repository": Path, "prompt": str, "timeout": int, "tools": list[str], "model": str|None}
    or {"error": str} — never both, and never raises."""
    prefix = _tool_name(tool_name)
    if not isinstance(args, dict):
        return {"error": _tool_error("JSON object required", prefix), "exit_code": 1}
    requested = str(args.get("repository") or "").strip()
    if requested and requested.lower() != "auto":
        try:
            repository = _approved_repository(requested)
        except (ValueError, OSError) as exc:
            return {"error": _tool_error(str(exc), prefix), "exit_code": 1}
    else:
        repository, why = default_repository()
        if repository is None:
            return {"error": _tool_error(f"{why}. {_describe_candidates()}", prefix), "exit_code": 1}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt or len(prompt) > 20000:
        return {"error": _tool_error("prompt is required and must be <= 20000 characters", prefix),
                "exit_code": 1}
    try:
        timeout = max(30, min(1800, int(args.get("timeout_seconds", 900))))
    except (TypeError, ValueError):
        timeout = 900
    requested_tools = args.get("allowed_tools")
    tools = requested_tools if requested_tools else default_tools()
    if not isinstance(tools, list) or not all(isinstance(item, str) and item for item in tools):
        return {"error": _tool_error("allowed_tools must be a list of strings", prefix), "exit_code": 1}
    # Normalize only the names whose casing is cosmetic.  Safety matching is
    # intentionally still exact, so malformed or unsafe Bash expressions do
    # not become valid through a broad IGNORECASE regex.
    tools = [_canonical_allowed_tool(item) for item in tools]
    rejected = [item for item in tools if not SAFE_TOOL.fullmatch(item)]
    accepted_hint = (
        f"Accepted: Read, Glob, Grep, Edit, Write, Bash(git <{_SAFE_GIT_SUBCOMMANDS.replace('|', '/')}>:*), "
        "Bash(<pytest/npm test/npm run typecheck|lint|build/./gradlew test|build/./mvnw test|verify>:*). "
        "Claude never pushes: publish its commits yourself afterwards."
    )
    dropped: list[str] = []
    dropped_note = ""
    if rejected and len(rejected) == len(tools):
        # Falling back to the known-safe defaults is strictly safer than
        # refusing the whole delegation: no caller-supplied permission survives
        # and the model gets a corrective note instead of retrying forever.
        dropped = rejected[:10]
        tools = default_tools()
        dropped_note = (f"Ignored unsafe allowed_tools {dropped}; none were safe, so ran with the defaults. "
                        f"{accepted_hint}")
    elif rejected:
        # A mixed list runs with its safe entries. Refusing the whole job cost
        # the 2026-09-13 run three rounds re-sending one list with `git push`
        # and a malformed entry in it; dropping can only narrow Claude's rights.
        dropped = rejected[:10]
        tools = [item for item in tools if SAFE_TOOL.fullmatch(item)]
    requested_model = str(args.get("model") or "").strip()
    raw_model = requested_model or str(_setting("claude_code_model", "") or "").strip()
    # Checked here, before any process starts: the CLI only reports a bad
    # model after spawning, and only runs Claude models at all.
    model, model_error = normalize_claude_model(raw_model)
    if model_error:
        source = "" if requested_model else " (from the claude_code_model setting)"
        return {"error": _tool_error(f"{model_error}{source}", prefix), "error_kind": "invalid_model",
                "exit_code": 1}
    if _flag_setting("claude_code_test_commands", True):
        # The checkout's own tests and builds, granted on every delegation
        # (project_test_rules), and named in the prompt so Claude runs them
        # in a form the allowlist accepts instead of guessing and being denied.
        test_rules, test_commands = project_test_rules(repository)
        tools = tools + [rule for rule in test_rules if rule not in tools]
        if test_commands:
            prompt = (f"{prompt}\n\n[Agamemnon] Test and build commands you may run in this checkout (the "
                      f"shell starts at its root): {', '.join(f'`{c}`' for c in test_commands[:12])}. "
                      "Other commands that run code are denied; run the relevant checks before you finish "
                      "and say which, if any, you could not run.")
    parsed = {"repository": repository, "prompt": prompt, "timeout": timeout, "tools": tools, "model": model}
    if raw_model and model != raw_model:
        if model:
            parsed["model_note"] = f"model {raw_model!r} was passed to Claude Code as {model!r}"
        else:
            parsed["model_note"] = f"model {raw_model!r} means the account default; no --model was passed"
        logger.info("claude_code: model %r normalised to %r", raw_model, model)
    if dropped:
        parsed["dropped_tools"] = dropped
        parsed["dropped_note"] = dropped_note or f"Ignored unsafe allowed_tools {dropped}; ran with the rest. {accepted_hint}"
    return parsed


def _with_dropped(result: dict, parsed: dict) -> dict:
    if parsed.get("dropped_tools"):
        # Keep the parser's concise names in the tool result as well as the
        # older descriptive aliases used by existing callers.
        result["dropped_tools"] = parsed["dropped_tools"]
        result["dropped_note"] = parsed["dropped_note"]
        result["dropped_allowed_tools"] = parsed["dropped_tools"]
        result["allowed_tools_note"] = parsed["dropped_note"]
    if parsed.get("model_note"):
        result["model_note"] = parsed["model_note"]
    return result


def normalize_claude_model(value: Any) -> tuple[Optional[str], Optional[str]]:
    """``(model, error)`` for the local Claude Code CLI's ``--model``.

    Aliases (``opus``, ``sonnet``, ``haiku``, ``fable``, ``best``,
    ``opusplan``) and ``claude-*`` IDs pass, each with an optional ``[1m]``
    suffix; human spellings are rewritten (``opus-5.5``, ``Opus 5.5``,
    ``claude opus 5.5`` -> ``claude-opus-5-5``; ``sonnet-5`` ->
    ``claude-sonnet-5``); ``default`` means the account default, so no model
    (``None``) is passed. Anything else — ``gpt-*``, ``o3``, ``gemini-*`` —
    is an error naming the valid choices. Only for the Claude CLI path:
    delegate_to_agent's other providers never reach this.
    """
    raw = str(value or "").strip()
    if not raw:
        return None, None
    text = re.sub(r"\s+", " ", raw.lower())
    suffix = ""
    if text.endswith("[1m]"):
        suffix, text = "[1m]", text[:-4].strip()
    choices = (f"Use an alias ({', '.join(_CLAUDE_MODEL_ALIASES)}) or a Claude model ID such as "
               f"{', '.join(_CLAUDE_MODEL_EXAMPLES)} (append [1m] for the 1M-token context), or omit model "
               "to use the configured default.")
    if text in _CLAUDE_MODEL_ALIASES:
        if text == "default":
            return (None, None) if not suffix else (None, f"model {raw!r} is not valid: 'default' takes no [1m]. {choices}")
        return text + suffix, None
    human = _HUMAN_CLAUDE_MODEL.fullmatch(text)
    if human:
        family, version = human.group(1), human.group(2)
        if not version:
            return family + suffix, None
        version = re.sub(r"[\s._\-]+", "-", version)
        return _CLAUDE_MODEL_IDS.get((family, version), f"claude-{family}-{version}") + suffix, None
    if _CLAUDE_MODEL_ID.fullmatch(text) and len(text) <= 100:
        return text + suffix, None
    if _FOREIGN_MODEL.match(text):
        return None, (f"model {raw!r} is not a Claude model: it belongs to another provider, and the local "
                      f"Claude Code CLI only runs Claude models. {choices}")
    return None, f"model {raw!r} is not a Claude Code model name or alias. {choices}"


# ── Binary probing ──

async def _capture(binary: Path, *args: str, timeout: float = 30, env: Optional[dict] = None) -> tuple[int, str]:
    # Probes (--version, --help) get the same allowlisted environment as a
    # run, never the server's full one.
    proc = await asyncio.create_subprocess_exec(
        str(binary), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env if env is not None else _base_child_environment(), cwd=str(binary.parent),
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
        return {"checked": True, "logged_in": None,
                "error": _redact(out.strip()[-400:], _secret_values()) or f"exit {code}"}
    return {
        "checked": True,
        "logged_in": bool(parsed.get("loggedIn")),
        "auth_method": parsed.get("authMethod"),
        "api_provider": parsed.get("apiProvider"),
    }


# ── Updating the CLI (action=update) ──
#
# 2026-09-26: a run failed with "Claude Code 2.1.267 does not support this
# model; version 2.1.280 or newer is required", the agent then tried
# `npm install -g @anthropic-ai/claude-code@latest` from bash, the bash guard
# refused it, and no supported way to upgrade was left. The binary lives in
# the persistent data directory (DEFAULT_BINARY), not in the image, so the
# fix is to run *that* install's own updater with the child's allowlisted
# environment:
#
# * an npm install — ``npm install -g --prefix /app/data/claude-code
#   @anthropic-ai/claude-code`` leaves <prefix>/bin/claude as a symlink into
#   <prefix>/lib/node_modules/@anthropic-ai/claude-code — is updated with npm
#   against that same prefix. `claude update` on an npm install targets npm's
#   *global* prefix (/usr/local in the image: neither persistent nor writable
#   by the app user), which is also why a bare `npm install -g` from bash
#   would not have fixed anything;
# * anything else is a native install, updated with the binary's own
#   ``claude update`` (``claude install <version>`` for an explicit target),
#   which downloads into ~/.local/share/claude/versions/ and repoints its
#   launcher. A configured path that is itself a symlink pinned to one file
#   in that versions directory is repointed to the newest one afterwards.
UPDATE_TIMEOUT_S = 600
_NPM_PACKAGE = "@anthropic-ai/claude-code"
_UPDATE_TARGET = re.compile(r"^(?:latest|stable|\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$")
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# "Claude Code 2.1.267 does not support this model; version 2.1.280 or newer
# is required. Run 'claude update', ..." — returned by the API (a 400) when the
# requested model needs a newer client.
_OUTDATED = re.compile(
    r"Claude Code\s+v?(?P<installed>\d+\.\d+\.\d+)\S*\s+does not support this model[;,.]?\s*"
    r"(?:version\s+)?v?(?P<required>\d+\.\d+\.\d+)\S*\s+or newer is required",
    re.IGNORECASE,
)
OUTDATED_ERROR_KIND = "claude_code_outdated"
_UPDATE_STATE: dict = {"running": False, "last": None}
# Claude processes in flight or waiting for the gate (chat runs and task
# runner jobs both pass through _run_claude). An update refuses while any
# exist; runs that start during an update wait for it (_wait_for_update).
_ACTIVE_RUNS = 0
# The newest "version X or newer is required" a run reported, so status can
# say why the binary needs an update even after the failing turn is gone.
_VERSION_REQUIREMENT: dict = {}


def _version_tuple(text: Any) -> Optional[tuple[int, int, int]]:
    match = _SEMVER.search(str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _auto_update_enabled() -> bool:
    # Settings › Claude Code only. A CLAUDE_CODE_AUTO_UPDATE fallback
    # used to sit here, but `claude_code_auto_update` always has a default
    # (False) in DEFAULT_SETTINGS, so the environment variable was never
    # consulted; it was removed on 2026-09-28 rather than documented as a
    # switch that does nothing.
    return _flag_setting("claude_code_auto_update", False)


def update_in_progress() -> bool:
    return bool(_UPDATE_STATE["running"])


def detect_outdated(result: Any) -> Optional[dict]:
    """``{"installed", "required"}`` when a run failed because the CLI is too
    old for the requested model, else None."""
    if not isinstance(result, dict):
        return None
    for key in ("error", "result", "stderr", "output"):
        value = result.get(key)
        if isinstance(value, str) and value:
            match = _OUTDATED.search(value)
            if match:
                return {"installed": match["installed"], "required": match["required"]}
    return None


def _annotate_outdated(result: dict) -> dict:
    """Turn the CLI's version refusal into a structured, actionable error.
    Only a failed run qualifies: a successful one may merely quote the text."""
    failed = bool(result.get("error") or result.get("is_error")) or result.get("exit_code") not in (0, None)
    found = detect_outdated(result) if failed else None
    if not found:
        return result
    binary = str(binary_path())
    original = str(result.get("error") or result.get("result") or "")
    _VERSION_REQUIREMENT.clear()
    _VERSION_REQUIREMENT.update({
        "installed": found["installed"], "required": found["required"], "model": result.get("model"),
        "binary": binary, "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    result.update({
        "error_kind": OUTDATED_ERROR_KIND,
        "installed_version": found["installed"],
        "required_version": found["required"],
        "claude_error": _clip(original, 600),
        "error": (
            f"Claude Code {found['installed']} at {binary} is too old for the requested model: version "
            f"{found['required']} or newer is required. Call delegate_to_claude_code with action=update "
            "(admin) to upgrade the configured binary, then retry this task. Do not install Claude Code "
            "from bash: that installs a second copy and leaves the configured binary as it is."
        ),
        "fix": {"tool": "delegate_to_claude_code", "action": "update"},
        "exit_code": result.get("exit_code") or 1,
    })
    return result


def version_requirement() -> dict:
    """The last recorded minimum version, from memory or the persisted task
    records (so it survives a restart)."""
    if _VERSION_REQUIREMENT:
        return dict(_VERSION_REQUIREMENT)
    try:
        records = list(get_task_runner().tasks.values())
    except Exception:
        return {}
    latest = None
    for record in records:
        if record.get("error_kind") == OUTDATED_ERROR_KIND and record.get("required_version"):
            if latest is None or (record.get("finished_at") or "") > (latest.get("finished_at") or ""):
                latest = record
    if latest is None:
        return {}
    return {"installed": latest.get("installed_version"), "required": latest["required_version"],
            "model": latest.get("model"), "seen_at": latest.get("finished_at"), "task_id": latest.get("task_id")}


def _install_method(binary: Path) -> dict:
    """How the configured binary was installed, so the matching updater runs."""
    try:
        resolved = binary.resolve()
    except (OSError, RuntimeError):
        resolved = binary
    parts = resolved.parts
    for i in range(len(parts) - 2):
        if parts[i] == "node_modules" and parts[i + 1] == "@anthropic-ai" and parts[i + 2] == "claude-code":
            owner = Path(*parts[:i]) if i else Path(".")
            if owner.name == "lib":  # <prefix>/lib/node_modules: an `npm -g --prefix` layout
                return {"method": "npm", "prefix": str(owner.parent), "global": True, "resolved": str(resolved)}
            return {"method": "npm", "prefix": str(owner), "global": False, "resolved": str(resolved)}
    # A copied (not symlinked) launcher next to an npm prefix.
    prefix = binary.parent.parent
    if binary.parent.name == "bin" and (prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code").is_dir():
        return {"method": "npm", "prefix": str(prefix), "global": True, "resolved": str(resolved)}
    versions_dir = str(resolved.parent) if resolved.parent.name == "versions" else None
    # The native updater downloads into $HOME/.local/share/claude/versions/ and
    # repoints *its* launcher, $HOME/.local/bin/claude — not whatever path
    # claude_code_binary names. A plain copy elsewhere (2026-09-27:
    # /app/data/claude-code/bin/claude) never moves; update_binary rebinds.
    launcher = _native_launcher()
    return {"method": "native", "resolved": str(resolved), "versions_dir": versions_dir,
            "launcher": str(launcher), "is_launcher": _same_path(binary, launcher)}


def _native_launcher(home: Optional[str] = None) -> Path:
    """The launcher the native installer maintains for the child's HOME."""
    launcher = Path(home or _claude_home()).expanduser() / ".local" / "bin" / "claude"
    if os.name == "nt" and not launcher.exists() and launcher.with_suffix(".exe").exists():
        return launcher.with_suffix(".exe")
    return launcher


def _same_path(a: Path, b: Path) -> bool:
    """Whether two paths name the same file *path* (not the same target: a
    configured symlink into versions/ is not the launcher even when both
    point at one version)."""
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _save_binary_setting(path: Path) -> Optional[str]:
    """Point ``claude_code_binary`` at ``path`` the way manage_settings does.
    Returns why it could not, or None."""
    try:
        from src.settings import load_settings, save_settings
        settings = dict(load_settings())
        settings["claude_code_binary"] = str(path)
        save_settings(settings)
    except Exception as exc:  # a read-only data dir, a bad settings file
        return f"{type(exc).__name__}: {exc}"
    if not _same_path(binary_path(), path):
        # CLAUDE_CODE_BINARY cannot shadow a saved setting, but a caller that
        # patched _setting (or a racing writer) can; report it rather than
        # claiming the switch happened.
        return f"the setting still resolves to {binary_path()}"
    return None


async def _other_installs(binary: Path, env: dict, extra: tuple = ()) -> list[dict]:
    """Claude Code installs Agamemnon knows about that ``binary`` is not: the
    image default, the native launcher, and a path just rebound away from.
    Reported, never deleted — the admin decides."""
    found: list[dict] = []
    seen = {os.path.normcase(os.path.abspath(str(binary)))}
    for candidate in (*extra, Path(DEFAULT_BINARY).expanduser(), _native_launcher(env.get("HOME"))):
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        found.append({"path": str(candidate), "version": await _probe_version(candidate, env)})
    return found


def _update_environment() -> dict[str, str]:
    """The run's allowlisted environment (HOME, CLAUDE_CONFIG_DIR, PATH,
    proxy/CA), plus npm's own non-secret configuration (registry, cache)."""
    env = _base_child_environment()
    for name, value in os.environ.items():
        if name.lower().startswith("npm_config_") and not _SECRET_NAME.search(name):
            env[name] = value
    return env


def _update_command(binary: Path, method: dict, target: Optional[str], env: dict) -> tuple[list[str], str, str]:
    """``(argv, cwd, note)`` for the updater that matches the install."""
    note = ""
    if method["method"] == "npm":
        npm = shutil.which("npm", path=env.get("PATH"))
        if npm:
            argv = [npm, "install", *(["-g"] if method.get("global") else []), "--prefix", method["prefix"],
                    "--no-fund", "--no-audit", f"{_NPM_PACKAGE}@{target or 'latest'}"]
            return argv, method["prefix"], note
        note = "npm is not on PATH, so the binary's own updater was used instead."
    if target:
        return [str(binary), "install", target], str(binary.parent), note
    return [str(binary), "update"], str(binary.parent), note


async def _exec_capture(argv: list[str], *, cwd: str, env: dict, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env, cwd=cwd,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {int(timeout)}s"
    return proc.returncode or 0, (out or b"").decode("utf-8", errors="replace")


async def _probe_version(binary: Path, env: dict) -> str:
    try:
        _, out = await _capture(binary, "--version", timeout=45, env=env)
    except OSError:
        return ""
    out = _ANSI.sub("", out or "").strip()
    return out.splitlines()[0].strip() if out else ""


def _relink_native(binary: Path, method: dict, before: Optional[tuple]) -> Optional[str]:
    """Repoint a configured symlink that is pinned to one file in the native
    versions directory at the newest version there. The native updater only
    moves its own launcher (~/.local/bin/claude), so without this a
    ``claude_code_binary`` pointing straight into versions/ never moves."""
    versions_dir = method.get("versions_dir")
    if not versions_dir or not binary.is_symlink():
        return None
    candidates = []
    for entry in Path(versions_dir).iterdir():
        if re.fullmatch(r"\d+\.\d+\.\d+", entry.name) and entry.is_file() and os.access(entry, os.X_OK):
            candidates.append((_version_tuple(entry.name), entry))
    if not candidates:
        return None
    newest_version, newest = max(candidates, key=lambda item: item[0])
    if before and newest_version <= before:
        return None
    temp = binary.with_name(f".{binary.name}.odysseus-relink")
    if temp.is_symlink() or temp.exists():
        temp.unlink()
    os.symlink(str(newest), str(temp))
    os.replace(str(temp), str(binary))
    return str(newest)


def _active_task_ids() -> list[str]:
    try:
        return [task_id for task_id, record in get_task_runner().tasks.items()
                if record.get("status") in _ACTIVE_STATUSES]
    except Exception:
        return []


async def _wait_for_update(limit: float = UPDATE_TIMEOUT_S + 120) -> None:
    """Hold a new run back while the binary is being replaced."""
    deadline = time.monotonic() + limit
    while _UPDATE_STATE["running"] and time.monotonic() < deadline:
        await asyncio.sleep(0.25)


async def update_binary(target: Optional[str] = None, *, timeout: int = UPDATE_TIMEOUT_S) -> dict:
    """Update the configured Claude Code binary with its own updater.

    Refuses while any Claude run is in flight (the binary would be replaced
    under it); runs that start meanwhile wait until the update is done.
    Returns the version before and after, the command, and its redacted
    output. ``target`` is ``latest``/``stable``/an exact version, or None
    for the install's default channel.
    """
    target = str(target or "").strip() or None
    if target and target.lower() in ("latest", "stable"):
        target = target.lower()
    if target and not _UPDATE_TARGET.fullmatch(target):
        return {"error": _tool_error("version must be 'latest', 'stable', or an exact version such as 2.1.280"),
                "exit_code": 1}
    binary = binary_path()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return {"error": _tool_error(
            f"binary unavailable at {binary}, so there is nothing to update. Install Claude Code there first "
            "(see clients/claude/README.md) or set Settings › Claude Code › Advanced › Binary."), "exit_code": 1}
    if _UPDATE_STATE["running"]:
        return {"error": _tool_error("an update is already running; call action=status in a minute"),
                "update_in_progress": True, "exit_code": 1}
    if _ACTIVE_RUNS:
        ids = _active_task_ids()
        listing = f" (task ids: {', '.join(ids)})" if ids else ""
        return {"error": _tool_error(
            f"{_ACTIVE_RUNS} Claude Code run(s) are in progress{listing}; updating now would replace the binary "
            "under them. Wait for them (action=poll with wait_seconds) or cancel them, then call action=update "
            "again."), "active_task_ids": ids, "active_runs": _ACTIVE_RUNS, "exit_code": 1}
    # No await between the check above and this flag, so no run can slip in.
    _UPDATE_STATE["running"] = True
    hints: list[str] = []
    relinked = None
    configured = binary
    rebound: Optional[dict] = None
    launcher: Optional[Path] = None  # a native launcher at another path than the configured binary
    launcher_version = ""
    others: list[dict] = []
    requirement = version_requirement()
    required_v = _version_tuple(requirement.get("required"))
    try:
        env = _update_environment()
        method = _install_method(binary)
        before = await _probe_version(binary, env)
        argv, cwd, note = _update_command(binary, method, target, env)
        if note:
            hints.append(note)
        try:
            code, out = await _exec_capture(argv, cwd=cwd, env=env, timeout=timeout)
        except OSError as exc:
            code, out = 127, f"could not start {argv[0]}: {exc}"
        _BINARY_INFO.clear()
        after = await _probe_version(binary, env)
        logger.info("claude_code update: method=%s command=%s %s before=%r after=%r exit=%s binary=%s",
                    method["method"], os.path.basename(argv[0]), argv[1] if len(argv) > 1 else "",
                    before, after, code, binary)
        if code == 0 and method["method"] == "native" and _version_tuple(after) == _version_tuple(before):
            try:
                relinked = _relink_native(binary, method, _version_tuple(before))
            except OSError as exc:
                hints.append(f"could not repoint {binary} at the newest downloaded version: {exc}")
            if relinked:
                _BINARY_INFO.clear()
                after = await _probe_version(binary, env)
                logger.info("claude_code update: relinked %s -> %s (now %r)", binary, relinked, after)
        # 2026-09-27: the configured binary was a plain copy at
        # /app/data/claude-code/bin/claude; `claude update` installed 2.1.283
        # and repointed $HOME/.local/bin/claude, the copy stayed at 2.1.267,
        # and five retries reported the same failure. When the updater's own
        # launcher is newer than the configured path (and new enough for the
        # model), point claude_code_binary at it.
        candidate = Path(method["launcher"]) if method.get("launcher") and not method.get("is_launcher") else None
        if code == 0 and candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            launcher = candidate
            launcher_version = await _probe_version(launcher, env)
            launcher_v, current_v = _version_tuple(launcher_version), _version_tuple(after)
            if (launcher_v and current_v == _version_tuple(before) and (current_v is None or launcher_v > current_v)
                    and (not required_v or launcher_v >= required_v)):
                problem = _save_binary_setting(launcher)
                if problem is None:
                    logger.info("claude_code update: rebound claude_code_binary %s (%r) -> %s (%r)",
                                binary, after, launcher, launcher_version)
                    rebound = {"from": str(binary), "to": str(launcher)}
                    binary, after = launcher, launcher_version
                    _BINARY_INFO.clear()
                else:
                    logger.warning("claude_code update: could not rebind claude_code_binary to %s: %s",
                                   launcher, problem)
                    hints.append(
                        f"The native updater installed {launcher_version} at {launcher}, but the configured binary "
                        f"{binary} is a separate copy it never updates, and switching the setting failed "
                        f"({problem}). Set Settings › Claude Code › Advanced › Binary (manage_settings key "
                        f"claude_code_binary) to {launcher}; no further update is needed.")
        if code == 0:
            others = await _other_installs(binary, env, (Path(rebound["from"]),) if rebound else ())
    finally:
        _UPDATE_STATE["running"] = False
    before_v, after_v = _version_tuple(before), _version_tuple(after)
    satisfies = None if not required_v else bool(after_v and after_v >= required_v)
    text = _ANSI.sub("", out or "").strip()
    result: dict = {
        "binary": str(binary),
        "method": method["method"],
        "command": argv,
        "target": target or ("latest" if argv[0] != str(configured) else "the install's release channel"),
        "version_before": before,
        "version_after": after,
        "updated": bool(before_v and after_v and after_v > before_v),
        "output": _redact(text[-4000:], _secret_values()),
        "exit_code": code,
    }
    if method["method"] == "npm":
        result["npm_prefix"] = method["prefix"]
    if relinked:
        result["relinked_to"] = relinked
    if rebound:
        result["rebound_from"] = rebound["from"]
        result["rebound_to"] = rebound["to"]
    elif launcher is not None:
        result["native_launcher"] = {"path": str(launcher), "version": launcher_version}
    if others:
        current_v = _version_tuple(after)
        for other in others:
            other_v = _version_tuple(other["version"])
            other["stale"] = bool(current_v and (other_v is None or other_v <= current_v))
        result["other_installs"] = others
        stale = [f"{o['path']} ({o['version'] or 'no version'})" for o in others if o["stale"]]
        if stale:
            hints.append(f"Left in place: {', '.join(stale)} — an older Claude Code install Agamemnon no longer "
                         f"runs (it runs {binary}). Nothing was deleted; remove it by hand once nothing else "
                         "points at it.")
    if required_v:
        result["required_version"] = requirement.get("required")
        result["satisfies_requirement"] = satisfies
    if code != 0:
        tail = " | ".join(line for line in result["output"].splitlines()[-4:] if line.strip())
        result["error"] = _tool_error(f"update command exited {code}: {tail or 'no output'}")
        if "EACCES" in text or "permission denied" in text.lower():
            hints.append(f"The install at {method.get('prefix') or method['resolved']} is not writable by the "
                         "Agamemnon process; chown it to the container's PUID:PGID and retry.")
        if "DISABLE_UPDATES" in text:
            hints.append("Updates are disabled by DISABLE_UPDATES in Claude Code's settings; remove it to update.")
    elif required_v and not satisfies:
        result["exit_code"] = 1
        required = requirement.get("required")
        launcher_v = _version_tuple(launcher_version)
        if launcher is not None and launcher_v and launcher_v >= required_v:
            # The rebind was attempted and failed (see hints): the fix is the
            # setting, not another update.
            result["error"] = _tool_error(
                f"{binary} still reports {after or 'no version'}, older than the {required} the model needs, "
                f"but the native updater's launcher {launcher} already reports {launcher_version}. Set "
                f"claude_code_binary to {launcher} (manage_settings) and retry the task; do not update again.")
        elif launcher is not None:
            # The configured path is not what the native updater maintains.
            # Suggesting the exact minimum here (as this message used to) got
            # it run as `claude install <minimum>`, which moves the shared
            # launcher *back* to that version.
            result["error"] = _tool_error(
                f"the updater finished but {binary} reports {after or 'no version'}, still older than the "
                f"{required} the model needs. {binary} is not the native updater's launcher ({launcher}, now "
                f"{launcher_version or 'no version'}), so updates land there. Retry with version=\"latest\" "
                "(not an exact version, which would move that launcher back); the configured binary is switched "
                "to the launcher once it is new enough.")
        else:
            result["error"] = _tool_error(
                f"the updater finished but the binary reports {after or 'no version'}, still older than the "
                f"{required} the model needs. Retry with version=\"latest\" or version=\"{required}\".")
    if result["exit_code"] == 0:
        if required_v and satisfies:
            _VERSION_REQUIREMENT.clear()
        if rebound:
            result["response"] = (
                f"Claude Code now runs {rebound['to']} ({after or '?'}): the native updater installed there, not at "
                f"the configured {rebound['from']} ({before or '?'}), so claude_code_binary was switched to it.")
        else:
            result["response"] = (f"Claude Code updated from {before or '?'} to {after or '?'}." if result["updated"]
                                  else f"Claude Code is at {after or before or '?'}; no newer version was installed.")
    if hints:
        result["hints"] = hints
    _UPDATE_STATE["last"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": method["method"],
        "version_before": before, "version_after": after, "exit_code": result["exit_code"],
    }
    if rebound:
        _UPDATE_STATE["last"]["rebound_to"] = rebound["to"]
    return result


async def status_report() -> dict:
    """Everything the primary agent needs before delegating.

    This is the preflight the 2026-09-10 test lacked: it says whether Claude
    Code is installed, signed in, which repositories it may work in, and
    whether the callback into Agamemnon is configured — in one call.
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
            process_uid = os.getuid() if hasattr(os, "getuid") else None
            process_gid = os.getgid() if hasattr(os, "getgid") else None
            if not path.is_file():
                token_problem = "it is not a regular file (a directory is created when the path did not exist at container start)"
            elif os.name != "nt" and st.st_mode & 0o077:
                token_problem = f"its mode is {oct(st.st_mode & 0o777)}; it must be 0600"
            elif not readable:
                # Typically created with `docker exec` (root) while the app
                # runs as PUID/PGID — the classic ZimaOS case.
                if process_uid is not None and process_gid is not None:
                    token_problem = (f"it is owned by uid {st.st_uid} but Agamemnon runs as uid {process_uid}; "
                                     f"chown it to {process_uid}:{process_gid}")
                else:
                    token_problem = "it is not readable by the Agamemnon process"
            callback_status["token_file_ok"] = not token_problem
            callback_status["token_file_owner"] = st.st_uid
            callback_status["process_uid"] = process_uid
    default_repo, how = default_repository()
    runner = get_task_runner()
    _process_limit()  # refresh the gate size from settings before reporting it
    active = [record for record in runner.tasks.values() if record.get("status") in _ACTIVE_STATUSES]
    ready = bool(info["available"] and not info["error"] and auth.get("logged_in"))
    hints: list[str] = []
    if not info["available"]:
        hints.append("Install Claude Code or set Settings › Claude Code › Advanced › Binary (CLAUDE_CODE_BINARY).")
    elif info["error"]:
        hints.append(info["error"])
    if info["available"] and auth.get("logged_in") is False:
        hints.append("The binary is not signed in. Ask the admin to open Settings › Claude Code "
                     "› Sign in and complete the sign-in in their browser (no SSH needed; the "
                     "agent cannot do this step). Alternatively run `claude auth login` as the container "
                     "user (HOME=%s), or provide ANTHROPIC_API_KEY. "
                     "To avoid signing in inside the container, use the cloud runner instead: Settings › "
                     "Claude Code › Cloud runner runs Claude Code in GitHub Actions with the credential kept in "
                     "the repository's secrets." % _claude_home())
    if info["available"] and "--permission-prompts" not in info["flags"]:
        hints.append("Claude Code is older than 2.1.259; upgrade (action=update) for --permission-prompts none "
                     "(prompts are still denied in headless mode, but Claude may retry them).")
    if default_repo is None:
        hints.append(how)
    if callback["token_file"] and not callback_status.get("token_file_ok"):
        # _claude_environment() refuses to run with a loose or unreadable
        # token, so this blocks every delegation, not just the callback.
        ready = False
        hints.append(
            f"The callback token file {callback_status.get('token_file')} blocks delegation: {token_problem}. "
            "It must be a regular file, mode 0600 on POSIX (or protected by a "
            "restricted ACL on Windows), owned by the Agamemnon process user "
            "(Settings › Claude Code › Advanced › Callback token file / CLAUDE_CODE_ODYSSEUS_TOKEN_FILE). "
            "Fix it, or clear the callback URL and token file to delegate without the callback."
        )
    update_required = None
    requirement = version_requirement() if info["available"] else {}
    required_v = _version_tuple(requirement.get("required"))
    if required_v:
        installed_v = _version_tuple(info.get("version"))
        if installed_v is None or installed_v < required_v:
            update_required = {**requirement, "installed": info.get("version") or requirement.get("installed")}
            model = f" (model {requirement['model']})" if requirement.get("model") else ""
            hints.insert(0, f"Claude Code {info.get('version') or requirement.get('installed') or '?'} is older than "
                            f"{requirement['required']}, which a previous run's model required{model}. Call "
                            "delegate_to_claude_code with action=update to upgrade the configured binary.")
        else:
            _VERSION_REQUIREMENT.clear()
    available_slots = max(0, _PROCESS_LIMIT_SIZE - len(active))
    cloud_fallback = backend_warning()
    if cloud_fallback:
        hints.append(cloud_fallback)
    return {
        "response": (
            f"Claude Code is {'ready' if ready else 'not ready'}; "
            f"{len(active)} active of {_PROCESS_LIMIT_SIZE} configured task slot(s) "
            f"({available_slots} available)."
        ),
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
        # Set when the configured backend is not the one delegations use
        # (cloud chosen, no cloud repository); None when they agree.
        "backend_warning": cloud_fallback,
        "callback": callback_status,
        "max_concurrent_tasks": _PROCESS_LIMIT_SIZE,
        "active_tasks": len(active),
        "available_task_slots": available_slots,
        "hints": hints,
        # A previous run's "version X or newer is required", while the binary
        # is still older; action=update fixes it.
        "update_required": update_required,
        "update": {
            "method": _install_method(binary)["method"] if info["available"] else None,
            "in_progress": update_in_progress(),
            "auto_update": _auto_update_enabled(),
            "last": _UPDATE_STATE["last"],
        },
        # The report itself succeeded; "not ready" is its answer (``ready``),
        # not a tool failure. exit_code=1 here was logged as a failed call on
        # every preflight of a not-yet-signed-in install (2026-09-27).
        "exit_code": 0,
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
    if "--disallowedTools" in flags:
        # Deny rules win over allow rules: no delete, force, or remote access
        # even where an allowed prefix would otherwise cover it. Placed before
        # the variadic --tools/--allowedTools pair so each list ends at the
        # next option.
        argv += ["--disallowedTools", *DISALLOWED_TOOLS]
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
        # Tool results carry whatever Claude read or ran; both the task
        # record and the activity feed see them only redacted.
        self.secrets = _secret_values()

    def _add(self, entry: dict) -> None:
        if len(self.entries) >= MAX_TRANSCRIPT_ENTRIES:
            self.truncated = True
            return
        entry = _redact(entry, self.secrets)
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.entries.append(entry)

    def _publish(self, kind: str, title: str, *, detail: Optional[str] = None,
                 data: Optional[dict] = None, level: str = "info") -> None:
        activity.publish(self.session_id, kind, _redact(title, self.secrets), source="claude_code",
                         run_id=self.run_id, owner=self.owner, detail=_redact(detail, self.secrets),
                         data=_redact(data, self.secrets), level=level)

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
    """Run the Claude Code CLI once (see :func:`_run_claude_process`).

    Waits while the binary is being updated, counts itself as in flight so an
    update cannot start underneath it, and turns the CLI's "version X or
    newer is required" refusal into a structured error naming action=update.
    """
    global _ACTIVE_RUNS
    await _wait_for_update()
    _ACTIVE_RUNS += 1
    try:
        result = await _run_claude_process(repository, prompt, timeout, tools, on_process=on_process, model=model)
    finally:
        _ACTIVE_RUNS -= 1
    return _annotate_outdated(result) if isinstance(result, dict) else result


async def _run_with_auto_update(repository: Path, prompt: str, timeout: int, tools: list[str], **kwargs) -> dict:
    """``_run_claude``, plus one update-and-retry when the CLI is too old for
    the model and the ``claude_code_auto_update`` setting is on.

    Off by default: an update replaces the binary every delegation uses. Only
    retried when the failed run left the checkout untouched (the refusal
    comes before Claude's first turn, so it normally does).
    """
    result = await _run_claude(repository, prompt, timeout, tools, **kwargs)
    if not isinstance(result, dict) or result.get("error_kind") != OUTDATED_ERROR_KIND or not _auto_update_enabled():
        return result
    if result.get("changes") or result.get("commits"):
        result["auto_update"] = {"skipped": "the failed run changed the checkout; update manually with action=update"}
        return result
    update = await update_binary()
    summary = {key: update.get(key) for key in ("response", "error", "method", "version_before", "version_after",
                                                  "satisfies_requirement", "exit_code") if key in update}
    if update.get("exit_code") != 0 or update.get("satisfies_requirement") is False:
        result["auto_update"] = summary
        result["error"] += f" An automatic update was attempted and did not fix it: {update.get('error') or 'no newer version'}"
        return result
    retry = await _run_claude(repository, prompt, timeout, tools, **kwargs)
    if isinstance(retry, dict):
        retry["auto_update"] = summary
        retry["retried_after_update"] = True
    return retry


async def _run_claude_process(
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
        return {"error": _tool_error(f"binary unavailable at {binary}", _RUN_CONTEXT.get().get("tool_name")), "exit_code": 1}
    info = await binary_info(binary)
    if info.get("error"):
        return {"error": _tool_error(str(info["error"]), _RUN_CONTEXT.get().get("tool_name")), "exit_code": 1}
    stream = bool(info.get("stream_json")) and _flag_setting("claude_code_stream_transcript", True)
    argv = _build_argv(binary, prompt, tools, info.get("flags", []), model=model, stream=stream)
    try:
        child_env = _claude_environment()
    except ValueError as exc:
        return {"error": _tool_error(str(exc), _RUN_CONTEXT.get().get("tool_name")), "exit_code": 1}
    # The toolchains the sandboxed shell uses for this checkout (Node, JDK,
    # Maven under /opt/toolchains, src/toolchains.py): the server's own PATH
    # has no java, so a granted `./mvnw test` still could not start. CI=true
    # keeps test runners out of watch mode and interactive prompts.
    try:
        from src.toolchains import shell_env

        child_env.update(shell_env(str(repository), child_env.get("PATH")))
    except Exception:  # noqa: BLE001 - a missing toolchain must not stop the run
        logger.debug("claude_code: toolchain environment unavailable", exc_info=True)
    child_env.setdefault("CI", "true")
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
            _redact_result(result, transcript.secrets)
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
            if isinstance(parsed, dict):
                # `output` here is the raw stream-json the transcript was parsed
                # *from*: same events, same final text, one machine-readable
                # layer less. Shipping both put ~50KB of duplicate JSONL in every
                # delegation result — into the agent's context, the offload
                # store, and the embedding queue. Keep the structured view and
                # a short tail for debugging; keep the raw bytes only when
                # there is no envelope, which is exactly when they are the only
                # evidence of what went wrong.
                result["output"] = full_out[-RAW_TAIL_ON_ENVELOPE:]
                result["output_truncated"] = len(full_out) > RAW_TAIL_ON_ENVELOPE
        result.update(await _git_report(repository))
        result.update(await _git_changes(repository, start_sha))
        # Everything above that came from the child (stdout, stderr, the
        # envelope, error text, commit subjects) is redacted before it is
        # returned to the model or published.
        _redact_result(result, transcript.secrets)
        _finish_run(ctx, run_id, title, result)
        return result


_ACTIONS = ("run", "start", "poll", "get", "cancel", "list", "status", "list_repositories", "repositories",
            "update", "upgrade")
# Longest a single poll may block. The 2026-09-12 logs show the agent
# alternating `bash sleep 20..120` with poll for ten minutes until the
# loop-breaker ended the turn; one poll that waits on the job replaces both.
MAX_POLL_WAIT_S = 600
# A model that polls without wait_seconds gets an answer at once and polls
# again, one model round per check: the 2026-09-18 logs show nine rounds of
# identical polls before the loop-breaker ended the turn. So a repeat poll of a
# still-running task waits on the job itself, doubling each time (30s, 60s,
# 120s…, capped so a steer or stop is still picked up within minutes). The
# wait ends as soon as the task finishes.
_POLL_BACKOFF_START_S = 30
_POLL_BACKOFF_MAX_S = 240
_POLL_REPEAT_WINDOW_S = 900
_last_polls: dict[str, tuple[float, int]] = {}


def _poll_wait(task_id: str, requested: int) -> int:
    """Seconds this poll should wait: the caller's request, or the backoff
    floor when it is polling the same task again soon after the last poll."""
    now = time.monotonic()
    last = _last_polls.get(task_id)
    repeats = last[1] + 1 if last and now - last[0] < _POLL_REPEAT_WINDOW_S else 0
    _last_polls[task_id] = (now, repeats)
    for stale in [tid for tid, (ts, _) in _last_polls.items() if now - ts > _POLL_REPEAT_WINDOW_S]:
        _last_polls.pop(stale, None)
    if repeats == 0:
        return requested
    floor = min(_POLL_BACKOFF_MAX_S, _POLL_BACKOFF_START_S * 2 ** min(repeats - 1, 8))
    return max(requested, floor)


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
    # The agent loop's stall detector reads this instead of the whole report,
    # so a poll that only shows a larger elapsed time is not mistaken for
    # progress. It is removed before the model sees the result.
    out["progress_key"] = json.dumps([record.get("status"), progress[-6:]], default=str)
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
    * ``update`` (alias ``upgrade``) — run the configured binary's own
      updater (:func:`update_binary`); optional ``version``.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        invoked_tool = _tool_name((ctx or {}).get("tool_name") if isinstance(ctx, dict) else None)
        try:
            args = json.loads(content) if (content or "").strip() else {}
        except (TypeError, json.JSONDecodeError):
            return {"error": _tool_error("JSON object required", invoked_tool), "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": _tool_error("JSON object required", invoked_tool), "exit_code": 1}
        action = str(args.get("action") or "run").strip().lower()
        if action not in _ACTIONS:
            return {"error": _tool_error(f"unknown action {action!r}; one of {', '.join(_ACTIONS)}", invoked_tool),
                    "exit_code": 1}
        owner = (ctx or {}).get("owner") if isinstance(ctx, dict) else None
        session_id = (ctx or {}).get("session_id") if isinstance(ctx, dict) else None
        if action == "status":
            report = await status_report()
            if _cloud_configured():
                try:
                    from src import claude_cloud
                    report["cloud"] = await claude_cloud.status()
                except Exception as exc:
                    report["cloud"] = {"ready": False, "hints": [str(exc)]}
            return report
        if action in ("run", "start", "update", "upgrade"):
            scoped = _worker_scope(action, args, session_id, invoked_tool)
            if scoped is not None:
                if "error" in scoped:
                    return scoped
                args = scoped
        if action in ("update", "upgrade"):
            # The tool is admin-only (tool_security), so this is too.
            try:
                timeout = max(60, min(1800, int(args.get("timeout_seconds") or UPDATE_TIMEOUT_S)))
            except (TypeError, ValueError):
                timeout = UPDATE_TIMEOUT_S
            result = await update_binary(args.get("version") or args.get("target"), timeout=timeout)
            if "error" in result:
                result["error"] = result["error"].replace(_DEFAULT_TOOL_NAME + ":", invoked_tool + ":", 1)
            return result
        cloud = await _cloud_action(action, args, owner=owner, session_id=session_id, tool_name=invoked_tool)
        if cloud is not None:
            return cloud
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
            started = await runner.start(args, owner=owner, session_id=session_id, tool_name=invoked_tool)
            if "error" in started:
                return {**started, "exit_code": 1}
            return {**started, "exit_code": 0,
                    "note": "Poll with action=poll and this task_id; cancel with action=cancel."}
        if action in ("poll", "get", "cancel"):
            task_id = str(args.get("task_id") or "").strip()
            if not task_id:
                return {"error": _tool_error("task_id is required", invoked_tool), "exit_code": 1}
            steered: list = []
            if action == "cancel":
                record = await runner.cancel(task_id, owner=owner)
            else:
                try:
                    wait = max(0, min(MAX_POLL_WAIT_S, int(args.get("wait_seconds") or 0)))
                except (TypeError, ValueError):
                    wait = 0
                if action == "poll":
                    wait = _poll_wait(task_id, wait)
                record = (await runner.wait(task_id, wait, owner=owner, session_id=session_id, steered=steered)
                          if wait else runner.get(task_id, owner=owner))
            if record is None:
                return {"error": _tool_error(f"task {task_id} not found", invoked_tool), "exit_code": 1}
            if record.get("status") in _ACTIVE_STATUSES:
                report = _running_report(record)
                if action == "poll" and wait:
                    report["waited_seconds"] = wait
                return _with_steer_note(report, steered)
            _last_polls.pop(task_id, None)
            return {**record, "exit_code": record.get("exit_code", 1)}
        if action == "list":
            tasks = runner.summaries(owner=owner)
            if _cloud_configured():
                from src import claude_cloud
                tasks = tasks + claude_cloud.summaries(owner=owner)
            return {"tasks": tasks, "exit_code": 0}
        parsed = _parse_args(args, invoked_tool)
        if "error" in parsed:
            return {**parsed, "exit_code": 1}
        label = str(args.get("label") or "").strip()[:120] or None
        with run_context(session_id=session_id, owner=owner, label=label, tool_name=invoked_tool):
            result = await _run_with_auto_update(parsed["repository"], parsed["prompt"], parsed["timeout"],
                                                 parsed["tools"], model=parsed["model"])
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
                   "total_additions", "total_deletions", "error_kind", "required_version")


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
                    session_id: Optional[str] = None,
                    tool_name: str = _DEFAULT_TOOL_NAME) -> dict:
        parsed = _parse_args(args if isinstance(args, dict) else {}, tool_name)
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
            "tool_name": _tool_name(tool_name),
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
                             task_id=task_id, label=record.get("label"),
                             tool_name=record.get("tool_name")):
                result = await _run_with_auto_update(
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

    async def wait(self, task_id: str, timeout: float, *, owner: Optional[str] = None,
                   session_id: Optional[str] = None, steered: Optional[list] = None) -> dict | None:
        """``get``, after waiting up to ``timeout`` seconds for the job to end.

        ``asyncio.wait`` never cancels the job, so an abandoned wait (the chat
        turn stopped) leaves the delegation running. A user message in
        ``session_id`` also ends the wait (2026-10-02: a poll of up to 600 s
        held a steer for its whole length); ``steered`` then gets a True.
        """
        job = self.jobs.get(task_id)
        if job is not None and not job.done() and timeout > 0:
            from src import agent_control
            if await agent_control.wait_or_steer(session_id, timeout, job) and steered is not None:
                steered.append(True)
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
