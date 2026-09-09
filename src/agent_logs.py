"""Read-only access to Odysseus's own application logs.

The agent debugs the app it runs inside, so it needs to see what the app
recorded. Two things make that safe to expose:

* **Only known log roots.** A log is addressed by *name*, resolved against a
  fixed set of directories (the app log dir, the repo ``logs/`` folder, the tmux
  serve logs). A caller-supplied path is never opened, so there is no traversal
  surface and no way to read an arbitrary file through this tool.
* **Redaction on the way out.** Logs contain endpoint URLs with userinfo,
  ``Authorization`` headers, API keys and tokens that other subsystems logged
  before anyone thought about an agent reading them back. Every line is scrubbed
  before it is returned.

The tool is read-only: it lists, tails, and greps. It cannot delete or rotate.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from core.log_safety import redact_url
from src.constants import DATA_DIR

MAX_LINES = 500
DEFAULT_LINES = 100
MAX_LINE_CHARS = 4000
# Tail window. Reading the whole of a rotated 5 MB log to return 100 lines
# wastes memory for no benefit, so only the tail is read off disk.
_TAIL_BYTES = 2 * 1024 * 1024

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def log_roots() -> List[str]:
    """Directories that may contain readable logs, most specific first."""
    from src.runtime_paths import get_app_root

    roots = [
        os.path.join(DATA_DIR, "logs"),
        os.path.join(get_app_root(), "logs"),
        os.path.join("/tmp", "odysseus-tmux"),
    ]
    seen, out = set(), []
    for root in roots:
        real = os.path.realpath(root)
        if real not in seen:
            seen.add(real)
            out.append(real)
    return out


@dataclass(frozen=True)
class LogFile:
    name: str
    path: str
    size: int
    modified: float


def _is_log_name(name: str) -> bool:
    # app.log plus its RotatingFileHandler siblings app.log.1 .. app.log.N
    return name.endswith(".log") or bool(re.match(r".+\.log\.\d+$", name))


def list_logs() -> List[LogFile]:
    found: Dict[str, LogFile] = {}
    for root in log_roots():
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if not _is_log_name(entry):
                continue
            path = os.path.join(root, entry)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            # First root wins so the app's own log shadows a same-named file.
            found.setdefault(
                entry, LogFile(name=entry, path=path, size=st.st_size, modified=st.st_mtime)
            )
    return sorted(found.values(), key=lambda f: f.modified, reverse=True)


def resolve(name: Optional[str]) -> Optional[LogFile]:
    """Find a log by name. Never accepts a path.

    Any separator or ``..`` in the input is a rejection rather than something to
    normalize: the only legal input is a bare file name that appears in
    :func:`list_logs`.
    """
    logs = list_logs()
    if not logs:
        return None
    if not name or not name.strip():
        # Default to the application log when it exists, else the newest file.
        for candidate in logs:
            if candidate.name == "app.log":
                return candidate
        return logs[0]
    raw = name.strip()
    if "/" in raw or "\\" in raw or ".." in raw or os.path.isabs(raw):
        return None
    for candidate in logs:
        if candidate.name == raw:
            return candidate
    stem = raw[:-4] if raw.endswith(".log") else raw
    for candidate in logs:
        if candidate.name.startswith(stem):
            return candidate
    return None


# Secret-shaped fragments seen in real log lines. Each pattern keeps the label
# so the line stays diagnosable, and replaces only the value.
_REDACTIONS = (
    # Consumes the scheme *and* the credential: matching only the first token
    # would rewrite "Authorization: Bearer <token>" to "Authorization: ***
    # <token>" and leave the secret in place.
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*"
                r"[^\s,;\"']+(?:\s+[^\s,;\"']+)?"), r"\1: ***"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)"
                r"(\"?\s*[:=]\s*\"?)([^\s\"',;]{4,})"), r"\1\2***"),
    # Provider key shapes that appear without a nearby label.
    (re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_\-]{16,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "***"),
    # Fine-grained PAT — the fallback credential .env.example documents.
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "***"),
    (re.compile(r"\bghs_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "***"),
)

_URL_WITH_USERINFO = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s\"'<>]*@[^\s\"'<>]*")
_URL_WITH_QUERY = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s\"'<>]*\?[^\s\"'<>]*")


def redact_line(line: str) -> str:
    """Remove credential material from one log line."""
    out = line or ""
    # URLs first: userinfo and query strings carry keys, and redact_url strips
    # both while keeping scheme/host/path readable.
    for pattern in (_URL_WITH_USERINFO, _URL_WITH_QUERY):
        out = pattern.sub(lambda m: redact_url(m.group(0)), out)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out[:MAX_LINE_CHARS]


def _tail_lines(path: str, limit: int) -> List[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the partial first line
            data = fh.read()
    except OSError as exc:
        raise RuntimeError(f"cannot read log: {exc}")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-limit:] if limit > 0 else lines


def read_log(
    name: Optional[str] = None,
    *,
    lines: int = DEFAULT_LINES,
    contains: Optional[str] = None,
    level: Optional[str] = None,
) -> Dict[str, object]:
    """Return the tail of one log, filtered and redacted."""
    target = resolve(name)
    if target is None:
        available = [f.name for f in list_logs()]
        raise RuntimeError(
            f"no log named {name!r}. Available: " + (", ".join(available) or "(none)")
        )
    try:
        count = int(lines)
    except (TypeError, ValueError):
        count = DEFAULT_LINES
    count = max(1, min(MAX_LINES, count))

    # Filtering happens before the tail is trimmed, so "last 100 ERROR lines"
    # means what it says instead of "errors within the last 100 lines".
    raw = _tail_lines(target.path, 0 if (contains or level) else count)

    needle = (contains or "").lower() or None
    wanted_level = (level or "").strip().upper() or None
    if wanted_level and wanted_level not in LEVELS:
        raise RuntimeError(f"level must be one of {', '.join(LEVELS)}")
    if wanted_level:
        # Match the level token as the app's formatter writes it.
        allowed = set(LEVELS[LEVELS.index(wanted_level):])
        raw = [ln for ln in raw if any(f" - {lv} - " in ln or ln.startswith(lv) for lv in allowed)]
    if needle:
        raw = [ln for ln in raw if needle in ln.lower()]
    if contains or wanted_level:
        raw = raw[-count:]

    return {
        "name": target.name,
        "path": target.path,
        "size": target.size,
        "modified": datetime.fromtimestamp(target.modified).isoformat(timespec="seconds"),
        "line_count": len(raw),
        "lines": [redact_line(ln) for ln in raw],
    }


def logs_index() -> List[Dict[str, object]]:
    return [
        {
            "name": f.name,
            "path": f.path,
            "bytes": f.size,
            "modified": datetime.fromtimestamp(f.modified).isoformat(timespec="seconds"),
        }
        for f in list_logs()
    ]
