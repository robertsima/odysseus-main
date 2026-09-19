"""Run the agent's bash and python in a bubblewrap sandbox bound to the workspace.

bash and python are unrestricted subprocesses, so they used to need the
chat's private-vault grant: nothing stopped `cat /app/data/personal_docs/...`.
Filtering commands is not a boundary (globs, `python -c`, symlinks, base64 all
get round it), so this uses the operating system instead. The sandbox sees:

* the chat's workspace, read-write, as the working directory;
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

The sandbox needs the `bwrap` binary and permission to create user namespaces,
which Docker's default seccomp/AppArmor profiles refuse (see the compose
files). `status()` probes once and says why it is unavailable; callers then
fall back to requiring the private-vault grant, as before.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

SETTING = "shell_sandbox"            # "auto" (default) | "off"
NETWORK_SETTING = "shell_sandbox_network"   # True (default) | False
SANDBOX_HOME = "/tmp/home"
_PROBE_RETRY_S = 600.0
_PROBE_TIMEOUT_S = 10.0

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


def build_argv(inner: List[str], *, workspace: str, env: Optional[Mapping[str, str]] = None,
               extra_ro_binds: Optional[Mapping[str, str]] = None, new_session: bool = True) -> List[str]:
    """``inner`` wrapped in bwrap: runs in ``workspace`` and sees only that,
    the read-only system and its own scratch space. ``extra_ro_binds`` maps
    host paths to where they appear inside (a background job's script)."""
    bwrap = shutil.which("bwrap") or "bwrap"
    ws = os.path.realpath(workspace)
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
    args += ["--proc", "/proc", "--dev", "/dev",
             "--tmpfs", "/tmp", "--dir", SANDBOX_HOME,
             "--dir", "/var", "--tmpfs", "/var/tmp",
             "--bind", ws, ws, "--chdir", ws]
    for host, inside in (extra_ro_binds or {}).items():
        args += ["--ro-bind", host, inside]
    return args + ["--"] + list(inner)


def _run_probe() -> Dict[str, object]:
    if os.name != "posix":
        return {"available": False, "reason": "the shell sandbox needs Linux"}
    if not shutil.which("bwrap"):
        return {"available": False, "reason": "bubblewrap (bwrap) is not installed"}
    import tempfile

    # Also checks the point of it: the app's data directory must be absent.
    from src.constants import DATA_DIR

    probe_dir = tempfile.mkdtemp(prefix="odysseus-sandbox-probe-")
    argv = build_argv(["/bin/sh", "-c", f'test -d /proc/self && ! test -e "{os.path.realpath(DATA_DIR)}"'],
                      workspace=probe_dir)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"bwrap could not start: {exc}"}
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return {"available": False,
                "reason": ("bwrap failed: " + (detail[-1] if detail else f"exit {done.returncode}")
                           + ". Docker's default seccomp/AppArmor profiles block the user "
                           "namespaces it needs; see security_opt in the compose file.")}
    return {"available": True, "reason": ""}


def status(*, refresh: bool = False) -> Dict[str, object]:
    """``{"enabled", "available", "reason"}``. The probe runs once; a failure
    is re-probed after a while, so a fixed container is noticed."""
    if not enabled():
        return {"enabled": False, "available": False, "reason": "turned off in settings (shell_sandbox)"}
    with _lock:
        stale = (not _probe or refresh
                 or (not _probe.get("available")
                     and time.monotonic() - float(_probe.get("at", 0.0)) > _PROBE_RETRY_S))
        if stale:
            result = _run_probe()
            result["at"] = time.monotonic()
            if not result["available"] and result.get("reason") != _probe.get("reason"):
                logger.warning("[shell-sandbox] unavailable: %s", result["reason"])
            elif result["available"] and not _probe.get("available"):
                logger.info("[shell-sandbox] available: bash/python run confined to the workspace")
            _probe.clear()
            _probe.update(result)
        return {"enabled": True, "available": bool(_probe.get("available")),
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
