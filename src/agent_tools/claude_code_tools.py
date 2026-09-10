"""Safe delegation to a locally installed Claude Code CLI."""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.atomic_io import atomic_write_json
from src.constants import CLAUDE_CODE_TASKS_FILE

DEFAULT_BINARY = os.environ.get("CLAUDE_CODE_BINARY", "/app/data/claude-code/bin/claude")
DEFAULT_ROOTS = tuple(filter(None, os.environ.get(
    "CLAUDE_CODE_REPOSITORY_ROOTS", "/app/data/development:/app/data/agent_worktrees"
).split(os.pathsep)))
# Claude may inspect, edit, test, and commit — never push, touch a remote, or
# run arbitrary shell (see tool_schemas.py's delegate_to_claude_code
# description, which promises exactly this).
DEFAULT_TOOLS = (
    "Read", "Edit", "Write",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git add:*)", "Bash(git commit:*)",
)
MAX_OUTPUT = 50000
MAX_CONCURRENT_TASKS = max(1, int(os.environ.get("CLAUDE_CODE_MAX_CONCURRENT_TASKS", "2")))
# Each git subcommand is spelled out explicitly — NOT a bare "git" prefix.
# Claude Code's --allowedTools patterns are prefix matches, so a bare
# "Bash(git:*)" would also permit "git push ..." (it starts with "git").
# Enumerating exactly the safe subcommands is what actually keeps push/pull/
# clone/remote/fetch out of reach.
_SAFE_GIT_SUBCOMMANDS = "status|diff|log|show|branch|rev-parse|add|commit"
_SAFE_TEST_RUNNERS = "pytest|python -m pytest|npm test|pnpm test|yarn test"
SAFE_TOOL = re.compile(
    rf"^(Read|Edit|Write"
    rf"|Bash\(git (?:{_SAFE_GIT_SUBCOMMANDS})(?::\*)?\)"
    rf"|Bash\((?:{_SAFE_TEST_RUNNERS})(?::\*)?\))$"
)

# Per-repository serialization shared by every caller (the admin chat tool
# AND the HTTP task runner below) so two delegations never run concurrently
# against the same checkout and stomp on each other's working tree.
_REPO_LOCKS: dict[str, asyncio.Lock] = {}
_PROCESS_LIMIT = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


def _repo_lock(repository: Path) -> asyncio.Lock:
    return _REPO_LOCKS.setdefault(str(repository), asyncio.Lock())

def _approved_repository(value: str) -> Path:
    path = Path(value or "").expanduser().resolve(strict=True)
    if not path.is_dir() or not (path / ".git").exists():
        raise ValueError("repository must be an existing Git repository or worktree")
    roots = [Path(root).expanduser().resolve() for root in DEFAULT_ROOTS]
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("repository is outside Claude Code approved roots")
    return path



def _claude_environment() -> dict[str, str]:
    """Build the child environment without persisting or exposing secrets.

    A deployment may give delegated Claude sessions scoped access back to
    Odysseus with a private token file. Keeping the token out of argv avoids
    process-list exposure; the child receives it only in its environment.
    """
    env = {
        **os.environ,
        "HOME": os.environ.get("CLAUDE_CODE_HOME", os.environ.get("HOME", "/app/data")),
    }
    url = os.environ.get("CLAUDE_CODE_ODYSSEUS_URL", "").strip()
    if url:
        env["ODYSSEUS_URL"] = url
    token_file = os.environ.get("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", "").strip()
    if token_file:
        path = Path(token_file).expanduser()
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
    {"repository": Path, "prompt": str, "timeout": int, "tools": list[str]}
    or {"error": str} — never both, and never raises."""
    if not isinstance(args, dict):
        return {"error": "delegate_to_claude_code: JSON object required"}
    try:
        repository = _approved_repository(str(args.get("repository") or ""))
    except (ValueError, OSError) as exc:
        return {"error": f"delegate_to_claude_code: {exc}"}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt or len(prompt) > 20000:
        return {"error": "delegate_to_claude_code: prompt is required and must be <= 20000 characters"}
    try:
        timeout = max(30, min(1800, int(args.get("timeout_seconds", 900))))
    except (TypeError, ValueError):
        timeout = 900
    tools = args.get("allowed_tools") or list(DEFAULT_TOOLS)
    if not isinstance(tools, list) or not all(isinstance(item, str) and item for item in tools):
        return {"error": "delegate_to_claude_code: allowed_tools must be a list of strings"}
    if any(not SAFE_TOOL.fullmatch(item) for item in tools):
        return {"error": "delegate_to_claude_code: unsafe allowed tool"}
    return {"repository": repository, "prompt": prompt, "timeout": timeout, "tools": tools}


async def _run_claude(
    repository: Path,
    prompt: str,
    timeout: int,
    tools: list[str],
    on_process=None,
) -> dict:
    """Run the Claude Code CLI once, serialized per repository.

    ``on_process`` (optional) is called with the live ``Process`` as soon as
    it starts, so a caller (the task runner) can kill it on cancellation.
    Never shells out through a string — argv is built from a fixed binary
    path and an allowlist-checked ``--allowedTools`` list, so nothing here is
    vulnerable to shell interpolation.
    """
    binary = Path(DEFAULT_BINARY)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return {"error": f"delegate_to_claude_code: binary unavailable at {binary}", "exit_code": 1}
    # ``--tools`` limits which built-ins exist for this run; ``--allowedTools``
    # grants only the allowlist-validated operations without an interactive
    # permission prompt. Both are required in headless mode.
    tool_names = list(dict.fromkeys(tool.split("(", 1)[0] for tool in tools))
    argv = [
        str(binary), "-p", prompt,
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-prompts", "none",
        "--tools", *tool_names,
        "--allowedTools", *tools,
    ]
    try:
        child_env = _claude_environment()
    except ValueError as exc:
        return {"error": f"delegate_to_claude_code: {exc}", "exit_code": 1}
    # The repository lock prevents worktree collisions. The process limit also
    # bounds aggregate CPU/memory/API use across different repositories.
    async with _PROCESS_LIMIT, _repo_lock(repository):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(repository), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
            if on_process is not None:
                on_process(proc)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            assert proc is not None
            proc.kill()
            await proc.wait()
            return {"error": f"Claude Code timed out after {timeout}s", "exit_code": 124}
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        except OSError as exc:
            return {"error": f"Claude Code could not start: {exc}", "exit_code": 127}
        out = stdout.decode("utf-8", errors="replace")[-MAX_OUTPUT:]
        err = stderr.decode("utf-8", errors="replace")[-10000:]
        result = {"output": out, "stderr": err, "exit_code": proc.returncode, "repository": str(repository)}
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                result["claude"] = parsed
                # Claude Code's JSON result commonly includes these metrics;
                # surface them without making callers parse the raw envelope.
                for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "is_error"):
                    if key in parsed:
                        result[key] = parsed[key]
        except json.JSONDecodeError:
            pass
        result.update(await _git_report(repository))
        return result


class ClaudeCodeTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return {"error": "delegate_to_claude_code: JSON object required", "exit_code": 1}
        parsed = _parse_args(args)
        if "error" in parsed:
            return {**parsed, "exit_code": 1}
        return await _run_claude(parsed["repository"], parsed["prompt"], parsed["timeout"], parsed["tools"])


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

    async def start(self, args: dict, *, owner: Optional[str] = None) -> dict:
        parsed = _parse_args(args if isinstance(args, dict) else {})
        if "error" in parsed:
            return {**parsed, "exit_code": 1}
        task_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self.tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "repository": str(parsed["repository"]),
            "owner": owner,
            "created_at": now,
        }
        self._save()
        job = asyncio.create_task(self._run(task_id, parsed))
        self.jobs[task_id] = job
        return {"task_id": task_id, "status": "queued"}

    async def _run(self, task_id: str, parsed: dict):
        record = self.tasks[task_id]
        record.update({"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        self._save()

        def _register(proc: "asyncio.subprocess.Process") -> None:
            self.procs[task_id] = proc

        try:
            result = await _run_claude(
                parsed["repository"], parsed["prompt"], parsed["timeout"], parsed["tools"],
                on_process=_register,
            )
            record.update(result)
            if task_id in self._cancelling:
                record.update({"status": "cancelled", "error": "Task cancelled", "exit_code": 130})
            else:
                record["status"] = "completed" if result.get("exit_code") == 0 else "failed"
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
