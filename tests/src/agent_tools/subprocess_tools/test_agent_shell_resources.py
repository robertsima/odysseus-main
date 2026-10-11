"""An agent's shell command must not be able to take the app down with it.

On 2026-10-10 an in-app worker ran a test command through `bash` twice. The
container ran out of memory, the kernel killed uvicorn (PID 1) instead of the
test processes, and every running agent run died with it. These tests cover the
parts that decide that: which process the OOM killer prefers, whether a timeout
leaves grandchildren running, and how much of a noisy command the app keeps.
"""
import asyncio
import os
import re
import sys
import time

import pytest

from src import shell_sandbox
from src.agent_tools import subprocess_tools as st

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="oom_score_adj is a Linux /proc interface")

HOST_CTX = {"allow_private": True}  # the unsandboxed path, as a direct caller takes it


@pytest.fixture(autouse=True)
def workdir(monkeypatch, tmp_path):
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))
    return tmp_path


def _alive(pid: int) -> bool:
    """Running, not just a zombie nobody has reaped (a container without an init)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


# -- the OOM killer picks agent processes first ------------------------------

@linux_only
async def test_bash_command_runs_with_the_highest_oom_score():
    """Without it the OOM killer can pick uvicorn (PID 1) over the test run that filled memory."""
    result = await st.BashTool().execute("cat /proc/self/oom_score_adj", dict(HOST_CTX))
    assert result["output"].strip() == "1000"


@linux_only
async def test_python_tool_runs_with_the_highest_oom_score():
    """The python tool spawns like bash does; a gap there leaves one way to exhaust memory."""
    code = "print(open('/proc/self/oom_score_adj').read().strip())"
    result = await st.PythonTool().execute(code, dict(HOST_CTX))
    assert result["output"].strip() == "1000"


@linux_only
async def test_children_of_the_command_inherit_the_oom_score():
    """pytest workers are grandchildren of the shell; the score has to reach them."""
    result = await st.BashTool().execute("sh -c 'cat /proc/self/oom_score_adj'", dict(HOST_CTX))
    assert result["output"].strip() == "1000"


@posix_only
async def test_sandboxed_launch_is_wrapped_and_lets_bwrap_keep_its_own_session(monkeypatch, tmp_path):
    """bwrap's --new-session fails (setsid EPERM) in a process that already leads a group."""
    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"], seen["kwargs"] = list(argv), kwargs
        raise OSError("stop here")

    monkeypatch.setattr(st, "_sandbox_for", lambda ctx: (str(tmp_path), None))
    monkeypatch.setattr(st.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shell_sandbox, "_procfs_mode", lambda: True)
    await st.BashTool().execute("true", {"allow_private": False})
    argv = seen["argv"]
    if sys.platform.startswith("linux"):
        assert argv[:2] == ["/bin/sh", "-c"] and "oom_score_adj" in argv[2]
    assert "--new-session" not in argv
    assert seen["kwargs"]["start_new_session"] is True


@posix_only
async def test_unsandboxed_pane_is_marked_as_an_agent_shell(monkeypatch):
    """The persistent pane is the server shell; it must carry the same test-worker bound."""
    calls = []

    async def run_exec(*args, timeout=10):
        calls.append(args)
        return "", "", 0

    started = iter([False, True, True])

    async def has_session(name):
        return next(started, True)

    monkeypatch.setattr(st, "_run_exec", run_exec)
    monkeypatch.setattr(st, "_tmux_has_session", has_session)
    await st._ensure_tmux_session("pane", os.getcwd(), None, None)
    new = next(c for c in calls if c[:2] == ("tmux", "new-session"))
    assert "ODYSSEUS_AGENT_SHELL=1" in new
    if sys.platform.startswith("linux"):
        assert any("oom_score_adj" in part for part in new)


# -- a timeout or a cancel ends the whole process group ----------------------

GRANDCHILD = "sleep 300 & echo $! > {pidfile}; wait"


@posix_only
async def test_timeout_kills_grandchildren(monkeypatch, tmp_path):
    """pytest-xdist workers outlived a timed-out `pytest` and kept using the container's memory."""
    pidfile = tmp_path / "pid"
    monkeypatch.setattr(st, "DEFAULT_BASH_TIMEOUT", 1)
    result = await st.BashTool().execute(GRANDCHILD.format(pidfile=pidfile), dict(HOST_CTX))
    pid = int(pidfile.read_text())
    try:
        assert result["exit_code"] == 124
        assert await _gone(pid), "the grandchild survived the timeout"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


@posix_only
async def test_cancel_kills_grandchildren(tmp_path):
    """Stop on a run must not leave its test workers running."""
    pidfile = tmp_path / "pid"
    task = asyncio.create_task(
        st.BashTool().execute(GRANDCHILD.format(pidfile=pidfile), dict(HOST_CTX)))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _gone(pid), "the grandchild survived the cancel"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


@posix_only
async def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(monkeypatch, tmp_path):
    """SIGTERM first lets pytest clean up; a worker that ignores it still has to die."""
    pidfile = tmp_path / "pid"
    monkeypatch.setattr(st, "DEFAULT_BASH_TIMEOUT", 1)
    monkeypatch.setattr(st, "KILL_GRACE_S", 0.3)
    command = f"trap '' TERM; sleep 300 & echo $! > {pidfile}; while true; do sleep 1; done"
    result = await st.BashTool().execute(command, dict(HOST_CTX))
    pid = int(pidfile.read_text())
    try:
        assert result["exit_code"] == 124
        assert await _gone(pid), "SIGKILL never followed the ignored SIGTERM"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


# -- output kept in memory is bounded ----------------------------------------

async def _stream(script: str):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return await st._run_subprocess_streaming(proc, timeout=60)


async def test_small_output_comes_back_whole():
    """The cap must not change what an ordinary command returns."""
    out, err, rc, stopped = await _stream("import sys; sys.stdout.buffer.write(b'a\\nb\\n')")
    assert (out, err, rc, stopped) == ("a\nb", "", 0, "")


async def test_a_command_printing_far_more_than_the_cap_keeps_head_and_tail_only():
    """A test run printing gigabytes grew the app's own memory without limit."""
    total = 300_000
    script = ("import sys; sys.stdout.buffer.write("
              f"''.join(f'line {{i}}\\n' for i in range(1, {total + 1})).encode())")
    out, _, rc, _ = await _stream(script)
    cap = st.OUTPUT_KEEP_HEAD_CHARS + st.OUTPUT_KEEP_TAIL_CHARS
    assert rc == 0
    assert len(out) < cap + 1000, "the retained buffer is over the cap"
    lines = out.splitlines()
    assert "line 1" in lines[:3]
    assert lines[-1] == f"line {total}"
    marker = next(ln for ln in lines if "lines dropped" in ln)
    dropped = int(next(tok for tok in marker.replace("(", " ").split() if tok.isdigit()))
    kept = sum(1 for ln in lines if ln.startswith("line "))
    assert kept + dropped == total


@posix_only
async def test_the_model_sees_head_tail_and_the_dropped_count_of_a_flood():
    """End to end: the dropped-lines note must survive the middle cut the tool result gets."""
    total = 300_000
    script = f"for i in range(1, {total + 1}): print('line', i)"
    command = f"{sys.executable} -c \"{script}\""
    result = await st.BashTool().execute(command, dict(HOST_CTX))
    text = result["output"]
    assert "line 1\n" in text
    assert text.rstrip().endswith(f"line {total}")
    assert re.search(r"\d+ lines in the middle were dropped", text)
    assert len(text) < st.MAX_OUTPUT_CHARS + 1000
