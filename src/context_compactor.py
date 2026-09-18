"""
context_compactor.py

Auto-compacts conversation history when approaching context window limits.
Summarizes older messages via the same LLM, preserving key context.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from src.model_context import get_context_length, estimate_tokens
from src.llm_core import llm_call_async
from src.endpoint_resolver import resolve_endpoint
from core.models import ChatMessage

logger = logging.getLogger(__name__)


def _content_as_text(content: Any) -> str:
    """Flatten a message's content to plain text.

    Handles the three shapes that flow through history: a plain string, a
    multimodal list of content blocks (vision/image attachments), and None
    (assistant turns that carried only native tool_calls persist content as
    None). Returns "" for anything without text so callers can safely slice
    the result.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


COMPACT_THRESHOLD = 0.85  # Trigger compaction at 85% of context window
# When a trim is unavoidable, cut to this fraction of the budget rather than to
# the budget itself. See the anchor/hysteresis note in trim_for_context.
TRIM_TARGET_RATIO = 0.8
SUMMARY_MAX_TOKENS = 1024
SUMMARY_INPUT_MAX_TOKENS = 4096
SMALL_CONTEXT_LIMIT = 8192  # Models with context <= this get aggressive trimming

# Cursor-style self-summarization prompt — produces structured, dense summaries
SELF_SUMMARY_SYSTEM_PROMPT = """You are summarizing a conversation to preserve context after compaction. Produce a structured summary that lets the conversation continue seamlessly.

Use this format:

## Conversation Summary
**Turns summarized:** {count}  |  **Compactions so far:** {n}

### User Goal
One sentence describing what the user is trying to accomplish.

### What Was Done
- Bullet points of completed actions, decisions made, and key outputs
- Include specific file paths, function names, variable names, URLs, and config values
- Note any errors encountered and how they were resolved

### Current State
What is the system/code/task state right now? What was the last thing discussed?

### Pending / Next Steps
- What remains to be done
- Any open questions or blockers

### Key Context
- Important constraints, preferences, or decisions that must not be forgotten
- Specific values: model names, ports, paths, credentials references, versions

Keep the summary under 1000 tokens. Be dense — every token should carry information. Do not include pleasantries or meta-commentary."""


def normalize_compaction_summary(summary: str) -> str:
    """Remove redundant leading title text before adding our wrapper."""
    text = (summary or "").strip()
    text = re.sub(r"^(?:#{1,3}\s*)?Conversation Summary\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\*\*Conversation Summary\*\*\s*", "", text, flags=re.IGNORECASE)
    return text.lstrip()


def _is_compaction_summary(message: Any) -> bool:
    """Recognize persisted and request-local compaction summary messages."""
    if isinstance(message, dict):
        content = message.get("content", "")
        metadata = message.get("metadata") or {}
    else:
        content = getattr(message, "content", "")
        metadata = getattr(message, "metadata", None) or {}
    return bool(metadata.get("compacted")) or "[Conversation summary" in str(content or "")


def _compaction_generation(messages: List[Any]) -> int:
    """Return the highest persisted generation, with legacy summaries counting once."""
    generations = []
    for message in messages:
        metadata = (
            (message.get("metadata") or {}) if isinstance(message, dict)
            else (getattr(message, "metadata", None) or {})
        )
        try:
            generations.append(max(1, int(metadata.get("compaction_count") or 1)))
        except (TypeError, ValueError):
            generations.append(1)
    return max(generations, default=0)


def _bounded_compaction_source(prior_summaries: List[Dict], older: List[Dict]) -> str:
    """Build bounded recursive-summary input while retaining old and new state.

    Previous implementations kept every old summary as a system message and
    added another on each compaction. Context and provider-prefix costs therefore
    grew with compaction count. Fold prior summaries into the next summary and
    cap the source sent to the utility model.
    """
    prior_text = "\n\n".join(
        _content_as_text(msg.get("content")) for msg in prior_summaries
    ).strip()
    older_text = "\n".join(
        f"{msg.get('role', 'user').upper()}: {_content_as_text(msg.get('content'))[:2000]}"
        for msg in older
    ).strip()
    if prior_text and older_text:
        # Reserve half for each source. Truncating only after concatenation can
        # put the section boundary in the discarded middle and leave the model
        # without either the prior state or the new turns it must fold in.
        section_budget = (SUMMARY_INPUT_MAX_TOKENS - 64) // 2
        prior_text = _truncate_text_to_token_budget(prior_text, section_budget)
        older_text = _truncate_text_to_token_budget(older_text, section_budget)
    elif prior_text:
        prior_text = _truncate_text_to_token_budget(prior_text, SUMMARY_INPUT_MAX_TOKENS - 32)
    else:
        older_text = _truncate_text_to_token_budget(older_text, SUMMARY_INPUT_MAX_TOKENS - 32)

    sections = []
    if prior_text:
        sections.append("PRIOR COMPACTED CONTEXT:\n" + prior_text)
    if older_text:
        sections.append("NEW TURNS TO FOLD IN:\n" + older_text)
    return "\n\n".join(sections)


def _sanitize_tool_messages(msgs: List[Dict]) -> List[Dict]:
    """Drop orphaned `tool` messages and dangling assistant `tool_calls`.

    OpenAI's API requires every `role:"tool"` message to immediately
    follow an assistant message that carries `tool_calls` (or another
    tool message in the same batch). Front-trimming the history can cut
    the assistant `tool_calls` parent while keeping its tool responses,
    which triggers: "messages with role 'tool' must be a response to a
    preceding message with 'tool_calls'". This pass repairs that:
      - drops `tool` messages with no valid preceding tool_calls
      - drops assistant `tool_calls` messages whose tool responses were
        all trimmed away (some providers reject unanswered tool_calls)
    """
    # Pass 1: drop orphan tool messages.
    cleaned: List[Dict] = []
    in_batch = False  # are we right after an assistant tool_calls (or mid-batch)?
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            if in_batch:
                cleaned.append(m)
            # else: orphan — drop
            continue
        if role == "assistant" and m.get("tool_calls"):
            in_batch = True
        else:
            in_batch = False
        cleaned.append(m)

    # Pass 2: drop assistant tool_calls messages that have NO following
    # tool response (dangling) — walk backwards so we know what follows.
    out: List[Dict] = []
    for i, m in enumerate(cleaned):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = cleaned[i + 1] if i + 1 < len(cleaned) else None
            if not (nxt and nxt.get("role") == "tool"):
                # Dangling tool_calls — keep the message but strip the
                # tool_calls so it's a plain assistant turn (preserves any
                # text content the model produced alongside the calls).
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                if not (m.get("content") or "").strip():
                    continue  # nothing left worth keeping
        out.append(m)
    return out


def _message_text_token_estimate(text: str) -> int:
    if not isinstance(text, str):
        return 4
    return int(len(text) * 0.3) + 4


def _truncate_text_to_token_budget(text: str, token_budget: int) -> str:
    """Trim a too-large current user message instead of dropping it entirely."""
    if token_budget <= 32:
        return "[Current user message omitted: it exceeded the model context window.]"

    if not isinstance(text, str):
        # This helper is typed/used as text downstream, so return an empty
        # string rather than the raw non-string (which would move the crash
        # into the caller that concatenates/measures the result).
        return ""
    # Match src.model_context.estimate_tokens' rough chars * 0.3 estimate.
    max_chars = max(200, int((token_budget - 16) / 0.3))
    if len(text) <= max_chars:
        return text

    notice = (
        "\n\n[Notice: the pasted message was too large for this model's context "
        "window, so Odysseus kept the beginning and end.]"
    )
    keep_chars = max(200, max_chars - len(notice))
    head_len = max(100, int(keep_chars * 0.7))
    tail_len = max(80, keep_chars - head_len)
    return text[:head_len].rstrip() + notice + "\n\n" + text[-tail_len:].lstrip()


def _truncate_tool_call_args(msg: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
    """Shrink oversized assistant ``tool_calls`` arguments to fit ``token_budget``.

    A tool-only turn persists ``content=None`` with its whole payload in
    ``tool_calls[].function.arguments`` (e.g. a large create_document body), which
    the text-content truncation can't reach — so the message could stay over
    budget and the upstream call would 400. Replace each argument string that
    overflows its share of the budget with a small valid-JSON placeholder,
    preserving ``id``/``type``/``function.name`` so tool/result pairing and
    provider validation are unaffected. Returns msg unchanged when there is
    nothing oversized.
    """
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return msg
    # Budget left after whatever content survived (estimate_tokens counts tool
    # arguments too, so measure content alone here).
    content_tokens = estimate_tokens([{"role": msg.get("role", "assistant"), "content": msg.get("content")}])
    per_call = max(16, (max(0, token_budget - content_tokens)) // len(tool_calls))
    new_calls = []
    changed = False
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args, str) and int(len(args) * 0.3) > per_call:
            new_fn = dict(fn)
            new_fn["arguments"] = json.dumps({"_truncated_for_context": len(args)})
            new_tc = dict(tc)
            new_tc["function"] = new_fn
            new_calls.append(new_tc)
            changed = True
        else:
            new_calls.append(tc)
    if not changed:
        return msg
    out = dict(msg)
    out["tool_calls"] = new_calls
    return out


def _truncate_message_to_token_budget(msg: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
    """Return a copy of msg whose text content (and tool-call args) fit token_budget."""
    out = dict(msg)
    content = out.get("content", "")
    if isinstance(content, str):
        out["content"] = _truncate_text_to_token_budget(content, token_budget)
    elif isinstance(content, list):
        remaining = token_budget
        new_content = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                new_content.append(item)
                continue
            text = item.get("text", "")
            truncated = _truncate_text_to_token_budget(text, remaining)
            cloned = dict(item)
            cloned["text"] = truncated
            new_content.append(cloned)
            remaining -= _message_text_token_estimate(truncated)
        out["content"] = new_content
    # A tool-only turn (content=None) carries its payload in tool_calls args,
    # which the branches above can't shrink — handle it so the message can fit.
    return _truncate_tool_call_args(out, token_budget)


def trim_for_context(messages: List[Dict], context_length: int, reserve_tokens: int = 512,
                     target_ratio: Optional[float] = None) -> List[Dict]:
    """Trim system messages to fit within context_length.

    For small-context models, progressively strips:
    1. RAG/memory system messages (keep preset system prompt)
    2. Older conversation turns
    Reserves space for the response.
    """
    # The reserve (output room + tool schemas) must never eat the whole budget.
    # An agent with ~6K of tool schemas against a 6K budget got a NEGATIVE
    # budget, so every step below failed to fit and the harshest path ran every
    # round: system prompt cut to 2000 chars, tool results truncated, the
    # user's request reduced to a fragment. Keep at least half for messages.
    reserve_tokens = max(0, min(reserve_tokens, context_length // 2))
    budget = context_length - reserve_tokens
    used = estimate_tokens(messages)
    if used <= budget:
        return messages

    logger.info(f"Trimming messages: {used} tokens > {budget} budget (ctx={context_length})")

    # Separate system messages from conversation.
    # Messages marked _protected (e.g. active document) are never trimmed.
    system_msgs = []
    protected_msgs = []
    convo_msgs = []
    for msg in messages:
        if msg.get("_protected"):
            protected_msgs.append(msg)
        elif msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            convo_msgs.append(msg)

    # Protected messages count toward budget but are never dropped
    protected_tokens = estimate_tokens(protected_msgs)
    budget -= protected_tokens

    # Priority: keep first system msg (preset prompt), drop others (memory, RAG, memo).
    # Exception: a research-spinoff primer (the seeded report that grounds a
    # "Discuss" chat) must never be dropped — it is the conversation's whole
    # knowledge base. Treat any system message carrying research_spinoff_from
    # metadata as essential alongside the leading system prompt.
    def _is_research_primer(m):
        return bool((m.get("metadata") or {}).get("research_spinoff_from"))
    _primers = [m for m in system_msgs if _is_research_primer(m)]
    _non_primer = [m for m in system_msgs if not _is_research_primer(m)]
    essential_system = (_non_primer[:1] if _non_primer else []) + _primers
    extra_system = _non_primer[1:]

    # Try dropping extra system messages one by one (from the end)
    trimmed = essential_system + convo_msgs
    if estimate_tokens(trimmed) <= budget:
        # Dropping extras was enough — try adding back some
        result = list(essential_system)
        for msg in extra_system:
            candidate = result + [msg] + convo_msgs
            if estimate_tokens(candidate) <= budget:
                result.append(msg)
            else:
                break
        return _sanitize_tool_messages(result + protected_msgs + convo_msgs)

    # Still too big — truncate the first system message (but keep more than 500 chars)
    if essential_system:
        sys_text = essential_system[0].get("content", "")
        if len(sys_text) > 2000:
            truncated_system = dict(essential_system[0])
            truncated_system["content"] = sys_text[:2000] + "\n[System prompt truncated for context limits]"
            essential_system[0] = truncated_system
            trimmed = essential_system + convo_msgs
            if estimate_tokens(trimmed) <= budget:
                return _sanitize_tool_messages(essential_system + protected_msgs + convo_msgs)

    # Still too big — drop older conversation turns BUT always keep the current
    # turn AND the request that started the conversation. If a pasted message
    # alone exceeds the model context, truncate that message with a visible
    # notice instead of dropping it; otherwise the model appears to "ignore"
    # large pastes because it never receives them.
    #
    # Recency alone is the wrong rule inside an agent run. "Keep the current
    # turn" protects convo_msgs[-1], but mid-run that is a TOOL RESULT — the
    # user's actual request is further back, and front-trimming reaches it
    # first. So a long run kept the last log dump and dropped what it was
    # supposed to be doing with it. That is what "the agent goes flat after a
    # few rounds" looks like from the inside.
    #
    # The anchor is the LAST user message, not the first: in a long chat the
    # first one is a stale topic from 200 turns ago, while the last one is the
    # request the current work actually serves.
    PROTECT_RECENT = 10
    current_msg = convo_msgs[-1:] if convo_msgs else []
    prior_convo = convo_msgs[:-1] if convo_msgs else []

    anchor = []
    for i in range(len(prior_convo) - 1, -1, -1):
        if prior_convo[i].get("role") == "user":
            anchor = [prior_convo.pop(i)]
            break

    # Trim to a target BELOW the budget, not just to the edge of it. Trimming
    # rewrites the front of the prompt, which invalidates the provider's prefix
    # cache from that point on; stopping exactly at the budget means the next
    # round is over again and re-trims, paying a full re-prefill every single
    # round. One deeper cut buys many cheap rounds.
    trim_target = int(budget * (target_ratio or TRIM_TARGET_RATIO))

    def _fits(msgs, limit):
        return estimate_tokens(essential_system + anchor + msgs) <= limit

    if len(prior_convo) >= PROTECT_RECENT:
        old_msgs = prior_convo[:-(PROTECT_RECENT - 1)]
        recent_msgs = prior_convo[-(PROTECT_RECENT - 1):] + current_msg
        while old_msgs and not _fits(old_msgs + recent_msgs, trim_target):
            old_msgs.pop(0)
        convo_msgs = anchor + old_msgs + recent_msgs
    else:
        while prior_convo and not _fits(prior_convo + current_msg, trim_target):
            prior_convo.pop(0)
        convo_msgs = anchor + prior_convo + current_msg

    # The anchor is re-inserted ahead of what survived, so a batch of tool
    # messages can no longer be separated from the assistant turn that called
    # them — _sanitize_tool_messages at the end repairs any pairing this broke.

    # The anchor is protected from being DROPPED, not from being shortened. A
    # 50k-character paste as the opening message would otherwise consume the
    # whole window and starve the work that followed it.
    if anchor and estimate_tokens(essential_system + protected_msgs + convo_msgs) > budget:
        rest = [m for m in convo_msgs if m is not anchor[0]]
        room = max(256, budget - estimate_tokens(essential_system + protected_msgs + rest))
        if estimate_tokens(anchor) > room:
            trimmed_anchor = _truncate_message_to_token_budget(anchor[0], room)
            convo_msgs = [trimmed_anchor] + rest

    # If the current message itself is too large, shrink only that message.
    if current_msg and estimate_tokens(essential_system + protected_msgs + convo_msgs) > budget:
        prefix = essential_system + protected_msgs + convo_msgs[:-1]
        available_for_current = max(64, budget - estimate_tokens(prefix))
        convo_msgs[-1] = _truncate_message_to_token_budget(convo_msgs[-1], available_for_current)

    result = _sanitize_tool_messages(essential_system + protected_msgs + convo_msgs)
    logger.info(f"Trimmed to {estimate_tokens(result)} tokens ({len(result)} messages)")
    return result


async def maybe_compact(
    session,
    endpoint_url: str,
    model: str,
    messages: List[Dict],
    headers: Optional[Dict] = None,
    owner: Optional[str] = None,
    *,
    persist: bool = True,
    compaction_state: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Check context usage and compact if above threshold.

    Returns (messages, context_length, was_compacted).
    """
    context_length = get_context_length(endpoint_url, model)
    used = estimate_tokens(messages)
    pct = (used / context_length) * 100 if context_length else 0

    if pct < COMPACT_THRESHOLD * 100:
        return messages, context_length, False

    logger.info(
        f"Context at {pct:.1f}% ({used}/{context_length} tokens) — compacting"
    )

    # Split into system preface and conversation
    system_msgs = []
    convo_msgs = []
    for msg in messages:
        if msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            convo_msgs.append(msg)

    if len(convo_msgs) < 4:
        return messages, context_length, False

    # Split conversation: summarize older half, keep recent half
    split_point = len(convo_msgs) // 2
    older = convo_msgs[:split_point]
    recent = convo_msgs[split_point:]

    # Fold prior summaries into one replacement instead of recursively retaining
    # every summary as a system message. Bound the utility-model input so each
    # compaction has a predictable token cost even in very long agent sessions.
    prior_summaries = [m for m in system_msgs if _is_compaction_summary(m)]
    retained_system_msgs = [m for m in system_msgs if not _is_compaction_summary(m)]
    convo_text = _bounded_compaction_source(prior_summaries, older)
    compaction_count = _compaction_generation(prior_summaries)

    # Use utility model if configured, otherwise fall back to session model
    util_url, util_model, util_headers = resolve_endpoint("utility", owner=owner)
    compact_url = util_url or endpoint_url
    compact_model = util_model or model
    compact_headers = util_headers if util_url else headers

    prompt = SELF_SUMMARY_SYSTEM_PROMPT.replace(
        "{count}", str(len(older))
    ).replace(
        "{n}", str(compaction_count + 1)
    )
    summary_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": convo_text},
    ]

    try:
        summary = await llm_call_async(
            compact_url,
            compact_model,
            summary_messages,
            temperature=0.2,
            max_tokens=SUMMARY_MAX_TOKENS,
            headers=compact_headers,
            timeout=30,
        )
    except Exception as e:
        logger.error(f"Compaction summary failed: {e}")
        # Degrade gracefully: keep the conversation intact rather than
        # silently dropping the older half. was_compacted=False signals the
        # caller nothing was summarized; trim_for_context handles length.
        return messages, context_length, False
    summary = normalize_compaction_summary(summary)

    summary_msg = {
        "role": "system",
        "content": f"[Conversation summary — earlier messages were compacted]\n{summary}",
        "metadata": {"compacted": True, "compaction_count": compaction_count + 1},
    }

    compacted = retained_system_msgs + [summary_msg] + recent

    # Update session history to match. Pass len(system_msgs) so the
    # recent_history slice in _update_session_history uses the correct
    # offset — session.history INCLUDES the system messages, but
    # split_point is indexed against convo_msgs which does NOT. Without
    # this, the slice drops the leading system message(s).
    if compaction_state is not None:
        compaction_state.update({
            "split_point": split_point,
            "summary": summary,
            "system_msg_count": len(system_msgs),
            "applied": False,
        })
    if persist:
        _update_session_history(
            session, split_point, summary, system_msg_count=len(system_msgs),
            compaction_count=compaction_count + 1,
        )
        if compaction_state is not None:
            compaction_state["applied"] = True

    new_used = estimate_tokens(compacted)
    logger.info(
        f"Compacted: {used} -> {new_used} tokens "
        f"({len(older)} messages summarized, {len(recent)} kept)"
    )

    return compacted, context_length, True


def apply_compaction_state(session, compaction_state: Optional[Dict[str, Any]]) -> bool:
    """Persist a route-specific compaction after that route commits output.

    Candidate prompts may be compacted speculatively while an explicit
    foreground fallback chain is being tried.  Persisting at construction time
    would let an unavailable route rewrite history before another route answers,
    so callers hold this small plan and apply only the winning route's plan.
    """

    state = compaction_state if isinstance(compaction_state, dict) else None
    if not state or state.get("applied"):
        return False
    summary = state.get("summary")
    split_point = state.get("split_point")
    system_msg_count = state.get("system_msg_count", 0)
    if not isinstance(summary, str) or not isinstance(split_point, int):
        return False
    _update_session_history(
        session,
        split_point,
        summary,
        system_msg_count=system_msg_count if isinstance(system_msg_count, int) else 0,
    )
    state["applied"] = True
    return True


def apply_compaction_state_for_session(
    session_id: Optional[str],
    compaction_state: Optional[Dict[str, Any]],
) -> bool:
    """Resolve an in-memory session and apply a deferred compaction plan."""

    if not session_id:
        return False
    try:
        from core.models import get_session_manager_instance

        manager = get_session_manager_instance()
        session = manager.get_session(session_id) if manager else None
    except Exception:
        session = None
    return apply_compaction_state(session, compaction_state) if session else False


def _update_session_history(session, split_point: int, summary: str,
                            system_msg_count: int = 0,
                            compaction_count: int = 1):
    """Update the in-memory session history after compaction.

    `split_point` is the index in `convo_msgs` (system-stripped). The
    in-memory `session.history` includes leading system messages, so the
    actual recent-history slice starts at `system_msg_count + split_point`.
    Prepending `session.history[:system_msg_count]` to the new history
    preserves persona, preset, and RAG system messages that would
    otherwise be dropped.
    """
    if not session or not hasattr(session, "history"):
        return

    effective_split = system_msg_count + split_point
    if effective_split >= len(session.history):
        return

    # Keep the recent messages, prepend summary AND the leading system
    # messages so the system prompt survives compaction.
    system_prefix = [
        msg for msg in session.history[:system_msg_count]
        if not _is_compaction_summary(msg)
    ]
    recent_history = session.history[effective_split:]
    summary = normalize_compaction_summary(summary)
    summary_msg = ChatMessage(
        role="system",
        content=f"[Conversation summary]\n{summary}",
        metadata={
            "compacted": True,
            "summarized_count": split_point,
            "compaction_count": compaction_count,
        },
    )
    new_history = system_prefix + [summary_msg] + recent_history
    try:
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
    except Exception:
        manager = None
    if manager and getattr(session, "id", None):
        if manager.replace_messages(session.id, new_history):
            return
    session.history = new_history


# ─────────────────────────────────────────────────────────────────────────────
# Execution ledger: collapsing completed tool exchanges
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY
#
# A tool result is written into the transcript once and then replayed to the
# model on every remaining round of the turn. The Sept 15-16 production audit
# measured one workflow at 26 rounds / ~96k prompt tokens with tool results from
# long-finished phases still riding along. `tool_output_store` already caps a
# SINGLE oversized result; it does nothing about twenty ordinary ones
# accumulating. This is the accumulation half of that problem.
#
# WHAT A LEDGER ENTRY KEEPS, AND WHY THAT LIST
#
# `format_tool_result` has a structural property we can lean on instead of
# guessing: facts live OUTSIDE fenced blocks and bulk lives INSIDE them.
#
#     ### read_file: /srv/app/src/agent_loop.py      <- fact (what, and on what)
#     **content (31204 chars):**                     <- fact (shape of result)
#     ```                                            <- bulk begins
#     ...31k characters of source...
#     ```                                            <- bulk ends
#     **exit_code:** 0                               <- fact (outcome)
#
# So an entry keeps every non-fenced line — the `### <tool>: <target>` header
# (the path read, the command run, the query issued, the file written), the
# `File written: …` / `Document created … (id: …, v3)` / `Session created …
# (id: …)` outcome lines, exit codes, error text — and drops the fenced payload.
# That is the split the audit asks for: drop bulk, keep facts. A path or an id
# the agent would otherwise have to re-derive by re-running a tool is never
# inside a fence, so it is never dropped.
#
# WHAT IT DROPS, AND WHY THAT IS SAFE
#
# Nothing is destroyed. Before a message is collapsed its ORIGINAL text is
# written to `tool_output_store`, and the entry names the resulting `toolout-…`
# ref, so `recall_tool_output` can still search or page through the full
# verbatim result. If the store write fails and the result carries no ref of its
# own, the exchange is left untouched rather than trimmed — the offload is the
# precondition for compacting, not a bonus. A ledger entry is therefore a
# pointer plus the facts you would otherwise have had to open the pointer for.
#
# WHAT IS NEVER COLLAPSED
#
# - The most recent `keep_rounds` tool exchanges. Recency is where an exact
#   `read_file` body is still needed by the `edit_file` that follows it.
# - Anything under `LEDGER_MIN_RESULT_CHARS`. Short results are ids, paths and
#   confirmations — pure fact, no bulk. Compacting them saves nothing and is all
#   downside.
# - Results from `_LEDGER_NEVER_COMPACT` tools. `recall_tool_output` is the
#   model's *second* attempt to obtain something the head/tail excerpt did not
#   give it; collapsing it invites exactly the re-ask loop documented in
#   `tool_output_store`. `ask_user` ends the turn and holds the live question.
# - Failed / blocked / non-zero-exit exchanges, until a LATER round ran the same
#   tool successfully. An unresolved error is active state, not a completed
#   phase. Once something resolved it the entry still records the failure line,
#   so the model does not blindly retry into it.
#
# HOW THIS AVOIDS PER-ROUND PREFIX INVALIDATION
#
# `specs/prompt-prefix-stability.md` documents the incident this mechanism could
# easily repeat: pruning replayed reasoning items a little every round moved the
# provider cache boundary every round, and `cached=` sat flat at the static
# prefix for 28 rounds. Editing a tool result in the middle of an already-cached
# prompt costs the same. So:
#
#   1. Compaction fires in BATCHES. Unprocessed exchanges accumulate to
#      `keep_rounds + slack_rounds` before anything is touched, then everything
#      older than `keep_rounds` collapses in one pass and every exchange in that
#      pass is marked processed. Invalidation happens once per ~`slack_rounds`
#      rounds instead of every round — the same discipline, and the same slack
#      rule, as the reasoning-item prune in `_append_tool_results`.
#   2. Entries are APPEND-ONLY and never rewritten. Each batch collapses a
#      contiguous stretch of exchanges IN PLACE and leaves earlier entries
#      byte-identical, so the invalidation point advances monotonically toward
#      the tail. A single growing consolidated ledger message would instead have
#      to be rewritten on every batch, moving the boundary back to the front of
#      the conversation each time — worse than doing nothing.
#
# Note the deliberate difference from `maybe_compact` above, which REPLACES
# prior summaries per `specs/bounded-recursive-compaction.md`. That rule exists
# because summaries are derived from each other and would otherwise stack.
# Ledger entries are not recursive: an entry is derived once, from one message,
# and is then immutable, so there is nothing to accumulate and the replacement
# rule has nothing to act on. An entry is also never fed to the utility model —
# it is not a summary, it is the surviving non-bulk text — so it cannot consume
# summary-input budget. When `maybe_compact` later summarizes a stretch of
# history containing entries, it folds them in like any other message.
#
# SCOPE
#
# This edits the request-local `messages` list only. Persisted history and the
# UI re-render from `tool_events` (desc/command/output), which this never
# touches, so a reloaded conversation still shows full tool output.

LEDGER_KEEP_ROUNDS = 3
# Unprocessed exchanges may overrun the keep window by this much before a batch
# fires. Mirrors the reasoning-replay slack rule (`max(4, window)`).
LEDGER_SLACK_ROUNDS = 4
# Below this, a result is facts rather than bulk — leave it alone.
LEDGER_MIN_RESULT_CHARS = 600
# Ceiling on the fact text a single entry may carry forward.
LEDGER_MAX_ENTRY_CHARS = 900
# Per-line ceiling, so one pathological unfenced line cannot fill the entry.
_LEDGER_MAX_LINE_CHARS = 240

_LEDGER_NEVER_COMPACT = frozenset({"recall_tool_output", "ask_user"})

_LEDGER_REF_RE = re.compile(r"\btoolout-[0-9a-f]{10}\b")
_LEDGER_FENCE_RE = re.compile(r"^\s*```")
_LEDGER_HEADER_RE = re.compile(r"^###[ \t]+(?P<desc>.*)$", re.MULTILINE)
# How far into a result to look for that header. Comfortably past the untrusted
# wrapper (`UNTRUSTED_CONTEXT_HEADER` + guard + `Source:` line ≈ 480 chars) that
# fronts a textual-path tool-results message, and short enough that a stray
# markdown `###` deep inside the payload cannot be mistaken for it.
_LEDGER_HEADER_SCAN_CHARS = 1200
_LEDGER_EXIT_RE = re.compile(r"^\*\*exit_code:\*\*\s*(?P<code>\S+)")
# Boilerplate the offload excerpt appends. The ledger emits its own pointer, so
# carrying this too would be ~350 characters of duplicated instructions.
_LEDGER_BOILERPLATE = (
    "This output was large, so only its head and tail are shown.",
)
_LEDGER_FAIL_MARKERS = (
    "**Error:**",
    ": BLOCKED",
    "APPROVAL REQUIRED",
    "misformatted tool call",
)
# Lines that must survive the entry budget whatever else is dropped: the tool
# header, structured outcome lines, and the omission notes.
_LEDGER_STRUCTURAL_PREFIXES = (
    "###",
    "**",
    "[",
    "File written:",
    "Document created:",
    "Document updated:",
    "Document edited:",
    "Session created:",
    "Error:",
)

LEDGER_HEADER = (
    "[Execution ledger — this completed tool exchange was compacted; "
    "the facts below are still current]"
)

# Stamped on every result message a ledger batch has considered, whether or not
# it was rewritten. It means "already processed", not "was collapsed": without
# that distinction a group holding one short result would stay countable
# forever, the batch gate would never fall back below its threshold, and a batch
# would fire EVERY round — reintroducing the per-round invalidation this design
# exists to avoid. Stripped before the provider call by the allow-list in
# `_sanitize_llm_messages`, so it never reaches an API.
LEDGER_MARK = "_ledger"


def _ledger_enabled() -> bool:
    """Kill switch. This runs on the path of every agent turn, so it needs one.

    Read from the environment rather than settings: `_append_tool_results` has
    no session/owner handle to resolve a setting against, and a per-round
    settings lookup would be a database hit on the hot path.
    """
    value = (os.getenv("ODYSSEUS_AGENT_EXECUTION_LEDGER", "1") or "1").strip().lower()
    return value not in ("0", "false", "off", "no")


def _ledger_tool_name(text: str, fallback: str = "") -> str:
    """The tool a formatted result came from, read off its `### <desc>` header.

    `desc` is built as `"{tool}: {first_line}"` (or bare `"{tool}"`) by
    `execute_tool_block`, so the token before the first colon is the tool name.

    The header is searched for within a leading window rather than required on
    the first line: a textual-path result sits behind the untrusted-context
    wrapper, and an already-collapsed entry sits behind LEDGER_HEADER. Both must
    still be identifiable, because the failure-resolution scan keys on the tool
    name and an unidentifiable failure is never collapsed at all.
    """
    match = _LEDGER_HEADER_RE.search((text or "")[:_LEDGER_HEADER_SCAN_CHARS])
    if not match:
        return fallback
    desc = match.group("desc").strip()
    return (desc.split(":", 1)[0] if ":" in desc else desc).strip() or fallback


def _ledger_failed(text: str) -> bool:
    """Did this result report a failure the agent may still be working around?

    Conservative by construction: any recognised failure marker, or any non-zero
    or unknown exit code, counts. A false positive costs some tokens; a false
    negative silently collapses the error the agent is mid-recovery from.
    """
    body = text or ""
    if any(marker in body for marker in _LEDGER_FAIL_MARKERS):
        return True
    for line in body.splitlines():
        match = _LEDGER_EXIT_RE.match(line.strip())
        if match and match.group("code") not in ("0", "None"):
            return True
    return False


def _ledger_facts(text: str) -> str:
    """Strip fenced bulk from a formatted tool result, keep the rest, bound it.

    Each fenced block becomes a one-line note of how much was dropped, so the
    agent can see output existed and roughly how big it was — a bare absence
    reads like the tool returned nothing, which invites a re-run.
    """
    kept: List[str] = []
    in_fence = False
    fence_chars = 0
    fence_lines = 0

    def _flush_fence() -> None:
        nonlocal fence_chars, fence_lines
        if fence_chars:
            kept.append(f"[{fence_chars:,} chars / {fence_lines:,} lines of output omitted]")
        fence_chars = 0
        fence_lines = 0

    for line in (text or "").splitlines():
        if _LEDGER_FENCE_RE.match(line):
            if in_fence:
                _flush_fence()
            in_fence = not in_fence
            continue
        if in_fence:
            fence_chars += len(line) + 1
            fence_lines += 1
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_LEDGER_BOILERPLATE):
            continue
        # The excerpt's own "[... N of M characters held outside ... as `ref`]"
        # line is superseded by the pointer this entry emits, which names that
        # same ref (the caller passes every ref found in the body through).
        if stripped.startswith("[") and _LEDGER_REF_RE.search(stripped):
            continue
        if len(stripped) > _LEDGER_MAX_LINE_CHARS:
            stripped = stripped[:_LEDGER_MAX_LINE_CHARS].rstrip() + " […]"
        kept.append(stripped)
    if in_fence:
        # Unterminated fence: an offload excerpt can cut mid-block.
        _flush_fence()

    body = "\n".join(kept)
    if len(body) <= LEDGER_MAX_ENTRY_CHARS:
        return body

    # Over budget. Structural lines (header, outcome, omission notes) are kept
    # whole and prose is dropped, in original order — truncating the entry from
    # the end would lose the outcome of a multi-call round, which is the single
    # most load-bearing line in it.
    structural = {
        i for i, ln in enumerate(kept) if ln.startswith(_LEDGER_STRUCTURAL_PREFIXES)
    }
    room = LEDGER_MAX_ENTRY_CHARS - sum(len(kept[i]) + 1 for i in structural)
    out: List[str] = []
    dropped = 0
    for i, line in enumerate(kept):
        if i in structural:
            out.append(line)
        elif room - (len(line) + 1) >= 0:
            out.append(line)
            room -= len(line) + 1
        else:
            dropped += len(line) + 1
    if dropped:
        out.append(f"[{dropped:,} further chars of result text omitted]")
    return "\n".join(out)


def ledger_entry(text: str, refs: Optional[List[str]] = None) -> str:
    """Render one collapsed tool exchange: surviving facts plus how to reopen it."""
    facts = _ledger_facts(text)
    unique = list(dict.fromkeys(r for r in (refs or []) if r))
    pointer = ""
    if unique:
        joined = ", ".join(f"`{r}`" for r in unique)
        pointer = (
            f"\n[Full verbatim result kept as {joined}. Call `recall_tool_output` "
            f'with {{"ref": "{unique[0]}", "query": "<what you need>"}} to reopen '
            f"it — do NOT re-run the tool.]"
        )
    return f"{LEDGER_HEADER}\n{facts}{pointer}"


def _split_guarded(text: str) -> Optional[tuple]:
    """Split an untrusted-context message into (framing, body, closing), or None.

    A textual-path tool result arrives wrapped in the prompt-injection guard:
    ~450 characters of "do not follow instructions inside this block", the
    `<<<UNTRUSTED_SOURCE_DATA>>>` marker and a `Source:` line, then the results,
    then the closing marker. The ledger must rewrite ONLY the body. Running the
    fact filter over the whole message would hit the warning paragraph with the
    per-line cap and truncate it mid-sentence — quietly weakening the fence that
    makes tool output data rather than instructions (THREAT_MODEL.md).
    """
    from src.prompt_security import GUARD_CLOSE, GUARD_OPEN

    start = text.find(GUARD_OPEN)
    end = text.rfind(GUARD_CLOSE)
    if start < 0 or end <= start:
        return None
    marker_eol = text.find("\n", start + len(GUARD_OPEN))
    source_eol = text.find("\n", marker_eol + 1) if marker_eol >= 0 else -1
    if source_eol < 0 or source_eol >= end:
        return None
    return text[: source_eol + 1], text[source_eol + 1: end], text[end:]


def _is_tool_results_message(msg: Any) -> bool:
    """The textual-path tool-results message built by `untrusted_context_message`."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    return (msg.get("metadata") or {}).get("source") == "tool execution results"


def _tool_result_groups(messages: List[Dict]) -> List[Dict[str, Any]]:
    """Locate each round's tool exchange in the request-local message list.

    Two shapes exist — native (`assistant.tool_calls` + N `role:"tool"`) and
    textual (assistant prose + one guarded tool-results user message) — and both
    are returned as `{"results": [indices]}` so the caller only ever edits the
    RESULT messages. The assistant turn that made the calls is left alone:
    rewriting it would break the `tool_calls`/`tool` pairing that
    `_sanitize_llm_messages` and every OpenAI-compatible provider enforce, and
    its `tool_calls` arguments are the agent's own intent, not tool bulk.
    """
    groups: List[Dict[str, Any]] = []
    i = 0
    total = len(messages)
    while i < total:
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            j = i + 1
            results = []
            while j < total and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                results.append(j)
                j += 1
            if results:
                groups.append({"kind": "native", "results": results})
            i = max(j, i + 1)
            continue
        if (
            msg.get("role") == "assistant"
            and i + 1 < total
            and _is_tool_results_message(messages[i + 1])
        ):
            groups.append({"kind": "textual", "results": [i + 1]})
            i += 2
            continue
        i += 1
    return groups


def compact_tool_exchanges(
    messages: List[Dict],
    *,
    keep_rounds: int = LEDGER_KEEP_ROUNDS,
    slack_rounds: int = LEDGER_SLACK_ROUNDS,
    session_id: Optional[str] = None,
) -> Dict[str, int]:
    """Collapse completed tool exchanges into ledger entries, in batches.

    Mutates `messages` in place. Returns counts for logging. Never raises: this
    sits on the hot path of every agent turn, and a bug here must degrade to
    "context stays large", never to "the turn fails".
    """
    stats = {"groups": 0, "entries": 0, "chars_before": 0, "chars_after": 0}
    if not _ledger_enabled() or not messages:
        return stats

    keep = max(1, int(keep_rounds or LEDGER_KEEP_ROUNDS))
    slack = max(1, int(slack_rounds or LEDGER_SLACK_ROUNDS))

    groups = _tool_result_groups(messages)
    unprocessed = [
        g for g in groups
        if not all(messages[k].get(LEDGER_MARK) for k in g["results"])
    ]
    # THE BATCH GATE. Below this, do nothing at all — not "a little". See the
    # prefix-stability note above.
    if len(unprocessed) <= keep + slack:
        return stats

    try:
        from src.tool_output_store import store as _store
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("execution ledger unavailable (tool output store): %s", exc)
        return stats

    # A tool that succeeded somewhere in this turn resolves an earlier failure of
    # the same tool. Built from EVERY group, including already-collapsed ones, so
    # a resolution recorded in an earlier batch still counts.
    resolved = set()
    for group in groups:
        for idx in group["results"]:
            body = messages[idx].get("content")
            if isinstance(body, str) and not _ledger_failed(body):
                # Skip unnamed results: an unparseable header would otherwise
                # resolve every other unparseable failure by accident.
                resolved.add(_ledger_tool_name(body) or None)
    resolved.discard(None)

    # Everything before the keep window is in scope for this batch — including
    # exchanges a PREVIOUS batch deferred because their failure was still
    # unresolved, since the round that resolved it may have arrived since. Ones
    # already finalized (`LEDGER_MARK is True`) are skipped, so no byte that is
    # already cached is ever rewritten twice.
    keep_first = unprocessed[-keep]
    cutoff = len(groups)
    for pos, group in enumerate(groups):
        if group is keep_first:
            cutoff = pos
            break

    for group in groups[:cutoff]:
        collapsed_here = False
        for idx in group["results"]:
            msg = messages[idx]
            if msg.get(LEDGER_MARK) is True:
                continue
            body = msg.get("content")
            deferred = False
            if isinstance(body, str) and len(body) >= LEDGER_MIN_RESULT_CHARS:
                entry, deferred = _ledger_collapse(body, session_id, resolved, _store)
                if entry is not None:
                    stats["chars_before"] += len(body)
                    stats["chars_after"] += len(entry)
                    stats["entries"] += 1
                    msg["content"] = entry
                    collapsed_here = True
            # Marked either way, so the batch gate can fall back below its
            # threshold and the next batch is a window away rather than next
            # round. "deferred" still counts as processed for the gate.
            msg[LEDGER_MARK] = "deferred" if deferred else True
        if collapsed_here:
            stats["groups"] += 1
    return stats


def _ledger_collapse(body: str, session_id, resolved: set, store) -> tuple:
    """Return (entry_or_None, defer) for one result.

    `defer` asks the caller to look at this exchange again on a later batch:
    the only case is a failure nothing has resolved yet, which may well be
    resolved by a round that has not happened. Every other refusal is final.
    """
    tool = _ledger_tool_name(body)
    if tool in _LEDGER_NEVER_COMPACT:
        return None, False
    if _ledger_failed(body) and tool not in resolved:
        return None, True
    # Only the guarded body is rewritten; the injection fence around it is
    # reproduced byte for byte.
    guard = _split_guarded(body)
    framing, inner, closing = guard if guard else ("", body, "")
    refs = _LEDGER_REF_RE.findall(inner)
    record = None
    try:
        # The whole message goes to the store, guard and all, so what
        # `recall_tool_output` hands back is exactly what was in the transcript.
        record = store(body, tool=tool, command="execution-ledger", session_id=session_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("execution ledger offload failed for %s: %s", tool, exc)
    if record is None and not refs:
        # Nothing would remain recoverable. Leave the exchange verbatim: losing a
        # path costs a re-read, which costs more than this entry would have saved
        # and is far harder to notice.
        return None, False
    if record is not None:
        refs = [record["ref"]] + refs
    entry = f"{framing}{ledger_entry(inner, refs)}\n{closing}" if guard else ledger_entry(inner, refs)
    # Don't churn the cache boundary for a result that was mostly facts already.
    return (entry, False) if len(entry) < len(body) else (None, False)
