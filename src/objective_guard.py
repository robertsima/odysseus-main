"""Stale-objective check for work launched right after a proposal is approved.

2026-09-27: the assistant proposed a Claude Code sign-in flow and the user
replied "i like that idea, you can use odysseus agents to inplement and
push". The agent started a worktree named ``rag-retrieval-health-improvements``
and delegated "RAG retrieval audit…" runs -- the previous day's task -- because
the reply carried no task words of its own and the proposal it answered was
buried behind the turn's context envelopes.

On a turn that approves a proposal, a call that launches new work (a
worktree, a delegated coding run, a worker) must share at least some
distinctive vocabulary with what was approved: the user's reply, the
assistant message it answers, and any correction the user sent mid-turn.
When it shares none, the call is not run and the model is told so, once; a
second identical call goes through, so a genuine sub-task phrased in other
words is delayed by one round, never blocked.

Pure functions only: the agent loop owns when to ask.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional, Set

# tool -> (actions that launch new work, default action when omitted,
#          argument fields that describe that work)
_LAUNCHERS: Dict[str, tuple] = {
    "manage_agent_worktree": ({"start"}, "status", ("name", "branch")),
    "delegate_to_claude_code": ({"run", "start"}, "run", ("prompt", "label")),
    "delegate_to_agent": ({"run", "start"}, "run", ("prompt", "label")),
    "manage_agent_loadout": ({"start"}, "list", ("task",)),
}

# Words that say nothing about WHICH task: English glue, plus the vocabulary
# every coding request in this app shares ("implement", "push", "agent",
# "odysseus", "repo", ...). Matching on those would let any stale task pass.
_STOPWORDS = frozenset("""
a an the and or but nor for with without from into onto over under about above
this that these those there here then than them they their its it's is are was
were be been being has have had do does did done can could would should will
shall may might must not no yes you your yours our ours we us me my mine i he
she his her him who whom which what when where why how all any each every some
such more most other also just only very too via per etc using use used make
made making need needs want wants please now new next first last same like
into onto upon while after before again still even ever much many few lot lots
get got let lets let's go going went way ways thing things stuff able sure okay
implement implementation improve improvement audit fix fixes add adds update
updates change changes create build run runs running task tasks work working
code coding repo repository branch branches worktree push pushed commit commits
dev main agent agents odysseus test tests testing feature features support check
review plan step steps file files bug bugs issue issues job jobs start started
finish done report result results pr prs merge project projects part parts
""".split())

_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "es", "ed", "s")


def _key(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:6]


_STOP_KEYS = frozenset(_key(word) for word in _STOPWORDS)


def distinctive_words(text: str) -> Set[str]:
    """Normalised keys of the words in ``text`` that could name a task."""
    keys: Set[str] = set()
    for word in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if len(word) < 3 or word.isdigit() or word in _STOPWORDS:
            continue
        key = _key(word)
        if key not in _STOP_KEYS:
            keys.add(key)
    return keys


def _args(content: Any) -> Optional[Dict[str, Any]]:
    if isinstance(content, dict):
        return content
    try:
        parsed = json.loads(content) if isinstance(content, str) else None
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def launch_task_text(tool: str, content: Any) -> Optional[str]:
    """The text describing the work a launching call starts, or None when the
    call does not launch new work (status, poll, list, …)."""
    spec = _LAUNCHERS.get(str(tool or ""))
    if not spec:
        return None
    actions, default, fields = spec
    args = _args(content)
    if args is None:
        return None
    action = str(args.get("action") or default).strip().lower()
    if action not in actions:
        return None
    text = " ".join(str(args.get(field) or "").strip() for field in fields).strip()
    return text or None


def _clip(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def stale_objective_refusal(tool: str, content: Any, approved: Iterable[str]) -> Optional[str]:
    """A refusal message when a launching call shares nothing with what the
    user approved; None when it is fine or cannot be judged."""
    task = launch_task_text(tool, content)
    if not task:
        return None
    task_keys = distinctive_words(task)
    if not task_keys:
        return None  # nothing distinctive to compare; do not guess
    approved_parts = [str(part or "").strip() for part in approved if str(part or "").strip()]
    approved_keys: Set[str] = set()
    for part in approved_parts:
        approved_keys |= distinctive_words(part)
    if not approved_keys:
        return None
    overlap = task_keys & approved_keys
    # A long brief can share one incidental word with anything; a short name
    # (a worktree, a label) sharing one real word is on topic.
    if overlap and not (len(task_keys) >= 12 and len(overlap) <= 1):
        return None
    approved_summary = " / ".join(_clip(part, 240) for part in approved_parts[:2])
    return (
        f"Not run: this task («{_clip(task, 160)}») shares nothing with what the user just approved "
        f"(«{approved_summary}»). Work on the approved proposal, or confirm with ask_user. If this "
        "call is part of it, say in one sentence how, then make it again."
    )
