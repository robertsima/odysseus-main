"""The agent's tmux pane must not hang on a pager, a prompt or a stopped command.

On the server every agent bash call runs in a per-chat tmux pane, which is a
real terminal. On 2026-09-29 `git log` opened `less` there and the run sat in
bash until a restart; every later command in the chat was typed into the
stuck pager, and Stop could not recover it. These tests drive the runner
against a fake tmux (CI has none); the real behaviour was checked against
tmux 3.7 with the same cases.
"""
import asyncio
import re

import pytest

from src.agent_tools import subprocess_tools as st


class FakeTmux:
    """Records keystrokes; answers capture-pane with the command's markers once
    ``finish`` is set, as a pane whose command completed would."""

    def __init__(self):
        self.sent = []
        self.killed = []
        self.finish = True
        self.output = "result line"

    async def run_exec(self, *args, timeout=10):
        if args[:2] == ("tmux", "send-keys") and "-l" in args:
            self.sent.append(args[-1])
        elif args[:2] == ("tmux", "kill-session"):
            self.killed.append(args[-1])
        return "", "", 0

    async def capture(self, name):
        typed = "\n".join(self.sent)
        start = re.search(r"__ODYSSEUS_CMD_START_[^_]+__", typed)
        end = re.search(r"(__ODYSSEUS_CMD_END_[^_]+__:)", typed)
        if not (start and end and self.finish):
            return f"\n{start.group(0)}\n" if start else ""
        return f"\n{start.group(0)}\n{self.output}\n{end.group(1)}0\n"


@pytest.fixture
def tmux(monkeypatch):
    fake = FakeTmux()

    async def ensure(*_a, **_k):
        return None

    monkeypatch.setattr(st, "_run_exec", fake.run_exec)
    monkeypatch.setattr(st, "_tmux_capture", fake.capture)
    monkeypatch.setattr(st, "_ensure_tmux_session", ensure)
    monkeypatch.setattr(st, "_TMUX_LOCKS", {})
    return fake


async def _run(cmd, timeout=5):
    return await st._run_tmux_bash(cmd, session_id="chat-1", cwd="/tmp", env=None, timeout=timeout)


@pytest.mark.asyncio
async def test_commands_run_without_pagers_prompts_or_terminal_input(tmux):
    out, _, rc, timed_out = await _run("git log")

    assert (out, rc, timed_out) == ("result line", 0, False)
    typed = "\n".join(tmux.sent)
    for setting in ("PAGER=cat", "GIT_PAGER=cat", "GIT_TERMINAL_PROMPT=0", "GIT_EDITOR=true"):
        assert setting in typed
    # The command reads /dev/null, not the lines typed after it.
    assert tmux.sent[tmux.sent.index("git log") - 1] == "{"
    assert tmux.sent[tmux.sent.index("git log") + 1] == "} < /dev/null"


@pytest.mark.asyncio
async def test_output_repeating_a_line_of_the_command_is_kept(tmux):
    tmux.output = "line one"
    out, _, _, _ = await _run("cat <<'EOF'\nline one\nEOF")
    assert out == "line one"


@pytest.mark.asyncio
async def test_a_stopped_command_ends_its_pane(tmux):
    tmux.finish = False
    task = asyncio.create_task(_run("sleep 600"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tmux.killed == [st._tmux_session_name("chat-1")]


@pytest.mark.asyncio
async def test_a_timed_out_command_ends_its_pane(tmux):
    tmux.finish = False
    _, _, rc, timed_out = await _run("sleep 600", timeout=0.2)
    assert (rc, timed_out) == (124, True)
    assert tmux.killed == [st._tmux_session_name("chat-1")]


@pytest.mark.asyncio
async def test_one_command_at_a_time_per_pane(tmux, monkeypatch):
    active = 0
    peak = 0
    real_send = st._tmux_send_line

    async def counting_send(name, line):
        nonlocal active, peak
        if line.startswith("export PAGER"):
            active += 1
            peak = max(peak, active)
        await real_send(name, line)
        await asyncio.sleep(0)
        if line.startswith("printf '\\n__ODYSSEUS_CMD_END"):
            active -= 1

    monkeypatch.setattr(st, "_tmux_send_line", counting_send)
    await asyncio.gather(*(_run(f"echo {i}") for i in range(3)))
    assert peak == 1


@pytest.mark.asyncio
async def test_output_longer_than_the_pane_keeps_still_finishes(tmux, monkeypatch):
    """A pane keeps about 2000 lines. Output past that pushed the start
    marker out, the runner waited for it, and a finished command held its
    run until the hour-long timeout (a 23-minute stall on 2026-09-29)."""
    async def capture(name):
        typed = "\n".join(tmux.sent)
        end = re.search(r"(__ODYSSEUS_CMD_END_[^_]+__:)", typed)
        tail = "\n".join(str(n) for n in range(2600, 4001))
        return f"{tail}\n{end.group(1)}0\n" if end else ""

    monkeypatch.setattr(st, "_tmux_capture", capture)
    out, _, rc, timed_out = await _run("seq 1 4000", timeout=3)

    assert (rc, timed_out) == (0, False)
    lines = out.splitlines()
    assert "first lines are gone" in lines[0] and "last 1401" in lines[0]
    assert lines[1] == "2600" and lines[-1] == "4000"


def test_marker_parsing():
    start, end = "__ODYSSEUS_CMD_START_1__", "__ODYSSEUS_CMD_END_1__:"
    # Before the command has printed anything.
    assert st._output_after_marker("", start, end) == ("", False, True)
    # Running, start marker visible.
    assert st._output_after_marker(f"old\n{start}\na\nb", start, end) == ("a\nb", False, False)
    # Finished.
    assert st._output_after_marker(f"old\n{start}\na\n{end}0\n", start, end) == ("a", True, False)
    # Finished, start marker scrolled out.
    assert st._output_after_marker(f"x\ny\n{end}3\n", start, end) == ("x\ny", True, True)
    # Another command's end marker does not count.
    assert st._output_after_marker("x\n__ODYSSEUS_CMD_END_2__:0\n", start, end)[1] is False
