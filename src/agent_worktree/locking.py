"""Cross-process lock for worktree and approval state.

Two things must not interleave: a git operation on the shared worktree, and the
read-modify-write that marks an approval used. Both run from a web worker, a
background task, and an operator CLI, so an in-process lock is not enough.

Implementation is an O_EXCL lock file rather than fcntl so the same code works
on Windows, where the app is also supported. A holder that died without
cleaning up is reclaimed only when its PID is gone or the lock is older than
the stale timeout — never on a bare timeout alone, which would let two live
processes both believe they hold it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import socket
import time
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_STALE_AFTER_S = 900.0
_POLL_S = 0.1


class LockBusy(RuntimeError):
    """Another process holds the lock and did not release it in time."""


def _read_holder(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _holder_is_dead(info: Optional[dict], stale_after_s: float, path: str) -> bool:
    """True when the recorded holder can be safely displaced."""
    if not info:
        # Unreadable, truncated, or deliberately clobbered lock file. Falling
        # through to "dead" here would let anything that can write the lock file
        # break mutual exclusion on demand, so displace it only once the file
        # itself is older than the stale window.
        try:
            age = time.time() - os.stat(path).st_mtime
        except OSError:
            return True  # it vanished; the next O_EXCL attempt decides
        return age > stale_after_s
    if info.get("host") != socket.gethostname():
        # Another machine (shared volume). PID liveness is meaningless here, so
        # fall back to age alone.
        return (time.time() - float(info.get("at") or 0)) > stale_after_s
    pid = info.get("pid")
    try:
        from core.platform_compat import pid_alive

        if not pid_alive(int(pid)):
            return True
    except Exception:
        pass
    return (time.time() - float(info.get("at") or 0)) > stale_after_s


@contextlib.contextmanager
def file_lock(
    path: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> Iterator[None]:
    """Hold an exclusive lock at `path` for the duration of the block."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "host": socket.gethostname(), "at": time.time()}
    ).encode("utf-8")
    deadline = time.monotonic() + max(0.0, timeout_s)
    fd = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if _holder_is_dead(_read_holder(path), stale_after_s, path):
                # Reclaim. A racing reclaimer may unlink first; the next
                # O_EXCL attempt decides the winner, so ignore the miss.
                with contextlib.suppress(OSError):
                    os.unlink(path)
                continue
            if time.monotonic() >= deadline:
                raise LockBusy(f"could not acquire lock {os.path.basename(path)}")
            time.sleep(_POLL_S)
    try:
        with contextlib.suppress(OSError):
            os.write(fd, payload)
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
