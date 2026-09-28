"""Run the agent's bash and python in a bubblewrap sandbox bound to the workspace.

bash and python are unrestricted subprocesses, so they used to need the
chat's private-vault grant: nothing stopped `cat /app/data/personal_docs/...`.
Filtering commands is not a boundary (globs, `python -c`, symlinks, base64 all
get round it), so this uses the operating system instead. The sandbox sees:

* the chat's workspace, read-write, as the working directory;
* the managed worktrees of the workspace's own repository (the ones
  ``manage_agent_worktree start`` makes under the worktree root), read-write
  at the same path;
* the system (/usr, /etc and the /bin, /lib links), read-only;
* its own empty /tmp and /var/tmp, a fresh /proc (own PID namespace, so the
  app's environment in /proc/<pid>/environ is out of reach) and a minimal /dev.

Everything else is absent: /app and /app/data (vault, private documents, the
app database, settings, keys), the Docker socket, and the app's environment
(the sandbox gets a short allowlist of variables, no API keys or tokens).

The network is shared, because pip, npm and git fetch need it. That leaves
services the container can reach, ChromaDB included (its vectors hold vault
excerpts, and Chroma 1.x has no built-in auth), so `shell_sandbox_network`
can turn the network off for the sandbox.

The sandbox needs the `bwrap` binary, permission to create user namespaces
(which Docker's default seccomp/AppArmor profiles refuse) and permission to
mount a fresh /proc (which Docker's masked /proc paths refuse); see
security_opt in the compose files. `status()` probes once and says which is
missing. Without user namespaces the sandbox is unavailable and callers fall
back to requiring the private-vault grant, as before. Without /proc it runs
degraded, with an empty /proc: confinement is unchanged, but ps/top, bash
process substitution and the /dev/fd and /dev/std{in,out,err} paths (bwrap's
/dev links them into /proc/self) do not work inside it. Redirects such as
``>&2``, python, git and pip are unaffected.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING = "shell_sandbox"            # "auto" (default) | "off"
NETWORK_SETTING = "shell_sandbox_network"   # True (default) | False
SANDBOX_HOME = "/tmp/home"
_PROBE_RETRY_S = 600.0
_PROBE_TIMEOUT_S = 10.0

# What to change, in words an operator can act on. The compose files carry
# these lines under the odysseus service.
_USERNS_FIX = ('add "seccomp=unconfined" and "apparmor=unconfined" under security_opt '
               'of the odysseus service in the compose file')
_PROC_FIX = 'add "systempaths=unconfined" under security_opt of the odysseus service in the compose file'
DEGRADED_REASON = "no procfs: ps/top, bash process substitution and /dev/fd may not work"

# bwrap's own wording. "Can't mount proc on /newroot/proc" comes after the
# namespaces were created, so it is never a seccomp/AppArmor problem.
_PROC_MOUNT_MARKERS = ("mount proc",)
_USERNS_MARKERS = ("namespace", "uid map", "gid map", "setgroups", "unshare")

# Variables the sandboxed shell may see. Everything else in the app's
# environment (API keys, tokens, passwords) stays outside.
_ENV_ALLOW = frozenset({
    "PATH", "LANG", "LANGUAGE", "TERM", "COLUMNS", "LINES", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST",
})
_ENV_ALLOW_PREFIXES = ("LC_", "GIT_CONFIG_")

_lock = threading.Lock()
_probe: Dict[str, object] = {}


def _setting(key: str, default):
    try:
        from src.settings import get_setting

        return get_setting(key, default)
    except Exception:
        return default


def enabled() -> bool:
    return str(_setting(SETTING, "auto") or "auto").strip().lower() != "off"


def network_enabled() -> bool:
    value = _setting(NETWORK_SETTING, True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value)


def sandbox_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The allowlisted environment the sandboxed shell runs with."""
    source = dict(base if base is not None else os.environ)
    env = {k: v for k, v in source.items()
           if k in _ENV_ALLOW or k.startswith(_ENV_ALLOW_PREFIXES)}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["HOME"] = SANDBOX_HOME
    env["TMPDIR"] = "/tmp"
    # The persistent tmux pane runs an interactive bash; with a clean
    # environment its default prompt ("bash-5.2$ ") lands in every output.
    env["PS1"] = ""
    env["PS2"] = ""
    return env


def _system_mounts() -> List[str]:
    args: List[str] = ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        path = f"/{name}"
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            args += ["--ro-bind", path, path]
    if os.path.isdir("/etc"):
        args += ["--ro-bind", "/etc", "/etc"]
    return args


def _procfs_mode() -> bool:
    """Whether the probe found a fresh /proc mountable (True until it has run)."""
    return _probe.get("procfs") is not False


def build_argv(inner: List[str], *, workspace: str, env: Optional[Mapping[str, str]] = None,
               extra_ro_binds: Optional[Mapping[str, str]] = None, new_session: bool = True,
               procfs: Optional[bool] = None, worktrees: bool = True) -> List[str]:
    """``inner`` wrapped in bwrap: runs in ``workspace`` and sees only that,
    the workspace repository's managed worktrees, the read-only system and
    its own scratch space. ``extra_ro_binds`` maps host paths to where they
    appear inside (a background job's script). ``procfs`` None follows the
    probe: an empty /proc where Docker refuses a fresh one."""
    bwrap = shutil.which("bwrap") or "bwrap"
    ws = os.path.realpath(workspace)
    if procfs is None:
        procfs = _procfs_mode()
    # bwrap is launched through `env -i` with only the allowlist. bwrap is
    # PID 1 of the sandbox's PID namespace, so its own environment is readable
    # inside at /proc/1/environ; --clearenv would clean only the child and
    # leave every API key in the app's environment one `cat` away.
    clean = [f"{key}={value}" for key, value in sandbox_env(env).items()]
    args = [shutil.which("env") or "/usr/bin/env", "-i", *clean,
            bwrap, "--die-with-parent", "--unshare-all"]
    if network_enabled():
        args.append("--share-net")
    if new_session:
        args.append("--new-session")
    args += _system_mounts()
    # Degraded mode is an empty /proc directory, never the container's /proc
    # bound in: the shell runs as the app's uid, so /proc/<app pid>/environ
    # and /proc/<pid>/root would hand it the app's secrets and its whole
    # filesystem.
    args += ["--proc", "/proc"] if procfs else ["--dir", "/proc"]
    args += ["--dev", "/dev",
             "--tmpfs", "/tmp", "--dir", SANDBOX_HOME,
             "--dir", "/var", "--tmpfs", "/var/tmp",
             "--bind", ws, ws]
    for tree in (workspace_worktrees(ws) if worktrees else ()):
        args += ["--bind", tree, tree]
    args += ["--chdir", ws]
    for host, inside in (extra_ro_binds or {}).items():
        args += ["--ro-bind", host, inside]
    return args + ["--"] + list(inner)


def _platform_problem() -> Optional[str]:
    if os.name != "posix":
        return "the shell sandbox needs Linux"
    if not shutil.which("bwrap"):
        return "bubblewrap (bwrap) is not installed"
    return None


def _run_bwrap(argv: List[str]) -> Tuple[int, str]:
    """Run one probe: ``(returncode, last line of its output)``."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    lines = (done.stderr or done.stdout or "").strip().splitlines()
    return done.returncode, (lines[-1].strip() if lines else "")


def _probe_once(*, procfs: bool) -> Tuple[int, str]:
    import tempfile

    # Also checks the point of it: the app's data directory must be absent.
    # With a procfs it must be the sandbox's own; without one, nothing of
    # the container's /proc may show through.
    from src.constants import DATA_DIR

    check = f'! test -e "{os.path.realpath(DATA_DIR)}"'
    check = ("test -d /proc/self && " if procfs else "! test -e /proc/1 && ") + check
    probe_dir = tempfile.mkdtemp(prefix="odysseus-sandbox-probe-")
    try:
        return _run_bwrap(build_argv(["/bin/sh", "-c", check], workspace=probe_dir,
                                     procfs=procfs, worktrees=False))
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _failure_kind(detail: str) -> str:
    text = (detail or "").lower()
    if any(marker in text for marker in _PROC_MOUNT_MARKERS):
        return "proc"
    if any(marker in text for marker in _USERNS_MARKERS):
        return "userns"
    return "other"


def _explain(kind: str, detail: str, code: int) -> str:
    detail = detail or f"exit {code}"
    if kind == "userns":
        return (f"bwrap cannot create a user namespace ({detail}): the container's seccomp or "
                f"AppArmor profile, or the kernel, forbids it. In Docker, {_USERNS_FIX}; on a "
                "bare host, allow unprivileged user namespaces "
                "(sysctl kernel.unprivileged_userns_clone=1).")
    if kind == "proc":
        return (f"bwrap cannot mount /proc ({detail}): Docker masks paths under /proc, and a "
                "new procfs is refused unless the container's /proc is fully visible; "
                f"{_PROC_FIX}.")
    return (f"bwrap failed: {detail}. In Docker, check security_opt in the compose file: "
            f"{_USERNS_FIX}, and {_PROC_FIX}.")


def _run_probe() -> Dict[str, object]:
    problem = _platform_problem()
    if problem:
        return {"available": False, "degraded": False, "reason": problem}
    try:
        code, detail = _probe_once(procfs=True)
        if code == 0:
            return {"available": True, "degraded": False, "procfs": True, "reason": ""}
        kind = _failure_kind(detail)
        if kind != "proc":
            return {"available": False, "degraded": False, "reason": _explain(kind, detail, code)}
        # On 2026-09-28 the live log read "bwrap: Can't mount proc on /proc:
        # Operation not permitted" behind a message blaming seccomp/AppArmor,
        # which were already unconfined: the namespaces had been created.
        # Docker's masked /proc paths refuse a new procfs, so run with an
        # empty /proc instead of not at all. Confinement is otherwise the same.
        code, degraded_detail = _probe_once(procfs=False)
        if code == 0:
            return {"available": True, "degraded": True, "procfs": False,
                    "reason": f"{DEGRADED_REASON}. For a full /proc, {_PROC_FIX}.",
                    "proc_error": detail}
        kind = _failure_kind(degraded_detail) if degraded_detail else "other"
        return {"available": False, "degraded": False,
                "reason": _explain(kind, degraded_detail, code)}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "degraded": False, "reason": f"bwrap could not start: {exc}"}


def mode() -> str:
    """The ``shell_sandbox`` setting as stored ("auto" or "off")."""
    return str(_setting(SETTING, "auto") or "auto").strip().lower()


def status(*, refresh: bool = False) -> Dict[str, object]:
    """``{"enabled", "available", "degraded", "reason"}``. The probe runs
    once; a failure is re-probed after a while, so a fixed container is
    noticed. ``degraded`` means available without a procfs, and ``reason``
    then says what does not work."""
    if not enabled():
        return {"enabled": False, "available": False, "degraded": False,
                "reason": "turned off in settings (shell_sandbox)"}
    with _lock:
        stale = (not _probe or refresh
                 or (not _probe.get("available")
                     and time.monotonic() - float(_probe.get("at", 0.0)) > _PROBE_RETRY_S))
        if stale:
            result = _run_probe()
            result["at"] = time.monotonic()
            if not result["available"] and result.get("reason") != _probe.get("reason"):
                logger.warning("[shell-sandbox] unavailable: %s", result["reason"])
            elif result.get("degraded") and not _probe.get("degraded"):
                logger.warning("[shell-sandbox] degraded, running with an empty /proc (%s): %s",
                               result.get("proc_error"), result["reason"])
            elif result["available"] and not _probe.get("available"):
                logger.info("[shell-sandbox] available: bash/python run confined to the workspace")
            _probe.clear()
            _probe.update(result)
        return {"enabled": True, "available": bool(_probe.get("available")),
                "degraded": bool(_probe.get("degraded")),
                "reason": str(_probe.get("reason") or "")}


def _within(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    if path == root:
        return True
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def workspace_problem(workspace: Optional[str]) -> Optional[str]:
    """Why ``workspace`` must not be handed to the sandbox, or None.

    The workspace is the one thing the sandbox exposes, read-write, so it must
    not be (or contain) what the sandbox exists to hide: the app's data
    directory, the document vault, the Docker socket.
    """
    if not workspace or not os.path.isdir(workspace):
        return "no workspace is set"
    ws = os.path.realpath(workspace)
    if os.path.dirname(ws) == ws:
        return "the workspace is a filesystem root"
    try:
        from src.constants import AGENT_WORKSPACE_DIR, DATA_DIR, PERSONAL_DIR
        from src.rag_sensitivity import vault_root
    except Exception:
        return "the app's protected directories could not be determined"
    data = os.path.realpath(DATA_DIR)
    if _within(data, ws):
        return "the workspace contains the app's data directory"
    for protected in {os.path.realpath(PERSONAL_DIR), os.path.realpath(vault_root())}:
        if _within(protected, ws) or _within(ws, protected):
            return "the workspace overlaps the document vault"
    if _within(ws, data):
        # Inside DATA_DIR only the repository roots and the agent's scratch
        # folder are ordinary work areas.
        try:
            from src.tool_execution import _repository_data_subdirs

            allowed = list(_repository_data_subdirs(data)) + [os.path.realpath(AGENT_WORKSPACE_DIR)]
        except Exception:
            allowed = [os.path.realpath(AGENT_WORKSPACE_DIR)]
        if not any(_within(ws, root) for root in allowed):
            return "the workspace is inside the app's data directory"
    for socket_path in ("/var/run/docker.sock", "/run/docker.sock"):
        if _within(socket_path, ws):
            return "the workspace contains the Docker socket"
    return None


def _plain_directory(path: str) -> bool:
    """A directory reached without a symlink anywhere on the way."""
    try:
        if not os.path.isdir(path) or os.path.islink(path):
            return False
        return (os.path.normcase(os.path.realpath(path))
                == os.path.normcase(os.path.normpath(os.path.abspath(path))))
    except (OSError, ValueError):
        return False


def workspace_worktrees(workspace: str) -> List[str]:
    """Managed worktrees of the workspace's own repository, to bind read-write.

    On 2026-09-28 two Lead Engineer workers made a worktree with
    ``manage_agent_worktree start`` (under the worktree root, e.g.
    /app/data/agent_worktrees/_repos/<repo>/<slug>) outside their workspace
    (/app/data/development/<repo>), and could not run a test in it: the
    sandbox showed only the workspace. The repository's shared git dir lists
    each linked worktree (``worktrees/<name>/gitdir`` holds the path of its
    ``.git`` file). One is bound only when it is a directory reached without
    symlinks, sits under the managed worktree root, belongs to this
    workspace's repository (``ownership.belongs_to_workspace``) and passes the
    same ``workspace_problem`` checks as the workspace. The main checkout's
    ``.git`` is inside the workspace, so git works in a bound worktree.
    Worktrees of other repositories are never bound.
    """
    try:
        from pathlib import Path

        from src.agent_worktree import ownership

        root = ownership.managed_worktree_root()
        if root is None:
            return []
        ws = os.path.realpath(workspace)
        common = ownership.git_common_dir(Path(ws))
        if common is None:
            return []
        entries = sorted((common / "worktrees").iterdir())
    except (OSError, ValueError, ImportError):
        return []
    found: List[str] = []
    for entry in entries:
        try:
            pointer = (entry / "gitdir").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not pointer or not os.path.isabs(pointer):
            continue
        tree = os.path.normpath(os.path.dirname(pointer))
        if tree in found or _within(tree, ws) or not _plain_directory(tree):
            continue
        try:
            if not ownership.belongs_to_workspace(Path(tree), Path(ws), root):
                continue
        except (OSError, ValueError):
            continue
        if workspace_problem(tree) is not None:
            continue
        found.append(tree)
    return found


def usable_for(workspace: Optional[str]) -> bool:
    """Whether bash/python may run sandboxed in this workspace."""
    return workspace_problem(workspace) is None and bool(status()["available"])


def unavailable_reason(workspace: Optional[str]) -> str:
    """Why bash/python cannot run sandboxed here (empty when they can)."""
    problem = workspace_problem(workspace)
    if problem:
        return problem
    state = status()
    return "" if state["available"] else str(state.get("reason") or "the sandbox is unavailable")


def _reset_for_tests() -> None:
    with _lock:
        _probe.clear()
