"""What an agent turn has done so far, read off its own event stream.

A turn's tool calls and per-round text are saved when the turn ends. A turn
that is stopped part-way (the Stop button, a new message that replaces it, a
restart) used to keep only its visible text: on 2026-09-29 a 27-round run was
replaced by the user's "did you get stuck?", and the saved reply was two
sentences. The tool cards were gone after a reload, and the next turn could
not tell what it had been doing, nor that a bash command was still running.

The chat route feeds every event it forwards to ``TurnTrail`` and, when the
turn is stopped, saves ``stopped_record()`` with the partial reply.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

# Kept from a tool_output event, as the finished turn saves them.
_EVENT_KEYS = ("tool", "command", "output", "exit_code", "diff", "doc_id",
               "image_url", "image_prompt", "image_model", "image_size", "image_quality")
_COMMAND_IN_NOTE = 200


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" if not secs or minutes >= 10 else f"{minutes} min {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class TurnTrail:
    def __init__(self) -> None:
        self.round = 1
        self._texts: Dict[int, str] = {}
        self.tool_events: List[Dict] = []
        # Tools started and not answered yet, oldest first.
        self._running: List[Dict] = []

    def text(self, delta: str) -> None:
        if delta:
            self._texts[self.round] = self._texts.get(self.round, "") + delta

    def event(self, data: Dict, now: Optional[float] = None) -> None:
        kind = data.get("type")
        now = time.monotonic() if now is None else now
        if kind == "agent_step":
            try:
                self.round = max(self.round, int(data.get("round") or 1))
            except (TypeError, ValueError):
                pass
        elif kind == "tool_start":
            self._running.append({
                "round": self.round,
                "tool": str(data.get("tool") or ""),
                "command": str(data.get("command") or ""),
                "started": now,
                "tail": "",
            })
        elif kind == "tool_progress":
            if self._running and data.get("tail"):
                self._running[-1]["tail"] = str(data.get("tail"))
        elif kind == "tool_output":
            tool = str(data.get("tool") or "")
            started = next((r for r in self._running if r["tool"] == tool), None)
            if started is not None:
                self._running.remove(started)
            event = {"round": started["round"] if started else self.round}
            event.update({k: data[k] for k in _EVENT_KEYS if k in data})
            self.tool_events.append(event)

    def take_before(self, round_num: int) -> Tuple[List[str], List[Dict]]:
        """Remove and return what rounds before ``round_num`` produced.

        A steer splits the reply there (2026-10-02): the pieces so far are
        saved as their own assistant message, and what this trail reports from
        then on, including a stopped turn's record, is only what came after.
        Returns ``(round_texts, tool_events)``; the texts list has one entry
        per round, so an index is that round minus one, as in saved metadata.
        """
        texts = [self._texts.pop(r, "") for r in range(1, round_num)]
        taken = [e for e in self.tool_events if int(e.get("round") or 1) < round_num]
        self.tool_events = [e for e in self.tool_events if int(e.get("round") or 1) >= round_num]
        return texts, taken

    def has_work(self) -> bool:
        return bool(self.tool_events or self._running)

    def stopped_record(self, now: Optional[float] = None) -> Tuple[str, List[Dict], List[str]]:
        """``(note, tool_events, round_texts)`` for a turn stopped now.

        The note says what was cut off: it goes at the end of the saved
        reply, where the next turn reads it, and is the last of the round
        texts so a reload shows it below the tool cards. A tool still running
        is saved as a stopped tool card.
        """
        now = time.monotonic() if now is None else now
        events = list(self.tool_events)
        for run in self._running:
            events.append({
                "round": run["round"],
                "tool": run["tool"],
                "command": run["command"],
                "output": run["tail"],
                "exit_code": None,
                "stopped": True,
            })
        finished = len(self.tool_events)
        calls = f" {finished} tool call{'s' if finished != 1 else ''} had finished." if finished else ""
        if self._running:
            run = self._running[0]
            cmd = _one_line(run["command"], _COMMAND_IN_NOTE).replace("`", "'")
            what = f"`{run['tool']}` had been running for {_duration(now - run['started'])}"
            note = f"[Turn stopped while {what}" + (f": `{cmd}`" if cmd else "") + f".{calls}]"
        elif finished:
            note = f"[Turn stopped.{calls}]"
        else:
            note = ""
        last = max([self.round, *self._texts.keys()])
        texts = [self._texts.get(r, "") for r in range(1, last + 1)]
        if note:
            texts.append(note)
        return note, events, texts


# ---------------------------------------------------------------------------
# The model's own record of a saved turn.
#
# 2026-10-02: the admin chat's last request of a turn held 265 items and the
# next turn started with 24. History crossing a turn boundary is role plus text
# only, so the agent no longer knew what it had run, edited or launched and
# re-found its own state with extra tool calls. A saved assistant message
# already carries ``metadata.tool_events`` for the UI; ``render_model_trail``
# turns those into a compact text the model reads after that message.
#
# Rendered ONCE, when the message is saved, and stored as
# ``metadata.model_trail``: the cross-turn prompt cache needs an old message to
# read the same bytes on every later turn (tests/test_cross_turn_prompt_prefix.py).
#
# Never copied into the trail: tool output text. A web page, an email, a file or
# an MCP server wrote it, and inside an assistant message it reads to the model
# as its own words. Only the model's own arguments, the status, sizes, and ids
# matched by strict patterns go in.
# ---------------------------------------------------------------------------
import json
import re
import uuid

# What an assistant message holds when the turn ran tools but wrote no text
# before a steer split it off (re-exported by src.agent_control).
STEER_SPLIT_NO_TEXT = "[Ran tools; wrote no reply text before the user's next message.]"

TRAIL_MAX_CHARS = 10_000          # about 2,500 tokens
_ARG_CHARS = 120
_LINE_CHARS = 330
_COLLAPSE_MIN = 3
_COLLAPSE_SHOWN = 3

TRAIL_LABEL = (
    "[Record of your own tool calls in this turn, kept by the harness. Outputs "
    "are not included: read one with recall_tool_output {\"ref\": \"<ref>\"}. "
    "A ref like evt-ab12cd34-3..9 stands for every number from 3 to 9.]"
)

_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "create_document", "edit_document",
    "update_document", "suggest_document", "manage_documents", "manage_memory",
    "manage_notes", "manage_tasks", "manage_calendar", "manage_settings",
    "manage_skills", "manage_endpoints", "manage_mcp", "manage_webhooks",
    "manage_tokens", "send_email", "reply_to_email", "bulk_email", "delete_email",
    "archive_email", "mark_email_read", "manage_contact", "edit_image",
    "api_call", "app_api", "update_plan", "todowrite", "ui_control",
})
_GIT_TOOLS = frozenset({"manage_git", "manage_agent_worktree"})
_WORKER_TOOLS = frozenset({
    "create_session", "send_to_session", "manage_session", "manage_agent_loadout",
    "orchestrate_agents", "delegate_to_agent", "delegate_to_claude_code",
    "message_agent", "manage_bg_jobs", "pipeline", "serve_model", "serve_preset",
    "download_model", "stop_served_model", "cancel_download",
})
_SHELL_TOOLS = frozenset({"bash", "python"})
_READ_TOOLS = frozenset({
    "read_file", "grep", "glob", "ls", "preview_file", "get_workspace",
    "web_search", "web_fetch", "search_documents", "search_chats",
    "recall_chat_history", "recall_tool_output", "read_email", "list_emails",
    "list_sessions", "list_models", "read_app_logs", "inspect_runtime",
    "tail_serve_output", "discover_tools",
})

_ARG_KEYS = ("command", "cmd", "path", "file_path", "file", "pattern", "action",
             "query", "url", "id", "ref", "name", "task", "message")
_BARE_KEYS = ("command", "cmd", "path", "file_path", "file", "pattern")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SESSION_ID_RE = re.compile(r"(?:session_id|worker_session|target_session|\bid)[\"'`\s:=(]{1,6}(" + _UUID + r")\b")
_COMMIT_RE = re.compile(r"\[[\w./@-]{1,80} ([0-9a-f]{7,40})\]")
_COMMIT_KV_RE = re.compile(r"\bcommit(?:_hash|_sha)?[\"'`\s:=]{1,5}([0-9a-f]{7,40})\b")
_PR_RE = re.compile(r"github\.com/[\w.-]{1,100}/[\w.-]{1,100}/pull/(\d{1,7})\b")
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(\S[^\t\r\n]*)$", re.M)
_ERROR_PREFIXES = ("error", "**error", "blocked", "approval required", "misformatted")


def _args_summary(raw: str) -> str:
    """The model's own arguments, on one line: the command, path, query or id."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    parsed = None
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        parts = []
        for key in _ARG_KEYS:
            value = parsed.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                parts.append(str(value) if key in _BARE_KEYS else f"{key}={value}")
            if len(parts) == 2:
                break
        if not parts:
            parts = ["{" + ", ".join(sorted(str(k) for k in parsed)[:6]) + "}"]
        text = " ".join(parts)
    else:
        text = raw
    return _one_line(text, _ARG_CHARS).replace("`", "'")


def _status(event: Dict) -> str:
    if event.get("stopped"):
        return "stopped"
    code = event.get("exit_code")
    if isinstance(code, bool):
        code = int(code)
    if isinstance(code, int) and code != 0:
        return f"error (exit {code})"
    head = str(event.get("output") or "")[:40].lstrip().lower()
    if head.startswith(_ERROR_PREFIXES):
        return "error"
    return "ok"


def _size(text: str) -> str:
    n = len(text or "")
    if n < 1000:
        return ""
    return f"{n / 1000:.1f}k chars out" if n < 100_000 else f"{n // 1000}k chars out"


def _changed(event: Dict, tool: str, args: str) -> str:
    diff = event.get("diff")
    files: List[str] = []
    counts = ""
    if isinstance(diff, dict):
        for match in _DIFF_FILE_RE.finditer(str(diff.get("text") or "")):
            path = match.group(1).strip()
            if path not in files:
                files.append(path)
        try:
            counts = f" +{int(diff.get('added') or 0)}/-{int(diff.get('removed') or 0)}"
        except (TypeError, ValueError):
            counts = ""
    elif tool in ("write_file", "edit_file") and args:
        files = [args.split(" ")[0]]
    if not files:
        return ""
    shown = ", ".join(_one_line(f, 80) for f in files[:3])
    more = f" +{len(files) - 3} more" if len(files) > 3 else ""
    return f"changed {shown}{more}{counts}"


def _ids(event: Dict, tool: str) -> List[str]:
    out: List[str] = []
    doc_id = event.get("doc_id")
    if isinstance(doc_id, (str, int)) and _DOC_ID_RE.match(str(doc_id)):
        out.append(f"doc={doc_id}")
    output = str(event.get("output") or "")
    if tool in _WORKER_TOOLS:
        seen: List[str] = []
        for match in _SESSION_ID_RE.finditer(output):
            if match.group(1) not in seen:
                seen.append(match.group(1))
        out.extend(f"session={s}" for s in seen[:3])
    if tool in _SHELL_TOOLS or tool in _GIT_TOOLS:
        commits: List[str] = []
        for rx in (_COMMIT_RE, _COMMIT_KV_RE):
            for match in rx.finditer(output):
                if match.group(1) not in commits:
                    commits.append(match.group(1))
        out.extend(f"commit={c[:12]}" for c in commits[:2])
        prs: List[str] = []
        for match in _PR_RE.finditer(output):
            if match.group(1) not in prs:
                prs.append(match.group(1))
        out.extend(f"PR #{p}" for p in prs[:2])
    return out


def _pinned(tool: str, status: str, has_effect: bool) -> bool:
    return (
        status != "ok"
        or has_effect
        or tool in _WRITE_TOOLS
        or tool in _GIT_TOOLS
        or tool in _WORKER_TOOLS
    )


def _offloaded_refs(events: List[Dict], session_id: Optional[str]) -> Dict[int, str]:
    """``toolout-`` refs for the events whose output went to the overflow store.

    The agent loop stores an output over its inline limit but the saved event
    does not name the ref; the store's own record does (session, tool, round,
    command), so match on those. Only events big enough to have been offloaded
    are looked up, and a failure leaves them with their ``evt-`` ref.
    """
    big = [i for i, e in enumerate(events)
           if len(str(e.get("output") or "")) >= 3500 and not e.get("output_ref")]
    if not big or not session_id:
        return {}
    try:
        from src import tool_output_store as store

        records = store.list_recent(session_id, 500)
    except Exception:
        return {}
    found: Dict[int, str] = {}
    used = set()
    for i in big:
        event = events[i]
        for record in records:
            ref = record.get("ref")
            if ref in used or record.get("tool") != event.get("tool"):
                continue
            if record.get("round") != event.get("round"):
                continue
            if (record.get("command") or "") != str(event.get("command") or "")[:400]:
                continue
            found[i] = ref
            used.add(ref)
            break
    return found


def render_model_trail(events: List[Dict], trail_id: str, session_id: Optional[str] = None,
                       budget: int = TRAIL_MAX_CHARS) -> str:
    """One line per tool call, grouped by round, within ``budget`` characters.

    Over budget, every write, git, worker and error line stays and the rest
    fills what is left, in order; the end counts what was omitted. Runs of
    three or more successful read-only calls of one tool collapse to one line.
    """
    events = [e for e in (events or []) if isinstance(e, dict)]
    if not events:
        return ""
    offloaded = _offloaded_refs(events, session_id)

    def ref_of(i: int) -> str:
        return str(events[i].get("output_ref") or offloaded.get(i) or f"evt-{trail_id}-{i}")

    def round_of(i: int) -> int:
        try:
            return int(events[i].get("round") or 1)
        except (TypeError, ValueError):
            return 1

    # (round, text, pinned) in original order.
    entries: List[Tuple[int, str, bool]] = []
    i = 0
    while i < len(events):
        e = events[i]
        tool = str(e.get("tool") or "tool")
        status = _status(e)
        args = _args_summary(e.get("command"))
        if tool in _READ_TOOLS and status == "ok":
            j = i
            while (j + 1 < len(events) and events[j + 1].get("tool") == e.get("tool")
                   and round_of(j + 1) == round_of(i) and _status(events[j + 1]) == "ok"):
                j += 1
            if j - i + 1 >= _COLLAPSE_MIN:
                shown = ", ".join(_args_summary(events[k].get("command"))[:60]
                                  for k in range(i, min(j + 1, i + _COLLAPSE_SHOWN)))
                tail = ", …" if j - i + 1 > _COLLAPSE_SHOWN else ""
                if ref_of(i).startswith("evt-") and ref_of(j).startswith("evt-"):
                    refs = f"{ref_of(i)}..{j}"
                else:
                    refs = f"{ref_of(i)}, …"
                text = _one_line(f"- {tool} ×{j - i + 1}: {shown}{tail} [{refs}]", _LINE_CHARS)
                entries.append((round_of(i), text, False))
                i = j + 1
                continue
        bits = [status]
        changed = _changed(e, tool, args)
        ids = _ids(e, tool)
        if changed:
            bits.append(changed)
        bits.extend(ids)
        size = _size(str(e.get("output") or ""))
        if size:
            bits.append(size)
        head = f"- {tool} {args}".rstrip()
        text = _one_line(f"{head} -> {'; '.join(bits)} [{ref_of(i)}]", _LINE_CHARS)
        entries.append((round_of(i), text, _pinned(tool, status, bool(changed or ids))))
        i += 1

    def build(keep: List[bool], omitted: int) -> str:
        lines = [TRAIL_LABEL]
        last = None
        for (rnd, text, _), k in zip(entries, keep):
            if not k:
                continue
            if rnd != last:
                lines.append(f"Round {rnd}:")
                last = rnd
            lines.append(text)
        if omitted:
            lines.append(f"[{omitted} more line{'s' if omitted != 1 else ''} omitted for length; "
                         "recall_tool_output with no ref lists stored outputs.]")
        return "\n".join(lines)

    keep = [True] * len(entries)
    full = build(keep, 0)
    if len(full) <= budget:
        return full

    # Pinned lines first (newest win when even those do not fit), then the
    # rest in order while room lasts. A round's header is paid once, by the
    # first line kept in that round.
    room = budget - len(TRAIL_LABEL) - 160
    keep = [False] * len(entries)
    rounds_paid = set()

    def take(idx: int) -> bool:
        nonlocal room
        rnd, text, _ = entries[idx]
        cost = len(text) + 1 + (0 if rnd in rounds_paid else len(f"Round {rnd}:") + 1)
        if room < cost:
            return False
        room -= cost
        rounds_paid.add(rnd)
        keep[idx] = True
        return True

    for idx in range(len(entries) - 1, -1, -1):
        if entries[idx][2]:
            take(idx)
    for idx in range(len(entries)):
        if not entries[idx][2]:
            take(idx)
    omitted = keep.count(False)
    return build(keep, omitted)


def attach_model_trail(message, session_id: Optional[str] = None) -> None:
    """Render and store ``metadata.model_trail`` on a message being saved.

    Called from the two add-message paths, so every save of an assistant
    message that carries tool events (a finished turn, a steer split, a stopped
    turn, a worker run, a hand-back) gets one. A message that already has a
    trail keeps it, and messages saved before this existed are never touched:
    their bytes in the model's history must not change.
    """
    try:
        if getattr(message, "role", None) != "assistant":
            return
        md = getattr(message, "metadata", None)
        if not isinstance(md, dict) or md.get("model_trail"):
            return
        events = md.get("tool_events")
        if not isinstance(events, list) or not events:
            return
        trail_id = uuid.uuid4().hex[:8]
        trail = render_model_trail(events, trail_id, session_id)
        if trail:
            md["trail_id"] = trail_id
            md["model_trail"] = trail
    except Exception:  # never block a save over a convenience record
        pass
