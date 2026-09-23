"""Deterministic capability selection, separate from intent and authorization.

Callers propose candidates with provenance. This composer applies the permission
ceiling once, budgets the eager set, and leaves other permitted tools discoverable.
It performs no I/O and never modifies schemas, policy, or caller-owned sets.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


SOURCE_PRIORITY = {
    "core": 100,
    "profile": 95,
    "caller": 95,
    "forced": 95,
    "explicit": 90,
    "skill": 85,
    "context": 80,
    "semantic": 65,
    "domain": 55,
    "retained": 50,
}
PROTECTED_SOURCES = frozenset({"core", "profile", "caller", "forced", "explicit", "skill"})
DEFERRED_ONLY_SOURCES = frozenset({"connected"})


@dataclass(frozen=True)
class ToolSelectionPlan:
    selected: tuple[str, ...]
    deferred: tuple[str, ...]
    blocked: tuple[str, ...]
    unknown: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]]
    estimated_schema_tokens: int
    budget_exceeded_by_explicit: bool

    def trace(self) -> dict:
        """Bounded routing evidence without prompts, credentials or descriptions."""
        return {
            "version": 1,
            "selected": list(self.selected),
            "sources": {name: list(self.reasons[name]) for name in self.selected},
            "deferred_count": len(self.deferred),
            "blocked_count": len(self.blocked),
            "unknown_count": len(self.unknown),
            "estimated_schema_tokens": self.estimated_schema_tokens,
            "explicit_budget_override": self.budget_exceeded_by_explicit,
        }


def plan_tool_selection(
    available_tools: Iterable[str],
    candidates: Mapping[str, Iterable[str]],
    *,
    disabled_tools: Iterable[str] = (),
    allowed_tools: Iterable[str] | None = None,
    soft_excluded: Iterable[str] = (),
    schema_costs: Mapping[str, int] | None = None,
    max_tools: int = 24,
    max_schema_tokens: int = 4000,
) -> ToolSelectionPlan:
    """Compose one eager set; a relevance miss is never an authorization denial.

    Explicit bindings and named requests can exceed the advisory token/count
    budget. They cannot exceed permissions. Contextual pruning affects only
    suggestions; explicitly requested tools survive it and discovery can find
    the remainder. Unknown names are not promoted into invented capabilities.
    """
    inventory = {str(name) for name in available_tools if name}
    denied = set(disabled_tools)
    permitted = inventory - denied
    if allowed_tools is not None:
        permitted &= set(allowed_tools)
    soft = set(soft_excluded)
    costs = schema_costs or {}
    reasons: dict[str, set[str]] = {}
    for source, names in candidates.items():
        # Connectivity makes a capability discoverable; it is not evidence
        # that an unrelated turn should pay to attach its schema eagerly.
        if source in DEFERRED_ONLY_SOURCES:
            continue
        for name in names:
            if name:
                reasons.setdefault(str(name), set()).add(source)
    proposed = set(reasons)
    eligible = proposed & permitted
    protected = {
        name for name in eligible if reasons[name] & PROTECTED_SOURCES
    }
    ordered = sorted(
        eligible,
        key=lambda name: (
            name not in protected,
            -max(SOURCE_PRIORITY.get(source, 0) for source in reasons[name]),
            name,
        ),
    )
    chosen: set[str] = set()
    tokens = 0
    for name in ordered:
        cost = max(0, int(costs.get(name, 0)))
        if name not in protected and (
            name in soft
            or len(chosen) >= max(0, max_tools)
            or tokens + cost > max(0, max_schema_tokens)
        ):
            continue
        chosen.add(name)
        tokens += cost
    return ToolSelectionPlan(
        selected=tuple(sorted(chosen)),
        deferred=tuple(sorted(permitted - chosen)),
        blocked=tuple(sorted(proposed & (inventory - permitted))),
        unknown=tuple(sorted(proposed - inventory)),
        reasons=MappingProxyType({name: tuple(sorted(sources)) for name, sources in reasons.items()}),
        estimated_schema_tokens=tokens,
        budget_exceeded_by_explicit=(len(chosen) > max_tools or tokens > max_schema_tokens),
    )
