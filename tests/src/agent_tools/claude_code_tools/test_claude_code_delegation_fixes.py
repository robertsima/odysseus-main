"""Claude Code delegation fixes from the 2026-09-27 diagnostics bundle.

1. action=update on a native install whose configured binary is a plain copy
   (/app/data/claude-code/bin/claude): `claude update` moved
   $HOME/.local/bin/claude to 2.1.283, the configured copy stayed at 2.1.267,
   and five retries reported the same failure. The update now rebinds
   claude_code_binary to the launcher and names the stale copy.
2. `opus-5.5` reached the CLI and `gpt-6-sol` was sent to it through
   delegate_to_agent; the model is now normalised/validated before a process
   starts, for the Claude CLI path only.
3. A saved claude_code_repository_roots dropped /app/data/agent_worktrees, so
   runs on a fresh worktree were "outside Claude Code approved roots".
4. action=status on a not-ready install returned exit_code=1 and was logged as
   a failed tool call.
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_tools import claude_code_tools as cct
from src.agent_tools.claude_code_tools import ClaudeCodeTaskRunner, ClaudeCodeTool

pytestmark = pytest.mark.area_security


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """In-memory settings, a private HOME, and no leftover module state."""
    values: dict = {}

    def fake_setting(key, default=None):
        value = values.get(key)
        if value is None or value == "" or value == []:
            return default
        return value

    monkeypatch.setattr(cct, "_setting", fake_setting)
    monkeypatch.setenv("CLAUDE_CODE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CODE_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(cct, "_ACTIVE_RUNS", 0)
    monkeypatch.setattr(cct, "_UPDATE_STATE", {"running": False, "last": None})
    monkeypatch.setattr(cct, "_VERSION_REQUIREMENT", {})
    monkeypatch.setattr(cct, "_BINARY_INFO", {})
    runner = ClaudeCodeTaskRunner(store_path=str(tmp_path / "tasks.json"))
    monkeypatch.setattr(cct, "get_task_runner", lambda: runner)
    # No real worktree root unless a test sets one.
    monkeypatch.setattr("src.agent_worktree.config.load_config",
                        lambda: SimpleNamespace(worktree_root=str(tmp_path / "no-worktrees")))
    return values


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ── 1. A native update rebinds to the updater's launcher ──

@pytest.fixture
def native(monkeypatch, tmp_path, settings):
    """The production layout: a plain copy configured, the launcher in HOME."""
    configured = _executable(tmp_path / "claude-code" / "bin" / "claude")
    launcher = _executable(tmp_path / "home" / ".local" / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(configured))
    versions = {str(configured): "2.1.267 (Claude Code)", str(launcher): "2.1.283 (Claude Code)"}
    commands: list[list[str]] = []
    saved: list[Path] = []

    async def probe(binary, env):
        return versions.get(str(binary), "")

    async def run(argv, *, cwd, env, timeout):
        commands.append(list(argv))
        return 0, "Successfully updated to 2.1.283"

    def save(path):
        saved.append(Path(path))
        return None

    monkeypatch.setattr(cct, "_probe_version", probe)
    monkeypatch.setattr(cct, "_exec_capture", run)
    monkeypatch.setattr(cct, "_save_binary_setting", save)
    return SimpleNamespace(configured=configured, launcher=launcher, versions=versions,
                           commands=commands, saved=saved)


def test_install_method_names_the_native_launcher(native):
    method = cct._install_method(native.configured)
    assert method["method"] == "native"
    assert Path(method["launcher"]) == native.launcher and method["is_launcher"] is False
    assert cct._install_method(native.launcher)["is_launcher"] is True


async def test_native_update_rebinds_the_configured_copy_to_the_launcher(native, caplog):
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280"})
    with caplog.at_level(logging.INFO, logger=cct.__name__):
        result = await cct.update_binary()

    assert native.commands == [[str(native.configured), "update"]]
    assert native.saved == [native.launcher]
    assert result["exit_code"] == 0 and "error" not in result
    assert result["rebound_from"] == str(native.configured)
    assert result["rebound_to"] == str(native.launcher)
    assert result["binary"] == str(native.launcher)
    assert result["version_before"].startswith("2.1.267") and result["version_after"].startswith("2.1.283")
    assert result["updated"] is True and result["satisfies_requirement"] is True
    assert "switched" in result["response"]
    assert cct._VERSION_REQUIREMENT == {}
    # The leftover copy is named, not deleted.
    [stale] = [o for o in result["other_installs"] if o["path"] == str(native.configured)]
    assert stale["stale"] is True and stale["version"].startswith("2.1.267")
    assert any("Left in place" in hint and str(native.configured) in hint for hint in result["hints"])
    assert native.configured.exists()
    assert cct._UPDATE_STATE["last"]["rebound_to"] == str(native.launcher)
    logged = caplog.text
    assert "method=native command=claude update" in logged
    assert "rebound claude_code_binary" in logged


async def test_rebind_without_a_recorded_requirement(native):
    result = await cct.update_binary()
    assert result["rebound_to"] == str(native.launcher) and result["exit_code"] == 0


async def test_a_failed_rebind_says_to_set_the_setting_not_to_update_again(native, monkeypatch):
    monkeypatch.setattr(cct, "_save_binary_setting", lambda path: "PermissionError: read-only")
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280"})
    result = await cct.update_binary()
    assert "rebound_to" not in result
    assert result["exit_code"] == 1
    assert "claude_code_binary" in result["error"] and str(native.launcher) in result["error"]
    assert 'version="2.1.280"' not in result["error"]
    assert any("read-only" in hint and "claude_code_binary" in hint for hint in result["hints"])


async def test_a_launcher_still_too_old_suggests_latest_not_the_minimum(native):
    native.versions[str(native.launcher)] = "2.1.270 (Claude Code)"
    cct._VERSION_REQUIREMENT.update({"installed": "2.1.267", "required": "2.1.280"})
    result = await cct.update_binary()
    assert native.saved == [] and "rebound_to" not in result
    assert result["exit_code"] == 1
    assert 'version="latest"' in result["error"]
    # Suggesting the exact minimum got it run as `claude install 2.1.280`,
    # which moves the shared launcher back down.
    assert 'version="2.1.280"' not in result["error"]
    assert result["native_launcher"] == {"path": str(native.launcher), "version": "2.1.270 (Claude Code)"}


async def test_no_rebind_when_the_configured_binary_is_the_launcher(native, monkeypatch):
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(native.launcher))
    native.versions[str(native.launcher)] = "2.1.267 (Claude Code)"
    result = await cct.update_binary()
    assert native.saved == [] and "rebound_to" not in result
    assert native.commands == [[str(native.launcher), "update"]]
    # The old image copy is not the configured binary either, but it is not
    # DEFAULT_BINARY here, so nothing is reported for it.
    assert "native_launcher" not in result


async def test_no_rebind_when_the_configured_binary_itself_moved(native, monkeypatch):
    probes = iter(["2.1.267 (Claude Code)", "2.1.290 (Claude Code)"])

    async def probe(binary, env):
        if str(binary) == str(native.configured):
            return next(probes, "2.1.290 (Claude Code)")
        return native.versions[str(binary)]

    monkeypatch.setattr(cct, "_probe_version", probe)
    result = await cct.update_binary()
    assert native.saved == [] and "rebound_to" not in result
    assert result["version_after"].startswith("2.1.290") and result["exit_code"] == 0


# ── 2. Model names for the local Claude CLI ──

@pytest.mark.parametrize("given, expected", [
    ("opus-5.5", "claude-opus-5-5"),
    ("Opus 5.5", "claude-opus-5-5"),
    ("claude opus 5.5", "claude-opus-5-5"),
    ("claude-opus-5.5", "claude-opus-5-5"),
    ("claude-opus-5-5", "claude-opus-5-5"),
    ("sonnet-5", "claude-sonnet-5"),
    ("Sonnet 5", "claude-sonnet-5"),
    ("fable 5.1", "claude-fable-5-1"),
    ("haiku 4.5", "claude-haiku-4-5-20251001"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ("Opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
    ("fable", "fable"),
    ("opusplan", "opusplan"),
    ("best", "best"),
    ("sonnet[1m]", "sonnet[1m]"),
    ("opus-5.5[1m]", "claude-opus-5-5[1m]"),
    ("claude-opus-5-5[1m]", "claude-opus-5-5[1m]"),
    ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20241022"),
    ("claude-haiku-4-5@20251001", "claude-haiku-4-5@20251001"),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    ("default", None),
    ("", None),
])
def test_claude_model_names_are_normalised(given, expected):
    assert cct.normalize_claude_model(given) == (expected, None)


@pytest.mark.parametrize("given", ["gpt-6-sol", "gpt-5", "GPT-4o", "o3", "o4-mini", "gemini-2.5-pro",
                                   "llama3", "deepseek-coder"])
def test_other_providers_models_are_rejected_by_name(given):
    model, error = cct.normalize_claude_model(given)
    assert model is None
    assert "not a Claude model" in error and "another provider" in error
    assert "claude-opus-5-5" in error and "opus" in error and "sonnet" in error


@pytest.mark.parametrize("given", ["sonnet; rm -rf /", "opus 5.5 && id", "--help", "my-model", "default[1m]"])
def test_malformed_models_are_rejected(given):
    model, error = cct.normalize_claude_model(given)
    assert model is None and error and "claude-sonnet-5" in error


@pytest.fixture
def repo(monkeypatch, tmp_path, settings):
    dev = tmp_path / "development"
    checkout = dev / "project"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
    monkeypatch.setattr(cct, "DEFAULT_ROOTS", (str(dev),))
    monkeypatch.setattr(cct, "DEFAULT_REPOSITORY", "")
    monkeypatch.delenv("ODYSSEUS_AGENT_SOURCE_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ODYSSEUS_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", raising=False)
    return checkout


def test_parse_args_normalises_and_reports_the_model(repo):
    parsed = cct._parse_args({"repository": str(repo), "prompt": "x", "model": "opus-5.5"})
    assert parsed["model"] == "claude-opus-5-5"
    assert "claude-opus-5-5" in parsed["model_note"]
    assert cct._with_dropped({}, parsed)["model_note"] == parsed["model_note"]
    assert "model_note" not in cct._parse_args({"repository": str(repo), "prompt": "x", "model": "sonnet"})


def test_parse_args_rejects_a_foreign_model_under_the_called_name(repo):
    parsed = cct._parse_args({"repository": str(repo), "prompt": "x", "model": "gpt-6-sol"}, "delegate_to_agent")
    assert parsed["exit_code"] == 1 and parsed["error_kind"] == "invalid_model"
    assert parsed["error"].startswith("delegate_to_agent: ")
    assert "gpt-6-sol" in parsed["error"] and "claude-sonnet-5" in parsed["error"]


def test_a_bad_configured_default_model_names_the_setting(repo, settings):
    settings["claude_code_model"] = "gpt-5"
    parsed = cct._parse_args({"repository": str(repo), "prompt": "x"})
    assert "claude_code_model setting" in parsed["error"]
    settings["claude_code_model"] = "Opus 5.5"
    assert cct._parse_args({"repository": str(repo), "prompt": "x"})["model"] == "claude-opus-5-5"


async def test_a_foreign_model_never_starts_a_process(repo, monkeypatch):
    async def no_process(*args, **kwargs):
        raise AssertionError("no process may start for an invalid model")

    monkeypatch.setattr(cct.asyncio, "create_subprocess_exec", no_process)
    monkeypatch.setattr(cct, "_run_with_auto_update", no_process)
    for action in ("run", "start"):
        out = await ClaudeCodeTool().execute(
            json.dumps({"action": action, "repository": str(repo), "prompt": "x", "model": "gpt-6-sol"}),
            {"tool_name": "delegate_to_agent"})
        assert out["exit_code"] == 1 and out["error_kind"] == "invalid_model"


async def test_delegate_to_agent_leaves_models_alone_for_a_non_claude_provider(monkeypatch):
    """Only the Claude CLI path validates: a remote MCP coding agent takes its
    own model names."""
    from src import delegation
    from src.agent_tools.delegation_tools import DelegationTool

    seen = {}

    class Remote:
        id, title = "mcp", "Remote coding agent"

        async def delegate(self, request, ctx):
            seen.update(request)
            return {"status": "completed", "result": "ok", "exit_code": 0}

    monkeypatch.setattr(delegation, "selection", lambda *a, **k: (Remote(), "configured"))
    out = await DelegationTool().execute(json.dumps({"prompt": "x", "model": "gpt-6-sol"}), {})
    assert seen["model"] == "gpt-6-sol" and out["exit_code"] == 0


def test_tool_schemas_describe_the_model_per_provider():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    by_name = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS if s.get("type") == "function"}
    agent = by_name["delegate_to_agent"]["parameters"]["properties"]["model"]["description"]
    claude = by_name["delegate_to_claude_code"]["parameters"]["properties"]["model"]["description"]
    assert "provider-specific" in agent and "claude-opus-5-5" in agent
    assert "claude-opus-5-5" in claude and "gpt" in claude


# ── 3. The managed worktree root is always approved ──

@pytest.fixture
def worktrees(monkeypatch, tmp_path, repo):
    root = tmp_path / "agent_worktrees"
    gitdir = repo / ".git" / "worktrees" / "rag-fix"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/agent/odysseus/rag-fix\n", encoding="utf-8")
    linked = root / "rag-fix"
    linked.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    monkeypatch.setattr("src.agent_worktree.config.load_config",
                        lambda: SimpleNamespace(worktree_root=str(root)))
    return SimpleNamespace(root=root, linked=linked)


def test_custom_roots_keep_the_worktree_root(settings, repo, worktrees):
    settings["claude_code_repository_roots"] = [str(repo.parent)]
    roots = cct.repository_roots()
    assert roots == (repo.parent.resolve(), worktrees.root.resolve())
    assert cct._approved_repository(str(worktrees.linked)) == worktrees.linked.resolve()
    listed = {r["path"]: r for r in cct.discover_repositories()}
    assert listed[str(worktrees.linked.resolve())]["kind"] == "worktree"
    assert listed[str(worktrees.linked.resolve())]["branch"] == "agent/odysseus/rag-fix"
    assert listed[str(repo.resolve())]["kind"] == "checkout"


def test_the_worktree_root_is_not_listed_twice(settings, repo, worktrees):
    settings["claude_code_repository_roots"] = [str(repo.parent), str(worktrees.root)]
    assert cct.repository_roots().count(worktrees.root.resolve()) == 1


def test_a_missing_worktree_root_is_not_added(settings, repo):
    settings["claude_code_repository_roots"] = [str(repo.parent)]
    assert cct.repository_roots() == (repo.parent.resolve(),)


def test_other_paths_stay_outside(settings, repo, worktrees, tmp_path):
    settings["claude_code_repository_roots"] = [str(repo.parent)]
    outside = tmp_path / "elsewhere"
    (outside / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="outside Claude Code approved roots"):
        cct._approved_repository(str(outside))


# ── 4. A "not ready" status is a successful report ──

async def test_status_not_ready_is_not_a_tool_failure(repo, monkeypatch, tmp_path):
    binary = _executable(tmp_path / "bin" / "claude")
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))

    async def info(binary=None):
        return {"path": str(binary), "available": True, "version": "2.1.283 (Claude Code)",
                "flags": list(cct._OPTIONAL_FLAGS), "error": ""}

    async def signed_out(binary=None):
        return {"checked": True, "logged_in": False}

    monkeypatch.setattr(cct, "binary_info", info)
    monkeypatch.setattr(cct, "auth_status", signed_out)
    monkeypatch.setattr(cct, "_cloud_repositories", lambda: [])
    report = await ClaudeCodeTool().execute(json.dumps({"action": "status"}), {})
    assert report["ready"] is False and report["exit_code"] == 0
    assert "not ready" in report["response"]
