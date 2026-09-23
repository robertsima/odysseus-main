"""Select conversation evidence without treating worker/runtime data as a user."""

from src.prompt_security import GUARD_OPEN, UNTRUSTED_CONTEXT_HEADER

_SYNTHETIC_PREFIXES = (
    "[Context —", "[Tool execution results]", "[Harness directive —",
    "[Message from agent session ",
    "[Mid-task instruction from the user] [Message from agent session ",
    "[Worker ",
)

# Persisted messages created by workers/agent-to-agent delivery. These are
# useful transcript context, but never statements made by the human owner.
# Keep `steer` out: chat_routes uses it for genuine mid-task user input.
_NONHUMAN_SOURCES = frozenset({
    "worker", "worker_followup", "peer", "agent", "agent_message",
})


def conversation_for_extraction(messages, *, limit: int) -> list:
    """Filter before windowing, strip media and bound each message's text.

    Raw tool output, retrieved memory and peer messages are not statements the
    human made. In particular, do not learn a worker's 'I prefer …' as its
    owner's preference. Keep assistant prose for context, not as fact authority.
    """
    result = []
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        metadata = message.get("metadata") or {}
        if isinstance(metadata, dict):
            source = str(metadata.get("source") or "").strip().casefold()
            if (metadata.get("trusted") is False or metadata.get("kind") == "peer"
                    or source in _NONHUMAN_SOURCES):
                continue
        content = message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(str(b.get("text") or "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(content, str):
            continue
        content = content.strip()
        if not content or content.startswith(_SYNTHETIC_PREFIXES) or GUARD_OPEN in content or content.startswith(UNTRUSTED_CONTEXT_HEADER):
            continue
        result.append({"role": message["role"], "content": content[:4000]})
    return result[-limit:]
