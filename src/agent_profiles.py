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
# honoured for anyone who wants one, up to MAX_ROUNDS_CAP, as a wrap-up point
# rather than a cutoff: at that round the worker's tools are switched off and it
# is asked to write its final answer from what it has and name what is left
# (stream_agent_loop's `wrap_up_round`).
MAX_ROUNDS_CAP = 200
UNLIMITED_ROUNDS = 0
DEFAULT_ROUNDS = UNLIMITED_ROUNDS
MAX_INSTRUCTIONS = 8000
MAX_PERSONA_NAME = 60
MAX_TOKENS_CAP = 65536
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
_MEMORY_ACCESS = {"none", "read", "write"}
_SELECTION_ACCESS = {"all", "selected", "none"}
_MODEL_ACCESS = {"current", "selected", "all"}
_DELEGATION = {"never", "explicit", "auto"}
# How much a ChatGPT-subscription model thinks before each step ("" = provider
# default). Lower is markedly faster per round for skim-and-collect workers.
REASONING_EFFORTS = ("minimal", "low", "medium", "high")


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


def _optional_number(raw: Any, field: str, profile: str, lo: float, hi: float, cast):
    """A bounded number, or None (blank) meaning "use the app default"."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"profile {profile!r}: {field} must be a number")
    return max(lo, min(hi, value))


def _reasoning_effort(raw: Any, profile: str) -> str:
    value = str(raw or "").strip().lower()
    if value and value not in REASONING_EFFORTS:
        raise ValueError(f"profile {profile!r}: reasoning_effort must be one of {', '.join(REASONING_EFFORTS)} (or empty)")
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
            # 0 (and a missing value) mean "no round budget"; anything positive
            # is an explicit budget the author chose: the round at which the
            # worker is asked to wrap up and hand back what it has.
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
            # The loadout's own voice. A chat running under a loadout uses these
            # instead of the shared persona from the Prompt window, so two
            # agents can sound and sample differently. Blank = app default.
            "persona_name": str(raw.get("persona_name") or "").strip()[:MAX_PERSONA_NAME],
            "temperature": _optional_number(raw.get("temperature"), "temperature", name, 0.0, 2.0, float),
            "max_tokens": _optional_number(raw.get("max_tokens"), "max_tokens", name, 0, MAX_TOKENS_CAP, int),
            "reasoning_effort": _reasoning_effort(raw.get("reasoning_effort"), name),
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
    """Persist the runtime parts of a profile on the worker chat itself.

    An allowlist is stored **as an allowlist** — ``tool_access`` plus
    ``enabled_tools`` — and inverted where it is evaluated
    (:func:`src.tool_policy.allowlist_permits`, applied by the agent loop and
    again at execution). This used to write the inversion here instead: the
    complement of ``known_tool_names()`` at save time, persisted on the
    session. That was wrong three ways. It listed no MCP names, because that
    registry holds only native ones, so a role narrowed to three tools kept
    every tool of every connected server. It was a snapshot, so a builtin added
    by an upgrade or a server connected the next day was missing from the
    stored denylist and read back as *allowed* — policy failing open. And it
    made the stored policy unreadable: 89 denied names where the operator wrote
    three allowed ones.

    ``disabled_tools`` keeps its own meaning: extra names denied on top of
    ``tool_access``, whatever that is. It is passed through unchanged, so a
    profile saved before this change — including one whose ``disabled_tools``
    is a complement written by :mod:`src.agent_loadouts` — keeps denying
    exactly what it denied before.
    """
    from src.tool_policy import reconcile_tool_and_mcp_access

    tool_access = profile.get("tool_access", "all")
    # `tool_access` and `mcp_access` are reconciled into one allowlist here, so
    # what the chat stores is the whole answer to "what may this role call".
    enabled_tools, mcp_servers = reconcile_tool_and_mcp_access(
        tool_access=tool_access,
        enabled_tools=profile.get("enabled_tools") or [],
        mcp_access=profile.get("mcp_access", "all"),
        allowed_mcp_servers=profile.get("allowed_mcp_servers") or [],
    )
    patch = {
        "agent_profile": profile.get("name"),
        # Snapshot the loadout's persona onto the child.  Profiles are reusable
        # defaults and may be edited later; an existing agent must not silently
        # acquire another agent's (or a newly edited) personality.
        "agent_instructions": profile.get("instructions") or None,
        "agent_persona_name": profile.get("persona_name") or None,
        "agent_temperature": profile.get("temperature"),
        "agent_max_tokens": profile.get("max_tokens"),
        "agent_reasoning_effort": profile.get("reasoning_effort") or None,
        "disabled_tools": profile.get("disabled_tools") or None,
        "tool_access": tool_access,
        "enabled_tools": enabled_tools,
        "memory_access": profile.get("memory_access", "read"),
        "skill_access": profile.get("skill_access", "all"),
        "skill_names": profile.get("skill_names") or [],
        "model_access": profile.get("model_access", "current"),
        "allowed_models": profile.get("allowed_models") or [],
        "delegation_policy": profile.get("delegation_policy", "explicit"),
        "max_parallel_workers": profile.get("max_parallel_workers", 1),
        "allowed_mcp_servers": mcp_servers,
        "private_vault_access": bool(profile.get("private_vault_access", False)),
    }
    if profile.get("approval_mode") != "inherit":
        patch["approval_mode"] = profile.get("approval_mode")
    return patch


# What a loadout edit may carry into chats already running under it: policy,
# not voice. `session_patch` snapshots the persona on purpose (an existing
# agent must not silently acquire a newly edited personality), so the voice
# keys stay whatever the chat was given.
_VOICE_KEYS = frozenset({"agent_profile", "agent_instructions", "agent_persona_name",
                         "agent_temperature", "agent_max_tokens", "agent_reasoning_effort"})


def _same_setting(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return a == b


def propagate_profile_edits(old_profiles: Any, new_profiles: List[Dict[str, Any]]) -> Dict[str, int]:
    """Carry a loadout's policy edits into the user chats that run under it.

    A chat switched to a loadout gets a *copy* of its policy (`session_patch`),
    so editing the loadout in Settings used to change nothing for those chats —
    only the Agents Control Room, which edits the chat's own copy, took effect.
    That read as "the settings page does not work" (2026-09-24: delegation set
    to "Agent decides" in Settings, chat still refused to launch agents).

    Three-way merge, per key: a chat's value is replaced only where it still
    equals what the OLD loadout gave it, so a setting someone changed on that
    one chat in the Control Room is left alone. Worker chats (those with a
    `parent_session`) are skipped: they were launched for one task under the
    loadout as it was, and changing their tools mid-task is not an edit anyone
    asked for. Returns ``{profile name: chats updated}``.
    """
    try:
        old_by_name = {p["name"].casefold(): p for p in validate_profiles(old_profiles or [])}
    except ValueError:
        return {}
    changed = {}
    for new in new_profiles or []:
        old = old_by_name.get(new["name"].casefold())
        if old is None:
            continue
        before, after = session_patch(old), session_patch(new)
        keys = {k for k in set(before) | set(after) if k not in _VOICE_KEYS
                and not _same_setting(before.get(k), after.get(k))}
        if keys:
            changed[new["name"]] = (before, after, keys)
    if not changed:
        return {}

    import json
    from core.database import Session, get_db_session, update_session_settings

    updated: Dict[str, int] = {}
    with get_db_session() as db:
        rows = db.query(Session.id, Session.settings_json).filter(Session.settings_json.isnot(None)).all()
    for session_id, raw in rows:
        try:
            settings = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(settings, dict) or settings.get("parent_session"):
            continue
        name = str(settings.get("agent_profile") or "")
        match = next((v for k, v in changed.items() if k.casefold() == name.casefold()), None) if name else None
        if match is None:
            continue
        before, after, keys = match
        patch = {k: after.get(k) for k in keys if _same_setting(settings.get(k), before.get(k))}
        if patch and update_session_settings(session_id, patch) is not None:
            updated[name] = updated.get(name, 0) + 1
    return updated


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
