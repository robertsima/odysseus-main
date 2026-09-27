"""Deterministic request assessment shared by routing and the agent loop.

This module deliberately has no tool-registry, policy, or model dependencies.
An assessment describes the human request; callers remain responsible for
authorization and for intersecting suggestions with their permitted tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class IntentAssessment:
    latest_text: str
    retrieval_query: str
    continuation: bool
    low_signal: bool
    domains: FrozenSet[str] = frozenset()
    needs_tools: bool = False
    route_category: str = ""
    reason: str = ""
    explicit_web: bool = False
    explicit_browser: bool = False
    # Set when the turn approves or points at the assistant's last message
    # ("i like that idea", "go ahead"): a clipped excerpt of that message.
    proposal_excerpt: str = ""

    def as_agent_dict(self) -> dict[str, object]:
        """Compatibility shape used by the existing agent-loop consumers."""
        return {
            "low_signal": self.low_signal,
            "continuation": self.continuation,
            "domains": set(self.domains),
            "retrieval_query": self.retrieval_query,
        }


_SYNTHETIC_PREFIXES = (
    "[Context —", "[Tool execution results]", "[Harness directive —",
    "[Message from agent session ",
)


def plain_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            str(block.get("text") or "") if isinstance(block, Mapping) else str(block)
            for block in content
            if isinstance(block, (Mapping, str))
        )
    return str(content or "")


def human_user_text(message: Mapping[str, Any]) -> Optional[str]:
    """Return human-authored user text, excluding runtime/evidence envelopes."""
    if message.get("role") != "user":
        return None
    content = plain_text(message.get("content", ""))
    if (message.get("metadata") or {}).get("trusted") is False:
        return None
    # Keep this module dependency-light while recognizing the canonical prompt
    # security envelope produced by untrusted_context_message.
    if (
        content.startswith(("UNTRUSTED SOURCE DATA\n", "UNTRUSTED SOURCE DATA:"))
        or "<<<UNTRUSTED_SOURCE_DATA>>>" in content
        or content.startswith(_SYNTHETIC_PREFIXES)
    ):
        return None
    return content


def human_texts(messages: Iterable[Mapping[str, Any]]) -> list[str]:
    return [text for msg in messages if (text := human_user_text(msg)) is not None]


def latest_human_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for msg in reversed(messages):
        if (text := human_user_text(msg)) is not None:
            return text
    return ""


def human_turn_count(messages: Iterable[Mapping[str, Any]]) -> int:
    return sum(human_user_text(msg) is not None for msg in messages)


def recent_human_context(messages: Sequence[Mapping[str, Any]], max_user: int = 3,
                         max_chars: int = 600) -> str:
    collected: list[str] = []
    for msg in reversed(messages):
        text = human_user_text(msg)
        if not text or not text.strip():
            continue
        collected.append(text.strip())
        if len(collected) >= max_user:
            break
    return "\n".join(collected)[:max_chars]


_LOW_SIGNAL_RE = re.compile(r"^[\W_]*$", re.UNICODE)
_CASUAL_OPENING_RE = re.compile(
    r"^\s*(?:h+i+|hey+|hello+|yo+|sup+|what'?s up|wass?up|hiya|howdy|"
    r"lol|lmao|haha+|hehe+|thanks?|thank you|ty|idk|dunno|meh|bruh|bro)\b(?P<tail>.*)$", re.I,
)
_CASUAL_BLOCKLIST_RE = re.compile(
    r"\b(?:cookbook|serve|serving|launch|start|vllm|sglang|llama\.?cpp|ollama|"
    r"download|model|email|document|doc|note|calendar|task|search|web|research|"
    r"file|folder|repo|git|settings?|endpoint|api|token|mcp)\b", re.I,
)
_ACTION_SIGNAL_RE = re.compile(
    r"\b(?:fix|resolve|reconcile|debug|diagnose|troubleshoot|investigate|check|verify|test|explain|find|look|search|"
    r"read|write|create|make|build|draft|add|remove|delete|update|change|edit|run|"
    r"launch|start|stop|deploy|refactor|implement|summari[sz]e|compare|list|show|"
    r"open|send|reply|fetch|handle|review|audit|plan|schedule|help|tell|set|enable|disable)\b",
    re.I,
)
_PHRASAL_ACTION_SIGNAL_RE = re.compile(
    r"\b(?:take\s+care\s+of|work\s+out|sort\b[^.!?\n]{0,40}\bout|"
    r"use\s+(?:the\s+)?(?:appropriate|available|named|specified)\s+"
    r"(?:[a-z0-9_-]+\s+){0,2}(?:tool|capability|function))\b",
    re.I,
)


def is_casual_low_signal(text: str) -> bool:
    s = str(text or "").strip()
    match = _CASUAL_OPENING_RE.match(s)
    if not match:
        return False
    tail = match.group("tail") or ""
    if (_CASUAL_BLOCKLIST_RE.search(tail) or _ACTION_SIGNAL_RE.search(tail)
            or _PHRASAL_ACTION_SIGNAL_RE.search(tail)):
        return False
    return len(re.findall(r"[A-Za-z0-9_'-]+", tail)) <= 2


_EXPLICIT_CONTINUATION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:yes|y|yeah|yep|ok|okay|sure|do it(?: anyway)?|go ahead|"
    r"continue(?: anyway)?|carry on|keep going|keep on|proceed(?: anyway)?|"
    r"pick up where you left off|run it|launch it|start it|use that|that one|same|the same|"
    r"first|second|third|the first one|the second one|the third one|[123]|[abc])"
    r"(?:\s{1,20}(?:with|on)?\s{0,20}(?:the\s{1,20})?(?:previous|last|prior|same|that|this)"
    r"\s{1,20}(?:task|thing|one|request|conversation|chat|topic))?"
    r"\s*(?:please\s*)?(?:[.!?]+\s*)?$", re.I,
)
_RETRY_RE = re.compile(
    r"\b(?:try again|retry|again|rerun|re-run|run it again|launch it again|start it again|"
    r"failed|fails?|died|crashed|broke|insta|instantly)\b", re.I,
)
_CONTINUE_WORK_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,.!]?\s{1,5}|great[,.!]?\s{1,5}|thanks[,.!]?\s{1,5}|"
    r"now\s{1,5}|please\s{1,5})?(?:continue|keep\s(?:going|working|at\sit)|carry\son|"
    r"proceed|resume|go\son|move\son|finish(?:\sup)?|do\sthe\snext|start\sthe\snext|"
    r"next\s(?:slice|step|task|part|phase|one|item))\b", re.I,
)

_BACKWARD_REFERENCE_RE = re.compile(
    r"\b(?:you|we|it|that|those|this)\b[^.!?\n]{0,80}"
    r"\b(?:before|earlier|previously|already)\b",
    re.I,
)
_METHOD_CAPABILITY_RE = re.compile(
    r"^\s*(?:please\s+)?(?:use|try|switch\s+to)\s+"
    r"(?:(?:your|the|those|these|available|connected|appropriate|actual|right|proper)\s+){1,3}"
    r"(?:(?:mcp|native|built[ -]?in)\s+)?(?:tools?|capabilit(?:y|ies)|functions?)\b",
    re.I,
)
_UNRESOLVED_ASSISTANT_RE = re.compile(
    r"\b(?:can(?:not|'t)|unable|could(?: not|n't)|do(?: not|n't) have|no access)\b|"
    r"\b(?:need(?: the| a| your)?|provide|send|share|give me|which|what)\b"
    r"[^.!?\n]{0,50}\b(?:url|link|path|folder|director(?:y|ies)|repo(?:sitory)?|slug|target)\b",
    re.I,
)
_LOCATOR_ONLY_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"https?://[^\s]+|"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|"
    r"(?:[A-Za-z]:[\\/]|/)[^\r\n]+?"
    r")(?:\s+please)?\s*[.!?]*\s*$",
    re.I,
)
_LOCATOR_PRIOR_ACTION_RE = re.compile(
    r"\b(?:git\s+)?(?:pull|fetch|clone|checkout|rebase)\b",
    re.I,
)


def _locator_grounding_continuation(
    messages: Sequence[Mapping[str, Any]], text: str
) -> bool:
    """Recognize a locator supplied to unblock the immediately prior task.

    A bare URL normally means "open this page", so it is not intrinsically a
    continuation.  It inherits context only for the narrow conversational
    shape where an assistant has just left an actionable human request
    unresolved.  Runtime envelopes and tool output cannot satisfy either side
    of that shape.
    """
    if not _LOCATOR_ONLY_RE.fullmatch(str(text or "").strip()):
        return False

    latest_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1)
         if human_user_text(messages[i]) is not None),
        None,
    )
    if latest_human_index is None:
        return False

    assistant_index = latest_human_index - 1
    if assistant_index < 0 or messages[assistant_index].get("role") != "assistant":
        return False
    assistant_text = plain_text(messages[assistant_index].get("content", "")).strip()
    if not assistant_text or not _UNRESOLVED_ASSISTANT_RE.search(assistant_text):
        return False

    prior_human = next(
        (human_user_text(messages[i]) for i in range(assistant_index - 1, -1, -1)
         if human_user_text(messages[i]) is not None),
        None,
    )
    return bool(
        prior_human
        and (looks_like_request(prior_human) or _LOCATOR_PRIOR_ACTION_RE.search(prior_human))
    )


def is_contextual_reference(messages: Sequence[Mapping[str, Any]], text: str) -> bool:
    """Whether a short turn points back at prior task state or its mechanism."""
    if human_turn_count(messages) <= 1:
        return False
    value = str(text or "").strip()
    backward = _BACKWARD_REFERENCE_RE.search(value)
    if backward and len(value) <= 180:
        # A correction can introduce a new task in a later clause. In that
        # shape the actionable clause is self-contained and must not inherit
        # the old retrieval query ("we already pulled; now search weather").
        later_clauses = re.split(r"[;.!?]+", value[backward.end():])
        if not any(looks_like_request(clause) for clause in later_clauses if clause.strip()):
            return True
    method = _METHOD_CAPABILITY_RE.match(value)
    if not method:
        return False
    # A method-only correction inherits the task. If another action appears
    # after the capability phrase, that tail is a new self-contained request.
    tail = value[method.end():]
    return not _ACTION_SIGNAL_RE.search(tail) and len(re.findall(r"[A-Za-z0-9_'-]+", tail)) <= 5


def is_explicit_continuation(text: str) -> bool:
    return bool(_EXPLICIT_CONTINUATION_RE.match(str(text or "").strip()))


def is_retry_continuation(messages: Sequence[Mapping[str, Any]], text: str) -> bool:
    return bool(_RETRY_RE.search(str(text or ""))) and human_turn_count(messages) > 1


def is_work_continuation(messages: Sequence[Mapping[str, Any]], text: str) -> bool:
    return bool(_CONTINUE_WORK_RE.match(str(text or ""))) and human_turn_count(messages) > 1


_QUESTION_SIGNAL_RE = re.compile(
    r"^\s*(?:what|why|how|when|where|who|which|can|could|should|would|will|"
    r"do|does|did|is|are|was|were|has|have|am)\b", re.I,
)


def looks_like_request(text: str) -> bool:
    """Cheap positive evidence that an unknown-domain turn is substantive."""
    value = str(text or "").strip()
    if not value or is_casual_low_signal(value):
        return False
    return bool(
        _ACTION_SIGNAL_RE.search(value) or _PHRASAL_ACTION_SIGNAL_RE.search(value)
        or "?" in value or _QUESTION_SIGNAL_RE.match(value)
    )


def assess_request(messages: Sequence[Mapping[str, Any]], latest_text: Optional[str] = None, *,
                   domains: Iterable[str] = (), request_like: Optional[bool] = None,
                   assistant_followup: bool = False) -> IntentAssessment:
    """Build the shared context portion of an assessment.

    Domain and request-like evidence are supplied by the deterministic caller
    vocabulary. Absence of a known domain never suppresses a substantive
    request: ``request_like`` clears low-signal and leaves semantic discovery on.
    """
    text = str(latest_text if latest_text is not None else latest_human_text(messages)).strip()
    if request_like is None:
        request_like = looks_like_request(text)
    anchor = proposal_reply_anchor(messages, text)
    continuation = bool(
        anchor
        or is_explicit_continuation(text) or assistant_followup
        or is_retry_continuation(messages, text)
        or is_work_continuation(messages, text)
        or is_contextual_reference(messages, text)
        or _locator_grounding_continuation(messages, text)
    )
    domain_set = frozenset(domains)
    if anchor:
        retrieval_query = anchored_retrieval_query(messages, anchor)
    else:
        retrieval_query = recent_human_context(messages) if continuation else text
    clearly_casual = not text or bool(_LOW_SIGNAL_RE.match(text)) or is_casual_low_signal(text)
    low_signal = clearly_casual or (not continuation and not domain_set and not request_like)
    return IntentAssessment(text, retrieval_query, continuation, low_signal, domain_set,
                            proposal_excerpt=clip_proposal(anchor))


# ── Replies that approve or point at the assistant's last message ────────────
#
# 2026-09-27: the assistant proposed a Claude Code sign-in flow; the user
# answered "i like that idea, you can use odysseus agents to inplement and
# push". That reply names no task of its own, and between it and the proposal
# sat the turn's context envelopes (memory, skills index, integrations, date).
# The turn was classified continuation=False low_signal=True, retrieval ran on
# older human turns, and the agent resumed the previous day's RAG task. A
# reply of this shape is anchored to the assistant message it answers.

STOPPED_BEFORE_REPLY_TEXT = "[Stopped by user before replying]"

_PROPOSAL_REPLY_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|yes|yeah|yep|yup|sure|great|perfect|cool|nice|alright|all\s+right|"
    r"awesome|excellent)\s*[,.!]?\s+)?"
    r"(?P<opener>"
    r"i\s+(?:really\s+|do\s+)?(?:like|love)\s+(?:that|this|the|your|it)"
    r"(?:\s+(?:idea|plan|approach|proposal|suggestion|one|flow|design))?"
    r"|(?:that|this|it)\s+(?:sounds|looks)\s+(?:good|great|perfect|fine|right|like\s+a\s+plan)"
    r"|sounds\s+(?:good|great|perfect|fine|like\s+a\s+plan)|looks\s+good|lgtm"
    r"|let'?s\s+(?:do|go\s+with|try|build|implement)\s+(?:it|that|this)(?:\s+one)?|let'?s\s+go"
    r"|go\s+(?:ahead|for\s+it)|make\s+it\s+so|ship\s+it"
    r"|(?:please\s+)?(?:do|implement|build|make)\s+(?:it|that|this)"
    r"|(?:can|could|would|will)\s+(?:u|you|ya)\s+(?:please\s+)?(?:do|implement|build|make)\s+(?:that|it|this)"
    r"|yes\s*,?\s*please|yes|yeah|yep|yup|sure"
    r"|agreed|approved|that\s+works|works\s+for\s+me|(?:that|this)\s+one|that|(?:i\s+)?agree"
    r")\b(?P<tail>.*)$",
    re.I | re.S,
)
# A tail that starts something else ("sounds good, now what's the weather")
# is a new request; one that says how to carry the proposal out ("…, use the
# odysseus agents to implement and push") is not.
_PROPOSAL_TAIL_NEW_TASK_RE = re.compile(
    r"\b(?:again|search|google|look\s+up|find|e-?mail|mail|send|reply|schedule|remind|"
    r"summari[sz]e|explain|translate|weather|news|what|when|where|who|why|how|"
    r"another|different|unrelated|forget|instead\s+of\s+(?:that|this|it))\b",
    re.I,
)
_PROPOSAL_TAIL_ACK_RE = re.compile(r"^\W*(?:thanks|thank\s+you|thx|ty|cheers)\W*$", re.I)
_PROPOSAL_EXCERPT_CHARS = 600


def is_proposal_reply(text: str) -> bool:
    """A short reply that approves, or refers to, the assistant's last message.

    Deliberately narrow: an approval opener, then at most a short clause about
    how to carry the proposal out. A long message, or a tail that starts a new
    request, is a request of its own.
    """
    value = str(text or "").strip()
    if not value or len(value) > 200:
        return False
    match = _PROPOSAL_REPLY_RE.match(value)
    if not match:
        return False
    tail = (match.group("tail") or "").strip()
    if not tail or re.fullmatch(r"[\W_]*", tail):
        return True
    if not re.match(r"^(?:[,.;:!?—–-]|and\b|but\b|then\b|so\b|now\b|anyway\b|please\b)", tail, re.I):
        return False  # "that is wrong", "do it again" — not a reply to the proposal
    tail = tail.lstrip(",.;:!?—–- ")
    if _PROPOSAL_TAIL_ACK_RE.match(tail) or _PROPOSAL_TAIL_NEW_TASK_RE.search(tail):
        return False
    return len(re.findall(r"[A-Za-z0-9_'-]+", tail)) <= 14


def _latest_human_index(messages: Sequence[Mapping[str, Any]]) -> Optional[int]:
    return next(
        (i for i in range(len(messages) - 1, -1, -1) if human_user_text(messages[i]) is not None),
        None,
    )


def last_assistant_reply(messages: Sequence[Mapping[str, Any]],
                         latest_text: Optional[str] = None) -> str:
    """Text of the assistant reply the latest human turn answers.

    Walks back from the latest human message (or from the end, when the
    current turn is not in ``messages`` yet), skipping context envelopes,
    tool results and tool-call-only assistant rounds, and stops at the
    previous human message: the reply must belong to the exchange directly
    before this turn. A turn the user stopped before any reply has none.
    """
    idx = _latest_human_index(messages)
    start = len(messages)
    if idx is not None:
        latest = (human_user_text(messages[idx]) or "").strip()
        if latest_text is None or latest == str(latest_text).strip():
            start = idx
    for i in range(start - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant":
            text = plain_text(msg.get("content", "")).strip()
            if text == STOPPED_BEFORE_REPLY_TEXT:
                return ""
            if text:
                return text
            continue
        if human_user_text(msg) is not None:
            return ""
    return ""


def proposal_reply_anchor(messages: Sequence[Mapping[str, Any]],
                          latest_text: Optional[str] = None) -> str:
    """The assistant reply a proposal-approval turn refers to, or ""."""
    text = str(latest_text if latest_text is not None else latest_human_text(messages)).strip()
    if not is_proposal_reply(text):
        return ""
    return last_assistant_reply(messages, text)


def clip_proposal(text: str, limit: int = _PROPOSAL_EXCERPT_CHARS) -> str:
    """Head and tail of a long proposal: the plan opens it and the question
    ("Want me to …?") usually closes it."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    tail = max(limit // 4, 1)
    head = max(limit - tail - 3, 1)
    return value[:head].rstrip() + " … " + value[-tail:].lstrip()


def anchored_retrieval_query(messages: Sequence[Mapping[str, Any]], anchor: str) -> str:
    """Retrieval text for a proposal reply: this turn, the request the
    proposal answered, and the proposal itself -- not every older human turn,
    which is where a stale task comes back from."""
    humans = recent_human_context(messages, max_user=2, max_chars=600)
    return f"{humans}\n{clip_proposal(anchor)}".strip()


def proposal_anchor_directive(anchor: str) -> str:
    """Harness directive placed directly before a proposal-reply turn."""
    return (
        "The user is replying to your previous message. Their request refers to what you "
        f"proposed there: «{clip_proposal(anchor)}». Act on that. Do not resume older tasks "
        "unless the user names them. Any context blocks between your reply and theirs are "
        "background reference, not part of the conversation."
    )


_WEB_HINT_RE = re.compile(
    r"\b(search|look\s*up|lookup|google|browse|web|online|latest|current|today|news|"
    r"weather|forecast|rate|exchange\s+rate)\b", re.I,
)
_BROWSER_HINT_RE = re.compile(
    r"\b(browser|browse|open\s+(?:the\s+)?(?:site|page|url|link)|click|fill(?:\s+out)?|"
    r"submit|send\s+(?:the\s+)?form|contact\s+form|web\s*form|form\s+submission)\b", re.I,
)


def explicit_route_hints(text: str) -> tuple[bool, bool]:
    value = str(text or "")
    return bool(_WEB_HINT_RE.search(value)), bool(_BROWSER_HINT_RE.search(value))
