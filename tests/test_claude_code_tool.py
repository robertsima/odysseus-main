"""Bidirectional Claude Code integration: the admin-only `delegate_to_claude_code`
tool, its restart-safe task runner, and the scoped /api/claude-code/* HTTP routes.

Mirrors the parity checks in tests/test_agent_worktree_tool_registration.py: a
tool that is dispatchable but not in every policy set is a privilege hole; a
tool that is in the policy sets but not dispatchable is dead code.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools import claude_code_tools as cct
from src.agent_tools.claude_code_tools import ClaudeCodeTool, ClaudeCodeTaskRunner, _approved_repository
from src.tool_execution import _ADMIN_TOOLS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    is_public_blocked_tool,
    plan_mode_disabled_tools,
)
from routes.claude_code_routes import (
    CLAUDE_CODE_READ_SCOPES,
    CLAUDE_CODE_WRITE_SCOPES,
    _require_claude_code_scope,
)

pytestmark = pytest.mark.area_security

TOOL_NAME = "delegate_to_claude_code"


@pytest.fixture
def approved_repo(tmp_path, monkeypatch):
    """A real Git checkout that is the only approved root, so the tests do
    not depend on /app/data/development/odysseus-main existing on the host."""
    root = tmp_path / "development"
    repo = root / "odysseus-main"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
    monkeypatch.setattr(cct, "DEFAULT_ROOTS", (str(root),))
    monkeypatch.setattr(cct, "DEFAULT_REPOSITORY", "")
    monkeypatch.delenv("ODYSSEUS_AGENT_SOURCE_REPO", raising=False)
    return repo


@pytest.fixture
def fake_binary_info(monkeypatch):
    """Skip probing --version/--help so runs with a stubbed subprocess work."""
    async def _info(binary=None):
        return {"path": str(binary or cct.binary_path()), "available": True, "version": "2.1.267 (Claude Code)",
                "flags": list(cct._OPTIONAL_FLAGS), "error": ""}
    monkeypatch.setattr(cct, "binary_info", _info)
    return _info


# ── Registration parity (dispatchable <-> admin-gated <-> plan-mode-blocked) ──

def test_tool_is_dispatchable():
    assert TOOL_NAME in TOOL_HANDLERS
    assert TOOL_NAME in TOOL_TAGS


def test_tool_has_a_native_function_schema():
    names = {(t.get("function") or {}).get("name") for t in FUNCTION_TOOL_SCHEMAS}
    assert TOOL_NAME in names


def test_tool_is_admin_only():
    assert TOOL_NAME in _ADMIN_TOOLS
    assert TOOL_NAME in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool(TOOL_NAME) is True


def test_tool_is_blocked_in_plan_mode():
    assert TOOL_NAME in plan_mode_disabled_tools()
    assert TOOL_NAME not in PLAN_MODE_READONLY_TOOLS


# ── Repository approval + input validation ──

def test_approved_repository_accepts_repo(approved_repo):
    assert _approved_repository(str(approved_repo)).name == "odysseus-main"


def test_approved_repository_rejects_outside_root(tmp_path, approved_repo):
    outside = tmp_path / "elsewhere"
    (outside / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="outside Claude Code approved roots"):
        _approved_repository(str(outside))


def test_approved_repository_rejects_non_checkout(tmp_path, approved_repo):
    with pytest.raises(ValueError, match="existing Git"):
        _approved_repository(str(tmp_path))


def test_rejection_lists_roots_and_candidates(approved_repo):
    """The 2026-09-10 failure: the agent passed /app, got 'not a Git
    repository', and had no way to learn the right path. The error now
    carries the approved roots and the checkouts under them."""
    with pytest.raises(ValueError) as exc:
        _approved_repository("/app")
    text = str(exc.value)
    assert "Approved roots" in text
    assert str(approved_repo) in text
    assert "(dev)" in text


async def _run_with_allowed_tools(monkeypatch, repo, allowed):
    """Unsafe entries are dropped, not granted: the run goes ahead with the
    safe defaults and the result names what was refused. Returns the result
    and the tools the CLI would actually have received."""
    seen = {}

    async def fake_run(repository, prompt, timeout, tools, on_process=None, model=None):
        seen["tools"] = list(tools)
        return {"exit_code": 0, "result": "ok", "repository": str(repository)}

    monkeypatch.setattr(cct, "_run_claude", fake_run)
    result = await ClaudeCodeTool().execute(json.dumps({
        "repository": str(repo),
        "prompt": "inspect",
        "allowed_tools": allowed,
    }), {})
    return result, seen["tools"]


async def test_tool_rejects_push_permission(approved_repo, monkeypatch):
    result, tools = await _run_with_allowed_tools(monkeypatch, approved_repo, ["Bash(git push:*)"])
    assert "Bash(git push:*)" not in tools
    assert tools == cct.default_tools()
    assert result["dropped_tools"] == ["Bash(git push:*)"]
    assert "unsafe" in result["dropped_note"]


async def test_tool_rejects_sudo_permission(approved_repo, monkeypatch):
    result, tools = await _run_with_allowed_tools(monkeypatch, approved_repo, ["Bash(sudo:*)"])
    assert not any("sudo" in tool for tool in tools)
    assert result["dropped_tools"] == ["Bash(sudo:*)"]
    assert "unsafe" in result["dropped_note"]


async def test_tool_rejects_arbitrary_shell(approved_repo, monkeypatch):
    result, tools = await _run_with_allowed_tools(monkeypatch, approved_repo, ["Bash"])
    assert "Bash" not in tools
    assert result["dropped_tools"] == ["Bash"]
    assert "unsafe" in result["dropped_note"]


async def test_tool_rejects_malformed_json():
    result = await ClaudeCodeTool().execute("{not json", {})
    assert result["exit_code"] == 1
    assert "JSON object required" in result["error"]


async def test_tool_rejects_non_object_json():
    result = await ClaudeCodeTool().execute("[]", {})
    assert result["exit_code"] == 1
    assert "JSON object required" in result["error"]


async def test_tool_rejects_missing_prompt(approved_repo):
    result = await ClaudeCodeTool().execute(json.dumps({
        "repository": str(approved_repo),
    }), {})
    assert result["exit_code"] == 1
    assert "prompt" in result["error"]


def test_callback_token_file_must_be_private(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("ody_secret", encoding="utf-8")
    token_file.chmod(0o644)
    monkeypatch.setenv("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", str(token_file))
    with pytest.raises(ValueError, match="private regular file"):
        cct._claude_environment()


def test_callback_token_is_added_only_to_child_environment(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("ody_secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CLAUDE_CODE_ODYSSEUS_URL", "http://127.0.0.1:7000")
    monkeypatch.delenv("ODYSSEUS_API_TOKEN", raising=False)
    child = cct._claude_environment()
    assert child["ODYSSEUS_API_TOKEN"] == "ody_secret"
    assert child["ODYSSEUS_URL"] == "http://127.0.0.1:7000"


# ── Child environment: allowlist, not os.environ ──

_SERVER_SECRETS = {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_" + "a" * 36,
    "ODYSSEUS_AGENT_GITHUB_TOKEN": "github_pat_" + "b" * 40,
    "GH_TOKEN": "gho_" + "c" * 36,
    "OPENAI_API_KEY": "sk-" + "d" * 40,
    "GOOGLE_CLIENT_SECRET": "client-secret-value-1234",
    "DATABASE_URL": "postgresql://odysseus:dbpass1234@db/odysseus",
    "SMTP_PASSWORD": "smtp-password-5678",
    "ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH": "/run/secrets/app.pem",
    "GIT_ASKPASS": "/usr/local/bin/odysseus-askpass",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "GIT_CONFIG_PARAMETERS": "'credential.helper'='store'",
    "CLAUDE_CODE_ODYSSEUS_TOKEN_FILE": "",
    "CLAUDE_CODE_SOME_SECRET": "should-not-pass-9999",
}


@pytest.fixture
def server_env(monkeypatch, tmp_path):
    for name, value in _SERVER_SECRETS.items():
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ODYSSEUS_URL", raising=False)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/ca.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/ca.pem")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "e" * 40)
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "8000")
    monkeypatch.setenv("CLAUDE_CODE_HOME", str(tmp_path / "claude-home"))
    return tmp_path


def test_child_environment_drops_server_secrets(server_env):
    child = cct._claude_environment()
    for name in _SERVER_SECRETS:
        assert name not in child, name
    values = "\n".join(child.values())
    for value in _SERVER_SECRETS.values():
        if value:
            assert value not in values


def test_child_environment_keeps_allowlisted_variables(server_env):
    child = cct._claude_environment()
    assert child["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert child["LANG"] == child["LC_ALL"] == "C.UTF-8"
    assert child["HTTPS_PROXY"] == "http://proxy.local:3128"
    assert child["NO_PROXY"] == "localhost"
    assert child["SSL_CERT_FILE"] == child["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/ca.pem"
    assert child["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8000"
    assert child["HOME"] == str(server_env / "claude-home")
    assert child["GIT_TERMINAL_PROMPT"] == "0"
    # Claude Code's own credential is the one secret the CLI needs to run.
    assert child["ANTHROPIC_API_KEY"].startswith("sk-ant-")
    # Odysseus' own CLAUDE_CODE_* configuration is not Claude Code's.
    assert "CLAUDE_CODE_HOME" not in child


def test_child_environment_has_no_github_credentials_by_default(server_env):
    child = cct._claude_environment()
    assert not [name for name in child if "GITHUB" in name or name.startswith(("GH_", "GIT_ASKPASS", "GIT_CONFIG"))]
    assert "SSH_AUTH_SOCK" not in child


async def test_binary_probe_uses_the_allowlisted_environment(server_env, tmp_path):
    script = tmp_path / "printenv-claude"
    script.write_text("#!/bin/sh\nenv\n", encoding="utf-8")
    script.chmod(0o755)
    code, out = await cct._capture(script, "--version")
    assert code == 0
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in out
    assert _SERVER_SECRETS["OPENAI_API_KEY"] not in out


# ── Deletes, force and remotes are denied in the invocation ──

def test_argv_denies_destructive_commands():
    tools = cct.default_tools()
    argv = cct._build_argv(Path("/x/claude"), "fix it", tools, list(cct._OPTIONAL_FLAGS))
    assert "--disallowedTools" in argv
    denied = argv[argv.index("--disallowedTools") + 1:argv.index("--tools")]
    for rule in ("Bash(rm:*)", "Bash(git branch -D:*)", "Bash(git branch -d:*)", "Bash(git push:*)",
                 "Bash(git reset:*)", "Bash(git clean:*)", "Bash(gh:*)", "Bash(git tag -d:*)",
                 "Bash(env:*)", "Bash(printenv:*)"):
        assert rule in denied, rule
    # The deny list is never itself something the allowlist could grant.
    assert not [rule for rule in denied if rule in tools]
    # And the allowlist still never grants a destructive command.
    assert not [tool for tool in tools if cct.SAFE_TOOL.fullmatch(tool) is None]
    for unsafe in ("Bash(git push:*)", "Bash(rm:*)", "Bash(gh:*)", "Bash(git reset:*)", "Bash(git clean:*)"):
        assert cct.SAFE_TOOL.fullmatch(unsafe) is None


def test_argv_without_disallowed_flag_support_still_runs():
    argv = cct._build_argv(Path("/x/claude"), "fix it", ["Read"],
                           ["--tools", "--allowedTools", "--output-format", "--no-session-persistence"])
    assert "--disallowedTools" not in argv


# ── Redaction of what the child prints back ──

def test_redact_result_masks_server_secrets_and_known_shapes(server_env):
    result = {
        "output": "token=" + _SERVER_SECRETS["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "stderr": "db " + _SERVER_SECRETS["DATABASE_URL"],
        "error": "failed with " + _SERVER_SECRETS["SMTP_PASSWORD"],
        "result": "found key " + _SERVER_SECRETS["OPENAI_API_KEY"] + " and ghp_" + "z" * 36,
        "transcript": [{"kind": "tool_result", "excerpt": "ANTHROPIC_API_KEY=sk-ant-" + "e" * 40}],
        "exit_code": 0,
        "repository": "/repo",
    }
    cct._redact_result(result)
    text = json.dumps(result)
    for value in _SERVER_SECRETS.values():
        if value and len(value) >= 8 and value.startswith(("ghp_", "github_pat_", "sk-", "postgresql", "smtp")):
            assert value not in text
    assert "sk-ant-" + "e" * 40 not in text
    assert "ghp_" + "z" * 36 not in text
    assert result["exit_code"] == 0 and result["repository"] == "/repo"


async def test_run_output_is_redacted_before_it_is_returned(server_env, monkeypatch, tmp_path, fake_binary_info):
    monkeypatch.setattr(cct, "DEFAULT_BINARY", "/bin/sh")
    monkeypatch.setattr(cct, "_git_report", lambda repository: _async_result({}))
    monkeypatch.setattr(cct, "_git_changes", lambda repository, start: _async_result({}))
    monkeypatch.setattr(cct, "_git_head", lambda repository: _async_result(None))
    leaked = _SERVER_SECRETS["GITHUB_PERSONAL_ACCESS_TOKEN"]

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return f"not json {leaked}".encode(), f"auth failed for {leaked}".encode()

    async def fake_exec(*argv, **kwargs):
        assert leaked not in "\n".join(kwargs["env"].values())
        return FakeProc()

    monkeypatch.setattr(cct.asyncio, "create_subprocess_exec", fake_exec)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    result = await cct._run_claude(repo, "x", 30, ["Read"])
    assert leaked not in json.dumps(result)
    assert "***" in result["error"] and "***" in result["stderr"]


# ── Per-repository serialization ──

async def test_repo_lock_serializes_concurrent_runs(monkeypatch, tmp_path, fake_binary_info):
    """Two delegations against the same repo must not run concurrently."""
    monkeypatch.setattr(cct, "DEFAULT_BINARY", "/bin/sh")
    monkeypatch.setattr(cct, "_git_report", lambda repository: _async_result({}))
    order = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            order.append("start")
            await asyncio.sleep(0.05)
            order.append("end")
            return b"{}", b""

        def kill(self):
            pass

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await asyncio.gather(
        cct._run_claude(tmp_path, "one", 5, ["Read"]),
        cct._run_claude(tmp_path, "two", 5, ["Read"]),
    )
    # If the two runs overlapped, both "start" entries would land before
    # either "end". The shared per-repository lock forces them to alternate.
    assert order == ["start", "end", "start", "end"]


async def test_global_process_limit_bounds_different_repositories(monkeypatch, tmp_path, fake_binary_info):
    monkeypatch.setattr(cct, "DEFAULT_BINARY", "/bin/sh")
    monkeypatch.setattr(cct, "_PROCESS_LIMIT", asyncio.Semaphore(1))
    monkeypatch.setattr(cct, "_git_report", lambda repository: _async_result({}))
    active = 0
    peak = 0

    class FakeProc:
        returncode = 0

        async def communicate(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return b"{}", b""

        def kill(self):
            pass

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    await asyncio.gather(
        cct._run_claude(tmp_path / "one", "one", 5, ["Read"]),
        cct._run_claude(tmp_path / "two", "two", 5, ["Read"]),
    )
    assert peak == 1


def _async_result(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner()


async def test_git_report_preserves_first_porcelain_status_path(monkeypatch, tmp_path):
    outputs = iter([b"branch-name\n", b" M app.py\n?? new.py\n", b"abc123\n"])

    class FakeProc:
        async def communicate(self):
            return next(outputs), b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    report = await cct._git_report(tmp_path)
    assert report["branch"] == "branch-name"
    assert report["changed_files"] == ["app.py", "new.py"]


# ── Task runner: lifecycle, persistence across a simulated restart, cancel ──

def _fresh_runner(tmp_path) -> ClaudeCodeTaskRunner:
    return ClaudeCodeTaskRunner(store_path=str(tmp_path / "claude_code_tasks.json"))


async def test_start_rejects_invalid_args_without_creating_a_task(tmp_path, monkeypatch):
    monkeypatch.setattr(cct, "_approved_repository", lambda v: Path(v))
    runner = _fresh_runner(tmp_path)
    result = await runner.start({"repository": str(tmp_path), "prompt": ""})
    assert result["exit_code"] == 1
    assert "prompt is required" in result["error"]
    assert runner.tasks == {}


async def test_task_lifecycle_fails_cleanly_without_a_binary(tmp_path, monkeypatch):
    """No Claude Code binary in the test environment: the task should still
    reach a terminal state (not hang) and report why."""
    monkeypatch.setattr(cct, "_approved_repository", lambda v: Path(v))
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(tmp_path / "no-such-claude-binary"))
    runner = _fresh_runner(tmp_path)
    started = await runner.start({"repository": str(tmp_path), "prompt": "do work"})
    task_id = started["task_id"]
    assert started["status"] == "queued"

    job = runner.jobs[task_id]
    await asyncio.wait_for(job, timeout=5)

    record = runner.get(task_id)
    assert record["status"] == "failed"
    assert "binary unavailable" in record["error"]


async def test_cancel_kills_a_running_task(tmp_path, monkeypatch):
    monkeypatch.setattr(cct, "_approved_repository", lambda v: Path(v))
    runner = _fresh_runner(tmp_path)
    killed = {"value": False}

    class FakeProc:
        returncode = None

        def kill(self):
            killed["value"] = True
            self.returncode = -9

    async def fake_run_claude(repository, prompt, timeout, tools, on_process=None, model=None):
        proc = FakeProc()
        if on_process is not None:
            on_process(proc)
        await asyncio.sleep(30)
        return {"exit_code": 0}

    monkeypatch.setattr(cct, "_run_claude", fake_run_claude)

    started = await runner.start({"repository": str(tmp_path), "prompt": "do work"})
    task_id = started["task_id"]

    for _ in range(200):
        if task_id in runner.procs:
            break
        await asyncio.sleep(0.01)
    assert task_id in runner.procs, "task never reached the running subprocess"

    cancelled = await runner.cancel(task_id)
    assert cancelled["status"] == "cancelled"
    assert killed["value"] is True

    # Idempotent: cancelling an already-finished task is a no-op that returns
    # the same terminal record instead of erroring.
    again = await runner.cancel(task_id)
    assert again["status"] == "cancelled"


async def test_cancel_unknown_task_returns_none(tmp_path):
    runner = _fresh_runner(tmp_path)
    assert await runner.cancel("no-such-task") is None


def test_persisted_record_has_no_secrets_or_env(tmp_path):
    store = tmp_path / "claude_code_tasks.json"
    store.write_text(json.dumps({
        "abc123": {
            "task_id": "abc123",
            "status": "running",
            "repository": "/fake/repo",
            "prompt": "do work",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:00+00:00",
        }
    }), encoding="utf-8")
    ClaudeCodeTaskRunner(store_path=str(store))
    persisted = json.loads(store.read_text(encoding="utf-8"))
    blob = json.dumps(persisted)
    assert "prompt" not in persisted["abc123"]
    assert "do work" not in blob
    assert "env" not in persisted["abc123"]
    assert "HOME" not in blob
    assert "CLAUDE_CODE_BINARY" not in blob


def test_restart_reconciles_orphaned_running_task_to_interrupted(tmp_path):
    """A task record left 'running' when the process died must not be
    reported as still running forever — a fresh runner loading the same
    store file marks it interrupted."""
    store = tmp_path / "claude_code_tasks.json"
    store.write_text(json.dumps({
        "abc123": {
            "task_id": "abc123",
            "status": "running",
            "repository": "/fake/repo",
            "prompt": "do work",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:00+00:00",
        }
    }), encoding="utf-8")

    runner = ClaudeCodeTaskRunner(store_path=str(store))
    record = runner.get("abc123")
    assert record["status"] == "interrupted"
    assert "restart" in record["error"]

    # And it's not just in-memory — a second runner loading the same file
    # (simulating a second restart) still sees the reconciled state.
    runner2 = ClaudeCodeTaskRunner(store_path=str(store))
    assert runner2.get("abc123")["status"] == "interrupted"


def test_get_missing_task_returns_none(tmp_path):
    runner = _fresh_runner(tmp_path)
    assert runner.get("no-such-task") is None


def test_task_store_is_private_and_does_not_persist_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cct, "_approved_repository", lambda v: Path(v))
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(tmp_path / "missing-claude"))

    async def exercise():
        runner = _fresh_runner(tmp_path)
        started = await runner.start({
            "repository": str(tmp_path),
            "prompt": "sensitive task instructions",
        }, owner="alice")
        await runner.jobs[started["task_id"]]
        return Path(runner.store_path)

    store = asyncio.run(exercise())
    persisted = store.read_text(encoding="utf-8")
    assert "sensitive task instructions" not in persisted
    assert store.stat().st_mode & 0o777 == 0o600


def test_task_records_are_owner_scoped(tmp_path):
    runner = _fresh_runner(tmp_path)
    runner.tasks["alice-task"] = {"task_id": "alice-task", "owner": "alice", "status": "completed"}
    assert runner.get("alice-task", owner="alice") is not None
    assert runner.get("alice-task", owner="mallory") is None
    # An admin caller deliberately omits owner to inspect all task diagnostics.
    assert runner.get("alice-task") is not None


# ── HTTP route auth gate: /api/claude-code/tasks* ──

def _cookie_request(*, current_user="bob", is_admin=False):
    auth_mgr = SimpleNamespace(
        is_configured=True,
        is_admin=lambda user: is_admin and user == "bob",
    )
    return SimpleNamespace(
        state=SimpleNamespace(current_user=current_user, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        headers={},
    )


def _api_token_request(*, scopes=None, owner="alice"):
    return SimpleNamespace(
        state=SimpleNamespace(
            current_user="api",
            api_token=True,
            api_token_scopes=scopes or [],
            api_token_owner=owner,
        ),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={},
    )


class TestClaudeCodeRouteScopeGate:
    def test_non_admin_cookie_session_rejected(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        req = _cookie_request(is_admin=False)
        with pytest.raises(HTTPException) as exc:
            _require_claude_code_scope(req, CLAUDE_CODE_WRITE_SCOPES)
        assert exc.value.status_code == 403

    def test_admin_cookie_session_allowed(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        req = _cookie_request(is_admin=True)
        owner = _require_claude_code_scope(req, CLAUDE_CODE_WRITE_SCOPES)
        assert owner == "bob"

    def test_token_with_write_scope_allowed_for_write(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        req = _api_token_request(scopes=["claude_code:write"])
        owner = _require_claude_code_scope(req, CLAUDE_CODE_WRITE_SCOPES)
        assert owner == "alice"

    def test_token_with_read_only_scope_rejected_for_write(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        req = _api_token_request(scopes=["claude_code:read"])
        with pytest.raises(HTTPException) as exc:
            _require_claude_code_scope(req, CLAUDE_CODE_WRITE_SCOPES)
        assert exc.value.status_code == 403

    def test_token_with_write_scope_allowed_for_read(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        req = _api_token_request(scopes=["claude_code:write"])
        owner = _require_claude_code_scope(req, CLAUDE_CODE_READ_SCOPES)
        assert owner == "alice"

    def test_token_missing_scope_entirely_rejected(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "true")
        req = _api_token_request(scopes=["todos:read"])
        with pytest.raises(HTTPException) as exc:
            _require_claude_code_scope(req, CLAUDE_CODE_READ_SCOPES)
        assert exc.value.status_code == 403


def test_claude_code_router_registers_task_lifecycle_routes():
    from routes.claude_code_routes import setup_claude_code_routes

    routes = {
        (method, route.path)
        for route in setup_claude_code_routes().routes
        for method in route.methods
    }
    assert ("POST", "/api/claude-code/tasks") in routes
    assert ("GET", "/api/claude-code/tasks/{task_id}") in routes
    assert ("POST", "/api/claude-code/tasks/{task_id}/cancel") in routes


# ── Token scopes / profiles for both directions of the integration ──

def test_claude_code_scopes_are_registered():
    from routes.api_token_routes import ALLOWED_SCOPES, TOKEN_PROFILES

    assert {"claude_code:read", "claude_code:write"} <= ALLOWED_SCOPES
    assert TOKEN_PROFILES["claude_code_tasks"] == ["claude_code:write"]
    assert "claude_agent" in TOKEN_PROFILES


def test_claude_code_write_scope_implies_read_scope():
    from routes.api_token_routes import _normalize_scopes

    normalized = _normalize_scopes(["claude_code:write"])
    assert normalized.index("claude_code:read") < normalized.index("claude_code:write")
