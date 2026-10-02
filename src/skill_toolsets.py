"""Resolve a skill's ``requires_toolsets`` entries to real tool names.

Moved out of the fork's agent loop when the loop was replaced by upstream's
(2026-09-18). `src.tools.system` uses it to warn a skill's author, at write
time, about entries that name nothing; the re-ported skill routing will use
it to load the tools a skill declares.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

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
    # Exact qualified MCP tool names ("mcp__penpot_studio__build_design"):
    # known_tool_names() lists native tools only, so these fell through to
    # "names nothing" (2026-09-30: the Penpot designer's skill preflight read
    # DEGRADED although every tool it named was connected and callable).
    mcp_tool_names: Set[str] = set().union(*mcp_index.values()) if mcp_index else set()
    tools: Set[str] = set()
    unknown: Set[str] = set()
    disabled = disabled_tools or set()
    for skill in skills or []:
        for name in (skill.get("requires_toolsets") or []):
            if not name or name in disabled:
                continue
            if not known or name in known or name in mcp_tool_names:
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


# ---------------------------------------------------------------------------
# What a skill needs versus what this turn can call
#
# A skill reaches the model by three paths: the agent loop's skills index, the
# chat route's skills index, and the keyword-matched "Relevant skills" block.
# Each filtered differently (2026-10-01 review, website/architecture-
# integrations-2026-10-01.md): the loop compared `requires_toolsets` with
# native tool names only, so a skill that needs a connected Penpot or Pi worker
# was hidden from it, while the chat route and the keyword block showed
# everything. One computation feeds all three.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillVisibility:
    """What this turn can call. ``None`` means unknown: nothing is hidden."""

    active_toolsets: Optional[frozenset] = None
    available_integrations: Optional[frozenset] = None


OPEN = SkillVisibility()

# Probing integrations runs requirement checks (binary lookups, env reads), and
# the three paths ask within one turn. A short lifetime keeps that to one probe
# per turn without holding a stale answer after Settings changes a connection.
_CACHE_TTL_SECONDS = 5.0
_cache: Dict[Any, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key: Any, compute: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    value = compute()
    with _cache_lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (now, value)
    return value


def _resolve_manager(mcp_mgr):
    if mcp_mgr is not None:
        return mcp_mgr
    try:
        from src.tool_utils import get_mcp_manager

        return get_mcp_manager()
    except Exception:
        return None


def _native_callable(disabled: frozenset) -> frozenset:
    from src import capabilities, capabilities_builtin  # noqa: F401 - registers
    from src.tool_policy import known_tool_names

    names = set(known_tool_names()) - set(disabled)
    # The same rule the loop applies when it builds the schema list: a tool
    # whose capability is off on this host (Claude Code without the binary)
    # is not callable, so a skill that needs it is not worth showing.
    names -= set(capabilities.unavailable_tools())
    try:
        from src.settings import get_setting

        if not get_setting("image_gen_enabled", False):
            names.discard("generate_image")
    except Exception:
        pass
    return frozenset(names)


def _mcp_callable(mcp_mgr, mcp_disabled_map, disabled: frozenset) -> Tuple[Set[str], Set[str]]:
    """Qualified tool names callable now, and the server names skills may use."""
    tools: Set[str] = set()
    servers: Set[str] = set()
    if mcp_mgr is None:
        return tools, servers
    try:
        catalog = mcp_mgr.get_all_tools(mcp_disabled_map) or []
    except TypeError:
        catalog = mcp_mgr.get_all_tools() or []
    for tool in catalog:
        qualified = str(tool.get("qualified_name") or "")
        if not qualified or tool.get("is_disabled") or qualified in disabled:
            continue
        tools.add(qualified)
        for key in (tool.get("server_id"), tool.get("server_name")):
            if key:
                servers.add(str(key))
                servers.add(str(key).strip().casefold())
    return tools, servers


def _status_signature(mcp_mgr) -> Any:
    try:
        return tuple(sorted(
            (str(k), str((v or {}).get("status"))) for k, v in mcp_mgr.get_all_statuses().items()
        ))
    except Exception:
        return None


def _available_integrations(mcp_mgr) -> frozenset:
    from src import integration_registry

    probe = integration_registry.available_ids
    key = ("integrations", id(mcp_mgr), id(probe), _status_signature(mcp_mgr))
    return _cached(key, lambda: frozenset(probe(mcp_mgr)))


def skill_visibility(
    disabled_tools: Optional[Iterable[str]] = None,
    mcp_mgr=None,
    mcp_disabled_map: Optional[Dict[str, set]] = None,
) -> SkillVisibility:
    """The tools callable this turn and the integrations that are up.

    ``active_toolsets`` holds native tools left after policy and capabilities,
    connected MCP tools by qualified name (``mcp__<server>__<tool>``), their
    server ids and names (what an operator writes in ``requires_toolsets``),
    and every prose alias whose tools are all callable. ``available_integrations``
    is ``integration_registry.available_ids()``.

    Fails open: if either set cannot be computed it is ``None``, which
    ``SkillsManager.index_for`` reads as "hide nothing", the behaviour before
    this existed.
    """
    disabled = frozenset(str(n) for n in (disabled_tools or ()))
    mgr = _resolve_manager(mcp_mgr)
    active: Optional[frozenset]
    try:
        native = _cached(("native", disabled), lambda: _native_callable(disabled))
        mcp_tools, servers = _mcp_callable(mgr, mcp_disabled_map, disabled)
        callable_tools = set(native) | mcp_tools
        names = set(callable_tools) | servers
        for alias, tools in _SKILL_TOOLSET_ALIASES.items():
            if all(t in callable_tools for t in tools):
                names.add(alias)
        active = frozenset(names)
    except Exception:
        logger.debug("active toolsets unavailable for skill filtering", exc_info=True)
        active = None
    try:
        integrations: Optional[frozenset] = _available_integrations(mgr)
    except Exception:
        logger.debug("available integrations unavailable for skill filtering", exc_info=True)
        integrations = None
    return SkillVisibility(active, integrations)


def visible_skills(skills: Iterable[Dict], visibility: SkillVisibility) -> List[Dict]:
    """``skills`` minus those this turn cannot use.

    The same rules ``SkillsManager.index_for`` applies, for a caller that
    already holds the skill dicts (the keyword match). Keep the two in step.
    """
    out = []
    active = visibility.active_toolsets
    integrations = visibility.available_integrations
    for skill in skills or ():
        need = skill.get("requires_integration")
        if integrations is not None and need and need not in integrations:
            continue
        req = skill.get("requires_toolsets") or []
        if req and active is not None and not all(t in active for t in req):
            continue
        fallback = skill.get("fallback_for_toolsets") or []
        if fallback and active and any(t in active for t in fallback):
            continue
        out.append(skill)
    return out


def skill_scope_from_settings(settings: Optional[Dict[str, Any]]) -> Optional[Set[str]]:
    """The skills a chat may use: None for all, else casefolded names.

    ``skill_access``/``skill_names`` are what a profile or loadout saves. The
    executor already refuses loading any other skill, so every prompt path
    follows the same scope instead of advertising skills the agent may not open.
    """
    access = str((settings or {}).get("skill_access") or "all")
    if access == "none":
        return set()
    if access == "selected":
        return {str(n).casefold() for n in ((settings or {}).get("skill_names") or []) if n}
    return None


def scope_skills(skills, scope: Optional[Set[str]]):
    if scope is None:
        return list(skills or [])
    return [sk for sk in (skills or []) if str(sk.get("name") or "").casefold() in scope]


def session_skill_context(session_id: Optional[str], mcp_mgr=None) -> Tuple[SkillVisibility, Optional[Set[str]]]:
    """Visibility and loadout scope for a chat that is not inside the agent loop.

    The chat route builds its skills index before the loop runs, so it applies
    the chat's saved tool policy itself. Fails open to ``(OPEN, None)``.
    """
    try:
        settings: Dict[str, Any] = {}
        if session_id:
            from core.database import get_session_settings

            settings = get_session_settings(session_id) or {}
        mgr = _resolve_manager(mcp_mgr)
        mcp_tools = mgr.get_all_tools() if mgr is not None else ()
        from src.tool_security import session_policy_disabled_tools

        disabled = session_policy_disabled_tools(settings, mcp_tools)
        return skill_visibility(disabled, mgr), skill_scope_from_settings(settings)
    except Exception:
        logger.debug("session skill context unavailable", exc_info=True)
        return OPEN, None


def integration_routing_text(available: Optional[Iterable[str]]) -> str:
    """Routing rules the available integrations ship, joined for the system prompt.

    The text is repo-shipped (an integration manifest's ``prompt``), so it may
    sit in the trusted system role. It depends only on which integrations are
    available, which keeps the cached prompt prefix stable between turns.
    ``from_server_instructions`` is a marker, not text: server instructions are
    external content and travel with the MCP tool descriptions instead.
    """
    if not available:
        return ""
    try:
        from src import integration_registry
    except Exception:
        return ""
    parts: List[str] = []
    for integration_id in sorted(available):
        try:
            integration = integration_registry.get(integration_id)
            text = str(getattr(integration, "prompt", "") or "").strip()
        except Exception:
            continue
        if text and text != "from_server_instructions":
            parts.append(text)
    return "\n\n".join(parts)
