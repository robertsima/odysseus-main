"""Approval modes: which tool calls stop and ask the user first.

Chats, agent profiles and loadouts store an ``approval_mode``; a chat with none
follows the app-wide ``agent_approval_mode`` setting:

* ``auto``      -- nothing asks.
* ``ask_risky`` -- destructive or outward-facing actions ask: destructive shell
  commands, sending or deleting email, admin changes, deletes, and git
  publication or history rewrites.
* ``ask_all``   -- every tool that can change something asks, and so does any
  high-impact call once untrusted content (a web page, an email, a file) has
  entered the run.

The mode is enforced by :class:`src.tool_capabilities.ToolRunSecurityContext`,
which turns a "should ask" answer into upstream's exact-action approval card.
A run whose caller passes no mode (the skill tester, teacher escalation) keeps
upstream's behaviour: it asks only once untrusted content has influenced it.

Sub-agents resolve the mode from their own chat, so a worker asks on its own
card (listed in the Agents panel) rather than in its parent's chat.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

MODES = ("auto", "ask_risky", "ask_all")
DEFAULT_MODE = "auto"
# The settings API lists the modes (and the chat UI shows its control) only
# while this is True.
ENFORCED = True


def normalize_mode(value: Optional[str]) -> str:
    value = str(value or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


_SHELL_TOOLS = frozenset({"bash", "python"})

# Shell commands that destroy data, rewrite history, publish, or escalate.
_RISKY_SHELL = (
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*[rf][a-zA-Z]*\s+)+"), "deletes files recursively or forcibly"),
    (re.compile(r"\bgit\s+push\b"), "pushes to a remote"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s|branch\s+-D|stash\s+(?:drop|clear))"),
     "discards git changes"),
    (re.compile(r"\bgit\s+rebase\b|\bgit\s+commit\s+[^;&|]*--amend\b"), "rewrites git history"),
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bgh\s+(?:pr\s+merge|release\s+create|repo\s+delete)\b"),
     "publishes or merges"),
    (re.compile(r"\bsudo\b|\bchmod\s+-R\b|\bchown\s+-R\b"), "changes system permissions"),
    (re.compile(r"\b(?:mkfs|fdisk|parted)\b|\bdd\s+[^;&|]*\bof=|>\s*/dev/sd"), "writes to a disk device"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"), "shuts the machine down"),
    (re.compile(r"\bdocker\s+(?:rm|rmi|system\s+prune|volume\s+(?:rm|prune))\b|\bkubectl\s+delete\b"),
     "removes containers or resources"),
    (re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:ba|z)?sh\b"), "pipes a download into a shell"),
    (re.compile(r"\bDROP\s+(?:TABLE|DATABASE)\b|\bTRUNCATE\s+TABLE\b", re.I), "drops database data"),
    (re.compile(r"\bkill\s+-9\b|\bpkill\b|\bkillall\b"), "kills processes"),
    (re.compile(r"shutil\.rmtree|os\.remove\(|os\.unlink\("), "deletes files"),
)

# Non-shell tools that act on the outside world or change the app itself.
_RISKY_TOOLS = {
    "send_email": "sends an email",
    "reply_to_email": "sends an email reply",
    "bulk_email": "acts on many emails at once",
    "delete_email": "deletes email",
    "unsubscribe_email": "unsubscribes from a mailing list",
    "manage_webhooks": "changes webhooks",
    "manage_tokens": "changes API tokens",
    "manage_settings": "changes app settings",
    "manage_endpoints": "changes model endpoints",
    "manage_mcp": "changes MCP servers",
}

_GIT_RISKY = {
    "push": "publishes commits to a remote",
    "force_push_with_lease": "rewrites a remote branch",
    "delete_remote_branch": "deletes a remote branch",
    "merge": "merges into the current branch",
    "delete_branch": "deletes a local branch",
    "stash_pop": "applies and deletes a stash",
    "stash_drop": "deletes a stash",
    "reset": "rewrites the current branch",
    "rebase": "rewrites local commits",
}


def _json_action(content: Any) -> str:
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("action") or "").strip().lower()


def risky_reason(tool_name: Any, content: Any) -> Optional[str]:
    """Why ``ask_risky`` stops this call, or None to let it run."""
    from src.tool_capabilities import ToolEffect, capabilities_for_action

    tool = str(tool_name or "")
    text = content if isinstance(content, str) else json.dumps(content or "")
    if tool in _SHELL_TOOLS:
        for pattern, why in _RISKY_SHELL:
            if pattern.search(text):
                return why
        return None
    bare = tool[len("mcp__email__"):] if tool.startswith("mcp__email__") else tool
    if bare in _RISKY_TOOLS:
        return _RISKY_TOOLS[bare]
    if tool == "manage_git":
        return _GIT_RISKY.get(_json_action(text))
    capabilities = capabilities_for_action(tool, content)
    # Unknown tools (most MCP servers) declare every effect, destructive
    # included, so the flag says nothing about them. ask_all covers them.
    if capabilities.known and ToolEffect.DESTRUCTIVE in capabilities.effects:
        return "deletes or discards data"
    return None


# Bookkeeping that never asks: these touch only the agent's own checklist or
# already put a question to the user.
_NEVER_ASK = frozenset({"todowrite", "update_plan", "ask_user"})


def change_reason(tool_name: Any, content: Any) -> Optional[str]:
    """Why ``ask_all`` stops this call, or None for a read."""
    from src.tool_capabilities import ToolEffect, capabilities_for_action

    tool = str(tool_name or "")
    if tool in _NEVER_ASK:
        return None
    reason = risky_reason(tool, content)
    if reason:
        return reason
    capabilities = capabilities_for_action(tool, content)
    if not capabilities.known:
        return "is a tool whose effects are not classified"
    effects = capabilities.effects
    if ToolEffect.EXECUTE_CODE in effects:
        return "runs code"
    if effects & {ToolEffect.WRITE_WORKSPACE, ToolEffect.WRITE_PRIVATE}:
        return "changes files or data"
    if effects & {ToolEffect.EXTERNAL_SIDE_EFFECT, ToolEffect.ADMIN_CHANGE, ToolEffect.DESTRUCTIVE}:
        return "changes something outside this chat"
    return None


def mode_reason(mode: Any, tool_name: Any, content: Any) -> Optional[str]:
    """The approval card's reason for this call under ``mode``, or None."""
    mode = normalize_mode(mode)
    if mode == "auto":
        return None
    reason = risky_reason(tool_name, content) if mode == "ask_risky" else change_reason(tool_name, content)
    if not reason:
        return None
    label = "Ask for risky actions" if mode == "ask_risky" else "Ask for every change"
    return f"Approvals are set to “{label}”, and {tool_name} {reason}."
