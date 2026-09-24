"""Pure, turn-local discovery for canonical tool schemas.

Discovery narrows an already-authorized catalogue.  It never grants a
permission, refreshes an MCP server, or changes global/session state.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from src.tool_security import PLAN_MODE_READONLY_TOOLS, email_tool_policy_names
from src.private_access import tool_requires_private_grant

SemanticSearch = Callable[[str, int], Any]
_MCP_NAME = re.compile(r"^mcp__([^_][A-Za-z0-9_-]*)__([A-Za-z0-9_-]+)$")
_WORDS = re.compile(r"[a-z0-9]+")
_LEXICAL_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "tool", "use", "with",
})
_MCP_LABEL = re.compile(r"^\[MCP:([^\]]+)\]")
_BUILTIN_LABEL_PREFIX = re.compile(r"^\s*built-?in\s*:\s*", re.IGNORECASE)
# A server called "Penpot MCP Server" is named by "penpot", not by "mcp" or
# "server": those words would make every tool of every such server a hit for
# "check the mcp server". The same generic words
# `McpManager.discover_requested_tools` strips from a server name.
_GENERIC_LABEL_WORDS = frozenset({"mcp", "server", "servers", "tool", "tools", "builtin"})


def _schema_name(schema: Dict[str, Any]) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    return str((fn or {}).get("name") or schema.get("name") or "")


def _description(schema: Dict[str, Any]) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    return str((fn or {}).get("description") or schema.get("description") or "").strip()


def _metadata(schema: Dict[str, Any]) -> Dict[str, Any]:
    fn = schema.get("function") if isinstance(schema, dict) else None
    meta = schema.get("metadata") or schema.get("annotations") or {}
    if isinstance(fn, dict):
        meta = fn.get("metadata") or fn.get("annotations") or meta
    if not isinstance(meta, dict):
        meta = {
            key: getattr(meta, key, None)
            for key in ("readOnlyHint", "destructiveHint")
            if getattr(meta, key, None) is not None
        }
    return dict(meta)


def _is_readonly(name: str, schema: Dict[str, Any]) -> bool:
    meta = _metadata(schema)
    if meta.get("destructiveHint") is True:
        return False
    if "readOnlyHint" in meta:
        return meta.get("readOnlyHint") is True
    if name.startswith("mcp__"):
        from src.mcp_manager import _MIXED_MCP_TOOLS, mcp_tool_is_readonly

        # A CLI-wrapping tool reads or writes by its arguments; the executor
        # checks each call, so its schema may be offered to a reader.
        if name in _MIXED_MCP_TOOLS:
            return True
        # Otherwise the same name heuristic the executor applies to a
        # read-only worker (`mcp_call_is_readonly`) and plan mode applies
        # through `plan_mode_blocked_mcp`, on the bare tool name. The schemas
        # discovery searches carry no MCP annotations, and failing closed on
        # every unannotated name meant plan mode and read-only workers could
        # never discover a read such as Penpot's get_profile that the executor
        # would have run: the offer has to match the enforcement.
        match = _MCP_NAME.match(name)
        return bool(match) and mcp_tool_is_readonly({"name": match.group(2)})
    from src.tool_capabilities import READ_ACTION_TOOLS

    # Umbrella tools are offered to readers; the executor allows only their
    # read actions in a read-only workflow.
    return name in PLAN_MODE_READONLY_TOOLS or name == "discover_tools" or name in READ_ACTION_TOOLS


def _has_required_params(schema: Dict[str, Any]) -> bool:
    fn = schema.get("function") if isinstance(schema, dict) else None
    params = (fn if isinstance(fn, dict) else schema).get("parameters")
    return bool(isinstance(params, dict) and params.get("required"))


def _server_label_phrase(schema: Dict[str, Any]) -> str:
    """The server display name on an MCP schema, as lowercase words.

    ``McpManager.get_all_openai_schemas`` carries the label only as the
    description prefix ``[MCP:<label>] ...``: the qualified name holds the
    server id (``c5ec6d7a``), not "Penpot". A trailing ``(identity)`` is the
    connected account, not the server's name, so it is left out, as are a
    "Built-in:" prefix and generic words at either end.

    It is matched as a whole phrase, the rule `discover_requested_tools`
    applies to a server mention, not word by word: the builtin labels are
    "Built-in: GitHub Read" / "GitHub Write", and counting each word would
    make "built", "read" and "write" a hit for every tool of those servers,
    so "read the document" would load get_me and actions_list and no
    document tool.
    """
    match = _MCP_LABEL.match(_description(schema))
    if not match:
        return ""
    label = re.sub(r"\s*\([^)]*\)\s*$", "", match.group(1))
    words = _WORDS.findall(_BUILTIN_LABEL_PREFIX.sub("", label).casefold())
    while words and words[0] in _GENERIC_LABEL_WORDS:
        words.pop(0)
    while words and words[-1] in _GENERIC_LABEL_WORDS:
        words.pop()
    return " ".join(words) if set(words) - _LEXICAL_STOPWORDS else ""


def _name_written_out(name: str, text: str) -> bool:
    """Whether ``text`` contains this tool's name as a whole token.

    The test for "the caller already has this name", which is what makes it
    safe to say a tool was denied. A substring test would report `ls` against
    the word "tools"; a looser lexical match would report a tool the caller
    never asked about, which is the disclosure the filtering above exists to
    prevent.
    """
    return re.search(
        r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", text, re.IGNORECASE,
    ) is not None


def _compact_schema_cost(schema: Dict[str, Any]) -> int:
    """Conservative token estimate without changing the returned schema."""
    item = copy.deepcopy(schema)
    fn = item.get("function") if isinstance(item, dict) else None
    if isinstance(fn, dict):
        description = str(fn.get("description") or "").strip()
        if description:
            fn["description"] = description.split(". ", 1)[0].strip()[:220]

        def strip_nested(value: Any) -> None:
            if isinstance(value, dict):
                value.pop("description", None)
                for child in value.values():
                    strip_nested(child)
            elif isinstance(value, list):
                for child in value:
                    strip_nested(child)

        strip_nested(fn.get("parameters"))
    encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    # Match agent_loop._estimate_tool_schema_tokens (0.3 tokens/character).
    # Account for this item's list envelope and round upward per item; summing
    # these costs is therefore never lower than estimating the final list.
    return max(1, int((len(encoded) + 2) * 0.3) + 1)


class TurnToolDiscovery:
    """A bounded catalogue owned by one agent turn.

    ``schemas`` are deep-copied on construction and again on return, so callers
    cannot mutate the canonical catalogue through discovery results.
    """

    def __init__(
        self,
        schemas: Iterable[Dict[str, Any]],
        *,
        disabled_tools: Iterable[str] = (),
        allowed_tools: Optional[Iterable[str]] = None,
        semantic_search: Optional[SemanticSearch] = None,
        max_loaded: int = 32,
        max_schema_tokens: int = 4096,
    ) -> None:
        self._catalog: Dict[str, Dict[str, Any]] = {}
        for source in schemas or ():
            if not isinstance(source, dict):
                continue
            item = copy.deepcopy(source)
            name = _schema_name(item)
            if name and name != "discover_tools":
                self._catalog[name] = item
        self._disabled = {str(name) for name in disabled_tools if name}
        self._allowed = None if allowed_tools is None else {str(name) for name in allowed_tools if name}
        self._semantic_search = semantic_search
        self._max_loaded = max(0, min(int(max_loaded), 256))
        self._max_schema_tokens = max(0, min(int(max_schema_tokens), 65536))
        self._loaded: Dict[str, Dict[str, Any]] = {}
        self._loaded_schema_tokens = 0
        self._attached: Set[str] = set()

    @property
    def loaded_names(self) -> Set[str]:
        return set(self._loaded)

    @property
    def loaded_tools(self) -> List[Dict[str, Any]]:
        return [copy.deepcopy(self._loaded[name]) for name in sorted(self._loaded)]

    @property
    def loaded_schema_tokens(self) -> int:
        return self._loaded_schema_tokens

    def set_attached(self, names: Iterable[str]) -> None:
        """Record schemas the runtime already sends this round.

        Attached tools are not discovery results: they never appear in
        ``loaded_names`` or ``loaded_schema_tokens``, are never loaded again,
        and a query that matches one reports it as already attached. They do
        not spend discovery's allowance either -- ``max_loaded`` and
        ``max_schema_tokens`` bound only what discovery itself adds this turn
        (see ``discover``). Replacing the snapshot lets the runtime call this
        before every discovery after later-round policy/selection changes.
        """
        self._attached = {
            str(name) for name in (names or ())
            if name and str(name) in self._catalog
        }

    def permitted_tools(self, settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Return fresh canonical copies of the currently permitted inventory.

        This is an authorization-aware inventory view, not activation: reading
        it does not affect ``loaded_names`` or either turn budget.
        """
        permitted = self._permitted(settings)
        return [copy.deepcopy(permitted[name]) for name in sorted(permitted)]

    def permitted_names(self, settings: Optional[Dict[str, Any]] = None) -> Set[str]:
        """Names of the currently permitted inventory, without activating any."""
        return set(self._permitted(settings))

    def _permitted(self, settings: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        settings = dict(settings or {})
        runtime_disabled = {str(v) for v in settings.get("_runtime_disabled_tools", ()) if v}
        fresh_disabled = {str(v) for v in settings.get("disabled_tools", ()) if v}
        denied = self._disabled | runtime_disabled | fresh_disabled
        mode = str(settings.get("tool_access") or "all")
        enabled = {str(v) for v in settings.get("enabled_tools", ()) if v}
        if mode == "none":
            return {}

        allowed = None if self._allowed is None else set(self._allowed)
        if mode == "selected":
            # Positive allowlist is authoritative even if inventory changed.
            allowed = enabled if allowed is None else allowed & enabled

        allowed_servers = settings.get("allowed_mcp_servers")
        server_ceiling = None
        if isinstance(allowed_servers, list) and "*" not in allowed_servers:
            server_ceiling = {str(v) for v in allowed_servers}

        model_access = str(settings.get("model_access") or "all")
        memory_access = str(settings.get("memory_access") or "write")
        skill_access = str(settings.get("skill_access") or "all")
        plan_mode = bool(settings.get("plan_mode"))
        workflow_readonly = bool(settings.get("workflow_readonly"))
        result = {}
        for name, schema in self._catalog.items():
            if tool_requires_private_grant(name) and settings.get("private_vault_access") is not True:
                continue
            aliases = email_tool_policy_names(name)
            if not aliases.isdisjoint(denied) or (allowed is not None and aliases.isdisjoint(allowed)):
                continue
            match = _MCP_NAME.match(name)
            if match and server_ceiling is not None and match.group(1) not in server_ceiling:
                continue
            if memory_access == "none" and name in {"manage_memory", "mcp__memory__manage_memory"}:
                continue
            if model_access == "current" and name in {"chat_with_model", "ask_teacher", "list_models"}:
                continue
            if name == "manage_skills" and (
                skill_access == "none"
                or (skill_access == "selected" and not settings.get("skill_names"))
            ):
                continue
            if plan_mode and not _is_readonly(name, schema):
                continue
            if workflow_readonly and name != "manage_skills" and not _is_readonly(name, schema):
                continue
            result[name] = schema
        return result

    async def _semantic_names(self, query: str, limit: int) -> List[str]:
        if self._semantic_search is None:
            return []
        try:
            if inspect.iscoroutinefunction(self._semantic_search):
                value = await asyncio.wait_for(self._semantic_search(query, limit), timeout=2.0)
            else:
                value = await asyncio.wait_for(
                    asyncio.to_thread(self._semantic_search, query, limit), timeout=2.0,
                )
                if inspect.isawaitable(value):
                    value = await asyncio.wait_for(value, timeout=2.0)
            if isinstance(value, dict):
                value = value.keys()
            return [str(v) for v in (value or ()) if v]
        except Exception:
            return []

    async def discover(
        self,
        query: str,
        max_results: int = 5,
        *,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        query = str(query or "").strip()[:500]
        limit = max(1, min(int(max_results), 8))
        permitted = self._permitted(settings)
        if not query:
            return self._result(query, [], limit)

        # Tools the caller NAMED that policy withheld. Without this, discovery
        # could only say "No permitted tools matched that discovery query",
        # which is true of the permitted inventory and says nothing about the
        # tool the caller asked for by name — so on 2026-09-23 the model
        # supplied its own cause ("tool discovery hit the schema budget and
        # returned no loadout-management tool") and reported the invention to
        # the user as fact. "Degrade honestly" (website/design-patterns.md)
        # applies to the model as an audience too: it cannot report a reason it
        # was never given, and the drop reason already existed for the operator
        # in `[tool-routing]`'s `dropped_query_matches`.
        #
        # Scoped to names written out in the query, on a token boundary. That
        # is the line that keeps this from becoming a way to enumerate what the
        # chat may not have: a name the caller typed is one it already holds,
        # while a denied tool it never asked about stays invisible, which is
        # what the semantic-filter and fresh-revocation tests pin.
        policy_denied = sorted(
            name for name in self._catalog
            if name not in permitted and _name_written_out(name, query)
        )[:limit]
        if not permitted:
            return self._result(query, [], limit, policy_denied=policy_denied)

        words = set(_WORDS.findall(query.casefold()))
        meaningful_words = words - _LEXICAL_STOPWORDS
        exact = query.casefold()
        query_phrase = " " + " ".join(_WORDS.findall(exact)) + " "
        scored = []
        for name, schema in permitted.items():
            mcp_match = _MCP_NAME.match(name)
            searchable_name = mcp_match.group(2) if mcp_match else name
            name_words = set(_WORDS.findall(searchable_name.casefold()))
            # A query that names the server ("check penpot works") finds its
            # tools. The qualified name holds only the server's id, and the
            # "[MCP:Penpot]" description prefix needs a second content word
            # before it counts, so on 2026-09-24 a query of just "penpot"
            # matched none of the server's 81 permitted tools.
            label = _server_label_phrase(schema) if mcp_match else ""
            label_hit = bool(label) and f" {label} " in query_phrase
            qualified_exact = name.casefold() in exact
            overlap = len(words & name_words)
            description_words = set(_WORDS.findall(_description(schema).casefold())) - _LEXICAL_STOPWORDS
            desc_overlap = len(meaningful_words & description_words)
            # Description fallback needs two independent content-token hits.
            # That permits useful paraphrases while a generic word such as
            # "search", "manage", or "data" cannot flood the result set.
            description_match = desc_overlap >= 2
            if not qualified_exact and overlap == 0 and not label_hit and not description_match:
                continue
            # A tool-name hit outranks a server-name hit, which outranks a
            # description-only hit: "penpot delete team" still ranks
            # delete_team first, and a server's label never lifts its tools
            # over a tool the query actually named. Among equal name hits the
            # named server's tools go first.
            #
            # A query that names a server and no tool of it ties every one of
            # its tools on the name, so there reads and tools that need no
            # arguments go first, on the same read-only decision the executor
            # enforces. On 2026-09-24 the only Penpot schemas the model had
            # were writes (delete_team, update_team) and it probed the server
            # with update_team; "is it working?" should find get_profile and
            # list_teams instead. A tool the rest of the query describes
            # still goes ahead of them: "penpot equal spacing" means
            # distribute_shapes, not five unrelated reads. That takes two
            # description words besides the label (every description carries
            # the "[MCP:Penpot]" prefix), the same two-word bar as a
            # description-only match, so "penpot health check" is not
            # has_file_libraries ("Check if a file..."). Everywhere else the
            # description decides first and reads only break its ties: ahead
            # of it, "list my github branches" would load list_branches and
            # then four unrelated no-argument list_* tools, not list_commits /
            # list_issues, whose descriptions say "GitHub".
            reads_first = (not _is_readonly(name, schema), _has_required_params(schema))
            if label_hit and not overlap:
                described = len((meaningful_words - set(label.split())) & description_words)
                order = (described < 2, *reads_first, -described)
            else:
                order = (-desc_overlap, *reads_first)
            scored.append((
                not qualified_exact, 0 if overlap else 1 if label_hit else 2, -overlap,
                not label_hit, *order, name,
            ))

        # Exact canonical names need no embedding request (or cold-start wait).
        canonical_exact = next((name for name in permitted if name.casefold() == exact), None)
        semantic = [] if canonical_exact else await self._semantic_names(query, min(limit * 3, 24))
        semantic_rank = {name: index for index, name in enumerate(semantic) if name in permitted}
        lexical_rank = {row[-1]: row[1:] for row in scored}
        matched_names = {canonical_exact} if canonical_exact else set(lexical_rank) | set(semantic_rank)

        def rank(name: str) -> tuple:
            return (
                0 if name.casefold() in exact else 1,
                semantic_rank.get(name, 10_000),
                lexical_rank.get(name, (0, 0, 0, 0, 0, 0, name)),
                name,
            )

        # Best match first here too: the list is cut to max_results, and in
        # alphabetical order a query for get_profile reported get_workspace
        # and list_models as "already available" and cut get_profile itself.
        already_attached = sorted(matched_names & (self._attached | set(self._loaded)), key=rank)
        candidate_names = matched_names - set(self._loaded) - self._attached
        ranked = sorted(candidate_names, key=rank)
        # The allowance bounds what discovery itself adds this turn, not the
        # round's whole payload. It used to be shared with the schemas the
        # runtime already sends, which only worked while the first selection
        # was capped at 24 tools / 3,000 tokens. Upstream's selection routinely
        # sends 37-48 schemas (agent tool budget 40, sticky set 48, always-bound
        # MCP up to 24), so on 2026-09-24 the shared 32/4096 ceilings were
        # spent before discovery ran: every discover_tools returned
        # budget_limited, even for the exact `mcp__c5ec6d7a__get_profile` the
        # prompt listed, and the green Penpot server stayed out of reach. The
        # shared ceilings protected no hard limit -- nothing else caps a
        # round's tools, and the missing-tool re-arm attaches without one.
        # The instance lives for one turn and ``_loaded`` accumulates, so the
        # turn as a whole still adds at most max_loaded / max_schema_tokens.
        capacity = max(0, self._max_loaded - len(self._loaded))
        chosen = []
        remaining_tokens = max(0, self._max_schema_tokens - self._loaded_schema_tokens)
        for name in ranked:
            if len(chosen) >= min(limit, capacity):
                break
            cost = _compact_schema_cost(permitted[name])
            if cost > remaining_tokens:
                continue
            chosen.append(name)
            self._loaded[name] = copy.deepcopy(permitted[name])
            self._loaded_schema_tokens += cost
            remaining_tokens -= cost
        return self._result(
            query, chosen, limit, already_attached=already_attached[:limit],
            budget_limited=bool(ranked and not chosen),
            policy_denied=policy_denied,
        )

    def _result(
        self, query: str, chosen: List[str], limit: int,
        *, already_attached: Optional[List[str]] = None,
        budget_limited: bool = False,
        policy_denied: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        already_attached = list(already_attached or ())
        policy_denied = list(policy_denied or ())
        rows = []
        for name in chosen:
            schema = self._loaded[name]
            rows.append({
                "name": name,
                "metadata": _metadata(schema),
            })
        machine = {
            "query": query, "loaded_names": chosen,
            "already_attached_names": already_attached, "tools": rows,
            "budget_limited": budget_limited,
            "policy_denied_names": policy_denied,
        }
        output = (
            "Loaded tools for this turn: " + ", ".join(chosen)
            if chosen else (
                "Matching tools are already available: " + ", ".join(already_attached)
                if already_attached else (
                    "Permitted tools matched, but loading them would exceed this turn's tool budget. "
                    "Use already attached tools or ask the user to narrow the loadout/request."
                    if budget_limited else "No permitted tools matched that discovery query."
                )
            )
        )
        if chosen:
            # The loop attaches these to the very next round of this turn, so
            # say so: a model told only "loaded" used to stop and ask the user
            # to repeat the request before it would call the tool.
            output += (". They are attached to your next call in this same turn; "
                       "call the tool now instead of asking the user to retry.")
        elif already_attached and budget_limited:
            # Both facts, not the first alone. Replaying the 2026-09-24 Penpot
            # turn, a query for get_profile/list_teams also matched attached
            # tools on the word "list", and the reply said only "Matching
            # tools are already available: list_sessions, manage_settings",
            # which hid that the tools actually asked for were withheld.
            output += (". Other matching tools were withheld by this turn's discovery budget "
                       "and are not attached; use the tools above if they fit, or ask the user "
                       "to narrow the loadout/request.")
        elif not already_attached and not budget_limited:
            # Name the kind of answer this is. "Nothing matched" and "nothing
            # fitted" are different facts and the model was inventing the
            # second one to explain the first.
            output += " This is not a schema-budget result: nothing was withheld for size."
        if policy_denied:
            output = output.rstrip()
            if not output.endswith("."):
                output += "."
            plural = len(policy_denied) > 1
            output += (
                " Dropped by this chat's tool policy, not by the schema budget: "
                + ", ".join(policy_denied)
                + f". Policy denies {'them' if plural else 'it'} for this turn, so "
                f"discovery cannot attach {'them' if plural else 'it'} and retrying "
                "will not change that. If this blocks you, say that this chat's tool "
                "policy denies the tool by name — do not report a schema budget, a "
                "missing server, or any other cause."
            )
        return {
            "output": output,
            "exit_code": 0,
            "continue_same_turn": bool(chosen or already_attached),
            "loaded_names": list(chosen),
            "already_attached_names": already_attached,
            "policy_denied_names": policy_denied,
            "loaded_tools": [copy.deepcopy(self._loaded[name]) for name in chosen],
            "discovery": machine,
            "max_results": limit,
        }
