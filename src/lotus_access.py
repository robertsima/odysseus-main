"""Owner-specific trust policy for model access to Lotus data and tools."""

from __future__ import annotations

from typing import Any

from src.lotus_checkins import LotusCheckinStore, owner_has_lotus_data
from src.model_context import classify_endpoint_scope

ACCESS_DEFAULTS = {
    "local": True,
    "lan": True,
    "api": False,
}

_PREFERENCE_KEYS = {
    "local": "access_local",
    "lan": "access_lan",
    "api": "access_api",
}


def get_lotus_access_policy(owner: str | None) -> dict[str, bool]:
    """Return the owner's policy without creating an empty Lotus database."""
    scoped_owner = owner or ""
    if not owner_has_lotus_data(scoped_owner):
        return dict(ACCESS_DEFAULTS)
    prefs = LotusCheckinStore(scoped_owner).get_preferences()
    return {
        scope: bool(prefs.get(key, ACCESS_DEFAULTS[scope]))
        for scope, key in _PREFERENCE_KEYS.items()
    }


def save_lotus_access_policy(owner: str | None, values: dict[str, Any]) -> dict[str, bool]:
    """Persist only recognized access fields in the owner's hashed database."""
    updates = {
        _PREFERENCE_KEYS[scope]: bool(values[scope])
        for scope in _PREFERENCE_KEYS
        if scope in values
    }
    prefs = LotusCheckinStore(owner or "").save_preferences(updates)
    return {
        scope: bool(prefs.get(key, ACCESS_DEFAULTS[scope]))
        for scope, key in _PREFERENCE_KEYS.items()
    }


def lotus_endpoint_allowed(owner: str | None, endpoint_url: str) -> bool:
    """Whether this owner allows Lotus tools on the endpoint's trust scope."""
    scope = classify_endpoint_scope(endpoint_url or "")
    return bool(get_lotus_access_policy(owner).get(scope, False))
