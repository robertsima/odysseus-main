"""Named sub-agent profiles.

A profile is a reusable worker definition the agent can delegate to through
``send_to_session`` (``profile`` argument): instructions prepended as its system
prompt, an optional model (so cheap workers can run on a smaller model than
the planner), tools it must not use, and a round budget. Profiles live in the
``agent_profiles`` setting and are edited in Settings › Workbench.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MAX_PROFILES = 40
MAX_ROUNDS_CAP = 40
DEFAULT_ROUNDS = 12
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")


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
        tools = raw.get("disabled_tools") or []
        if isinstance(tools, str):
            tools = [t for t in re.split(r"[\s,]+", tools) if t]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError(f"profile {name!r}: disabled_tools must be a list of tool names")
        try:
            rounds = int(raw.get("max_rounds") or DEFAULT_ROUNDS)
        except (TypeError, ValueError):
            raise ValueError(f"profile {name!r}: max_rounds must be a number")
        out.append({
            "name": name,
            "description": str(raw.get("description") or "").strip()[:300],
            "instructions": str(raw.get("instructions") or "").strip()[:8000],
            "model": str(raw.get("model") or "").strip()[:300],
            "disabled_tools": sorted({t.strip() for t in tools if t.strip()})[:200],
            "max_rounds": max(1, min(MAX_ROUNDS_CAP, rounds)),
        })
    return out


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
