"""One-click diagnostics bundle: recent logs plus the configuration behind them.

Pasting raw ``app.log`` lines into another tool loses what is needed to
diagnose an agent problem: which chat or worker a line belongs to, which
loadout, model and tool policy it ran under, whether the shell or private vault
was granted, how workers link to their parents, and the state of Claude Code
and the MCP servers. This module packs all of that into one zip:

* ``manifest.json`` / ``README.txt`` — window, versions, file list, what was
  redacted, what failed, and how to read the rest.
* ``logs/<name>`` — every known log (``src.agent_logs``), limited to the time
  window and a per-file line cap, each line through the existing redaction.
* ``sessions/<id>.json`` — each chat the logs (or the caller) mention: model,
  endpoint, effective tool policy, loadout, worker lineage and runs, and a
  content-free summary of the last turn. Message text only when asked.
* ``loadouts/<name>.json`` — the portable export of each referenced loadout
  (``src.agent_profile_transfer``, already redacted) plus its readiness.
* ``system/`` — Claude Code status, MCP servers, service health, scheduler
  lanes, and the settings store with secret-shaped values masked.

Every component is collected under a guard: one failure becomes an entry in
``manifest.errors`` instead of failing the export. The bundle has a hard size
cap; anything cut is listed in ``manifest.truncated``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import platform
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core.log_safety import redact_url
from src import agent_logs
from src.agent_logs import redact_line, redact_text

logger = logging.getLogger(__name__)

FORMAT = "odysseus-diagnostics-bundle"
VERSION = 1

DEFAULT_MINUTES = 60
MAX_MINUTES = 7 * 24 * 60
DEFAULT_MAX_LINES = 20_000          # per log file, newest kept
MAX_MAX_LINES = 200_000
MAX_SESSIONS = 25
MAX_LOADOUTS = 20
MAX_RUN_CANDIDATES = 200
MESSAGES_PER_SESSION = 20
MESSAGE_CHARS = 4000
MAX_BUNDLE_BYTES = 20 * 1024 * 1024  # uncompressed, across every file
_MANIFEST_RESERVE = 512 * 1024
# A rotated log is 5 MB; this only guards a log someone pointed elsewhere.
_READ_BYTES = 32 * 1024 * 1024
_COMPONENT_TIMEOUT_S = 30

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID_RE = re.compile(_UUID, re.I)
_UUID_FULL_RE = re.compile(rf"^{_UUID}$", re.I)
# "session=<id>", "for chat <id>", '"session_id": "<id>"', "worker_session=",
# "parent_session=", "child=<id>" (the loadout start line), "sid=".
_SESSION_CTX_RE = re.compile(
    r"(?i)\b(?:session(?:_id)?|chat|worker_session|parent_session|target_session|child|sid)"
    r"[\"']?\s*[=:]?\s*[\"']?(" + _UUID + r")"
)
_SESSION_SLUG_RE = re.compile(r"\b(session-[A-Za-z0-9_]{4,40})\b")
# agent_activity.new_run_id: "<source>-<10 hex>" (odysseus-…, subagent-…, claude_code-…)
_RUN_ID_RE = re.compile(r"\b([a-z][a-z_]{1,30}-[0-9a-f]{10})\b")
_LOADOUT_START_RE = re.compile(r"\bloadout=(.{1,60}?) run=")
_LOADOUT_KV_RE = re.compile(r"\b(?:loadout|profile|agent_profile)=([^\s,;'\"]{1,60})")
_LOADOUT_JSON_RE = re.compile(r"[\"'](?:name|loadout|profile|agent_profile)[\"']\s*:\s*[\"']([^\"'\\]{1,60})[\"']")

# Keys whose string values are masked in every JSON file of the bundle.
_SECRET_KEY_RE = re.compile(r"(?i)secret|token|key|password|passwd|credential|cookie|auth")
_PATH_KEY_SUFFIXES = ("_file", "_path", "_dir", "_home", "_root")
_PATH_VALUE_RE = re.compile(r"^(?:/|~|\./|[A-Za-z]:[\\/])")
# "auth_method": "oauth_token" says which kind of credential is in use, not the
# credential; masking it would hide whether Claude Code is on a subscription
# or an API key.
_LABEL_KEY_SUFFIXES = ("_method", "_mode", "_type", "_source", "_status", "_provider", "_kind")
_LABEL_VALUE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
# Opaque credential-shaped strings with no label: long, mixed case + digits,
# no path separator. Model ids and UUIDs do not fit this shape.
_OPAQUE_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-+=.]{40,}$")
MASK = "***"

REDACTION_NOTICE = (
    "Log lines pass through src.agent_logs.redact_line (Authorization headers, bearer/basic "
    "tokens, api_key/password/secret values, provider key shapes, JWTs, and URL userinfo/query "
    "strings). Every JSON file is additionally masked: string values under keys matching "
    "secret|token|key|password|credential|cookie|auth become '***' (paths to a token file are "
    "kept), long opaque credential-shaped strings become '***', and endpoint URLs keep only "
    "scheme/host/path. Redaction is pattern-based: skim the bundle before sharing it outside "
    "your own machines."
)

HOW_TO_READ = [
    "manifest.json: the window, versions, what each file is, what failed (errors) and what was cut (truncated).",
    "logs/: each log limited to the window; timestamps are the server's local time. Tool lists a turn "
    "resolved appear in logs as '[agent-intent] selected_tools=' and '[agent-debug] ... tool_names=' lines.",
    "discovery (in manifest): the chats, runs and loadouts the logs mention, most recent first. "
    "Session ids that the logs name but that no longer exist are listed as not found.",
    "sessions/<id>.json: model + endpoint, the chat's stored settings, effective_policy (the tool "
    "allow/deny set, private_vault_access, approval mode, delegation policy, worker limit, depth), "
    "loadout, lineage (parent chat, child workers, runs with status/rounds/tool calls/errors) and "
    "last_turn (tools called, exit codes, models per round). No message text unless the bundle was "
    "built with include_messages.",
    "loadouts/<name>.json: the portable loadout export plus readiness as a worker of a top-level chat.",
    "system/: Claude Code status, MCP servers (connection state only), service health, scheduler "
    "lanes, and the settings store with secrets masked.",
]


# ── masking ─────────────────────────────────────────────────────────────── #

def _is_secret_key(key: Any) -> bool:
    return bool(key) and isinstance(key, str) and bool(_SECRET_KEY_RE.search(key))


def looks_like_secret(value: str) -> bool:
    """True for an unlabelled credential-shaped string (not a UUID or model id)."""
    if not isinstance(value, str) or not _OPAQUE_SECRET_RE.match(value):
        return False
    if _UUID_FULL_RE.match(value) or re.fullmatch(r"[0-9a-f]+", value, re.I):
        return False
    return (any(c.isdigit() for c in value) and any(c.isupper() for c in value)
            and any(c.islower() for c in value))


def _mask_string(key: Any, value: str, secret_key: bool) -> str:
    if not value:
        return value
    if secret_key:
        keyname = str(key or "").lower()
        if keyname.endswith(_PATH_KEY_SUFFIXES) and _PATH_VALUE_RE.match(value):
            return value  # where a token lives, not the token
        if keyname.endswith(_LABEL_KEY_SUFFIXES) and _LABEL_VALUE_RE.match(value):
            return value  # which kind of credential, not the credential
        return MASK
    if looks_like_secret(value):
        return MASK
    return redact_text(value)


def mask(value: Any, key: Any = None, _secret: bool = False) -> Any:
    """Deep copy of ``value`` safe to ship: secret-keyed strings masked, the
    shape kept (a masked value stays present, so "is it set" still reads).
    Numbers, booleans and None are kept — ``max_tokens: 4096`` is not a secret."""
    secret = _secret or _is_secret_key(key)
    if isinstance(value, dict):
        return {str(k): mask(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [mask(item, key, secret) for item in items]
    if isinstance(value, str):
        return _mask_string(key, value, secret)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return _mask_string(key, str(value), secret)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def _err_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {redact_text(str(exc))[:500]}"


# ── bundle assembly ─────────────────────────────────────────────────────── #

@dataclass
class _Bundle:
    max_bytes: int
    files: List[Tuple[str, bytes]] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    truncated: List[Dict[str, Any]] = field(default_factory=list)
    used: int = 0

    def guard(self, component: str, fn: Callable, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # one component never fails the export
            logger.warning("diagnostics bundle: %s failed: %s", component, type(exc).__name__)
            self.errors.append({"component": component, "error": _err_text(exc)})
            return None

    def _room(self) -> int:
        return max(0, self.max_bytes - _MANIFEST_RESERVE - self.used)

    def add_json(self, path: str, obj: Any) -> bool:
        data = _json_bytes(mask(obj))
        if len(data) > self._room():
            self.truncated.append({"path": path, "reason": "size cap", "bytes": len(data)})
            data = _json_bytes({"truncated": True, "reason": "bundle size cap reached",
                                "original_bytes": len(data)})
            if len(data) > self._room():
                return False
        self.files.append((path, data))
        self.used += len(data)
        return True

    def add_lines(self, path: str, lines: List[str]) -> Dict[str, Any]:
        """Add a log, dropping its oldest lines if the size cap requires."""
        encoded = [(ln + "\n").encode("utf-8") for ln in lines]
        total = sum(len(b) for b in encoded)
        room = self._room()
        kept = encoded
        if total > room:
            kept, size = [], 0
            for chunk in reversed(encoded):
                if size + len(chunk) > room:
                    break
                kept.append(chunk)
                size += len(chunk)
            kept.reverse()
            self.truncated.append({"path": path, "reason": "size cap: oldest lines dropped",
                                   "lines_dropped": len(encoded) - len(kept)})
        if not kept and lines:
            return {"written_lines": 0}
        data = b"".join(kept)
        self.files.append((path, data))
        self.used += len(data)
        return {"written_lines": len(kept)}

    def add_raw(self, path: str, data: bytes) -> None:
        self.files.append((path, data))
        self.used += len(data)

    def to_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path, data in self.files:
                zf.writestr(path, data)
        return buf.getvalue()


def _safe_filename(name: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._")
    return (cleaned or fallback)[:80]


# ── logs ────────────────────────────────────────────────────────────────── #

def _read_lines(path: str) -> List[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > _READ_BYTES:
            fh.seek(size - _READ_BYTES)
            fh.readline()
        data = fh.read()
    return data.decode("utf-8", errors="replace").splitlines()


def collect_logs(minutes: float, max_lines: int, errors: Optional[List[Dict[str, str]]] = None
                 ) -> List[Dict[str, Any]]:
    """Every known log limited to the window, newest ``max_lines`` kept, redacted.

    A log with no timestamped lines at all (a tmux serve log) keeps its tail
    when it was written to inside the window, since there is nothing to filter
    on.
    """
    since = datetime.now() - timedelta(minutes=minutes)
    since_ts = since.timestamp()
    out: List[Dict[str, Any]] = []
    for log in agent_logs.list_logs():
        entry: Dict[str, Any] = {"name": log.name, "bytes": log.size,
                                 "modified": datetime.fromtimestamp(log.modified).isoformat(timespec="seconds")}
        if log.modified < since_ts:
            entry.update(skipped="not written to inside the window", lines=[], lines_in_window=0)
            out.append(entry)
            continue
        try:
            raw = _read_lines(log.path)
        except OSError as exc:
            if errors is not None:
                errors.append({"component": f"logs/{log.name}", "error": _err_text(exc)})
            continue
        kept = agent_logs._entries_since(raw, since)
        if not kept and not any(agent_logs._ENTRY_TIME_RE.match(ln) for ln in raw[:2000]):
            kept = raw
            entry["note"] = "no timestamps in this log; its tail is included"
        entry["lines_in_window"] = len(kept)
        if len(kept) > max_lines:
            entry["line_cap"] = max_lines
            kept = kept[-max_lines:]
        entry["lines"] = [redact_line(ln) for ln in kept]
        out.append(entry)
    return out


# ── discovery ───────────────────────────────────────────────────────────── #

def _ranked(values: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def scan_references(lines_newest_first: Iterable[str]) -> Dict[str, List[str]]:
    """Candidate session ids, run ids and loadout names a log mentions, most
    recent first. Pure: nothing here is checked against the stores."""
    ctx_sessions: List[str] = []
    bare_uuids: List[str] = []
    runs: List[str] = []
    loadouts: List[str] = []
    for line in lines_newest_first:
        for m in _SESSION_CTX_RE.finditer(line):
            ctx_sessions.append(m.group(1).lower())
        for m in _SESSION_SLUG_RE.finditer(line):
            ctx_sessions.append(m.group(1))
        for m in _UUID_RE.finditer(line):
            bare_uuids.append(m.group(0).lower())
        for m in _RUN_ID_RE.finditer(line):
            runs.append(m.group(1))
        started = [m.group(1).strip() for m in _LOADOUT_START_RE.finditer(line)]
        loadouts.extend(started)
        if not started:  # "loadout=Lead Engineer run=" must not also yield "Lead"
            loadouts.extend(m.group(1).strip() for m in _LOADOUT_KV_RE.finditer(line))
        if "loadout" in line.lower():
            for m in _LOADOUT_JSON_RE.finditer(line):
                loadouts.append(m.group(1).strip())
    return {
        "sessions": _ranked(ctx_sessions),
        "uuids": _ranked(u for u in bare_uuids),
        "runs": _ranked(runs),
        "loadouts": _ranked(n for n in loadouts if n and n.lower() not in ("none", "null", "-")),
    }


def _existing_sessions(ids: List[str]) -> Dict[str, Optional[str]]:
    """{id: owner} for the ids that exist."""
    if not ids:
        return {}
    from core.database import Session, get_db_session

    found: Dict[str, Optional[str]] = {}
    with get_db_session() as db:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            for sid, owner in db.query(Session.id, Session.owner).filter(Session.id.in_(chunk)).all():
                found[sid] = owner
    return found


def discover(logs: List[Dict[str, Any]], session_ids: Optional[List[str]] = None,
             max_sessions: int = MAX_SESSIONS, errors: Optional[List[Dict[str, str]]] = None
             ) -> Dict[str, Any]:
    """Resolve what the logs mention against the session, run and loadout stores."""
    def lines_newest_first():
        for log in logs:  # list_logs is newest file first
            yield from reversed(log.get("lines") or [])

    refs = scan_references(lines_newest_first())
    explicit = _ranked(s.strip() for s in (session_ids or []) if s and s.strip())

    runs: List[Dict[str, Any]] = []
    run_sessions: List[str] = []
    try:
        from src import agent_activity

        for rid in refs["runs"][:MAX_RUN_CANDIDATES]:
            rec = agent_activity.get_run(rid)
            if not rec:
                continue
            summary = rec.get("summary") or {}
            runs.append({"run_id": rid, "session_id": rec.get("session_id"),
                         "source": rec.get("source"), "status": rec.get("status"),
                         "parent_session": summary.get("parent_session"),
                         "target_session": summary.get("target_session"),
                         "profile": summary.get("profile")})
            for sid in (rec.get("session_id"), summary.get("target_session"), summary.get("parent_session")):
                if sid:
                    run_sessions.append(str(sid))
            if summary.get("profile"):
                refs["loadouts"].append(str(summary["profile"]))
    except Exception as exc:
        if errors is not None:
            errors.append({"component": "discovery/runs", "error": _err_text(exc)})

    labelled = _ranked(explicit + refs["sessions"] + run_sessions)
    candidates = _ranked(labelled + refs["uuids"])
    try:
        existing = _existing_sessions(candidates[:2000])
    except Exception as exc:
        existing = {}
        if errors is not None:
            errors.append({"component": "discovery/sessions", "error": _err_text(exc)})
    ordered = [sid for sid in candidates if sid in existing]
    # Explicitly requested chats first, then the most recently mentioned.
    chosen = _ranked([s for s in explicit if s in existing] + ordered)
    capped = len(chosen) > max_sessions
    return {
        "sessions": chosen[:max_sessions],
        "session_owners": {sid: existing.get(sid) for sid in chosen[:max_sessions]},
        "sessions_capped": capped,
        "sessions_total": len(chosen),
        # Only ids the logs *labelled* as sessions are worth reporting missing;
        # a bare UUID could be a message or request id.
        "sessions_not_found": [s for s in labelled if s not in existing][:50],
        "runs": runs[:100],
        "loadout_mentions": _ranked(refs["loadouts"]),
    }


def resolve_loadouts(names: Iterable[str], limit: int = MAX_LOADOUTS) -> Tuple[List[str], List[str]]:
    """(stored loadout names, mentioned names that are not stored loadouts)."""
    from src import agent_profiles

    by_key = {p["name"].casefold(): p["name"] for p in agent_profiles.load_profiles()}
    found, missing = [], []
    for name in _ranked(str(n).strip() for n in names if n):
        real = by_key.get(name.casefold())
        if real:
            if real not in found:
                found.append(real)
        elif name not in missing:
            missing.append(name)
    return found[:limit], missing[:50]


# ── sessions ────────────────────────────────────────────────────────────── #

def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _jsonable_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in policy.items():
        if key == "known_tools":
            out["known_tools_count"] = len(value or ())
            continue
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        out[key] = value
    allowed = set(out.get("allowed_tools") or [])
    out["allowed_tools_count"] = len(allowed)
    out["bash_allowed"] = "bash" in allowed
    return out


def _tool_events(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = meta.get("tool_events")
    if not isinstance(events, list):
        events = (meta.get("metrics") or {}).get("tool_events") if isinstance(meta.get("metrics"), dict) else None
    rows = []
    for ev in (events or [])[:120]:
        if not isinstance(ev, dict):
            continue
        row = {"tool": ev.get("tool") or ev.get("name"), "exit_code": ev.get("exit_code"),
               "round": ev.get("round")}
        if ev.get("error"):
            row["error"] = redact_text(str(ev["error"]))[:200]
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def _last_turn(messages: List[Any]) -> Optional[Dict[str, Any]]:
    """Content-free summary of the newest assistant message that has metadata."""
    for msg in messages:  # newest first
        if msg.role != "assistant" or not msg.meta_data:
            continue
        try:
            meta = json.loads(msg.meta_data)
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        metrics = meta.get("metrics") if isinstance(meta.get("metrics"), dict) else meta
        out = {
            "timestamp": _iso(msg.timestamp),
            "model": metrics.get("model") or meta.get("model"),
            "round_models": metrics.get("round_models"),
            "round_endpoint_labels": metrics.get("round_endpoint_labels"),
            "run_id": meta.get("run_id"),
            "status": meta.get("status"),
            "error": redact_text(str(meta["error"]))[:500] if meta.get("error") else None,
            "input_tokens": metrics.get("input_tokens"),
            "output_tokens": metrics.get("output_tokens"),
            "context_percent": metrics.get("context_percent"),
            "response_time": metrics.get("response_time"),
            "tool_calls": _tool_events(meta),
        }
        return {k: v for k, v in out.items() if v not in (None, [], "")}
    return None


def _run_row(run: Dict[str, Any], include_messages: bool) -> Dict[str, Any]:
    """The manage_agent_loadout status row, plus the lineage fields."""
    summary = run.get("summary") or {}
    progress = run.get("progress") or {}
    row = {
        "run_id": run.get("run_id"), "source": run.get("source"), "status": run.get("status"),
        "title": run.get("title"), "session_id": run.get("session_id"),
        "worker_session": summary.get("target_session") or run.get("session_id"),
        "parent_session": summary.get("parent_session"), "parent_run_id": summary.get("parent_run_id"),
        "workflow_id": summary.get("workflow_id"),
        "loadout": summary.get("profile"), "model": summary.get("model"), "mode": summary.get("mode"),
        "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
        "tool_calls": summary.get("steps") or progress.get("tool_calls"),
        "round": progress.get("round"), "current_tool": progress.get("current_tool"),
        "max_rounds": summary.get("max_rounds"),
        "ran_out_of_rounds": bool(summary.get("rounds_exhausted")) or None,
        "error": redact_text(str(summary["error"]))[:500] if summary.get("error") else None,
    }
    if include_messages and summary.get("result_excerpt"):
        row["result_excerpt"] = redact_text(str(summary["result_excerpt"]))[:MESSAGE_CHARS]
    return {k: v for k, v in row.items() if v not in (None, "")}


def session_record(session_id: str, *, owner: Optional[str] = None, include_messages: bool = False,
                   messages_limit: int = MESSAGES_PER_SESSION) -> Dict[str, Any]:
    """Everything about one chat needed to explain a log line, minus its text."""
    from core.database import ChatMessage, Session, get_db_session, get_session_settings

    rec: Dict[str, Any] = {"id": session_id}
    with get_db_session() as db:
        row = db.query(Session).filter(Session.id == session_id).first()
        if row is None:
            return {"id": session_id, "found": False}
        rec.update({
            "found": True,
            "title": row.name, "mode": row.mode, "model": row.model,
            "endpoint": redact_url(row.endpoint_url or ""),
            "header_names": sorted((row.headers or {}).keys()) if isinstance(row.headers, dict) else [],
            "owner": row.owner, "archived": row.archived, "rag": row.rag, "folder": row.folder,
            "crew_member_id": row.crew_member_id, "forked_from": row.forked_from,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
            "last_message_at": _iso(row.last_message_at), "message_count": row.message_count,
            "total_input_tokens": row.total_input_tokens, "total_output_tokens": row.total_output_tokens,
        })
        session_owner = row.owner
        recent = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
                  .order_by(ChatMessage.timestamp.desc()).limit(max(50, messages_limit)).all())
        rec["last_turn"] = _last_turn(recent)
        if include_messages:
            if owner is not None and session_owner not in (owner, None):
                rec["messages_withheld"] = "this chat belongs to another user"
            else:
                rec["messages"] = [{
                    "role": m.role, "timestamp": _iso(m.timestamp),
                    "content": redact_text(m.content or "")[:MESSAGE_CHARS],
                    "content_truncated": len(m.content or "") > MESSAGE_CHARS,
                } for m in reversed(recent[:messages_limit])]

    settings = get_session_settings(session_id) or {}
    rec["settings"] = settings
    rec["loadout"] = settings.get("agent_profile")
    try:
        from src.agent_loadouts import caller_policy

        rec["effective_policy"] = _jsonable_policy(caller_policy(session_id, session_owner))
    except Exception as exc:
        rec["effective_policy"] = {"error": _err_text(exc)}

    lineage: Dict[str, Any] = {"parent_session": settings.get("parent_session")}
    try:
        from src.headless_agent import worker_depth

        lineage["depth"] = worker_depth(session_id)
    except Exception as exc:
        lineage["depth_error"] = _err_text(exc)
    try:
        from src import agent_activity

        runs = agent_activity.list_runs(session_id=session_id, limit=50, include_descendants=True)
        rows = [_run_row(r, include_messages) for r in runs]
        lineage["runs"] = rows
        lineage["children"] = _ranked(
            r["worker_session"] for r in rows
            if r.get("parent_session") == session_id and r.get("worker_session") not in (None, session_id)
        )
        lineage["started_by"] = [r for r in rows if r.get("worker_session") == session_id
                                 and r.get("parent_session")][:5]
    except Exception as exc:
        lineage["runs_error"] = _err_text(exc)
    rec["lineage"] = lineage
    return rec


# ── loadouts ────────────────────────────────────────────────────────────── #

def loadout_record(name: str, owner: Optional[str] = None) -> Dict[str, Any]:
    from src import agent_profiles
    from src.agent_profile_transfer import export_profiles

    doc = export_profiles([name])
    rec: Dict[str, Any] = {"name": name, "export": doc}
    try:
        from src.agent_loadouts import caller_policy
        from src.profile_readiness import profile_readiness

        profile = agent_profiles.get_profile(name)
        if profile:
            readiness = profile_readiness(profile, caller_policy(None, owner), owner)
            matrix = readiness.pop("capabilities", None) or {}
            readiness["capabilities"] = {k: (sorted(v) if isinstance(v, (set, frozenset)) else v)
                                         for k, v in matrix.items()}
            rec["readiness"] = readiness
            rec["readiness_note"] = "judged as a worker started by a top-level chat of this owner"
    except Exception as exc:
        rec["readiness_error"] = _err_text(exc)
    return rec


# ── system ──────────────────────────────────────────────────────────────── #

def mcp_servers() -> Dict[str, Any]:
    """Connection state only: no env, args, headers, OAuth material or auth URLs."""
    servers: List[Dict[str, Any]] = []
    try:
        from src.tool_utils import get_mcp_manager

        manager = get_mcp_manager()
    except Exception:
        manager = None
    statuses = manager.get_all_statuses() if manager else {}
    tools = getattr(manager, "_tools", {}) if manager else {}
    seen = set()
    try:
        from core.database import McpServer, get_db_session

        with get_db_session() as db:
            rows = db.query(McpServer).all()
            for row in rows:
                seen.add(row.id)
                st = statuses.get(row.id) or {}
                try:
                    disabled = len(json.loads(row.disabled_tools or "[]") or [])
                except (TypeError, ValueError):
                    disabled = None
                servers.append({
                    "id": row.id, "label": row.name, "transport": row.transport,
                    "enabled": bool(row.is_enabled),
                    "command": os.path.basename(row.command or "") or None,
                    "url": redact_url(row.url) if row.url else None,
                    "oauth_configured": bool(row.oauth_config),
                    "status": st.get("status", "disconnected"),
                    "connected": st.get("status") == "connected",
                    "tool_count": st.get("tool_count", len(tools.get(row.id) or [])),
                    "disabled_tools": disabled,
                    "last_error": redact_text(str(st["error"]))[:500] if st.get("error") else None,
                })
    except Exception as exc:
        servers.append({"error": _err_text(exc)})
    for sid, st in statuses.items():  # built-in servers have no DB row
        if sid in seen:
            continue
        servers.append({
            "id": sid, "label": st.get("name"), "transport": st.get("transport"),
            "builtin": bool(manager.is_builtin(sid)) if manager and hasattr(manager, "is_builtin") else None,
            "status": st.get("status"), "connected": st.get("status") == "connected",
            "tool_count": st.get("tool_count", len(tools.get(sid) or [])),
            "last_error": redact_text(str(st["error"]))[:500] if st.get("error") else None,
        })
    return {"servers": servers}


def scheduler_state(owner: Optional[str]) -> Dict[str, Any]:
    from src import runtime_introspection

    return {"tasks": runtime_introspection.list_tasks(owner or "", limit=100),
            "foreground_gate": runtime_introspection._gate_state()}


def settings_snapshot() -> Dict[str, Any]:
    from src.settings import load_features, load_settings

    return {"settings": load_settings(), "features": load_features(),
            "note": "secret-shaped values are masked to '***'; the key stays so you can see it is set"}


def _git_info() -> Dict[str, Optional[str]]:
    from src.runtime_paths import get_app_root

    out: Dict[str, Optional[str]] = {"git_sha": None, "git_branch": None}
    for env in ("ODYSSEUS_GIT_SHA", "GIT_SHA", "SOURCE_COMMIT"):
        if os.environ.get(env):
            out["git_sha"] = os.environ[env][:40]
            return out
    git = os.path.join(get_app_root(), ".git")
    try:
        if os.path.isfile(git):  # a worktree: "gitdir: <path>"
            with open(git, encoding="utf-8") as fh:
                git = fh.read().split(":", 1)[1].strip()
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            out["git_sha"] = head[:40]
            return out
        ref = head.split(":", 1)[1].strip()
        out["git_branch"] = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ref
        common = git
        commondir = os.path.join(git, "commondir")
        if os.path.isfile(commondir):
            with open(commondir, encoding="utf-8") as fh:
                common = os.path.normpath(os.path.join(git, fh.read().strip()))
        for base in (git, common):
            path = os.path.join(base, *ref.split("/"))
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    out["git_sha"] = fh.read().strip()[:40]
                return out
        packed = os.path.join(common, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().endswith(" " + ref):
                        out["git_sha"] = line.split(" ", 1)[0][:40]
                        break
    except (OSError, IndexError):
        pass
    return out


def _app_info() -> Dict[str, Any]:
    try:
        from src.constants import APP_VERSION
    except Exception:
        APP_VERSION = None
    return {"version": APP_VERSION, **_git_info()}


# ── entry points ────────────────────────────────────────────────────────── #

def _clamp_minutes(minutes: Any) -> float:
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        value = DEFAULT_MINUTES
    return max(1.0, min(float(MAX_MINUTES), value))


def _clamp_lines(max_lines: Any) -> int:
    try:
        value = int(max_lines)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_LINES
    return max(1, min(MAX_MAX_LINES, value))


def _normalize_ids(session_ids: Any) -> List[str]:
    if not session_ids:
        return []
    if isinstance(session_ids, str):
        session_ids = [session_ids]
    out: List[str] = []
    for item in session_ids:
        for part in str(item or "").replace(" ", ",").split(","):
            part = part.strip()
            if part and re.fullmatch(r"[A-Za-z0-9_.:-]{4,80}", part):
                out.append(part.lower() if _UUID_FULL_RE.match(part) else part)
    return _ranked(out)[:MAX_SESSIONS]


def summarize(*, minutes: Any = DEFAULT_MINUTES, max_lines: Any = DEFAULT_MAX_LINES,
              session_ids: Any = None) -> Dict[str, Any]:
    """What a bundle for these arguments would contain, without building it."""
    minutes_v, lines_v, ids = _clamp_minutes(minutes), _clamp_lines(max_lines), _normalize_ids(session_ids)
    errors: List[Dict[str, str]] = []
    logs = collect_logs(minutes_v, lines_v, errors)
    found = discover(logs, ids, errors=errors)
    loadout_names = list(found["loadout_mentions"])
    loadouts, loadouts_missing = resolve_loadouts(loadout_names)
    titles: Dict[str, Any] = {}
    try:
        from core.database import Session, get_db_session

        with get_db_session() as db:
            for sid, name, mode, model in (db.query(Session.id, Session.name, Session.mode, Session.model)
                                           .filter(Session.id.in_(found["sessions"] or [""])).all()):
                titles[sid] = {"title": name, "mode": mode, "model": model}
    except Exception as exc:
        errors.append({"component": "summary/titles", "error": _err_text(exc)})
    return {
        "window_minutes": minutes_v,
        "logs": [{k: v for k, v in log.items() if k != "lines"} | {"lines": len(log.get("lines") or [])}
                 for log in logs],
        "total_lines": sum(len(log.get("lines") or []) for log in logs),
        "sessions": [{"id": sid, **titles.get(sid, {})} for sid in found["sessions"]],
        "sessions_capped": found["sessions_capped"],
        "sessions_not_found": found["sessions_not_found"],
        "runs": len(found["runs"]),
        "loadouts": loadouts,
        "loadouts_not_found": loadouts_missing,
        "errors": errors,
    }


@dataclass
class BundleResult:
    data: bytes
    filename: str
    summary: Dict[str, Any]


def _collect_sync(bundle: _Bundle, *, minutes: float, max_lines: int, ids: List[str],
                  include_messages: bool, owner: Optional[str]) -> Dict[str, Any]:
    logs = bundle.guard("logs", collect_logs, minutes, max_lines, bundle.errors) or []
    found = bundle.guard("discovery", discover, logs, ids, errors=bundle.errors) or {
        "sessions": [], "sessions_not_found": [], "runs": [], "loadout_mentions": [],
        "sessions_capped": False, "sessions_total": 0}

    # Explicitly requested chats also pull in their worker tree and parent.
    session_list = list(found["sessions"])
    loadout_names = list(found.get("loadout_mentions") or [])
    records: Dict[str, Dict[str, Any]] = {}
    queue = list(session_list)
    while queue and len(records) < MAX_SESSIONS:
        sid = queue.pop(0)
        if sid in records:
            continue
        rec = bundle.guard(f"sessions/{sid}", session_record, sid, owner=owner,
                           include_messages=include_messages)
        if rec is None:
            continue
        records[sid] = rec
        if rec.get("loadout"):
            loadout_names.append(rec["loadout"])
        for run in (rec.get("lineage") or {}).get("runs") or []:
            if run.get("loadout"):
                loadout_names.append(run["loadout"])
        if sid in ids:
            lineage = rec.get("lineage") or {}
            related = list(lineage.get("children") or [])
            if lineage.get("parent_session"):
                related.insert(0, str(lineage["parent_session"]))
            queue.extend(s for s in related if s not in records)
    for sid, rec in records.items():
        bundle.add_json(f"sessions/{_safe_filename(sid)}.json", rec)

    resolved = bundle.guard("loadouts/resolve", resolve_loadouts, loadout_names) or ([], [])
    loadouts, loadouts_missing = resolved
    for name in loadouts:
        rec = bundle.guard(f"loadouts/{name}", loadout_record, name, owner)
        if rec is not None:
            bundle.add_json(f"loadouts/{_safe_filename(name, 'loadout')}.json", rec)

    for path, fn, args in (("system/mcp_servers.json", mcp_servers, ()),
                           ("system/scheduler.json", scheduler_state, (owner,)),
                           ("system/settings.json", settings_snapshot, ())):
        data = bundle.guard(path, fn, *args)
        if data is not None:
            bundle.add_json(path, data)

    log_index = []
    for log in logs:
        lines = log.pop("lines", None) or []
        entry = dict(log)
        if lines:
            entry["path"] = f"logs/{_safe_filename(log['name'], 'log')}"
            entry.update(bundle.add_lines(entry["path"], lines))
        log_index.append(entry)
    return {
        "logs": log_index,
        "sessions": [{"id": sid, "title": rec.get("title"), "mode": rec.get("mode"),
                      "model": rec.get("model"), "loadout": rec.get("loadout"),
                      "parent_session": (rec.get("lineage") or {}).get("parent_session")}
                     for sid, rec in records.items()],
        "sessions_capped": bool(found.get("sessions_capped")) or any(s not in records for s in queue),
        "sessions_not_found": found.get("sessions_not_found") or [],
        "runs_mentioned": found.get("runs") or [],
        "loadouts": loadouts,
        "loadouts_not_found": loadouts_missing,
    }


async def _guard_async(bundle: _Bundle, component: str, coro) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=_COMPONENT_TIMEOUT_S)
    except Exception as exc:
        logger.warning("diagnostics bundle: %s failed: %s", component, type(exc).__name__)
        bundle.errors.append({"component": component, "error": _err_text(exc) if str(exc) else type(exc).__name__})
        return None


async def build_bundle(*, minutes: Any = DEFAULT_MINUTES, max_lines: Any = DEFAULT_MAX_LINES,
                       session_ids: Any = None, include_messages: bool = False,
                       owner: Optional[str] = None, rag_manager: Any = None, memory_vector: Any = None,
                       include_health: bool = True, max_bytes: int = MAX_BUNDLE_BYTES) -> BundleResult:
    """Build the zip. Never raises for a component failure; see manifest.errors."""
    minutes_v, lines_v, ids = _clamp_minutes(minutes), _clamp_lines(max_lines), _normalize_ids(session_ids)
    now_local = datetime.now()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    bundle = _Bundle(max_bytes=max(_MANIFEST_RESERVE * 2, int(max_bytes)))

    async def claude_code():
        from src.agent_tools.claude_code_tools import status_report

        return await status_report()

    cc = await _guard_async(bundle, "system/claude_code.json", claude_code())
    if cc is not None:
        bundle.add_json("system/claude_code.json", cc)
    if include_health:
        async def health():
            from src.service_health import collect_service_health

            return await collect_service_health(rag_manager, memory_vector)

        report = await _guard_async(bundle, "system/service_health.json", health())
        if report is not None:
            bundle.add_json("system/service_health.json", report)

    contents = await asyncio.to_thread(
        _collect_sync, bundle, minutes=minutes_v, max_lines=lines_v, ids=ids,
        include_messages=bool(include_messages), owner=owner)

    privacy = {
        "include_messages": bool(include_messages),
        "message_content": (
            "INCLUDED: sessions/*.json carry the last messages of each chat (redacted, clipped). They may "
            "contain private vault content, email, or anything else the chat saw. Share with care."
            if include_messages else
            "Not included. Session files carry configuration and a content-free last-turn summary only."),
    }
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "generated_by": owner,
        "window": {"minutes": minutes_v,
                   "since_local": (now_local - timedelta(minutes=minutes_v)).isoformat(timespec="seconds"),
                   "until_local": now_local.isoformat(timespec="seconds"),
                   "note": "log timestamps are the server's local time"},
        "app": bundle.guard("manifest/app", _app_info) or {},
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform(),
                    "machine": platform.machine(), "pid": os.getpid()},
        "request": {"session_ids": ids, "include_messages": bool(include_messages),
                    "max_lines_per_log": lines_v, "max_bytes": bundle.max_bytes},
        "privacy": privacy,
        "redaction": REDACTION_NOTICE,
        "how_to_read": HOW_TO_READ,
        "discovery": contents,
        "files": [{"path": p, "bytes": len(d)} for p, d in bundle.files],
        "truncated": bundle.truncated,
        "errors": bundle.errors,
    }
    readme = "\n".join(
        ["Odysseus diagnostics bundle", "=" * 27, "",
         f"Generated {manifest['generated_at']} covering the last {minutes_v:g} minutes.", "",
         privacy["message_content"], "", REDACTION_NOTICE, "", "How to read this:"]
        + [f"- {line}" for line in HOW_TO_READ]
        + ["", f"{len(bundle.errors)} component error(s), {len(bundle.truncated)} truncation(s): "
           "see manifest.json."]
    ).encode("utf-8")
    bundle.add_raw("README.txt", readme)
    bundle.add_raw("manifest.json", _json_bytes(mask(manifest)))

    summary = {
        "window_minutes": minutes_v,
        "files": len(bundle.files),
        "bytes_uncompressed": bundle.used,
        "log_lines": sum(int(log.get("written_lines") or 0) for log in contents["logs"]),
        "sessions": [s["id"] for s in contents["sessions"]],
        "loadouts": contents["loadouts"],
        "include_messages": bool(include_messages),
        "errors": bundle.errors,
        "truncated": bundle.truncated,
    }
    stamp = now_local.strftime("%Y%m%d-%H%M%S")
    return BundleResult(data=bundle.to_zip(), filename=f"odysseus-diagnostics-{stamp}.zip", summary=summary)


EXPORT_KEEP = 10


def exports_dir() -> str:
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, "exports", "diagnostics")


def write_bundle(result: BundleResult, directory: Optional[str] = None) -> str:
    """Write a built bundle under the data dir, keeping the newest few."""
    target = directory or exports_dir()
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, result.filename)
    with open(path, "wb") as fh:
        fh.write(result.data)
    try:
        old = sorted((f for f in os.listdir(target)
                      if f.startswith("odysseus-diagnostics-") and f.endswith(".zip")), reverse=True)
        for name in old[EXPORT_KEEP:]:
            os.remove(os.path.join(target, name))
    except OSError:
        pass
    return path
