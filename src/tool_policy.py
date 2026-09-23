"""Per-turn tool policy composition for agent execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, List, Mapping, Optional, Set, Tuple


GUIDE_ONLY_DIRECTIVE = (
    "## GUIDE-ONLY MODE - TOOL POLICY\n"
    "The latest user turn explicitly forbids tool use. Do not call tools, do not "
    "run shell commands, and do not inspect local files or the environment. "
    "Respond in normal text by guiding the user or asking them to paste the "
    "output they will produce locally."
)

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})


def tool_toggle_enabled(value: object) -> bool:
    """Return true only for explicit true-like tool toggle values."""

    return str(value).lower() == "true"


def tool_toggle_explicitly_denied(value: object) -> bool:
    """Return true when a caller explicitly supplied a non-true toggle value."""

    return value is not None and not tool_toggle_enabled(value)


def is_web_search_explicitly_denied(allow_web_search: object) -> bool:
    """Whether the web-search agent toggle was explicitly set to false."""

    return tool_toggle_explicitly_denied(allow_web_search)


def web_search_enabled_for_turn(allow_web_search: object, use_web: object = None) -> bool:
    """Return true only when this request explicitly enables web search.

    Agent mode sends ``allow_web_search``; chat-mode pre-search sends
    ``use_web``. If both are present, an explicit ``allow_web_search=false``
    wins so a stale or conflicting intent path cannot re-enable web tools.
    """

    if is_web_search_explicitly_denied(allow_web_search):
        return False
    return tool_toggle_enabled(allow_web_search) or tool_toggle_enabled(use_web)


_COMMON_TOOL_NAMES = {
    "api_call",
    "app_api",
    "archive_email",
    "ask_teacher",
    "ask_user",
    "audit_emails",
    "bash",
    "bulk_email",
    "builtin_browser",
    "cancel_download",
    "chat_with_model",
    "create_document",
    "create_session",
    "delete_email",
    "delegate_to_agent",
    "download_model",
    "edit_document",
    "edit_file",
    "edit_image",
    "generate_image",
    "glob",
    "grep",
    "list_cached_models",
    "list_cookbook_servers",
    "list_downloads",
    "list_emails",
    "list_models",
    "list_serve_presets",
    "list_served_models",
    "list_sessions",
    "ls",
    "manage_agent_loadout",
    "orchestrate_agents",
    "manage_calendar",
    "manage_contact",
    "manage_documents",
    "manage_endpoints",
    "manage_mcp",
    "manage_memory",
    "manage_notes",
    "manage_research",
    "manage_session",
    "manage_settings",
    "manage_skills",
    "manage_tasks",
    "manage_tokens",
    "manage_webhooks",
    "mark_email_read",
    "pipeline",
    "python",
    "read_email",
    "read_file",
    "reply_to_email",
    "resolve_contact",
    "search_chats",
    "search_hf_models",
    "send_email",
    "message_agent",
    "send_to_session",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "suggest_document",
    "trigger_research",
    "ui_control",
    "update_document",
    "update_plan",
    "vault_get",
    "vault_search",
    "vault_unlock",
    "web_fetch",
    "web_search",
    "write_file",
}


_GUIDE_ONLY_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bguide[-\s]?only mode\b", "guide-only mode requested"),
        (r"\bno[-\s]?tools? mode\b", "no-tools mode requested"),
        (r"\bdo not use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bdon'?t use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to:?.{0,120}\buse (?:any )?tools?\b", "user forbade tool use"),
        (r"\bask (?:me )?(?:for confirmation )?before using tools?\b", "user requested confirmation before tools"),
    )
)


@dataclass(frozen=True)
class ToolPolicy:
    """Effective tool behavior for one agent turn."""

    disabled_tools: frozenset[str] = frozenset()
    hidden_tools: frozenset[str] = frozenset()
    reasons: Mapping[str, str] = field(default_factory=dict)
    mode: str = "normal"
    block_all_tool_calls: bool = False
    disable_mcp: bool = False

    def all_disabled_names(self) -> Set[str]:
        return set(self.disabled_tools) | set(self.hidden_tools)

    def blocks(self, tool_name: Optional[str]) -> bool:
        if not tool_name:
            return False
        return self.block_all_tool_calls or tool_name in self.disabled_tools or tool_name in self.hidden_tools

    def reason_for(self, tool_name: Optional[str]) -> str:
        if tool_name and tool_name in self.reasons:
            return self.reasons[tool_name]
        if self.block_all_tool_calls and self.mode == "guide_only":
            return "Tool use is disabled for this guide-only turn."
        return "Tool use is disabled for this turn."


def detect_guide_only_turn(message: object) -> Optional[str]:
    """Return a reason when the latest user turn strongly requests no tools."""

    if not isinstance(message, str) or not message.strip():
        return None
    text = re.sub(r"\s+", " ", message.strip())
    for pattern, reason in _GUIDE_ONLY_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def known_tool_names() -> Set[str]:
    """Best-effort set of native tool names for prompt hiding and denylisting."""

    names = set(_COMMON_TOOL_NAMES)
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

        for schema in FUNCTION_TOOL_SCHEMAS:
            name = (schema.get("function") or {}).get("name") or schema.get("name")
            if name:
                names.add(name)
    except Exception:
        pass
    try:
        from src.agent_loop import TOOL_SECTIONS

        names.update(TOOL_SECTIONS.keys())
    except Exception:
        pass
    try:
        from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

        names.update(PLAN_MODE_READONLY_TOOLS)
        names.update(_PLAN_MODE_KNOWN_MUTATORS)
    except Exception:
        pass
    return names


# ---------------------------------------------------------------------------
# Tool allowlists
#
# One definition, imported (website/design-patterns.md). Three places used to
# invert a role's allowlist into a denylist by hand — `agent_profiles.
# session_patch`, `task_scheduler` (against a *different* registry) and
# `agent_loadouts._clamp_tools`. Every copy inherited the same two holes: the
# registry it subtracted from held only native names, so MCP was never covered,
# and the subtraction was a snapshot, so a tool that appeared afterwards was
# absent from the stored denylist and therefore allowed.
#
# The allowlist is now STORED as an allowlist and inverted here, against the
# tools that exist at the moment of the check. `allowlist_permits` is the
# single rule; everything else in this section is a convenience over it.
# ---------------------------------------------------------------------------

MCP_TOOL_PREFIX = "mcp__"
#: Grants every tool on every connected MCP server. The only way to say "all
#: MCP" inside an allowlist, and it has to be written out: MCP tool names are
#: generated at runtime and carry a per-server id, so they can never be
#: enumerated in advance (website/design-patterns.md, "recognise by shape").
ALL_MCP_WILDCARD = "mcp__*"
#: Suffix of a per-server grant: ``mcp__<server>__*``.
_MCP_SERVER_WILDCARD_SUFFIX = "__*"


def split_mcp_tool_name(name: object) -> Optional[Tuple[str, str]]:
    """``('server', 'tool')`` for a qualified MCP name, else ``None``.

    Uses the same ``split("__", 2)`` convention as the execution gate in
    :mod:`src.tool_execution`, so the two cannot disagree about which part of
    ``mcp__<server>__<tool>`` is the server.
    """

    parts = str(name or "").split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def is_mcp_tool_name(name: object) -> bool:
    """Whether this name is a qualified MCP tool name."""

    return split_mcp_tool_name(name) is not None


def _allowlist_entries(enabled_tools: Optional[Iterable[str]]) -> Set[str]:
    return {str(t).strip() for t in (enabled_tools or []) if str(t).strip()}


def allowlist_permits(
    tool_name: object,
    tool_access: object,
    enabled_tools: Optional[Iterable[str]],
) -> bool:
    """Whether a role whose policy is ``tool_access``/``enabled_tools`` may call ``tool_name``.

    This is a **policy** decision, so it fails closed (website/design-patterns.md):
    an access mode this build does not recognise grants nothing, and a tool
    nobody listed is denied whether or not it existed when the role was saved.

    ``tool_access`` of ``"all"`` — which is also what a chat with no stored
    allowlist resolves to — means "no allowlist"; the separate ``disabled_tools``
    denylist still applies on top and is not this function's business.

    Accepted entries in ``enabled_tools``:

    * a native tool name (``web_search``);
    * a qualified MCP tool name (``mcp__email__list_emails``), so a role can be
      granted one tool of a server without the rest of it;
    * ``mcp__<server>__*`` for a whole server, and ``mcp__*`` for every
      connected server — the explicit way to widen, since runtime-generated
      names cannot be listed.
    """

    access = str(tool_access or "all").strip().lower()
    if access == "all":
        return True
    if access != "selected":
        # "none", and anything unrecognised. The unknown case goes in the
        # restrictive bucket: wrong this way costs a refused tool call the user
        # can see and fix, wrong the other way hands an unreviewed capability
        # to a role someone deliberately narrowed.
        return False
    allowed = _allowlist_entries(enabled_tools)
    if not allowed:
        return False
    # Match every policy-equivalent spelling. A bare built-in email tool name
    # and its mcp__email__ form dispatch to the same thing, and an allowlist
    # written in one spelling must not be bypassable — or defeated — by the
    # other.
    try:
        from src.tool_security import email_tool_policy_names

        names = set(email_tool_policy_names(str(tool_name)))
    except Exception:
        names = {str(tool_name)}
    if names & allowed:
        return True
    if ALL_MCP_WILDCARD in allowed:
        if any(is_mcp_tool_name(n) for n in names):
            return True
    for name in names:
        parts = split_mcp_tool_name(name)
        if parts and f"{MCP_TOOL_PREFIX}{parts[0]}{_MCP_SERVER_WILDCARD_SUFFIX}" in allowed:
            return True
    return False


def allowlist_is_active(tool_access: object) -> bool:
    """Whether ``tool_access`` restricts anything at all."""

    return str(tool_access or "all").strip().lower() != "all"


def denied_by_allowlist(
    candidates: Iterable[str],
    *,
    tool_access: object,
    enabled_tools: Optional[Iterable[str]] = None,
    disabled_tools: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Invert an allowlist over ``candidates``, unioned with an explicit denylist.

    Callers that need a concrete denylist (a schema filter, a headless worker's
    ``disabled_tools`` argument) build it here rather than open-coding the
    subtraction, and pass the tools that exist *now* as ``candidates`` —
    :func:`live_tool_names` is the usual source and includes connected MCP.
    """

    explicit = {str(t).strip() for t in (disabled_tools or []) if str(t).strip()}
    denied = set(explicit)
    for name in candidates or ():
        text = str(name).strip()
        if text and not allowlist_permits(text, tool_access, enabled_tools):
            denied.add(text)
    # `discover_tools` is the one exception, and it is not a hole: the
    # execution gate in :mod:`src.tool_execution` admits it for an agent with a
    # non-empty `selected` allowlist, so that the agent can read back its own
    # bindings instead of guessing. The offer and the enforcement have to agree
    # (website/design-patterns.md), so the inversion must not deny what execution
    # will run -- otherwise the tool is hidden from every schema list while
    # still being callable. An explicit `disabled_tools` entry still wins.
    if (
        str(tool_access or "").strip().lower() == "selected"
        and _allowlist_entries(enabled_tools)
        and "discover_tools" not in explicit
    ):
        denied.discard("discover_tools")
    return denied


def connected_mcp_tool_names() -> Set[str]:
    """Qualified names of every tool on every connected MCP server, best effort."""

    names: Set[str] = set()
    try:
        # `src.tool_utils` owns the process singleton and imports nothing from
        # the project, so it is the safe place to read it from. This used to
        # import the name from `src.mcp_manager`, which does not define it: the
        # ImportError landed in the best-effort `except` below and every caller
        # silently got an empty set. That made `live_tool_names()` identical to
        # `known_tool_names()`, so the one inversion that was supposed to cover
        # MCP — `session_settings.stored_disabled_tools`, and through it every
        # worker and headless continuation — denied no MCP tool at all and a
        # role narrowed to three tools kept every tool of every connected
        # server. That is the exact hole this module's header says it closed,
        # failing open on a policy decision.
        from src.tool_utils import get_mcp_manager

        manager = get_mcp_manager()
        if manager is None:
            return names
        for tool in manager.get_all_tools() or ():
            qualified = str(tool.get("qualified_name") or "")
            if qualified:
                names.add(qualified)
    except Exception:
        # Best effort by design: an unreadable MCP manager must not turn into
        # an empty *universe* that reads as "nothing to deny". Every gate that
        # uses this also checks `allowlist_permits` per call at execution time,
        # which needs no universe at all.
        pass
    return names


def live_tool_names() -> Set[str]:
    """Every tool name callable right now: native plus connected MCP."""

    return known_tool_names() | connected_mcp_tool_names()


def mcp_servers_named_in_allowlist(enabled_tools: Optional[Iterable[str]]) -> Optional[Set[str]]:
    """Servers an allowlist reaches: ``None`` for "all of them" (``mcp__*``).

    An entry reaches a server through any of its policy-equivalent spellings,
    not only the qualified one. A bare ``read_email`` and ``mcp__email__read_email``
    are one permission -- :func:`allowlist_permits` says so -- so a role granted
    the bare name must keep the email server reachable, or the coarse server
    gate would refuse the very call the allowlist just permitted.
    """

    try:
        from src.tool_security import email_tool_policy_names
    except Exception:  # pragma: no cover - the alias table is best effort here
        def email_tool_policy_names(name):
            return {name}

    servers: Set[str] = set()
    for entry in _allowlist_entries(enabled_tools):
        if entry == ALL_MCP_WILDCARD:
            return None
        if entry.endswith(_MCP_SERVER_WILDCARD_SUFFIX):
            head = entry[: -len(_MCP_SERVER_WILDCARD_SUFFIX)]
            if head.startswith(MCP_TOOL_PREFIX) and head != MCP_TOOL_PREFIX:
                servers.add(head[len(MCP_TOOL_PREFIX):])
                continue
        for spelling in email_tool_policy_names(entry) or (entry,):
            parts = split_mcp_tool_name(spelling)
            if parts:
                servers.add(parts[0])
    return servers


def reconcile_tool_and_mcp_access(
    *,
    tool_access: object,
    enabled_tools: Optional[Iterable[str]] = None,
    mcp_access: object = "all",
    allowed_mcp_servers: Optional[Iterable[str]] = None,
) -> Tuple[List[str], List[str]]:
    """The effective ``(enabled_tools, allowed_mcp_servers)`` for a role.

    ``tool_access`` and ``mcp_access`` used to be fully independent knobs, and
    ``mcp_access`` defaults to ``"all"``. So ``tool_access="selected"`` with
    three named tools still produced ``allowed_mcp_servers: ["*"]`` and the
    worker kept every tool of every connected server — a user who narrows
    "tools" reasonably reads that as covering everything the agent can call.

    The rule here, and the reasoning for it:

    * **The tool allowlist is the whole answer.** ``enabled_tools`` names
      everything the role may call, MCP included, so ``allowed_mcp_servers``
      can no longer widen past it. That is the gate that fails closed, and it
      needs no agreement from ``mcp_access``.
    * **``mcp_access="all"`` grants nothing by itself**, because it is the
      *default* — it is what a role that never thought about MCP carries, and a
      default is not consent. Widening is written in the allowlist instead,
      where it is explicit and local: ``mcp__<server>__*`` for one server,
      ``mcp__*`` for all of them.
    * **``mcp_access="selected"`` with named servers IS consent**, so it is
      preserved rather than dropped: someone ticked those servers. It is
      translated into the equivalent ``mcp__<server>__*`` entries, so an
      install upgrading from the old behaviour keeps exactly the MCP reach it
      had, and the allowlist stays the single place that says what is callable.
      A role that already names MCP tools in ``enabled_tools`` is left alone —
      it was written against the new rule and means what it says, including
      "one tool of a server, not the server".
    * **The server list is then narrowed to what the allowlist can reach**, so
      the offer matches the enforcement (website/design-patterns.md). A server
      advertised in the prompt whose every tool the gate rejects is the
      phantom-tool failure that rule exists to stop.
    """

    access = str(tool_access or "all").strip().lower()
    entries = sorted(_allowlist_entries(enabled_tools))
    mcp = str(mcp_access or "all").strip().lower()
    if mcp == "selected":
        base: Optional[Set[str]] = {str(s).strip() for s in (allowed_mcp_servers or []) if str(s).strip()}
    elif mcp == "none":
        base = set()
    else:
        base = None  # "all": no server-level restriction.

    if access == "all":
        return entries, (sorted(base) if base is not None else ["*"])
    if access != "selected":
        return [], []  # "none", and anything unrecognised: nothing is callable.

    if base and not any(
        entry == ALL_MCP_WILDCARD or entry.startswith(MCP_TOOL_PREFIX) for entry in entries
    ):
        entries = sorted(set(entries) | {f"{MCP_TOOL_PREFIX}{server}{_MCP_SERVER_WILDCARD_SUFFIX}"
                                         for server in base})

    reachable = mcp_servers_named_in_allowlist(entries)
    if reachable is None:  # mcp__* — the allowlist imposes no server limit.
        return entries, (sorted(base) if base is not None else ["*"])
    if base is not None:
        reachable &= base
    return entries, sorted(reachable)


def build_effective_tool_policy(
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    last_user_message: object = "",
) -> ToolPolicy:
    """Compose the effective policy for one agent turn.

    Existing callers still provide the already-composed disabled-tool denylist.
    This function adds higher-level turn policy on top so enforcement is not
    delegated to prompt compliance.
    """

    disabled = {str(t) for t in (disabled_tools or []) if t}
    hidden: Set[str] = set()
    reasons = {tool: "Tool is disabled for this request." for tool in disabled}

    guide_reason = detect_guide_only_turn(last_user_message)
    if guide_reason:
        all_tools = known_tool_names()
        disabled.update(all_tools)
        hidden.update(all_tools)
        reasons.update({tool: f"{guide_reason}." for tool in all_tools})
        return ToolPolicy(
            disabled_tools=frozenset(disabled),
            hidden_tools=frozenset(hidden),
            reasons=MappingProxyType(dict(reasons)),
            mode="guide_only",
            block_all_tool_calls=True,
            disable_mcp=True,
        )

    return ToolPolicy(
        disabled_tools=frozenset(disabled),
        hidden_tools=frozenset(hidden),
        reasons=MappingProxyType(dict(reasons)),
    )
