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

SemanticSearch = Callable[[str, int], Any]
_MCP_NAME = re.compile(r"^mcp__([^_][A-Za-z0-9_-]*)__([A-Za-z0-9_-]+)$")
_WORDS = re.compile(r"[a-z0-9]+")
_LEXICAL_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "tool", "use", "with",
})


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
        # Unknown MCP annotations fail closed in a read-only workflow.
        return False
    return name in PLAN_MODE_READONLY_TOOLS or name == "discover_tools"


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
        self._attached_schema_tokens = 0

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
        """Record schemas already attached by the runtime selection plan.

        Attached tools consume the shared count/schema ceilings but are not
        discovery results and therefore never appear in ``loaded_names`` or
        ``loaded_schema_tokens``. Replacing the snapshot lets the runtime call
        this before every discovery after later-round policy/selection changes.
        """
        self._attached = {
            str(name) for name in (names or ())
            if name and str(name) in self._catalog
        }
        self._attached_schema_tokens = sum(
            _compact_schema_cost(self._catalog[name])
            for name in self._attached - set(self._loaded)
        )

    def permitted_tools(self, settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Return fresh canonical copies of the currently permitted inventory.

        This is an authorization-aware inventory view, not activation: reading
        it does not affect ``loaded_names`` or either turn budget.
        """
        permitted = self._permitted(settings)
        return [copy.deepcopy(permitted[name]) for name in sorted(permitted)]

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
        if not query or not permitted:
            return self._result(query, [], limit)

        words = set(_WORDS.findall(query.casefold()))
        meaningful_words = words - _LEXICAL_STOPWORDS
        exact = query.casefold()
        scored = []
        for name, schema in permitted.items():
            mcp_match = _MCP_NAME.match(name)
            searchable_name = mcp_match.group(2) if mcp_match else name
            name_words = set(_WORDS.findall(searchable_name.casefold()))
            qualified_exact = name.casefold() in exact
            overlap = len(words & name_words)
            description_words = set(_WORDS.findall(_description(schema).casefold())) - _LEXICAL_STOPWORDS
            desc_overlap = len(meaningful_words & description_words)
            # Description fallback needs two independent content-token hits.
            # That permits useful paraphrases while a generic word such as
            # "search", "manage", or "data" cannot flood the result set.
            description_match = desc_overlap >= 2
            if not qualified_exact and overlap == 0 and not description_match:
                continue
            scored.append((not qualified_exact, overlap == 0, -overlap, -desc_overlap, name))

        # Exact canonical names need no embedding request (or cold-start wait).
        canonical_exact = next((name for name in permitted if name.casefold() == exact), None)
        semantic = [] if canonical_exact else await self._semantic_names(query, min(limit * 3, 24))
        semantic_rank = {name: index for index, name in enumerate(semantic) if name in permitted}
        matched_names = {canonical_exact} if canonical_exact else {row[4] for row in scored} | set(semantic_rank)
        already_attached = sorted(matched_names & (self._attached | set(self._loaded)))
        candidate_names = matched_names - set(self._loaded) - self._attached
        ranked = sorted(
            candidate_names,
            key=lambda name: (
                0 if name.casefold() in exact else 1,
                semantic_rank.get(name, 10_000),
                next((row[1:] for row in scored if row[4] == name), (0, 0, 0, name)),
                name,
            ),
        )
        capacity = max(0, self._max_loaded - len(set(self._loaded) | self._attached))
        chosen = []
        remaining_tokens = max(
            0,
            self._max_schema_tokens - self._loaded_schema_tokens - self._attached_schema_tokens,
        )
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
        )

    def _result(
        self, query: str, chosen: List[str], limit: int,
        *, already_attached: Optional[List[str]] = None,
        budget_limited: bool = False,
    ) -> Dict[str, Any]:
        already_attached = list(already_attached or ())
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
        return {
            "output": output,
            "exit_code": 0,
            "loaded_names": list(chosen),
            "already_attached_names": already_attached,
            "loaded_tools": [copy.deepcopy(self._loaded[name]) for name in chosen],
            "discovery": machine,
            "max_results": limit,
        }
