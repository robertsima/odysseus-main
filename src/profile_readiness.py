"""Can this loadout do its job right now? READY, or DEGRADED with each reason.

A saved profile is not proof that its integrations are callable: a Todoist
binding survives a disconnected server, a skill can declare a toolset that
resolves to nothing, and "Vault access" meant four different things. This
checks each of those against the live instance and names the repair.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src import agent_loadouts

logger = logging.getLogger(__name__)

_INDEXED_READ_TOOLS = {"search_documents", "vault_search"}
_RAW_PRIVATE_TOOLS = {"read_file", "vault_get", "grep", "glob", "ls", "bash", "python"}
_WEB_TOOLS = {"web_search", "web_fetch"}


def _check(name: str, ok: bool, detail: str, repair: Optional[str] = None) -> Dict[str, Any]:
    row = {"check": name, "ok": bool(ok), "detail": detail}
    if not ok and repair:
        row["repair"] = repair
    return row


def _skill_rows(profile: Dict[str, Any], disabled: set) -> List[Dict[str, Any]]:
    names = list(profile.get("skill_names") or []) if profile.get("skill_access") == "selected" else []
    if not names:
        return []
    try:
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        from src.skill_toolsets import skill_declared_tools
        from src.tool_utils import get_mcp_manager

        wanted = {n.casefold() for n in names}
        skills = [s for s in SkillsManager(DATA_DIR).load() if str(s.get("name") or "").casefold() in wanted]
    except Exception as exc:
        return [_check("skills", False, f"skills could not be loaded: {type(exc).__name__}")]
    rows = []
    found = {str(s.get("name") or "").casefold() for s in skills}
    for name in names:
        if name.casefold() not in found:
            rows.append(_check(f"skill {name}", False, "no skill with this name is installed",
                               "install or rename the skill, or remove it from skill_names"))
    enabled = set(profile.get("enabled_tools") or [])
    manager = get_mcp_manager()
    for skill in skills:
        tools, unknown = skill_declared_tools([skill], set(), manager)
        missing = sorted(t for t in tools if profile.get("tool_access") == "selected" and t not in enabled)
        problems = []
        if unknown:
            problems.append("declares toolsets that name nothing: " + ", ".join(sorted(unknown)))
        if missing:
            problems.append("needs tools this profile does not bind: " + ", ".join(missing))
        blocked = sorted(t for t in tools if t in disabled)
        if blocked:
            problems.append("needs tools switched off for this user: " + ", ".join(blocked))
        rows.append(_check(
            f"skill {skill['name']}", not problems,
            "; ".join(problems) or f"dependencies resolve to {len(tools)} bound tool(s)",
            "add the missing tools to enabled_tools, or fix the skill's requires_toolsets",
        ))
    return rows


def profile_readiness(profile: Dict[str, Any], policy: Dict[str, Any],
                      owner: Optional[str] = None, *, required_tools=()) -> Dict[str, Any]:
    from src.tool_security import owner_baseline_disabled_tools

    checks: List[Dict[str, Any]] = []
    matrix = agent_loadouts.capability_matrix(
        list(profile.get("enabled_tools") or []), list(required_tools or []), profile, policy, owner)
    for row in matrix["denied"]:
        checks.append(_check(f"tool {row['tool']}", False, f"{row['reason']}: {row['detail']}",
                             {"mcp_server": "reconnect the MCP server or allow it for this profile",
                              "parent_policy": "grant it to the calling chat first",
                              "owner_policy": "an admin must allow it for this user",
                              "unknown": "use an exact tool name from action=capabilities"}.get(row["reason"])))
    for row in matrix["conditional"]:
        checks.append(_check(f"tool {row['tool']}", True, row["condition"]))
    if not matrix["denied"]:
        checks.append(_check("tools", True, f"{len(matrix['selected_for_profile'])} bound tool(s) callable"
                             + (f", {len(matrix['deferred_schema'])} attached on demand"
                                if matrix["deferred_schema"] else "")))

    checks.extend(_skill_rows(profile, owner_baseline_disabled_tools(owner)))

    tools = set(matrix["selected_for_profile"])
    private = bool(profile.get("private_vault_access"))
    try:
        from src.retrieval_health import cached_problems

        index_problems = cached_problems()
    except Exception:
        index_problems = ["index health could not be checked"]
    indexed = bool(tools & _INDEXED_READ_TOOLS)
    access = {
        "indexed_retrieval_public": indexed and not any("vector store is not available" in p for p in index_problems),
        "indexed_retrieval_private": indexed and private,
        "raw_private_file_access": private and bool(tools & _RAW_PRIVATE_TOOLS),
        "index_current": not index_problems,
    }
    if indexed:
        checks.append(_check("document index", not index_problems,
                             "current" if not index_problems else "; ".join(index_problems[:4]),
                             "mount the missing sources and reindex, or treat retrieved context as possibly stale"))
    if tools & _WEB_TOOLS:
        from src.settings import get_setting

        provider = str(get_setting("search_provider", "searxng") or "searxng")
        checks.append(_check("web lookup (weather, current events)", True,
                             f"{', '.join(sorted(tools & _WEB_TOOLS))} bound; search provider {provider}"))
    else:
        checks.append(_check("web lookup (weather, current events)", True,
                             "no web tools bound; this profile cannot look up weather or news"))

    failed = [row for row in checks if not row["ok"]]
    return {
        "status": "BLOCKED" if matrix["mission_critical_missing"] else ("DEGRADED" if failed else "READY"),
        "checks": checks,
        "access": access,
        "capabilities": matrix,
    }


def render(readiness: Dict[str, Any]) -> str:
    failed = [row for row in readiness["checks"] if not row["ok"]]
    if not failed:
        return f"{readiness['status']}: every check passed."
    return f"{readiness['status']}: " + "; ".join(
        f"{row['check']} — {row['detail']}" + (f" (repair: {row['repair']})" if row.get("repair") else "")
        for row in failed[:8])
