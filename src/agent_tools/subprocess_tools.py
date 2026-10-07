import asyncio
import logging
import os
import re
import shutil
import sys
import time
import collections
from typing import Any, Optional, Callable, Awaitable, Tuple, Dict
from core.platform_compat import IS_WINDOWS, find_bash
from src.constants import SHELL_OUTPUT_CHARS as MAX_OUTPUT_CHARS

DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour, for a command that keeps printing
DEFAULT_PYTHON_TIMEOUT = 60 * 60
# A bash command that prints nothing for this long is stopped (setting
# bash_idle_timeout_seconds; a call may pass `idle_timeout`). On 2026-09-30 a
# `./mvnw -q test` whose integration test hung printed nothing for 43 minutes
# and the agent waited, on its way to the hour limit, until the user stopped
# the turn. Quiet commands meant to run long go in the background (`#!bg`).
DEFAULT_BASH_IDLE_TIMEOUT = 60

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12
TMUX_CAPTURE_LINES = 2000

# Set before every command in an agent's tmux pane. The pane is a real
# terminal, so `git log` opened `less`, a git remote asked for a username and
# `git commit` opened an editor, each waiting for a key no one would press. On
# 2026-09-29 two runs sat in bash until a restart this way, and each later
# command in the chat was typed into the stuck pager. Empty prompts keep them
# out of the captured output, and without line editing bash stops echoing the
# typed command (so output that repeats a line of it, a heredoc's, survives
# the cleanup). (The pipe path never pages or prompts.)
_TMUX_NONINTERACTIVE = (
    "export PAGER=cat GIT_PAGER=cat MANPAGER=cat SYSTEMD_PAGER=cat "
    "GIT_TERMINAL_PROMPT=0 GIT_EDITOR=true DEBIAN_FRONTEND=noninteractive; "
    "PS1= PS2=; set +o emacs +o vi"
)
# One command at a time per pane: parallel tool calls typed into the same pane
# interleave, and a command reading its terminal swallows the next one's lines.
_TMUX_LOCKS: Dict[str, asyncio.Lock] = {}


async def _create_bash_subprocess(command: str, **kwargs):
    """Start the agent shell with Bash semantics on every supported OS.

    ``asyncio.create_subprocess_shell`` delegates to ``cmd.exe`` on native
    Windows.  That contradicts the Bash tool contract and makes POSIX commands
    such as ``pwd``, ``ls -la``, and ``cat`` unreliable even when the launcher
    has found Git Bash.  Pass the selected workspace as a structural ``cwd``
    argument; Git Bash inherits that native Windows directory and exposes it
    using its normal ``/c/...`` representation.
    """
    if IS_WINDOWS:
        bash = find_bash()
        if not bash:
            raise RuntimeError(
                "Git Bash is required for the Bash tool on Windows; "
                "install Git for Windows and restart Agamemnon"
            )
        return await asyncio.create_subprocess_exec(bash, "-c", command, **kwargs)
    return await asyncio.create_subprocess_shell(command, **kwargs)


def _tmux_session_name(session_id: Optional[str], sandbox_workspace: Optional[str] = None) -> str:
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "default")).strip("-")
    name = f"ody-agent-{raw[:80] or 'default'}"
    if sandbox_workspace:
        # A sandboxed pane is bound to one workspace; a different workspace (or
        # an unsandboxed run after the grant is switched on) gets its own pane.
        import hashlib

        # The pane's bwrap binds are fixed when it starts, so the managed
        # worktrees bound next to the workspace (shell_sandbox.workspace_worktrees)
        # are part of its identity: a worktree created after the pane opened
        # gets a new pane that can see it, instead of "No such file or directory".
        try:
            from src.shell_sandbox import workspace_worktrees

            bound = "|".join(sorted(workspace_worktrees(sandbox_workspace)))
        except Exception:
            bound = ""
        key = sandbox_workspace + ("|" + bound if bound else "")
        name += "-sbx-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return name


async def _run_exec(*args: str, timeout: float = 10) -> Tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "timeout", 124
    return (
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


async def _tmux_has_session(name: str) -> bool:
    _, _, rc = await _run_exec("tmux", "has-session", "-t", name, timeout=3)
    return rc == 0


async def _tmux_capture(name: str) -> str:
    out, _, _ = await _run_exec(
        "tmux", "capture-pane", "-p", "-J", "-S", f"-{TMUX_CAPTURE_LINES}", "-t", name,
        timeout=5,
    )
    return out


async def _tmux_send_line(name: str, line: str) -> None:
    if line:
        await _run_exec("tmux", "send-keys", "-t", name, "-l", line, timeout=5)
    await _run_exec("tmux", "send-keys", "-t", name, "C-m", timeout=5)


async def _ensure_tmux_session(name: str, cwd: str, env: Optional[dict],
                               sandbox_workspace: Optional[str] = None) -> None:
    if await _tmux_has_session(name):
        await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)
        return
    if sandbox_workspace:
        # The pane's shell is the sandbox itself, so every command sent to it
        # runs confined. No --new-session: the shell needs tmux's terminal.
        from src import shell_sandbox

        pane = shell_sandbox.build_argv(["/bin/bash", "--noprofile", "--norc"],
                                        workspace=sandbox_workspace, env=env, new_session=False,
                                        package_cache=True,
                                        extra_ro_binds=_attachment_binds())
    else:
        # The full server shell: the same toolchain choice as the sandbox
        # (src/toolchains.py), from the folder it starts in.
        try:
            from src.toolchains import shell_env

            tool_env = shell_env(cwd, (env or os.environ).get("PATH"))
        except Exception:  # noqa: BLE001
            tool_env = {}
        pane = [
            "env",
            f"TERM={env.get('TERM', 'xterm-256color') if env else 'xterm-256color'}",
            f"COLUMNS={env.get('COLUMNS', '120') if env else '120'}",
            f"LINES={env.get('LINES', '40') if env else '40'}",
            *(f"{key}={value}" for key, value in tool_env.items()),
            "/bin/bash",
            "--noprofile",
            "--norc",
        ]
    await _run_exec("tmux", "new-session", "-d", "-s", name, "-c", cwd, *pane, timeout=10)
    if not await _tmux_has_session(name):
        raise RuntimeError(f"failed to create tmux session {name}")
    await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)


def _output_after_marker(capture: str, start_marker: str, end_marker: str) -> Tuple[str, bool, bool]:
    """``(output, finished, clipped)`` of one command in a pane capture.

    The end marker alone says the command finished: its stamp is unique and
    it is printed last. The start marker can be gone, since a pane keeps
    about 2000 lines and a command that printed more pushes it out; the
    output is then everything before the end marker and ``clipped`` is set.
    Waiting for the start marker instead held a finished command until the
    hour-long timeout: on 2026-09-29 a run sat 23 minutes on one bash call
    whose output had scrolled past it.
    """
    lines = capture.splitlines()
    start_idx = -1
    for idx, line in enumerate(lines):
        if line.strip() == start_marker:
            start_idx = idx
    end_idx = -1
    for idx in range(start_idx + 1, len(lines)):
        if lines[idx].strip().startswith(end_marker):
            end_idx = idx
    clipped = start_idx < 0
    if end_idx < 0:
        return "\n".join(lines[start_idx + 1:]), False, clipped
    return "\n".join(lines[start_idx + 1:end_idx]), True, clipped


def _clipped_output_note(output: str) -> str:
    kept = len(output.splitlines())
    return (f"[Agamemnon] The command printed more than the terminal keeps: its first lines are "
            f"gone and the last {kept} are below. To see all of it, run it again with the output "
            f"sent to a file (`... > /tmp/out.log 2>&1`) and read or grep that file.\n")


def _extract_marker_rc(capture: str, end_marker: str) -> int:
    for line in reversed(capture.splitlines()):
        stripped = line.strip()
        if stripped.startswith(end_marker):
            suffix = stripped[len(end_marker):].strip()
            if suffix.isdigit():
                return int(suffix)
    return 0


async def _run_tmux_bash(
    content: str,
    *,
    session_id: str,
    cwd: str,
    env: Optional[dict],
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    sandbox_workspace: Optional[str] = None,
    idle_timeout: Optional[float] = None,
) -> Tuple[str, str, Optional[int], str]:
    """``(output, stderr, exit code, stopped)``; ``stopped`` is "" when the
    command finished, "timeout" past ``timeout``, "idle" after
    ``idle_timeout`` seconds without new output."""
    name = _tmux_session_name(session_id, sandbox_workspace)
    lock = _TMUX_LOCKS.setdefault(name, asyncio.Lock())
    async with lock:
        return await _run_tmux_bash_locked(name, content, cwd=cwd, env=env, timeout=timeout,
                                           progress_cb=progress_cb, sandbox_workspace=sandbox_workspace,
                                           idle_timeout=idle_timeout)


async def _kill_tmux_session(name: str) -> None:
    """End a pane whose command was stopped or timed out.

    Ctrl-C does not leave a pager, an editor or a prompt, and whatever is
    left running there receives the next command's keystrokes. A new pane
    loses the shell's cwd and variables; that beats hanging every later call.
    """
    try:
        await _run_exec("tmux", "kill-session", "-t", name, timeout=3)
    except Exception:
        pass


async def _run_tmux_bash_locked(
    name: str,
    content: str,
    *,
    cwd: str,
    env: Optional[dict],
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    sandbox_workspace: Optional[str] = None,
    idle_timeout: Optional[float] = None,
) -> Tuple[str, str, Optional[int], str]:
    await _ensure_tmux_session(name, cwd, env, sandbox_workspace)

    stamp = f"{int(time.time() * 1000)}-{abs(hash(content)) % 1000000}"
    start_marker = f"__ODYSSEUS_CMD_START_{stamp}__"
    end_prefix = f"__ODYSSEUS_CMD_END_{stamp}__:"
    # The command reads /dev/null, not the pane: something waiting on input
    # gets end-of-file instead of the lines typed after it.
    head = f"{_TMUX_NONINTERACTIVE}\nprintf '\\n{start_marker}\\n'\n{{\n"
    tail_lines = f"}} < /dev/null\n__ody_rc=$?\nprintf '\\n{end_prefix}%s\\n' \"$__ody_rc\"\n"
    wrapped = f"{head}{content}\n{tail_lines}"
    # Only the wrapper's own lines are scrubbed from the output; a line the
    # command printed is kept even when it repeats a line of the command.
    frame = head + tail_lines
    try:
        for line in wrapped.splitlines():
            await _tmux_send_line(name, line)

        started = time.time()
        last_tail = ""
        last_body = None
        last_output = started
        while True:
            capture = await _tmux_capture(name)
            body, done, clipped = _output_after_marker(capture, start_marker, end_prefix)
            if body != last_body:
                last_body, last_output = body, time.time()
            tail ="\n".join(body.splitlines()[-PROGRESS_TAIL_LINES:])
            if progress_cb and tail != last_tail:
                last_tail = tail
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": tail,
                        "tmux_session": name,
                    })
                except Exception:
                    pass
            if done:
                rc = _extract_marker_rc(capture, end_prefix)
                cleaned = _clean_tmux_command_output(body, frame)
                if clipped:
                    cleaned = _clipped_output_note(cleaned) + cleaned
                return cleaned, "", rc, ""
            now = time.time()
            stopped = ("timeout" if now - started > timeout
                       else "idle" if idle_timeout and now - last_output > idle_timeout else "")
            if stopped:
                await _kill_tmux_session(name)
                cleaned = _clean_tmux_command_output(body, frame)
                return cleaned, "", 124, stopped
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        # Stopped: end what the pane is running so the chat's next command
        # does not type into it.
        await asyncio.shield(_kill_tmux_session(name))
        raise


def _clean_tmux_command_output(text: str, wrapped_command: str) -> str:
    lines = text.splitlines()
    wrapped_lines = {ln.rstrip() for ln in wrapped_command.splitlines() if ln.strip()}
    cleaned = []
    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped:
            cleaned.append(raw)
            continue
        if stripped in wrapped_lines:
            continue
        if stripped.startswith("__ody_rc=") or stripped.startswith("printf "):
            continue
        if re.fullmatch(r"(?:bash|sh)-[\d.]+\$ ?", stripped):
            continue
        if re.fullmatch(r"[\w.@:/~+-]+[#$] ?", stripped):
            continue
        cleaned.append(raw)
    return "\n".join(cleaned).strip()

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    idle_timeout: Optional[float] = None,
) -> Tuple[str, str, Optional[int], str]:
    """``(stdout, stderr, exit code, stopped)``, ``stopped`` as in _run_tmux_bash."""
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)
    last_output = [started]

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            last_output[0] = time.time()
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = ""
    try:
        while True:
            now = time.time()
            if now - started > timeout:
                timed_out = "timeout"
            elif idle_timeout and now - last_output[0] > idle_timeout:
                timed_out = "idle"
            if timed_out:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except Exception:
                    pass
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=min(1.0, max(0.05, timeout - (now - started))))
                break
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )

def _attachment_binds() -> dict:
    """Read-only binds of the files attached to this chat and its parents.

    A worker's sandbox shows only its workspace, so on 2026-10-01 it could not
    even unzip the Penpot export the user had attached to the chat that started
    it. Only the lineage's own files are bound (src/attachment_access.py), at
    the path the model was given, never their directory.
    """
    try:
        from src.attachment_access import sandbox_read_only_binds

        return sandbox_read_only_binds()
    except Exception:  # noqa: BLE001 - no attachments beats no shell
        return {}


def _sandbox_for(ctx) -> Tuple[Optional[str], Optional[dict]]:
    """``(sandbox_workspace, refusal)`` for a bash/python call.

    Unrestricted when the dispatcher chose ``host`` for this call, sandboxed
    when it chose a sandbox workspace, refused otherwise (fail closed).
    """
    from src.tool_execution import get_shell_mode, get_shell_sandbox_workspace

    # The dispatcher decides from the chat's Shell setting (src/shell_access.py),
    # not from the vault grant: a vault-granted chat set to Sandboxed stays
    # sandboxed, and one set to Full server shell runs unrestricted.
    mode = get_shell_mode()
    if mode == "host":
        return None, None
    sandbox_ws = get_shell_sandbox_workspace()
    if sandbox_ws:
        return sandbox_ws, None
    if mode is None and isinstance(ctx, dict) and ctx.get("allow_private") is True:
        # A direct caller that never went through the dispatcher keeps the
        # rule it was written for.
        return None, None
    tool = (ctx or {}).get("tool_name") if isinstance(ctx, dict) else None
    return None, {
        "error": (f"Tool '{tool if tool in ('bash', 'python') else 'bash'}' was not executed: no shell "
                  "was granted for this call (the chat's Shell setting decides: Sandboxed, Full server "
                  "shell or Off)."),
        "blocked": True, "blocked_reason": "shell_not_granted", "exit_code": 1,
    }


class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd
        from src.tool_utils import _truncate_middle as _truncate
        # An unrestricted shell can read an absolute vault path or walk into it
        # through a command substitution, so prompt-only guidance is not a
        # privacy boundary. Missing context is denied too: a caller must carry
        # the explicit per-chat grant, or the dispatcher's sandbox decision,
        # all the way to the subprocess boundary.
        sandbox_ws, refusal = _sandbox_for(ctx)
        if refusal is not None:
            return refusal
        requested_idle = None
        if isinstance(content, dict):
            requested_idle = content.get("idle_timeout")
            content = str(content.get("command") or content.get("cmd") or content.get("code") or "")
        if requested_idle is None:
            from src.tool_execution import get_tool_options

            requested_idle = get_tool_options().get("idle_timeout")
        idle_timeout = bash_idle_timeout(requested_idle)

        # A push from here cannot authenticate: the shell tool carries no git
        # credential by design, and the publishing credential lives only in the
        # manage_agent_worktree flow. Left alone, git exits 128 with an
        # authentication error and the model concludes the token is wrong, then
        # hunts for a GitHub integration that does not exist. Point it at the
        # tool that can actually publish instead of letting it loop.
        from src.agent_worktree.push_guard import check as _publish_guard

        blocked = _publish_guard(content, cwd=agent_cwd())
        if blocked is not None:
            logger.info("bash: blocked a remote-publishing command; redirected to manage_agent_worktree")
            return blocked

        # Same idea for the Claude Code binary: running it from bash skips the
        # delegation allowlist, restricted mode, per-repo lock and task
        # tracking, and a headless run cannot answer permission prompts. The
        # 2026-09-10 logs show the agent probing `claude --help` for three
        # rounds and then running `claude -p` directly.
        from src.agent_tools.claude_code_guard import check as _claude_guard

        blocked = _claude_guard(content)
        if blocked is not None:
            logger.info("bash: blocked a direct Claude Code invocation; redirected to delegate_to_claude_code")
            return blocked

        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        session_id = ctx.get("session_id")
        # tmux is a POSIX persistence path. A stray MSYS/Cygwin tmux.exe on
        # native Windows must not bypass the Git Bash launcher below: the tmux
        # setup hard-codes /bin/bash and cannot safely consume a native cwd.
        if session_id and not IS_WINDOWS and shutil.which("tmux"):
            stdout, stderr, rc, timed_out = await _run_tmux_bash(
                content,
                session_id=str(session_id),
                cwd=agent_cwd(),
                env=_subproc_env,
                timeout=DEFAULT_BASH_TIMEOUT,
                progress_cb=progress_cb,
                sandbox_workspace=sandbox_ws,
                idle_timeout=idle_timeout,
            )
            if timed_out:
                return {
                    "error": _stopped_message(timed_out, idle_timeout, fresh_shell=True),
                    "exit_code": 124,
                    "stdout": _truncate(stdout, MAX_OUTPUT_CHARS),
                    "stderr": _truncate(stderr, MAX_OUTPUT_CHARS),
                    "tmux_session": _tmux_session_name(str(session_id), sandbox_ws),
                    "stopped": timed_out,
                }
            output = stdout.rstrip()
            err = stderr.rstrip()
            if err:
                output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
            return {
                "output": _with_remote_auth_hint(content, output, _truncate(output, MAX_OUTPUT_CHARS)) or "(no output)",
                "exit_code": rc or 0,
                "tmux_session": _tmux_session_name(str(session_id), sandbox_ws),
                **({"sandboxed": True} if sandbox_ws else {}),
            }

        try:
            if sandbox_ws:
                from src import shell_sandbox

                proc = await asyncio.create_subprocess_exec(
                    *shell_sandbox.build_argv(["/bin/bash", "-c", content], workspace=sandbox_ws,
                                              env=_subproc_env, package_cache=True,
                                              extra_ro_binds=_attachment_binds()),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=sandbox_ws,
                )
            else:
                proc = await _create_bash_subprocess(
                    content,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_subproc_env,
                    cwd=agent_cwd(),
                )
        except (RuntimeError, OSError) as e:
            return {"error": f"bash: {e}", "exit_code": 1}
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_BASH_TIMEOUT,
            progress_cb=progress_cb,
            idle_timeout=idle_timeout,
        )
        if timed_out:
            return {"error": _stopped_message(timed_out, idle_timeout), "exit_code": 124,
                    "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS),
                    "stopped": timed_out}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _with_remote_auth_hint(content, output, _truncate(output, MAX_OUTPUT_CHARS))
        return {"output": output or "(no output)", "exit_code": rc or 0}


def bash_idle_timeout(requested: Any = None) -> Optional[float]:
    """Seconds a bash command may print nothing, or None for no limit.

    A call's ``idle_timeout`` wins (at least 10 s, at most the hour limit);
    otherwise the setting bash_idle_timeout_seconds (60 by default, 0 = off).
    """
    try:
        if requested not in (None, ""):
            value = float(requested)
            return None if value <= 0 else max(10.0, min(float(DEFAULT_BASH_TIMEOUT), value))
    except (TypeError, ValueError):
        pass
    try:
        from src.settings import get_setting

        value = float(get_setting("bash_idle_timeout_seconds", DEFAULT_BASH_IDLE_TIMEOUT))
    except (TypeError, ValueError):
        value = float(DEFAULT_BASH_IDLE_TIMEOUT)
    return None if value <= 0 else min(float(DEFAULT_BASH_TIMEOUT), value)


def _stopped_message(stopped: str, idle_timeout: Optional[float], fresh_shell: bool = False) -> str:
    shell = " (the next command starts a fresh shell)" if fresh_shell else ""
    if stopped == "idle":
        return (f"bash: stopped after {int(idle_timeout or 0)}s without any new output — process killed{shell}. "
                "If it was working, it was working silently: run it again without quiet flags (-q, --quiet, "
                "--silent) so it reports progress, pass a larger `idle_timeout` for this one command, or start "
                "it in the background with `#!bg` as the first line and check on it with manage_bg_jobs. If "
                "it hung (a test waiting on a service or a network call), fix that before running it again; "
                "for Maven or Gradle tests also set a per-test timeout, e.g. -Dsurefire.timeout=300.")
    return f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process killed{shell}"


def _with_remote_auth_hint(command: str, full_output: str, shown: str) -> str:
    """``shown`` plus a pointer to manage_git when a git fetch/pull/clone in
    the command failed to authenticate to GitHub (push_guard.remote_auth_hint)."""
    from src.agent_worktree.push_guard import remote_auth_hint
    from src.tool_execution import agent_cwd

    hint = remote_auth_hint(command, full_output, agent_cwd())
    return f"{shown}\n\n[Agamemnon] {hint}" if hint else shown


class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd
        from src.tool_utils import _truncate_middle as _truncate
        sandbox_ws, refusal = _sandbox_for(dict(ctx or {}, tool_name="python") if isinstance(ctx, dict) else ctx)
        if refusal is not None:
            return refusal
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        argv = [(sys.executable or "python"), "-I", "-c", content]
        if sandbox_ws:
            from src import shell_sandbox

            # The sandbox shows /usr only; an interpreter elsewhere (a venv)
            # would not exist inside it.
            if not os.path.realpath(argv[0]).startswith("/usr/"):
                argv[0] = "python3"
            argv = shell_sandbox.build_argv(argv, workspace=sandbox_ws, env=_subproc_env,
                                            package_cache=True,
                                            extra_ro_binds=_attachment_binds())
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=None if sandbox_ws else _subproc_env,
            cwd=sandbox_ws or agent_cwd(),
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}
