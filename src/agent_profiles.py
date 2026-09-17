"""Named sub-agent profiles.

A profile is a reusable worker loadout the agent can delegate to through
``send_to_session`` (``profile`` argument): instructions, primary/fallback
models, tool/skill/MCP allowlists, memory and vault access, approvals,
delegation policy, worker concurrency and a round budget. Profiles live in the
``agent_profiles`` setting and are edited in Settings › Workbench.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MAX_PROFILES = 40
# A round budget is a safety stop, not a work allowance, and counting rounds was
# never what kept a worker safe. The 2026-09-17 logs cut workers off after 12 and
# 29 tool calls with the task unstarted, which is the only thing the counter
# reliably achieved. So: UNLIMITED BY DEFAULT (`max_rounds = 0`). What actually
# bounds a run is unchanged and is not a counter -- the per-run tool-call ceiling
# (`agent_max_tool_calls`, default 500), the request timeout, the worker's own
# tool policy, and the user's stop control. An explicit positive budget is still
# honoured for anyone who wants one, up to MAX_ROUNDS_CAP.
MAX_ROUNDS_CAP = 200
UNLIMITED_ROUNDS = 0
DEFAULT_ROUNDS = UNLIMITED_ROUNDS
MAX_INSTRUCTIONS = 8000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
_MEMORY_ACCESS = {"none", "read", "write"}
_SELECTION_ACCESS = {"all", "selected", "none"}
_MODEL_ACCESS = {"current", "selected", "all"}
_DELEGATION = {"never", "explicit", "auto"}


def _names(raw: Any, field: str, profile: str, limit: int = 300) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [item for item in re.split(r"[\n,]+", raw) if item.strip()]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"profile {profile!r}: {field} must be a list of names")
    return sorted({item.strip() for item in raw if item.strip()})[:limit]


def _choice(raw: Any, field: str, profile: str, allowed: set, default: str) -> str:
    value = str(raw or default).strip().lower()
    if value not in allowed:
        raise ValueError(f"profile {profile!r}: {field} must be one of {', '.join(sorted(allowed))}")
    return value


def validate_profiles(value: Any) -> List[Dict[str, Any]]:
    """Normalise a list of profiles, raising ValueError on bad input."""
    if not isinstance(value, list):
        raise ValueError("profiles must be a list")
    if len(value) > MAX_PROFILES:
        raise ValueError(f"at most {MAX_PROFILES} profiles")
    seen = set()
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"profile {i + 1} must be an object")
        name = str(raw.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"profile {i + 1}: name must be 1-40 letters, digits, spaces, dots, dashes or underscores")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"duplicate profile name {name!r}")
        seen.add(key)
        tools = _names(raw.get("disabled_tools"), "disabled_tools", name, 200)
        try:
            # 0 (and a missing value) mean "no round ceiling"; anything positive
            # is an explicit budget the author chose.
            rounds = int(raw.get("max_rounds") or DEFAULT_ROUNDS)
        except (TypeError, ValueError):
            raise ValueError(f"profile {name!r}: max_rounds must be a number")
        try:
            raw_workers = raw.get("max_parallel_workers")
            workers = int(1 if raw_workers in (None, "") else raw_workers)
        except (TypeError, ValueError):
            raise ValueError(f"profile {name!r}: max_parallel_workers must be a number")
        out.append({
            "name": name,
            "description": str(raw.get("description") or "").strip()[:300],
            # ``personality`` is an API-friendly alias; the existing editor and
            # runtime use ``instructions`` as the canonical persisted field.
            "instructions": str(raw.get("instructions") or raw.get("personality") or "").strip()[:MAX_INSTRUCTIONS],
            "model": str(raw.get("model") or "").strip()[:300],
            "model_fallbacks": _names(raw.get("model_fallbacks"), "model_fallbacks", name, 12),
            "model_access": _choice(raw.get("model_access"), "model_access", name, _MODEL_ACCESS, "current"),
            "allowed_models": _names(raw.get("allowed_models"), "allowed_models", name, 100),
            "disabled_tools": tools,
            "tool_access": _choice(raw.get("tool_access"), "tool_access", name, _SELECTION_ACCESS, "all"),
            "enabled_tools": _names(raw.get("enabled_tools"), "enabled_tools", name, 300),
            "memory_access": _choice(raw.get("memory_access"), "memory_access", name, _MEMORY_ACCESS, "read"),
            "skill_access": _choice(raw.get("skill_access"), "skill_access", name, _SELECTION_ACCESS, "all"),
            "skill_names": _names(raw.get("skill_names"), "skill_names", name, 100),
            "mcp_access": _choice(raw.get("mcp_access"), "mcp_access", name, _SELECTION_ACCESS, "all"),
            "allowed_mcp_servers": _names(raw.get("allowed_mcp_servers"), "allowed_mcp_servers", name, 100),
            "private_vault_access": bool(raw.get("private_vault_access", False)),
            "approval_mode": str(raw.get("approval_mode") or "inherit").strip().lower(),
            "delegation_policy": _choice(raw.get("delegation_policy"), "delegation_policy", name, _DELEGATION, "explicit"),
            "max_parallel_workers": max(0, min(8, workers)),
            "max_rounds": 0 if rounds <= 0 else min(MAX_ROUNDS_CAP, rounds),
        })
        if out[-1]["approval_mode"] not in {"inherit", "auto", "ask_risky", "ask_all"}:
            raise ValueError(f"profile {name!r}: invalid approval_mode")
    return out


def expand_tool_aliases(names) -> set:
    """Equivalent tool spellings share one permission, including email MCP."""
    from src.tool_security import email_tool_policy_names

    return {alias for name in names for alias in email_tool_policy_names(name)}


def session_patch(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the runtime parts of a profile on the worker chat itself."""
    patch = {
        "agent_profile": profile.get("name"),
        # Snapshot the loadout's persona onto the child.  Profiles are reusable
        # defaults and may be edited later; an existing agent must not silently
        # acquire another agent's (or a newly edited) personality.
        "agent_instructions": profile.get("instructions") or None,
        "tool_access": profile.get("tool_access", "all"),
        "enabled_tools": profile.get("enabled_tools") or [],
        "disabled_tools": profile.get("disabled_tools") or None,
        "memory_access": profile.get("memory_access", "read"),
        "skill_access": profile.get("skill_access", "all"),
        "skill_names": profile.get("skill_names") or [],
        "model_access": profile.get("model_access", "current"),
        "allowed_models": profile.get("allowed_models") or [],
        "delegation_policy": profile.get("delegation_policy", "explicit"),
        "max_parallel_workers": profile.get("max_parallel_workers", 1),
        "allowed_mcp_servers": (
            profile.get("allowed_mcp_servers") or []
            if profile.get("mcp_access") == "selected"
            else (["*"] if profile.get("mcp_access", "all") == "all" else [])
        ),
        "private_vault_access": bool(profile.get("private_vault_access", False)),
    }
    if profile.get("approval_mode") != "inherit":
        patch["approval_mode"] = profile.get("approval_mode")
    if profile.get("tool_access") in {"selected", "none"}:
        try:
            from src.tool_policy import known_tool_names
            enabled = expand_tool_aliases(profile.get("enabled_tools") or []) if profile.get("tool_access") == "selected" else set()
            patch["disabled_tools"] = sorted(
                (set(known_tool_names()) - enabled) | set(profile.get("disabled_tools") or [])
            )
        except Exception:
            pass
    return patch


def load_profiles() -> List[Dict[str, Any]]:
    try:
        from src.settings import get_setting

        return validate_profiles(get_setting("agent_profiles", []) or [])
    except Exception:
        return []


def get_profile(name: str) -> Optional[Dict[str, Any]]:
    key = str(name or "").strip().casefold()
    return next((p for p in load_profiles() if p["name"].casefold() == key), None)


def describe_for_prompt() -> str:
    """One line per profile for the agent's tool guidance ('' when none)."""
    profiles = load_profiles()
    if not profiles:
        return ""
    rows = [f"{p['name']}" + (f" — {p['description']}" if p["description"] else "") for p in profiles]
    return "Agent profiles for send_to_session (profile=...): " + "; ".join(rows) + "."
