"""Argument-array git runner.

Every git call in this package goes through :func:`run_git`. There is no shell
anywhere: commands are argv lists, so a branch name or path can never be
re-parsed as a second command. Two further rules matter for security:

* **Credentials never touch argv.** A token embedded in a remote URL
  (``https://x-access-token:TOKEN@github.com/...``) is visible to every user on
  the box via the process list. Instead the token is passed as an
  ``http.<url>.extraheader`` through ``GIT_CONFIG_*`` environment variables,
  which are readable only by the process owner. It is never written to
  ``.git/config``, a credential helper, or disk.
* **Nothing interactive, nothing implicit.** Terminal and GUI credential
  prompts are disabled so a missing token fails fast instead of hanging a
  worker, and the ambient environment is not inherited wholesale.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120.0
MAX_CAPTURE_CHARS = 200_000

# Committer identity for agent commits. Fixed so history shows plainly that a
# machine produced the commit, and so the agent cannot impersonate a person.
AGENT_NAME = "Odysseus Agent"
AGENT_EMAIL = "odysseus-agent@users.noreply.github.com"


class GitError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int, stderr: str):
        self.argv = list(argv)
        self.code = code
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv[:3])} failed ({code}): {stderr.strip()[:400]}")


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def git_path() -> Optional[str]:
    return shutil.which("git")


def _base_env() -> Dict[str, str]:
    """Minimal, non-interactive environment for a git child process.

    Built from a small allowlist rather than a copy of ``os.environ`` so an
    inherited ``GIT_*`` variable (an ``GIT_SSH_COMMAND``, a credential helper, a
    ``GIT_DIR`` left over from a hook) cannot redirect the operation.
    """
    keep = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            # No credential prompts of any kind: fail instead of blocking.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            # Deterministic, machine-readable output.
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            # Identity for commits made in the worktree.
            "GIT_AUTHOR_NAME": AGENT_NAME,
            "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
            "GIT_COMMITTER_NAME": AGENT_NAME,
            "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
        }
    )
    return env


def auth_env(token: Optional[str], remote_url: str) -> Dict[str, str]:
    """Extra env carrying an Authorization header for `remote_url`.

    Returns an empty mapping when there is no token, so an unauthenticated call
    simply fails the push rather than silently using ambient credentials.
    """
    if not token or not remote_url:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    # Scope the header to the exact remote so it cannot be replayed to another
    # host if git were ever redirected.
    #
    # sslVerify and proxy are pinned alongside it because environment config
    # outranks repository-local config. Without them, a `.git/config` inside a
    # directory the agent can write could set `http.proxy` and
    # `http.sslVerify=false` and route the Authorization header — and therefore
    # the installation token — through a host of its choosing.
    return {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": f"http.{remote_url}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_CONFIG_KEY_1": "http.sslVerify",
        "GIT_CONFIG_VALUE_1": "true",
        "GIT_CONFIG_KEY_2": "http.proxy",
        "GIT_CONFIG_VALUE_2": "",
    }


def _redact(text: str, secrets: Sequence[str]) -> str:
    """Remove any known secret material from text destined for logs or the model."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
            out = out.replace(
                base64.b64encode(f"x-access-token:{secret}".encode("utf-8")).decode("ascii"),
                "***",
            )
    return out


async def run_git(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    extra_env: Optional[Dict[str, str]] = None,
    secrets: Sequence[str] = (),
    check: bool = True,
) -> GitResult:
    """Run one git command as an argument array.

    `secrets` lists values that must never appear in the returned text; they are
    scrubbed from both streams before anything is logged or handed back.
    """
    exe = git_path()
    if not exe:
        raise GitError(["git"], 127, "git is not installed or not on PATH")
    argv: List[str] = [exe, *[str(a) for a in args]]
    env = _base_env()
    if extra_env:
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise GitError(argv[1:], 124, f"timed out after {timeout_s}s")

    stdout = _redact(out_b.decode("utf-8", errors="replace"), secrets)[:MAX_CAPTURE_CHARS]
    stderr = _redact(err_b.decode("utf-8", errors="replace"), secrets)[:MAX_CAPTURE_CHARS]
    code = proc.returncode or 0
    if check and code != 0:
        raise GitError(argv[1:], code, stderr or stdout)
    return GitResult(code=code, stdout=stdout, stderr=stderr)
