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
    continuation = bool(
        is_explicit_continuation(text) or assistant_followup
        or is_retry_continuation(messages, text)
        or is_work_continuation(messages, text)
    )
    domain_set = frozenset(domains)
    retrieval_query = recent_human_context(messages) if continuation else text
    clearly_casual = not text or bool(_LOW_SIGNAL_RE.match(text)) or is_casual_low_signal(text)
    low_signal = clearly_casual or (not continuation and not domain_set and not request_like)
    return IntentAssessment(text, retrieval_query, continuation, low_signal, domain_set)


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
