"""Read a worker's hand-back and decide what in it can be checked.

Workers end with `Outcome: / Changed: / Checked: / Open:` plus optional
`Needs user:` / `Needs parent:` lines (`_PARENT_CHAT_NOTE` in `agent_loop`,
`WRAP_UP_TEXT` in `agent_control`). `parse_handback` splits that text.
`evidence` keeps only claims backed by a fact: a commit, a pull request, files
the run changed, or a passed-test count. The words "done" and "it works" are
claims, not evidence, and yield nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_MAX_ITEMS = 20
_MAX_ITEM_CHARS = 500
_BOLD = r"(?:\*\*|__)"

_LABEL_RE = re.compile(
    rf"^[\s>\-*•#]*{_BOLD}?\s*(outcome|changed|checked|open)\s*{_BOLD}?\s*:\s*{_BOLD}?\s*(.*?)\s*$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_CLAUSE_END_RE = re.compile(r"[.;,:—(]| - ")

_PARTIAL_RE = re.compile(r"\b(?:partly|partially|partial|incomplete)(?:\s+done|\s+complete(?:d)?)?\b", re.IGNORECASE)
_BLOCKED_RE = re.compile(r"\bblocked\b", re.IGNORECASE)
_DONE_RE = re.compile(r"\b(?:done|complete|completed|finished)\b", re.IGNORECASE)
_NEGATED_DONE_RE = re.compile(r"\b(?:not|never|isn't|wasn't)\s+(?:yet\s+)?(?:done|complete|completed|finished)\b", re.IGNORECASE)

_PASSED_RE = re.compile(r"(\d[\d,]*)\s+(?:tests?\s+)?(?:passed|passing)\b|\ball\s+(\d[\d,]*)\s+tests?\s+pass", re.IGNORECASE)
_FAILED_RE = re.compile(r"\b([1-9][\d,]*)\s+(?:failed|failing|failures?|errors?)\b", re.IGNORECASE)


@dataclass
class Handback:
    outcome: Optional[str] = None            # "done" | "partial" | "blocked" | None
    outcome_text: str = ""                   # the worker's own words after `Outcome:`
    changed: List[str] = field(default_factory=list)
    checked: List[str] = field(default_factory=list)
    open: List[str] = field(default_factory=list)
    needs_user: List[str] = field(default_factory=list)
    needs_parent: List[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _clean_item(text: str) -> str:
    text = re.sub(rf"\s*{_BOLD}\s*$", "", text.strip()).strip()
    return text[:_MAX_ITEM_CHARS]


def _classify_outcome(text: str) -> Optional[str]:
    text = re.sub(rf"{_BOLD}", "", text or "").strip()
    head = text[:100]
    if not head or _NEGATED_DONE_RE.search(head):
        return None
    without_partial = _PARTIAL_RE.sub(" PARTIAL ", head)
    cats = set()
    if "PARTIAL" in without_partial:
        cats.add("partial")
    if _BLOCKED_RE.search(without_partial):
        cats.add("blocked")
    if _DONE_RE.search(without_partial):
        cats.add("done")
    # The prompt's own template ("done, partly done or blocked, and why") lists
    # every answer; a worker that echoes it has not picked one.
    if len(cats) >= 2 and re.search(r"\bor\b", head, re.IGNORECASE):
        return None
    clause = _CLAUSE_END_RE.split(_PARTIAL_RE.sub(" PARTIAL ", text), 1)[0]
    for name, found in (("partial", "PARTIAL" in clause), ("blocked", bool(_BLOCKED_RE.search(clause))),
                        ("done", bool(_DONE_RE.search(clause)))):
        if found:
            return name
    return None


def parse_handback(text: str) -> Handback:
    """Split a hand-back into its labelled parts. Unlabelled text is ignored."""
    from src.agent_loop import NEEDS_LINE_RE

    hb = Handback()
    body = str(text or "")
    for m in NEEDS_LINE_RE.finditer(body):
        target = hb.needs_user if m.group(1).lower() == "user" else hb.needs_parent
        target.append(m.group(2)[:300])

    outcome_text: Optional[str] = None
    current: Optional[str] = None
    items: Optional[List[str]] = None
    for raw in body.splitlines():
        line = raw.rstrip()
        label = _LABEL_RE.match(line)
        if NEEDS_LINE_RE.match(line.strip()) or (label is None and re.match(r"^[\W_]{0,4}needs\s+(?:user|parent)\b", line, re.IGNORECASE)):
            current, items = None, None
            continue
        if label:
            current = label.group(1).lower()
            inline = _clean_item(label.group(2))
            if current == "outcome":
                outcome_text = inline or None
                items = None
                continue
            items = {"changed": hb.changed, "checked": hb.checked, "open": hb.open}[current]
            if inline and len(items) < _MAX_ITEMS:
                items.append(inline)
            continue
        if current is None:
            continue
        if not line.strip():
            if current != "outcome" and items:
                current, items = None, None
            continue
        item = _clean_item(_BULLET_RE.sub("", line))
        if current == "outcome":
            if outcome_text is None:
                outcome_text = item
            current = None
        elif items is not None and item and len(items) < _MAX_ITEMS:
            items.append(item)

    hb.outcome_text = (outcome_text or "")[:300]
    hb.outcome = _classify_outcome(outcome_text or "")
    return hb


def _commit_ref(commit: Any) -> str:
    if isinstance(commit, dict):
        return str(commit.get("sha") or commit.get("hash") or commit.get("id") or commit.get("short") or "").strip()
    return str(commit or "").strip()


def evidence(hb: Handback, run: Optional[Dict[str, Any]], *, run_id: str = "", by: str = "system") -> List[Dict[str, Any]]:
    """Evidence items a hand-back and its run summary support.

    `run` is the activity run summary (`commit`, `commits`, `pull_request`,
    `changed_files`). A bare commit count has no sha to check and is skipped.
    """
    run = run or {}
    now = _now_iso()
    items: List[Dict[str, Any]] = []
    seen = set()

    def add(kind: str, ref: str, detail: str) -> None:
        if not ref or (kind, ref) in seen:
            return
        seen.add((kind, ref))
        items.append({"kind": kind, "ref": ref, "detail": detail[:200], "at": now, "by": by})

    branch = str(run.get("branch") or "").strip()
    add("commit", _commit_ref(run.get("commit")), f"commit on {branch}" if branch else "commit")
    commits = run.get("commits")
    if isinstance(commits, (list, tuple)):
        for commit in commits[:10]:
            subject = str(commit.get("subject") or "") if isinstance(commit, dict) else ""
            add("commit", _commit_ref(commit), subject or "commit")
    pr = run.get("pull_request")
    if isinstance(pr, dict):
        add("pull_request", str(pr.get("url") or ""), f"pull request #{pr.get('number')}" if pr.get("number") else "pull request")
    elif isinstance(pr, str):
        add("pull_request", pr.strip(), "pull request")
    files = [str(f) for f in (run.get("changed_files") or []) if f]
    if files:
        shown = ", ".join(files[:3]) + (", ..." if len(files) > 3 else "")
        add("run", run_id or branch or "run", f"{len(files)} files changed: {shown}")

    for line in hb.checked:
        if _FAILED_RE.search(line):
            continue
        counts = [int((m.group(1) or m.group(2)).replace(",", "")) for m in _PASSED_RE.finditer(line)]
        if counts and max(counts) > 0:
            add("tests", line[:200], line)
            break
    return items
