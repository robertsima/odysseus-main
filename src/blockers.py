"""One shape for everything that stops work.

Worker preflight, capacity, delegation policy, approvals, interrupted runs,
failed task runs and a worker's `Needs user:` / `Needs parent:` lines each
used to report a stop in their own format. A `Blocker` carries the same few
fields for all of them, so an outcome can store one and Today can sort by who
has to act. The builders adapt the existing shapes and change none of them.

`recovery` says who moves next:

* `auto`: the system or the parent chat handles it; the person is not asked.
* `retry`: safe to run again as-is.
* `user`: needs a person's input.
* `approval`: needs a person's approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

RECOVERIES = ("auto", "retry", "user", "approval")
_NEEDS_PERSON = ("user", "approval")
_FALLBACK_ACTION = "Open the linked chat or run and see what it is waiting for."


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


@dataclass
class Blocker:
    code: str
    summary: str
    recovery: str
    user_action: Optional[str] = None
    source: Dict[str, Any] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def __post_init__(self) -> None:
        if self.recovery not in RECOVERIES:
            raise ValueError(f"recovery must be one of {RECOVERIES}, got {self.recovery!r}")
        if self.recovery in _NEEDS_PERSON and not (self.user_action or "").strip():
            raise ValueError(f"a {self.recovery!r} blocker needs a user_action")
        if not self.at:
            self.at = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Blocker":
        """Read a stored blocker, repairing what older or hand-edited rows lack."""
        data = data if isinstance(data, dict) else {}
        recovery = data.get("recovery")
        if recovery not in RECOVERIES:
            recovery = "user"
        action = data.get("user_action")
        if recovery in _NEEDS_PERSON and not (action or "").strip():
            action = _FALLBACK_ACTION
        return cls(
            code=str(data.get("code") or "UNKNOWN"),
            summary=str(data.get("summary") or "Work is blocked."),
            recovery=recovery,
            user_action=action or None,
            source=dict(data.get("source") or {}),
            detail=dict(data.get("detail") or {}),
            at=str(data.get("at") or ""),
        )


# (summary, recovery, user_action) per preflight code. A code not listed falls
# back to the problem's own message and asks the person to look.
_PREFLIGHT = {
    "WORKSPACE_INVALID": ("The worker was not started because its workspace is not usable.", "user",
                          "Pick a repository folder that agents may use and start the worker again."),
    "WORKSPACE_UNREADABLE": ("The worker was not started because the server cannot read its workspace.",
                             "user", "Fix the permissions on the workspace folder, then start the worker again."),
    "TOOLS_UNAVAILABLE": ("The worker was not started because it lacks the tools to read the repository.",
                          "user", "Enable the file-reading tools for this worker's loadout."),
    "WRITE_NOT_ALLOWED": ("The worker was not started because the task changes files and it has no write tool.",
                          "user", "Start the task with a loadout that can write files, or ask for analysis only."),
    "SHELL_NOT_AVAILABLE": ("The worker was not started because the task runs commands and it has no shell.",
                            "user", "Run the command yourself, or start the task with a loadout that has a shell."),
    "UNKNOWN_REQUIREMENT": ("The worker was not started because the launch named an unknown requirement.",
                            "retry", None),
    "CONFLICTING_REQUIREMENT": ("The worker was not started because the launch listed conflicting requirements.",
                                "retry", None),
}


def from_preflight(payload: Dict[str, Any]) -> Blocker:
    """Adapt `worker_preflight.Preflight.blocked_payload()`."""
    payload = payload or {}
    problems = payload.get("problems") or []
    first = problems[0] if problems and isinstance(problems[0], dict) else {}
    code = str(payload.get("code") or first.get("code") or "PREFLIGHT_BLOCKED")
    message = first.get("message") or payload.get("error") or "The worker could not start."
    candidates = list(first.get("candidates") or [])
    detail: Dict[str, Any] = {"message": _clip(message, 400)}
    if candidates:
        detail["candidates"] = candidates[:8]
    next_action = payload.get("next_action")
    if next_action:
        detail["next_action"] = next_action

    if code == "WORKSPACE_REQUIRED":
        if candidates:
            summary = "The worker was not started because the task works on a repository and no workspace is set."
            recovery, action = "retry", None
        else:
            summary = "The worker was not started because the task needs a repository and none is available."
            recovery, action = "user", "Add a repository folder to the agent roots, or say the task needs no files."
    elif code in _PREFLIGHT:
        summary, recovery, action = _PREFLIGHT[code]
    else:
        summary, recovery = _clip(message, 200), "user"
        hint = next_action.get("do") if isinstance(next_action, dict) else ""
        action = _clip(hint, 200) or _FALLBACK_ACTION
    return Blocker(code=code, summary=summary, recovery=recovery, user_action=action,
                   source={"kind": "preflight", "ref": ""}, detail=detail)


def from_needs(text: str, *, source: Optional[Dict[str, Any]] = None) -> Optional[Blocker]:
    """Adapt a worker's `Needs user:` / `Needs parent:` lines; None when it has none.

    A `Needs user:` line wins over `Needs parent:`: it is the one that needs a person.
    """
    from src.agent_loop import NEEDS_LINE_RE

    needs: List[Dict[str, str]] = [
        {"who": m.group(1).lower(), "what": m.group(2)[:300]} for m in NEEDS_LINE_RE.finditer(str(text or ""))
    ]
    if not needs:
        return None
    chosen = next((n for n in needs if n["who"] == "user"), needs[0])
    src = dict(source) if source else {"kind": "chat", "ref": ""}
    if chosen["who"] == "user":
        return Blocker(code="NEEDS_USER", summary=f"The worker needs input from you: {_clip(chosen['what'], 160)}",
                       recovery="user", user_action=chosen["what"], source=src, detail={"needs": needs})
    return Blocker(code="NEEDS_PARENT",
                   summary=f"The worker needs its parent chat to act: {_clip(chosen['what'], 160)}",
                   recovery="auto", user_action=None, source=src, detail={"needs": needs})


def worker_reported_blocked(reason: str, *, source: Optional[Dict[str, Any]] = None) -> Blocker:
    """A worker said `Outcome: blocked` without a `Needs` line to say on what."""
    return Blocker(code="WORKER_BLOCKED",
                   summary=f"The worker reported it is blocked: {_clip(reason, 160) or 'no reason given'}",
                   recovery="user", user_action="Open the worker chat and read why it stopped.",
                   source=dict(source) if source else {"kind": "chat", "ref": ""}, detail={})


def from_interrupted_run(run: Dict[str, Any]) -> Blocker:
    """Adapt an activity run record whose status is `interrupted`."""
    run = run or {}
    summary = run.get("summary") or {}
    detail = {k: v for k, v in {
        "session_id": run.get("session_id"),
        "title": run.get("title"),
        "branch": summary.get("branch"),
        "parent_session": summary.get("parent_session"),
        "finished_at": run.get("finished_at"),
    }.items() if v}
    return Blocker(code="RUN_INTERRUPTED", summary="The run was interrupted before it finished.",
                   recovery="retry", user_action=None,
                   source={"kind": "agent_run", "ref": str(run.get("run_id") or "")}, detail=detail)


def from_task_run(task_run: Any) -> Blocker:
    """Adapt a `TaskRun` row whose status is `error` or `aborted`."""
    status = str(getattr(task_run, "status", "") or "")
    error = _clip(getattr(task_run, "error", "") or "", 300)
    try:
        name = getattr(getattr(task_run, "task", None), "name", "") or ""
    except Exception:  # a detached row cannot lazy-load its task
        name = ""
    detail = {k: v for k, v in {"task_id": getattr(task_run, "task_id", None), "task_name": name,
                                "status": status, "error": error}.items() if v}
    source = {"kind": "task_run", "ref": str(getattr(task_run, "id", "") or "")}
    label = f"The scheduled task {name!r}" if name else "A scheduled task"
    if status == "aborted":
        return Blocker(code="TASK_RUN_FAILED", summary=f"{label} was aborted before it finished.",
                       recovery="retry", user_action=None, source=source, detail=detail)
    action = (f"Read the error of the failed run and fix its cause: {error}" if error
              else "Open the failed run and read its error.")
    return Blocker(code="TASK_RUN_FAILED", summary=f"{label} failed.", recovery="user",
                   user_action=action, source=source, detail=detail)


def worker_capacity(limit: int, running: int, *, session_id: str = "") -> Blocker:
    """The chat's child-worker limit is reached.

    With room configured (`limit` above 0) the running workers free a slot
    when they finish, so nobody has to act: recovery `auto`. A limit of 0 never
    frees on its own and only the person can raise it.
    """
    detail = {"limit": int(limit), "running": int(running)}
    source = {"kind": "chat", "ref": str(session_id or "")}
    if limit > 0:
        return Blocker(code="WORKER_CAPACITY",
                       summary=f"The chat is at its limit of {limit} worker(s); a slot frees when one finishes.",
                       recovery="auto", user_action=None, source=source, detail=detail)
    return Blocker(code="WORKER_CAPACITY", summary="The chat allows no child workers, so none can start.",
                   recovery="user",
                   user_action="Raise this chat's Child workers limit in Agents > the chat > Loadout.",
                   source=source, detail=detail)
