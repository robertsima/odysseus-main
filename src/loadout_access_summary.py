"""What an agent can do, said in a few plain sentences.

A formatter, not a policy engine. The sets of tools it reads come from the
resolvers that already decide access (`agent_loadouts.caller_policy`,
`cap_to_starter`, `headless_agent.blocked_at_depth`, the owner's baseline), and
this module only turns "which tools, which shell, which approval mode" into the
sentences the Talk and Assign screens show.

Two modes, because the same agent holds different tools in each:

* ``talk``: a chat that runs under the agent (its own tools, minus the owner's
  baseline).
* ``assign``: the agent working separately for a chat. It never holds more than
  the chat that starts it, and it cannot start other agents unless nesting is
  allowed.

Each mode returns at most three facts (can / cannot / asks) for the main view;
everything else goes under ``advanced``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS, ToolEffect, capabilities_for_tool

FILE_READ = ("read_file", "grep", "ls", "glob")
FILE_WRITE = ("write_file", "edit_file", "apply_patch")
NOTE_READ = ("search_documents", "manage_notes", "manage_documents")
NOTE_WRITE = ("manage_notes", "manage_documents", "create_document", "update_document", "edit_document")
WEB = ("web_search", "web_fetch")
SHELL = ("bash", "python")
CONTACT_AGENTS = ("send_to_session", "delegate_to_agent", "orchestrate_agents", "manage_agent_loadout",
                  "message_agent", "delegate_to_claude_code")

RESULTS_RETURN = "Results return to this chat."
MAX_ADVANCED_TOOLS = 200


def _external_tools(tools: Iterable[str]) -> Set[str]:
    """Tools that act outside the app: send mail, push code, delete remotely."""
    out: Set[str] = set()
    for name in tools:
        if name not in KNOWN_CAPABILITY_TOOLS:
            continue
        effects = capabilities_for_tool(name).effects
        if ToolEffect.EXTERNAL_SIDE_EFFECT in effects:
            out.add(name)
    return out


def tool_set(profile: Dict[str, Any], allowed: Iterable[str]) -> Set[str]:
    """The tools ``profile`` grants, within ``allowed`` (what the chat or owner may use)."""
    from src.agent_profiles import expand_tool_aliases
    from src.tool_policy import allowlist_permits

    access = profile.get("tool_access") or "all"
    if access == "none":
        return set()
    enabled = list(profile.get("enabled_tools") or [])
    denied = expand_tool_aliases(profile.get("disabled_tools") or [])
    return {t for t in allowed if t not in denied and allowlist_permits(t, access, enabled)}


def _join(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _flags(tools: Set[str], profile: Dict[str, Any], *, can_nest: bool) -> Dict[str, Any]:
    shell_off = (profile.get("shell_access") or "sandbox") == "off"
    read = bool(tools & set(FILE_READ))
    write = bool(tools & set(FILE_WRITE))
    return {
        "read_files": read,
        "edit_files": write,
        "search_notes": bool(tools & set(NOTE_READ)),
        "edit_notes": bool(tools & set(NOTE_WRITE)),
        "web": bool(tools & set(WEB)),
        "commands": bool(tools & set(SHELL)) and not shell_off,
        "agents": bool(tools & set(CONTACT_AGENTS)) and can_nest
        and (profile.get("delegation_policy") or "explicit") != "never",
        "external": bool(_external_tools(tools)),
        "connected": any(t.startswith("mcp__") for t in tools),
    }


def facts_for(tools: Set[str], profile: Dict[str, Any], *, can_nest: bool = True) -> List[Dict[str, str]]:
    """At most three facts: what it can do, what it cannot, what it asks about."""
    f = _flags(tools, profile, can_nest=can_nest)
    approval = (profile.get("approval_mode") or "inherit")
    auto = approval == "auto"
    can: List[str] = []
    cannot: List[str] = []
    if f["read_files"] and f["edit_files"]:
        can.append("read and edit project files")
    elif f["read_files"]:
        can.append("read project files")
        cannot.append("change files")
    elif f["edit_files"]:
        can.append("edit project files")
    if f["search_notes"]:
        can.append("search and edit notes and documents" if f["edit_notes"] else "search indexed notes")
    if f["web"]:
        can.append("look things up on the web")
    if f["commands"]:
        can.append("run commands")
    else:
        cannot.insert(0, "run commands")
    if f["agents"]:
        can.append("contact other agents")
    else:
        cannot.insert(1 if not f["commands"] else 0, "contact other agents")
    if not (f["read_files"] or f["edit_files"] or f["search_notes"]):
        cannot.append("open files or notes")
    if f["connected"]:
        can.append("use connected apps")
    if f["external"] and auto:
        can.append("act outside the app without asking")
    if not can:
        can = ["answer from this conversation"]

    facts = [{"level": "can", "text": "Can " + _join(can[:4]) + "."}]
    if cannot:
        facts.append({"level": "cannot", "text": "Cannot " + _join(cannot[:3]).replace(" and ", " or ") + "."})
    if approval == "ask_all" and tools:
        facts.append({"level": "asks", "text": "Asks before using any tool."})
    elif f["external"] and not auto:
        facts.append({"level": "asks", "text": "Asks before external actions."})
    return facts


def _mode(tools: Set[str], profile: Dict[str, Any], *, can_nest: bool, returns: bool) -> Dict[str, Any]:
    facts = facts_for(tools, profile, can_nest=can_nest)
    summary = " ".join(f["text"] for f in facts)
    if returns:
        summary += " " + RESULTS_RETURN
    return {"summary": summary, "facts": facts}


def build_access(profile: Dict[str, Any], *, talk_allowed: Iterable[str], starter_allowed: Iterable[str],
                 worker_depth: int = 0, quick_actions: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """The ``/access`` body (without the blocker).

    ``talk_allowed``: tools the owner may use at all. ``starter_allowed``: tools
    the chat that would start the task may use; the task holds no more than
    that. ``worker_depth`` is how far below a person's chat that chat sits.
    """
    from src.headless_agent import blocked_at_depth, may_nest, withheld_reason

    talk_tools = tool_set(profile, talk_allowed)
    depth = int(worker_depth or 0) + 1
    delegation = profile.get("delegation_policy")
    blocked = blocked_at_depth(depth, delegation, tool_access=profile.get("tool_access"),
                               enabled_tools=profile.get("enabled_tools"))
    assign_tools = {t for t in tool_set(profile, starter_allowed) if t not in blocked}
    nest = may_nest(depth, delegation)

    withheld = [{"tool": t, "reason": withheld_reason(t, depth, delegation)}
                for t in sorted(tool_set(profile, starter_allowed) & blocked)]
    shell = profile.get("shell_access") or "sandbox"
    access = profile.get("tool_access") or "all"
    return {
        "name": profile.get("name") or "",
        "description": profile.get("description") or "",
        "talk": _mode(talk_tools, profile, can_nest=True, returns=False),
        "assign": {**_mode(assign_tools, profile, can_nest=nest, returns=True), "blocker": None},
        "advanced": {
            "tool_access": {"all": "All tools", "selected": "Selected tools", "none": "No tools"}.get(access, access),
            "tools": sorted(talk_tools)[:MAX_ADVANCED_TOOLS],
            "tool_count": len(talk_tools),
            "skills": ("all skills" if (profile.get("skill_access") or "all") == "all"
                       else list(profile.get("skill_names") or [])),
            "shell": {"off": "No commands", "sandbox": "Commands in a sandbox",
                      "host": "Commands on the server"}.get(shell, shell),
            "delegation": profile.get("delegation_policy") or "explicit",
            "approval": profile.get("approval_mode") or "inherit",
            "model": profile.get("model") or "",
            "memory": profile.get("memory_access") or "read",
            "private_notes": bool(profile.get("private_vault_access")),
            "withheld": withheld,
        },
        "quick_actions": list(quick_actions if quick_actions is not None else profile.get("quick_actions") or []),
    }
