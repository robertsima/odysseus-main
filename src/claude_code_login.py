"""Sign the in-container Claude Code CLI in from the web UI, without SSH.

Odysseus starts the CLI's *own* login, ``claude auth login``, as the container
user with the same allowlisted environment a delegation gets (``HOME``,
``CLAUDE_CONFIG_DIR``, proxy/CA variables; no server secrets). The command
prints an authorization URL and then reads a code from stdin::

    Opening browser to sign in…
    If the browser didn't open, visit: https://claude.ai/oauth/authorize?…
    Paste code here if prompted >

The admin opens that URL on their own desktop and signs in. Because the
browser cannot reach the CLI's localhost callback inside the container, the
authorization page shows a code instead (the documented fallback for "WSL2,
SSH sessions, and containers"). The admin pastes it into Settings; this module
writes it to the waiting process's stdin, the CLI exchanges it (with its own
PKCE verifier, which Odysseus never sees) and stores the login in its own
``$CLAUDE_CONFIG_DIR/.credentials.json`` (mode 0600, written by the CLI). It
prints ``Login successful.`` and exits 0, or ``Login failed: …`` on stderr
and exits 1. Verification then goes through the existing ``claude auth
status`` probe (:func:`claude_code_tools.auth_status`).

Why ``auth login`` rather than ``claude setup-token`` or ``/login``: it is a
plain (non-Ink) subcommand that reads the code with ``readline`` from stdin,
so it runs over ordinary pipes with no TTY, and it leaves the credential in
the CLI's own store — Odysseus never reads, stores, or forwards a Claude
OAuth token (the README's terms section). ``setup-token`` and ``/login`` are
Ink UIs that need a raw-mode TTY, and ``setup-token`` would hand Odysseus a
year-long token to store.

The code is written to the process and dropped: it is never logged, stored,
or echoed in a response. Only state transitions are logged. One session at a
time lives in memory; it expires (and its process is killed) after
:data:`SESSION_TTL_S`, on cancel, or at app shutdown.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import secrets
import signal
import time
from typing import Any, Optional

from src.agent_tools import claude_code_tools as cct

logger = logging.getLogger(__name__)

SESSION_TTL_S = 600          # the whole sign-in, start to finish
START_INTERVAL_S = 10        # rate limit on POST /login/start
URL_WAIT_S = 45              # how long the CLI may take to print the link
VERIFY_WAIT_S = 90           # code submitted -> CLI exit
LOGOUT_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 16 * 1024  # bounded tail of the CLI's own output
MAX_CODE_CHARS = 4096
METHODS = ("claudeai", "console")

STARTING, AWAITING_CODE, VERIFYING = "starting", "awaiting_code", "verifying"
DONE, FAILED, EXPIRED, CANCELLED = "done", "failed", "expired", "cancelled"
TERMINAL = frozenset({DONE, FAILED, EXPIRED, CANCELLED})

# CSI (colours, cursor), OSC (hyperlinks: ESC ] 8 ;; url BEL) and the lone
# two-byte escapes. The CLI only emits OSC 8 on a TTY, but strip it anyway.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# A URL is taken only once whitespace follows it, so a link split across two
# pipe reads is never captured half-way.
_URL = re.compile(r"https://[^\s\"'<>]+(?=\s)")
# What the authorization page shows: "<code>#<state>". The CLI splits on "#"
# and rejects anything else; newlines/whitespace are refused here so a paste
# can never smuggle a second line into the process's stdin.
_CODE = re.compile(r"^[A-Za-z0-9._~+/=\-]+#[A-Za-z0-9._~+/=\-]+$")
_INVALID_CODE = "Invalid code"
# Credentials the CLI prefers over a stored login (authentication precedence).
_OVERRIDING_ENV = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


class LoginError(Exception):
    """A request the flow refuses; ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def strip_ansi(text: str) -> str:
    return _CONTROL.sub("", _ANSI.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n"))


class LoginSession:
    """One ``claude auth login`` process and what the UI may know about it."""

    def __init__(self, method: str):
        self.id = secrets.token_urlsafe(16)
        self.method = method
        self.started_at = time.time()
        self.expires_at = self.started_at + SESSION_TTL_S
        self.state = STARTING
        self.url: Optional[str] = None
        self.error: Optional[str] = None
        self.message: Optional[str] = None
        self.account: Optional[dict] = None
        self.finished_at: Optional[float] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.exit_code: Optional[int] = None
        self._output = ""            # ANSI-stripped, bounded tail (stdout + stderr)
        self._stderr = ""            # bounded tail, for the failure message
        self._invalid_seen = 0
        self._changed = asyncio.Event()
        self._settled = asyncio.Event()
        self._scrub: list[str] = []  # the submitted code, only while verifying
        self._tasks: list[asyncio.Task] = []

    # ── state ──
    def _set(self, state: str, *, error: Optional[str] = None) -> None:
        if self.state == state and error is None:
            return
        previous, self.state = self.state, state
        if error is not None:
            self.error = error
        if state in TERMINAL:
            self.finished_at = time.time()
            self._scrub = []
            self._settled.set()
        self._changed.set()
        # State transitions only: never the URL, the code, or CLI output.
        logger.info("[claude-login] session=%s %s -> %s", self.id[:8], previous, state)

    def view(self) -> dict:
        now = time.time()
        out: dict[str, Any] = {
            "session_id": self.id,
            "state": self.state,
            "method": self.method,
            "started_at": int(self.started_at),
            "expires_at": int(self.expires_at),
            "expires_in": max(0, int(self.expires_at - now)) if self.state not in TERMINAL else 0,
        }
        if self.url and self.state in (AWAITING_CODE, VERIFYING):
            out["url"] = self.url
        if self.error:
            out["error"] = self.error
        if self.message:
            out["message"] = self.message
        if self.account:
            out["account"] = self.account
        return out

    # ── output ──
    def _clean(self, text: str) -> str:
        for value in self._scrub:
            if value:
                text = text.replace(value, "***")
        return text

    def _feed(self, text: str, *, stderr: bool) -> None:
        text = self._clean(strip_ansi(text))
        self._output = (self._output + text)[-MAX_OUTPUT_CHARS:]
        if stderr:
            self._stderr = (self._stderr + text)[-MAX_OUTPUT_CHARS:]
            self._invalid_seen += text.count(_INVALID_CODE)
        if self.url is None:
            self.url = _find_url(self._output)
            if self.url and self.state == STARTING:
                self._set(AWAITING_CODE)
        self._changed.set()

    def failure_message(self) -> str:
        """The CLI's own non-secret reason, e.g. ``Login failed: …``."""
        lines = [line.strip() for line in self._clean(self._stderr).splitlines() if line.strip()]
        picked = [line for line in lines if line.startswith(("Login failed", "Error", "OAuth"))] or lines
        text = " ".join(picked[-3:])[:600]
        if not text:
            return ""
        from src.agent_logs import redact_text
        return redact_text(text)


def _find_url(text: str) -> Optional[str]:
    candidates = _URL.findall(text)
    for url in candidates:
        if "oauth" in url or "authorize" in url:
            return url
    return candidates[0] if candidates else None


# ── module state: one session at a time ──

_SESSION: Optional[LoginSession] = None
_LAST_START = 0.0
_LOCK: Optional[asyncio.Lock] = None


def _lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def _binary_argv() -> list[str]:
    """argv prefix that runs the configured binary (tests swap in a stub)."""
    return [str(cct.binary_path())]


def _binary_ready() -> Optional[str]:
    binary = cct.binary_path()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return (f"Claude Code binary unavailable at {binary}. Install it or set Settings › "
                "Claude Code › Advanced › Binary first.")
    return None


def _login_environment() -> dict[str, str]:
    """The delegation allowlist (HOME, CLAUDE_CONFIG_DIR, PATH, proxy/CA), not
    the server environment. The callback token is not added: login does not
    need it. NO_COLOR keeps the output plain."""
    env = cct._base_child_environment()
    env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)  # would switch to the refresh-token path
    env["NO_COLOR"] = "1"
    return env


def overriding_credentials() -> list[str]:
    """Names (never values) of environment credentials that take precedence
    over a stored sign-in, so the UI can say why a login seems ignored."""
    env = cct._base_child_environment()
    return [name for name in _OVERRIDING_ENV if env.get(name)]


def _spawn_kwargs() -> dict:
    # Own process group on POSIX so a kill reaches anything the CLI started.
    return {"start_new_session": True} if os.name != "nt" else {}


def _kill(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def _reap(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None:
        return
    _kill(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _pump(session: LoginSession, stream: Optional[asyncio.StreamReader], *, stderr: bool) -> None:
    if stream is None:
        return
    while True:
        try:
            chunk = await stream.read(4096)
        except (ConnectionError, ValueError):
            return
        if not chunk:
            return
        session._feed(chunk.decode("utf-8", errors="replace"), stderr=stderr)


async def _account_details() -> Optional[dict]:
    """Non-secret account labels from ``claude auth status`` (JSON): the
    signed-in email/organization and plan, for the Settings card only."""
    try:
        code, out = await cct._capture(cct.binary_path(), "auth", "status", timeout=45, env=_login_environment())
    except OSError:
        return None
    import json
    try:
        parsed = json.loads(strip_ansi(out).strip() or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    fields = {"email": "email", "orgName": "organization", "subscriptionType": "subscription",
              "authMethod": "auth_method", "apiProvider": "api_provider"}
    return {label: parsed.get(key) for key, label in fields.items() if parsed.get(key)}


async def _finish(session: LoginSession) -> None:
    """Runs when the process exits: decide done/failed from the exit code and
    the CLI's own auth status."""
    if session.state in TERMINAL:
        return
    if session.exit_code == 0:
        session._set(VERIFYING)
        auth = await cct.auth_status()
        if session.state in TERMINAL:
            return
        if auth.get("logged_in"):
            session.account = await _account_details() or {}
            if auth.get("auth_method"):
                session.account.setdefault("auth_method", auth.get("auth_method"))
            overriding = overriding_credentials()
            session.message = "Claude Code is signed in."
            if overriding:
                session.message += (" Note: " + ", ".join(overriding) + " is set in the container environment "
                                    "and takes precedence over this sign-in.")
            session._set(DONE)
        else:
            reason = auth.get("error") or "claude auth status still reports not signed in"
            session._set(FAILED, error=f"The CLI finished, but {reason}.")
        return
    reason = session.failure_message() or f"claude auth login exited {session.exit_code}"
    session._set(FAILED, error=reason)


async def _watch(session: LoginSession, readers: list[asyncio.Task]) -> None:
    proc = session.proc
    assert proc is not None
    try:
        remaining = max(0.0, session.expires_at - time.time())
        try:
            await asyncio.wait_for(proc.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            if session.state not in TERMINAL:
                session._set(EXPIRED, error="The sign-in expired after 10 minutes. Start it again.")
            await _reap(proc)
            return
        # Let the readers drain what the process wrote before it exited.
        try:
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            pass
        session.exit_code = proc.returncode
        await _finish(session)
    except asyncio.CancelledError:
        await _reap(proc)
        raise
    except Exception:
        logger.exception("[claude-login] session=%s watcher failed", session.id[:8])
        if session.state not in TERMINAL:
            session._set(FAILED, error="Internal error while waiting for the CLI; see the server log.")
        await _reap(proc)


def _current() -> Optional[LoginSession]:
    session = _SESSION
    if session and session.state not in TERMINAL and time.time() >= session.expires_at:
        session._set(EXPIRED, error="The sign-in expired after 10 minutes. Start it again.")
        _kill(session.proc)
    return session


def _active_runs() -> int:
    return int(getattr(cct, "_ACTIVE_RUNS", 0) or 0) + len(cct._active_task_ids())


# ── public API ──

def status() -> dict:
    session = _current()
    if session is None:
        return {"state": "idle", "overriding_credentials": overriding_credentials()}
    out = session.view()
    out["overriding_credentials"] = overriding_credentials()
    return out


async def start(method: str = "claudeai") -> dict:
    """Start ``claude auth login`` and wait until it prints the link (or fails)."""
    global _SESSION, _LAST_START
    method = (method or "claudeai").strip().lower()
    if method not in METHODS:
        raise LoginError("method must be 'claudeai' (Claude subscription) or 'console' (API billing)")
    async with _lock():
        now = time.monotonic()
        if _LAST_START and now - _LAST_START < START_INTERVAL_S:
            wait = int(START_INTERVAL_S - (now - _LAST_START)) + 1
            raise LoginError(f"A sign-in was started moments ago; try again in {wait}s.", 429)
        problem = _binary_ready()
        if problem:
            raise LoginError(problem, 400)
        if cct.update_in_progress():
            raise LoginError("Claude Code is being updated; start the sign-in when the update finishes.", 409)
        _LAST_START = now
        previous = _SESSION
        if previous is not None and previous.state not in TERMINAL:
            previous._set(CANCELLED, error="Replaced by a new sign-in.")
            await _stop(previous)
        session = LoginSession(method)
        _SESSION = session
        logger.info("[claude-login] session=%s starting (method=%s)", session.id[:8], method)
        argv = [*_binary_argv(), "auth", "login", f"--{method}"]
        home = cct._claude_home()
        try:
            session.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=_login_environment(),
                cwd=home if os.path.isdir(home) else None, **_spawn_kwargs(),
            )
        except OSError as exc:
            session._set(FAILED, error=f"Could not start Claude Code: {exc.strerror or exc}")
            return session.view()
        readers = [asyncio.ensure_future(_pump(session, session.proc.stdout, stderr=False)),
                   asyncio.ensure_future(_pump(session, session.proc.stderr, stderr=True))]
        session._tasks = [*readers, asyncio.ensure_future(_watch(session, readers))]
    deadline = time.monotonic() + URL_WAIT_S
    while session.state == STARTING and time.monotonic() < deadline:
        session._changed.clear()
        try:
            await asyncio.wait_for(session._changed.wait(), timeout=max(0.05, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
    if session.state == STARTING:
        session._set(FAILED, error="Claude Code did not print a sign-in link. "
                                   + (session.failure_message() or "Check the binary with Check status."))
        await _stop(session)
    return session.view()


async def submit_code(session_id: str, code: str) -> dict:
    """Write the pasted code to the waiting CLI and wait for its verdict."""
    session = _current()
    if session is None or not session_id or not secrets.compare_digest(str(session_id), session.id):
        raise LoginError("No such sign-in; start it again.", 404)
    code = str(code or "").strip()
    if session.state != AWAITING_CODE:
        raise LoginError(f"This sign-in is {session.state.replace('_', ' ')}, not waiting for a code.", 409)
    if not code or len(code) > MAX_CODE_CHARS or not _CODE.fullmatch(code):
        # The CLI would say the same; answering here keeps the process waiting
        # and a malformed paste out of its stdin.
        raise LoginError("That does not look like the whole code. Copy the complete code from the "
                         "authorization page (it contains a '#').", 400)
    proc = session.proc
    if proc is None or proc.returncode is not None or proc.stdin is None:
        raise LoginError("The sign-in process is gone; start it again.", 409)
    session._scrub = [code, *code.split("#")]
    invalid_before = session._invalid_seen
    session.error = None
    session._set(VERIFYING)
    try:
        proc.stdin.write((code + "\n").encode("utf-8"))
        await proc.stdin.drain()
    except (ConnectionError, OSError):
        pass  # the watcher reports why the process ended
    finally:
        code = ""
    deadline = time.monotonic() + VERIFY_WAIT_S
    while session.state not in TERMINAL and time.monotonic() < deadline:
        if session._invalid_seen > invalid_before:
            session._scrub = []
            session._set(AWAITING_CODE, error="Claude Code rejected the code as incomplete. Copy the whole "
                                              "code from the authorization page and paste it again.")
            break
        session._changed.clear()
        try:
            await asyncio.wait_for(session._changed.wait(), timeout=min(1.0, max(0.05, deadline - time.monotonic())))
        except asyncio.TimeoutError:
            pass
    if session.state == VERIFYING and session.exit_code is None and time.monotonic() >= deadline:
        session._set(FAILED, error="Claude Code did not finish signing in within "
                                   f"{VERIFY_WAIT_S}s after the code was submitted.")
        await _stop(session)
    return session.view()


async def _stop(session: LoginSession) -> None:
    for task in session._tasks:
        if not task.done():
            task.cancel()
    await _reap(session.proc)
    if session._tasks:
        await asyncio.gather(*session._tasks, return_exceptions=True)


async def cancel(session_id: Optional[str] = None) -> dict:
    session = _SESSION
    if session is None:
        return {"state": "idle"}
    if session_id and not secrets.compare_digest(str(session_id), session.id):
        raise LoginError("No such sign-in.", 404)
    if session.state not in TERMINAL:
        session._set(CANCELLED, error="Sign-in cancelled.")
    await _stop(session)
    return session.view()


async def logout() -> dict:
    """``claude auth logout``: removes the CLI's stored login. Refused while a
    delegation runs (it would lose its credential mid-task) or during an
    update."""
    if _active_runs():
        raise LoginError("Claude Code delegations are running; wait for them or cancel them before "
                         "signing out.", 409)
    if cct.update_in_progress():
        raise LoginError("Claude Code is being updated; sign out when the update finishes.", 409)
    problem = _binary_ready()
    if problem:
        raise LoginError(problem, 400)
    session = _SESSION
    if session is not None and session.state not in TERMINAL:
        await cancel(session.id)
    logger.info("[claude-login] logout requested")
    try:
        proc = await asyncio.create_subprocess_exec(
            *_binary_argv(), "auth", "logout", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_login_environment(),
            **_spawn_kwargs(),
        )
    except OSError as exc:
        raise LoginError(f"Could not start Claude Code: {exc.strerror or exc}", 500)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=LOGOUT_TIMEOUT_S)
    except asyncio.TimeoutError:
        await _reap(proc)
        raise LoginError("claude auth logout did not finish in time.", 504)
    text = strip_ansi((out or b"").decode("utf-8", errors="replace")).strip()
    from src.agent_logs import redact_text
    auth = await cct.auth_status()
    result: dict[str, Any] = {
        "exit_code": proc.returncode,
        "message": redact_text(text[-600:]) if text else "",
        "logged_in": auth.get("logged_in"),
        "auth_method": auth.get("auth_method"),
    }
    overriding = overriding_credentials()
    if auth.get("logged_in") and overriding:
        result["note"] = (", ".join(overriding) + " is set in the container environment, so Claude Code "
                          "stays authenticated; remove it from the container to sign out completely.")
    logger.info("[claude-login] logout finished exit=%s logged_in=%s", proc.returncode, auth.get("logged_in"))
    return result


def shutdown() -> None:
    """Kill a pending login process (app shutdown / interpreter exit)."""
    session = _SESSION
    if session is None:
        return
    if session.state not in TERMINAL:
        session.state = CANCELLED
        session.finished_at = time.time()
    _kill(session.proc)


async def ashutdown() -> None:
    session = _SESSION
    if session is None:
        return
    if session.state not in TERMINAL:
        session._set(CANCELLED, error="Odysseus is shutting down.")
    await _stop(session)


def _reset_for_tests() -> None:
    global _SESSION, _LAST_START, _LOCK
    shutdown()
    _SESSION, _LAST_START, _LOCK = None, 0.0, None


atexit.register(shutdown)
