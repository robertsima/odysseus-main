"""bash/python run confined to the workspace when the chat has no private grant."""

import json
import os
import shutil
import subprocess
import sys
from collections import namedtuple

import pytest

from src import shell_sandbox as sb

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


@pytest.fixture(autouse=True)
def fresh_probe():
    sb._reset_for_tests()
    yield
    sb._reset_for_tests()


def _flag_pairs(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def test_argv_binds_only_the_workspace_and_the_system(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "network_enabled", lambda: True)
    ws = str(tmp_path)
    argv = sb.build_argv(["/bin/bash", "-c", "ls"], workspace=ws,
                         env={"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-secret", "GIT_CONFIG_COUNT": "1"})
    assert argv[-4:] == ["--", "/bin/bash", "-c", "ls"]
    assert "--unshare-all" in argv and "--share-net" in argv
    binds = _flag_pairs(argv, "--bind")
    assert binds == [(os.path.realpath(ws), os.path.realpath(ws))]
    assert ("--chdir" in argv) and argv[argv.index("--chdir") + 1] == os.path.realpath(ws)
    ro = [src for src, _ in _flag_pairs(argv, "--ro-bind")]
    assert not any(src == "/app" or src.startswith("/app/") for src in ro)
    # bwrap itself starts from `env -i` with the allowlist: it is PID 1 inside,
    # so its own environment must not hold secrets either.
    assert os.path.basename(argv[0]).lower().startswith("env") and argv[1] == "-i"
    bwrap_at = next(i for i, a in enumerate(argv) if os.path.basename(a).startswith("bwrap"))
    env = dict(a.split("=", 1) for a in argv[2:bwrap_at])
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "/usr/bin" and env["GIT_CONFIG_COUNT"] == "1" and env["HOME"] == sb.SANDBOX_HOME


def test_network_off_drops_share_net(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "network_enabled", lambda: False)
    argv = sb.build_argv(["true"], workspace=str(tmp_path))
    assert "--share-net" not in argv and "--unshare-all" in argv


def test_turned_off_in_settings(monkeypatch):
    monkeypatch.setattr(sb, "enabled", lambda: False)
    state = sb.status()
    assert state["available"] is False and "off" in state["reason"]


def test_missing_bwrap_is_reported(monkeypatch):
    monkeypatch.setattr(sb, "enabled", lambda: True)
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setattr(sb.os, "name", "posix")
    assert "not installed" in sb.status(refresh=True)["reason"]


# ── which workspaces may be handed to the sandbox ─────────────────────────

@pytest.fixture
def layout(tmp_path, monkeypatch):
    from src import constants
    import src.rag_sensitivity as rs
    import src.tool_execution as te

    data = tmp_path / "data"
    for sub in ("personal_docs/Journal", "development/repo", "agent_workspace", "uploads"):
        (data / sub).mkdir(parents=True)
    outside = tmp_path / "work" / "proj"
    outside.mkdir(parents=True)
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setattr(constants, "PERSONAL_DIR", str(data / "personal_docs"))
    monkeypatch.setattr(constants, "AGENT_WORKSPACE_DIR", str(data / "agent_workspace"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(data / "personal_docs"))
    monkeypatch.setattr(te, "_repository_data_subdirs", lambda d: (str((data / "development").resolve()),))
    return data, outside


def test_workspace_rules(layout):
    data, outside = layout
    assert sb.workspace_problem(str(outside)) is None
    assert sb.workspace_problem(str(data / "development" / "repo")) is None
    assert sb.workspace_problem(str(data / "agent_workspace")) is None
    assert "contains the app's data" in sb.workspace_problem(str(data.parent))
    assert "vault" in sb.workspace_problem(str(data / "personal_docs" / "Journal"))
    assert "inside the app's data" in sb.workspace_problem(str(data / "uploads"))
    assert sb.workspace_problem(None) == "no workspace is set"


# ── the dispatcher runs the shell sandboxed instead of refusing it ───────

@pytest.fixture
def captured_exec(monkeypatch):
    import src.tool_execution as te
    from src.agent_tools import subprocess_tools as st

    # bash/python are admin-only; pin the owner as admin (the check fails
    # closed without an auth manager) so the private-grant gate is under test.
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: True)

    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return object()

    async def fake_stream(proc, **kwargs):
        return "hello", "", 0, False

    monkeypatch.setattr(st.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(st, "_run_subprocess_streaming", fake_stream)
    return seen


async def _run(tool, content, workspace):
    import src.tool_execution as te

    return await te.execute_tool_block(
        ToolBlock(tool, content), session_id=None, owner=None, workspace=workspace,
        security_context=te.NO_TOOL_SECURITY_CONTEXT, allow_private=False,
    )


@pytest.mark.parametrize("tool,content", [("bash", "ls -la"), ("python", "print(1)")])
async def test_without_the_grant_the_shell_runs_sandboxed(tool, content, tmp_path, monkeypatch, captured_exec):
    monkeypatch.setattr(sb, "unavailable_reason", lambda ws: "")
    monkeypatch.setattr(sb, "network_enabled", lambda: True)
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_mcp_manager", lambda: None)

    _, result = await _run(tool, content, str(tmp_path))
    assert result.get("exit_code") == 0, result
    argv = captured_exec["argv"]
    assert argv[:2] == [argv[0], "-i"] and any(os.path.basename(a).startswith("bwrap") for a in argv)
    assert _flag_pairs(argv, "--bind") == [(os.path.realpath(str(tmp_path)),) * 2]


async def test_without_the_grant_or_a_sandbox_the_shell_is_refused_with_why(tmp_path, monkeypatch, captured_exec):
    monkeypatch.setattr(sb, "unavailable_reason", lambda ws: "bubblewrap (bwrap) is not installed")
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_mcp_manager", lambda: None)

    _, result = await _run("bash", "ls", str(tmp_path))
    assert result["blocked"] is True
    assert "bubblewrap (bwrap) is not installed" in result["error"]
    assert "argv" not in captured_exec


# ── background jobs ───────────────────────────────────────────────────────

def test_background_job_command_runs_inside_the_sandbox(tmp_path, monkeypatch):
    from src import bg_jobs

    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(bg_jobs, "_load", lambda: {})
    monkeypatch.setattr(bg_jobs, "_save", lambda jobs: None)

    class _Proc:
        pid = 4242

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **k: _Proc())
    ws = tmp_path / "ws"
    ws.mkdir()
    rec = bg_jobs.launch("make test", session_id="s1", cwd=str(ws), sandbox_workspace=str(ws))
    script = (tmp_path / "jobs" / f"{rec['id']}.sh").read_text()
    assert "bwrap" in script and " -i " in script
    assert "/tmp/.odysseus-job.sh" in script
    # The log and exit files are written by the wrapper, outside the sandbox.
    assert script.splitlines()[-1].startswith("echo $? > ")


# ── the loop offers the shell when it can be sandboxed ────────────────────

async def test_loop_offers_sandboxed_shell_and_says_where_it_runs(tmp_path, monkeypatch):
    import src.agent_loop as al

    sent = {}

    async def fake_stream(_candidates, messages, **kwargs):
        sent["tools"] = [t.get("function", {}).get("name") for t in (kwargs.get("tools") or [])]
        sent["messages"] = messages
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(sb, "unavailable_reason", lambda ws: "")
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    [e async for e in al.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o", [{"role": "user", "content": "run the tests with bash"}],
        relevant_tools={"bash", "read_file"}, max_rounds=1, allow_private=False, workspace=str(tmp_path),
    )]
    assert "bash" in sent["tools"]
    assert any("sandbox that contains only the workspace" in str(m.get("content")) for m in sent["messages"])


# ── the real thing, where it can run ─────────────────────────────────────

@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="needs Linux with bubblewrap")
def test_real_sandbox_hides_everything_but_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "network_enabled", lambda: False)
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "private.md").write_text("private")
    ws = tmp_path / "ws"
    ws.mkdir()
    script = (f'test ! -e "{secret_dir}/private.md" && echo hidden; '
              'echo "key=${ODY_TEST_SECRET:-none}"; '
              'echo "pid1=$(grep -a -c ODY_TEST_SECRET /proc/1/environ 2>/dev/null || echo 0)"; '
              'touch made.txt && echo wrote')
    env = dict(os.environ, ODY_TEST_SECRET="sk-should-not-leak")
    done = subprocess.run(sb.build_argv(["/bin/sh", "-c", script], workspace=str(ws), env=env),
                          capture_output=True, text=True, timeout=20, env=env)
    if done.returncode != 0 and "namespace" in (done.stderr or "").lower():
        pytest.skip("user namespaces are not permitted here: " + done.stderr.strip())
    assert "hidden" in done.stdout
    assert "key=none" in done.stdout
    assert "pid1=0" in done.stdout
    assert "wrote" in done.stdout and (ws / "made.txt").exists()
