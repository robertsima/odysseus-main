"""Resolve a skill's ``requires_toolsets`` entries to real tool names.

Moved out of the fork's agent loop when the loop was replaced by upstream's
(2026-09-18). `src.tools.system` uses it to warn a skill's author, at write
time, about entries that name nothing; the re-ported skill routing will use
it to load the tools a skill declares.
"""

from __future__ import annotations

import logging
from typing import Dict, Set, Tuple

logger = logging.getLogger(__name__)


def _mcp_toolset_index(mcp_mgr) -> Dict[str, Set[str]]:
    """Map MCP server id AND display name to that server's qualified tools.

    Skills name the server, because that is what the operator configured and
    what the MCP docs call it: ``requires_toolsets: [bsky-mcp, firecrawl]``.
    Matching only exact tool names turned both of those into "not tool names,
    ignored" in the 2026-09-17 logs — while the same run demoted those very
    servers' schemas for budget, so the declared dependency was the only thing
    that would have brought them back.
    """
    index: Dict[str, Set[str]] = {}
    if mcp_mgr is None:
        return index
    try:
        catalog = mcp_mgr.get_all_tools() or []
    except Exception:
        logger.debug("MCP catalogue unavailable for skill toolset resolution", exc_info=True)
        return index
    for tool in catalog:
        qualified = str(tool.get("qualified_name") or "")
        if not qualified:
            continue
        for key in (tool.get("server_id"), tool.get("server_name")):
            if key:
                index.setdefault(str(key).strip().casefold(), set()).add(qualified)
    return index


def skill_declared_tools(skills, disabled_tools, mcp_mgr=None) -> Tuple[Set[str], Set[str]]:
    """Split a skill's ``requires_toolsets`` into real tools and prose.

    The field is operator-authored free text in SKILL.md. A skill declaring
    prose ("email", "file search and edit", "todoist") used to put those
    strings straight into the selected tool set, where they can never resolve
    to a schema — the 2026-09-10 logs show nine of them in
    `selected_without_schema` on every round. The system prompt is built from
    the same set, so the model was also told it had tools that do not exist.

    An entry resolves, in order, as: an exact tool name, a connected MCP server
    (every read tool it exposes), or one of the prose aliases below.

    Returns ``(tools, unknown)``. ``unknown`` holds ONLY entries that name
    nothing real, which is the operator's cue to fix the front matter. An entry
    that does resolve but whose tools are all switched off for this turn is not
    unknown — policy is working as configured, and reporting it as bad metadata
    sent the operator to edit a skill that was already correct.
    """
    try:
        from src.tool_policy import known_tool_names

        known = known_tool_names()
    except Exception:
        known = set()
    mcp_index = _mcp_toolset_index(mcp_mgr)
    tools: Set[str] = set()
    unknown: Set[str] = set()
    disabled = disabled_tools or set()
    for skill in skills or []:
        for name in (skill.get("requires_toolsets") or []):
            if not name or name in disabled:
                continue
            if not known or name in known:
                tools.add(name)
                continue
            key = str(name).strip().casefold()
            # Agent-authored skills describe toolsets in prose or by MCP server
            # name; resolve those instead of dropping the dependency.
            candidates = set(mcp_index.get(key) or ()) | {
                tool for tool in (_SKILL_TOOLSET_ALIASES.get(key) or ()) if tool in known
            }
            if not candidates:
                unknown.add(name)
                continue
            usable = candidates - disabled
            if usable:
                tools |= usable
            else:
                logger.debug(
                    "skill toolset %r resolved to %s, all disabled for this turn",
                    name, sorted(candidates),
                )
    return tools, unknown


_FILE_READ_TOOLS = ("read_file", "grep", "glob", "ls")


_FILE_EDIT_TOOLS = ("edit_file", "write_file", "apply_patch")


_SKILL_TOOLSET_ALIASES: Dict[str, Tuple[str, ...]] = {
    "email": ("list_email_accounts", "list_emails", "read_email"),
    "calendar": ("manage_calendar",),
    "notes": ("manage_notes",),
    "todoist": ("mcp__todoist__todoist",),
    "memory": ("manage_memory",),
    "memory management": ("manage_memory",),
    "skills": ("manage_skills",),
    "skill management": ("manage_skills",),
    "git": ("bash",),
    "shell": ("bash",),
    "file editing": _FILE_READ_TOOLS + _FILE_EDIT_TOOLS,
    "file search and edit": _FILE_READ_TOOLS + _FILE_EDIT_TOOLS,
    "workspace file tools": ("get_workspace",) + _FILE_READ_TOOLS + _FILE_EDIT_TOOLS,
    "application-log access": ("read_app_logs",),
    "logs": ("read_app_logs",),
    "web search or retrieval": ("web_search", "web_fetch", "search_documents"),
    "web research": ("web_search", "web_fetch"),
    "internal context search": ("search_documents",),
    "search_documents when internal context is relevant": ("search_documents",),
    # Refusal prose says "capability" where a skill normally says "toolset".
    # Keep the same vocabulary available to targeted self-unblock recovery.
    "delegation": ("orchestrate_agents", "delegate_to_agent", "delegate_to_claude_code", "manage_agent_loadout"),
    "agent delegation": ("orchestrate_agents", "delegate_to_agent", "delegate_to_claude_code", "manage_agent_loadout"),
    "agent launcher": ("orchestrate_agents", "manage_agent_loadout"),
    "agent orchestration": ("orchestrate_agents", "manage_agent_loadout"),
}
