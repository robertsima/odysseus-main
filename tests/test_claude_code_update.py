"""Upgrading the delegated Claude Code CLI from inside Odysseus.

Production 2026-09-26: a delegation failed with "Claude Code 2.1.267 does not
support this model; version 2.1.280 or newer is required", the agent tried
`claude --version && npm install -g @anthropic-ai/claude-code@latest` from
bash, the bash guard refused it, and there was no supported way left to
upgrade. These tests pin the way out: the structured "outdated" error, the
admin-only action=update (and POST /api/claude-code/update), the status hint,
the opt-in auto-update, and the guard's pointer to action=update.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.agent_tools import claude_code_guard as guard
from src.agent_tools import claude_code_tools as cct
from src.agent_tools.claude_code_tools import ClaudeCodeTaskRunner, ClaudeCodeTool

pytestmark = pytest.mark.area_security

OUTDATED_TEXT = ("API Error: 400 Claude Code 2.1.267 does not support this model; version 2.1.280 or newer "
                 "is required. Run 'claude update', or update the Claude desktop app, then try again.")
SECRET = "ghp_" + "s" * 36


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No settings file, no leftover module state, a private task store."""
    monkeypatch.setattr(cct, "_setting", lambda key, default=None: default)
    monkeypatch.delenv("CLAUDE_CODE_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(cct, "_ACTIVE_RUNS", 0)
    monkeypatch.setattr(cct, "_UPDATE_STATE", {"running": False, "last": None})
    monkeypatch.setattr(cct, "_VERSION_REQUIREMENT", {})
    monkeypatch.setattr(cct, "_BINARY_INFO", {})
    runner = ClaudeCodeTaskRunner(store_path=str(tmp_path / "tasks.json"))
    monkeypatch.setattr(cct, "get_task_runner", lambda: runner)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", SECRET)
    return runner


def _executable(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Proc:
    def __init__(self, out: bytes, code: int = 0):
        self._out, self.returncode = out, code

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _fake_exec(monkeypatch, versions=("2.1.267 (Claude Code)", "2.1.290 (Claude Code)"),
               update_output=b"added 1 package", update_code=0):
    """Stub every subprocess: `--version` probes answer from ``versions`` in
    order; anything else is the update command."""
    calls: list[dict] = []
    probes = list(versions)

    async def fake(*argv, **kwargs):
        calls.append({"argv": list(argv), "env": kwargs.get("env") or {}, "cwd": kwargs.get("cwd")})
        if argv[-1] == "--version":
            version = probes.pop(0) if len(probes) > 1 else probes[0]
            return _Proc(version.encode())
        return _Proc(update_output, update_code)

    monkeypatch.setattr(cct.asyncio, "create_subprocess_exec", fake)
    return calls


def _update_calls(calls):
    return [c for c in calls if c["argv"][-1] != "--version"]


# ── The outdated-CLI error ──

def test_detect_outdated_reads_the_cli_refusal():
    assert cct.detect_outdated({"error": OUTDATED_TEXT}) == {"installed": "2.1.267", "required": "2.1.280"}
    assert cct.detect_outdated({"result": OUTDATED_TEXT}) == {"installed": "2.1.267", "required": "2.1.280"}
    assert cct.detect_outdated({"error": "API Error: 529 overloaded"}) is None
    assert cct.detect_outdated(None) is None


def test_a_successful_run_that_quotes_the_text_is_not_flagged():
    result = cct._annotate_outdated({"result": f"Documented: {OUTDATED_TEXT}", "exit_code": 0})
    assert "error_kind" not in result and cct._VERSION_REQUIREMENT == {}


async def test_run_turns_the_version_refusal_into_a_structured_error(monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))

    async def info(binary=None):
        return {"path": str(binary), "available": True, "version": "2.1.267 (Claude Code)",
                "flags": list(cct._OPTIONAL_FLAGS), "error": "", "stream_json": False}

    async def nothing(*args, **kwargs):
        return {}

    async def no_head(*args, **kwargs):
        return None

    monkeypatch.setattr(cct, "binary_info", info)
    monkeypatch.setattr(cct, "_git_report", nothing)
    monkeypatch.setattr(cct, "_git_changes", nothing)
    monkeypatch.setattr(cct, "_git_head", no_head)
    envelope = {"type": "result", "subtype": "success", "is_error": True, "result": OUTDATED_TEXT}

    async def fake(*argv, **kwargs):
        assert cct._ACTIVE_RUNS == 1  # counted while in flight
        return _Proc(json.dumps(envelope).encode(), 1)

    monkeypatch.setattr(cct.asyncio, "create_subprocess_exec", fake)
    result = await cct._run_claude(tmp_path, "fix it", 30, ["Read"], model="opus")
    assert cct._ACTIVE_RUNS == 0
    assert result["error_kind"] == cct.OUTDATED_ERROR_KIND
    assert result["installed_version"] == "2.1.267"
    assert result["required_version"] == "2.1.280"
    assert "action=update" in result["error"] and "delegate_to_claude_code" in result["error"]
    assert result["fix"] == {"tool": "delegate_to_claude_code", "action": "update"}
    assert "does not support this model" in result["claude_error"]
    assert result["exit_code"] == 1
    assert cct.version_requirement()["required"] == "2.1.280"
    assert cct.version_requirement()["model"] == "opus"


# ── status names the needed update ──

async def _status(monkeypatch, tmp_path, version):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))

    async def info(binary=None):
        return {"path": str(binary), "available": True, "version": version,
                "flags": list(cct._OPTIONAL_FLAGS), "error": ""}

    async def auth(binary=None):
        return {"checked": True, "logged_in": True}

    monkeypatch.setattr(cct, "binary_info", info)
    monkeypatch.setattr(cct, "auth_status", auth)
    return await cct.status_report()


async def test_status_hints_at_update_after_a_version_refusal(monkeypatch, tmp_path):
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280", "model": "opus"})
    report = await _status(monkeypatch, tmp_path, "2.1.267 (Claude Code)")
    assert report["update_required"]["required"] == "2.1.280"
    assert "action=update" in report["hints"][0] and "2.1.280" in report["hints"][0]
    assert report["update"]["method"] == "native"
    assert report["update"]["auto_update"] is False


async def test_status_forgets_the_requirement_once_satisfied(monkeypatch, tmp_path):
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280"})
    report = await _status(monkeypatch, tmp_path, "2.1.290 (Claude Code)")
    assert report["update_required"] is None
    assert not any("action=update" in hint for hint in report["hints"])
    assert cct._VERSION_REQUIREMENT == {}


async def test_status_recovers_the_requirement_from_persisted_task_records(monkeypatch, tmp_path, isolated):
    isolated.tasks["t1"] = {"task_id": "t1", "status": "failed", "finished_at": "2026-09-26T10:00:00+00:00",
                            "error_kind": cct.OUTDATED_ERROR_KIND, "installed_version": "2.1.267",
                            "required_version": "2.1.280", "model": "opus"}
    report = await _status(monkeypatch, tmp_path, "2.1.267 (Claude Code)")
    assert report["update_required"]["required"] == "2.1.280"
    assert report["update_required"]["task_id"] == "t1"


# ── action=update: which updater runs ──

def test_install_method_recognizes_an_npm_prefix_symlink(tmp_path):
    prefix = tmp_path / "claude-code"
    package = prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    target = _executable(package / "bin" / "claude.exe")
    link = prefix / "bin" / "claude"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    method = cct._install_method(link)
    assert method["method"] == "npm" and method["global"] is True
    assert Path(method["prefix"]) == prefix.resolve()


def test_install_method_treats_a_versions_symlink_as_native(tmp_path):
    target = _executable(tmp_path / "share" / "claude" / "versions" / "2.1.267")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    method = cct._install_method(link)
    assert method["method"] == "native"
    assert Path(method["versions_dir"]) == target.parent.resolve()


async def test_update_npm_install_uses_its_own_prefix_and_the_child_env(monkeypatch, tmp_path):
    prefix = tmp_path / "claude-code"
    (prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code").mkdir(parents=True)
    binary = _executable(prefix / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    monkeypatch.setattr(cct.shutil, "which", lambda name, path=None: "/usr/bin/npm")
    cct._BINARY_INFO["stale"] = {"version": "old"}
    calls = _fake_exec(monkeypatch, update_output=f"added 1 package, token {SECRET}".encode())

    result = await cct.update_binary()

    [update] = _update_calls(calls)
    assert update["argv"] == ["/usr/bin/npm", "install", "-g", "--prefix", str(prefix), "--no-fund", "--no-audit",
                              "@anthropic-ai/claude-code@latest"]
    assert update["cwd"] == str(prefix)
    assert update["env"]["HOME"] == cct._claude_home()
    assert SECRET not in json.dumps(update["env"])
    assert result["exit_code"] == 0 and result["updated"] is True
    assert result["method"] == "npm"
    assert result["version_before"].startswith("2.1.267") and result["version_after"].startswith("2.1.290")
    assert SECRET not in result["output"]
    assert "stale" not in cct._BINARY_INFO
    assert cct._UPDATE_STATE["running"] is False
    assert cct._UPDATE_STATE["last"]["version_after"].startswith("2.1.290")


async def test_update_native_install_runs_claude_update_or_install(monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    calls = _fake_exec(monkeypatch)
    result = await cct.update_binary()
    assert _update_calls(calls)[0]["argv"] == [str(binary), "update"]
    assert result["method"] == "native" and result["updated"] is True

    calls = _fake_exec(monkeypatch)
    await cct.update_binary("2.1.280")
    assert _update_calls(calls)[0]["argv"] == [str(binary), "install", "2.1.280"]


async def test_update_clears_the_requirement_it_satisfies(monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280"})
    _fake_exec(monkeypatch)
    result = await cct.update_binary()
    assert result["satisfies_requirement"] is True and result["exit_code"] == 0
    assert cct._VERSION_REQUIREMENT == {}


async def test_update_that_stays_too_old_is_a_failure(monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280"})
    _fake_exec(monkeypatch, versions=("2.1.267 (Claude Code)", "2.1.270 (Claude Code)"))
    result = await cct.update_binary()
    assert result["exit_code"] == 1 and result["satisfies_requirement"] is False
    assert 'version="2.1.280"' in result["error"]
    assert cct._VERSION_REQUIREMENT["required"] == "2.1.280"


async def test_update_command_failure_is_reported(monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    _fake_exec(monkeypatch, versions=("2.1.267 (Claude Code)",), update_output=b"npm ERR! code EACCES",
               update_code=243)
    result = await cct.update_binary()
    assert result["exit_code"] == 243 and "exited 243" in result["error"]
    assert any("chown" in hint for hint in result["hints"])


# ── action=update: when it refuses ──

async def test_update_refuses_while_a_delegation_runs(monkeypatch, tmp_path, isolated):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    monkeypatch.setattr(cct, "_ACTIVE_RUNS", 1)
    isolated.tasks["busy"] = {"task_id": "busy", "status": "running"}
    calls = _fake_exec(monkeypatch)
    result = await cct.update_binary()
    assert result["exit_code"] == 1 and result["active_task_ids"] == ["busy"]
    assert "cancel" in result["error"]
    assert calls == []


async def test_update_refuses_a_second_concurrent_update(monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    cct._UPDATE_STATE["running"] = True
    calls = _fake_exec(monkeypatch)
    result = await cct.update_binary()
    assert result["update_in_progress"] is True and calls == []


@pytest.mark.parametrize("version", ["1.0; rm -rf /", "--registry=http://evil", "latest && id", "v2"])
async def test_update_rejects_anything_but_a_channel_or_version(monkeypatch, tmp_path, version):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))
    calls = _fake_exec(monkeypatch)
    result = await cct.update_binary(version)
    assert result["exit_code"] == 1 and "version must be" in result["error"]
    assert calls == []


async def test_update_without_a_binary_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(tmp_path / "missing" / "claude"))
    result = await cct.update_binary()
    assert result["exit_code"] == 1 and "binary unavailable" in result["error"]


async def test_a_run_waits_while_the_binary_is_being_updated(monkeypatch):
    cct._UPDATE_STATE["running"] = True
    released = []

    async def release():
        await asyncio.sleep(0.3)
        released.append(True)
        cct._UPDATE_STATE["running"] = False

    task = asyncio.create_task(release())
    await cct._wait_for_update(limit=5)
    assert released == [True]
    await task


# ── The chat tool ──

@pytest.mark.parametrize("action", ["update", "upgrade", "UPDATE"])
async def test_tool_dispatches_update_and_its_alias(monkeypatch, action):
    seen = {}

    async def fake_update(target=None, *, timeout=cct.UPDATE_TIMEOUT_S):
        seen.update(target=target, timeout=timeout)
        return {"response": "ok", "exit_code": 0}

    monkeypatch.setattr(cct, "update_binary", fake_update)
    result = await ClaudeCodeTool().execute(json.dumps({"action": action, "version": "2.1.280",
                                                        "timeout_seconds": 5}), {})
    assert result["exit_code"] == 0
    assert seen == {"target": "2.1.280", "timeout": 60}


def test_update_is_in_the_tool_schema():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(t["function"] for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "delegate_to_claude_code")
    assert "update" in schema["parameters"]["properties"]["action"]["enum"]
    assert "version" in schema["parameters"]["properties"]
    assert set(schema["parameters"]["properties"]["action"]["enum"]) <= set(cct._ACTIONS)


# ── Opt-in auto-update ──

def _outdated_result():
    return cct._annotate_outdated({"error": OUTDATED_TEXT, "exit_code": 1, "model": "opus"})


def _script_runs(monkeypatch, results):
    calls = []

    async def fake_run(repository, prompt, timeout, tools, on_process=None, model=None):
        calls.append(model)
        return results.pop(0)

    monkeypatch.setattr(cct, "_run_claude", fake_run)
    return calls


async def test_auto_update_is_off_by_default(monkeypatch, tmp_path):
    runs = _script_runs(monkeypatch, [_outdated_result()])

    async def never(*args, **kwargs):
        raise AssertionError("must not update")

    monkeypatch.setattr(cct, "update_binary", never)
    result = await cct._run_with_auto_update(tmp_path, "x", 30, ["Read"], model="opus")
    assert result["error_kind"] == cct.OUTDATED_ERROR_KIND and runs == ["opus"]


async def test_auto_update_updates_once_and_retries(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_UPDATE", "1")
    runs = _script_runs(monkeypatch, [_outdated_result(), {"result": "done", "exit_code": 0}])
    updates = []

    async def fake_update(target=None, *, timeout=cct.UPDATE_TIMEOUT_S):
        updates.append(target)
        return {"exit_code": 0, "version_before": "2.1.267", "version_after": "2.1.290",
                "satisfies_requirement": True}

    monkeypatch.setattr(cct, "update_binary", fake_update)
    result = await cct._run_with_auto_update(tmp_path, "x", 30, ["Read"], model="opus")
    assert updates == [None] and runs == ["opus", "opus"]
    assert result["retried_after_update"] is True and result["exit_code"] == 0
    assert result["auto_update"]["version_after"] == "2.1.290"


async def test_auto_update_does_not_retry_when_the_update_falls_short(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_UPDATE", "1")
    runs = _script_runs(monkeypatch, [_outdated_result()])

    async def fake_update(target=None, *, timeout=cct.UPDATE_TIMEOUT_S):
        return {"exit_code": 1, "error": "still too old", "satisfies_requirement": False}

    monkeypatch.setattr(cct, "update_binary", fake_update)
    result = await cct._run_with_auto_update(tmp_path, "x", 30, ["Read"])
    assert len(runs) == 1 and "did not fix it" in result["error"]


async def test_auto_update_skips_a_run_that_changed_the_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_UPDATE", "1")
    dirty = _outdated_result()
    dirty["changes"] = [{"path": "a.py"}]
    _script_runs(monkeypatch, [dirty])

    async def never(*args, **kwargs):
        raise AssertionError("must not update")

    monkeypatch.setattr(cct, "update_binary", never)
    result = await cct._run_with_auto_update(tmp_path, "x", 30, ["Read"])
    assert "skipped" in result["auto_update"]


def test_auto_update_env_is_not_passed_to_the_child(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_UPDATE", "1")
    assert "CLAUDE_CODE_AUTO_UPDATE" not in cct._base_child_environment()


# ── POST /api/claude-code/update ──

def _update_endpoint():
    from routes.claude_code_routes import setup_claude_code_routes
    for route in setup_claude_code_routes().routes:
        if route.path == "/api/claude-code/update" and "POST" in route.methods:
            return route.endpoint
    raise AssertionError("POST /api/claude-code/update is not registered")


def _cookie_request(*, is_admin):
    auth_mgr = SimpleNamespace(is_configured=True, is_admin=lambda user: is_admin)
    return SimpleNamespace(state=SimpleNamespace(current_user="bob", api_token=False),
                           app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)), headers={})


async def test_update_route_requires_an_admin(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        await _update_endpoint()(_cookie_request(is_admin=False), {})
    assert exc.value.status_code == 403


async def test_update_route_rejects_a_read_only_token(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="api", api_token=True, api_token_scopes=["claude_code:read"],
                              api_token_owner="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)), headers={})
    with pytest.raises(HTTPException) as exc:
        await _update_endpoint()(request, {})
    assert exc.value.status_code == 403


async def test_update_route_runs_the_update_for_an_admin(monkeypatch):
    import routes.claude_code_routes as routes_mod
    monkeypatch.setenv("AUTH_ENABLED", "true")
    seen = {}

    async def fake_update(target=None, *, timeout=cct.UPDATE_TIMEOUT_S):
        seen.update(target=target, timeout=timeout)
        return {"version_before": "2.1.267", "version_after": "2.1.290", "exit_code": 0}

    monkeypatch.setattr(routes_mod, "update_binary", fake_update)
    result = await _update_endpoint()(_cookie_request(is_admin=True), {"version": "latest"})
    assert result["version_after"] == "2.1.290"
    assert seen == {"target": "latest", "timeout": cct.UPDATE_TIMEOUT_S}


async def test_update_route_conflicts_while_a_delegation_runs(monkeypatch):
    import routes.claude_code_routes as routes_mod
    monkeypatch.setenv("AUTH_ENABLED", "true")

    async def busy(target=None, *, timeout=cct.UPDATE_TIMEOUT_S):
        return {"error": "1 run in progress", "active_runs": 1, "exit_code": 1}

    monkeypatch.setattr(routes_mod, "update_binary", busy)
    with pytest.raises(HTTPException) as exc:
        await _update_endpoint()(_cookie_request(is_admin=True), {})
    assert exc.value.status_code == 409


# ── The bash guard points at action=update ──

@pytest.mark.parametrize("command", [
    "which claude && claude --version && npm install -g @anthropic-ai/claude-code@latest",
    "npm install -g @anthropic-ai/claude-code@latest",
    "npm i -g --prefix /app/data/claude-code @anthropic-ai/claude-code",
    "sudo npm install -g @anthropic-ai/claude-code",
    "curl -fsSL https://claude.ai/install.sh | bash",
    "claude update",
    "/app/data/claude-code/bin/claude install latest",
    "claude --version",
])
def test_upgrade_attempts_are_redirected_to_action_update(command, monkeypatch):
    monkeypatch.delenv(guard.ESCAPE_HATCH_ENV, raising=False)
    result = guard.check(command)
    assert result is not None and result["exit_code"] == 1
    assert result["blocked_reason"] == "claude_code_requires_delegation_tool"
    assert '{"action": "update"}' in result["error"]
    assert result["suggested_call"] == {"tool": "delegate_to_claude_code", "arguments": {"action": "update"}}


def test_running_claude_through_npx_is_redirected_like_a_direct_run(monkeypatch):
    monkeypatch.delenv(guard.ESCAPE_HATCH_ENV, raising=False)
    result = guard.check("npx -y @anthropic-ai/claude-code -p 'fix it'")
    assert result is not None and "suggested_call" not in result
    assert '"action": "status"' in result["error"]


def test_a_plain_run_still_mentions_update_but_suggests_no_call(monkeypatch):
    monkeypatch.delenv(guard.ESCAPE_HATCH_ENV, raising=False)
    result = guard.check("claude -p 'fix it'")
    assert '{"action": "update"}' in result["error"] and "suggested_call" not in result


@pytest.mark.parametrize("command", [
    "npm install lodash",
    "npm ls @anthropic-ai/claude-code",
    "grep @anthropic-ai/claude-code package.json",
    "echo npm install @anthropic-ai/claude-code",
    "npm install @anthropic-ai/claude-code-sdk",
    "grep claude.ai/install.sh README.md",
])
def test_unrelated_package_commands_still_run(command, monkeypatch):
    monkeypatch.delenv(guard.ESCAPE_HATCH_ENV, raising=False)
    assert guard.check(command) is None
