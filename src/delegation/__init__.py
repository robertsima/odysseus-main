"""Which code-delegation provider serves this host, and why.

The registry exists so the ``delegation_provider`` setting has exactly one
meaning, in one place:

* ``auto`` — the first registered provider that reports itself usable. An
  operator who installs a CLI or links a subscription gets delegation without
  reading a settings page.
* an explicit id (``claude_code_cli``, ``claude_subscription``, ``mcp``) — that
  provider or nothing. An explicit choice is not silently overridden by a
  different vendor, even when the chosen one is unusable; the operator gets a
  reason instead.
* ``none`` — delegation is off. The ``code_delegation`` capability then keeps
  ``delegate_to_agent``/``delegate_to_claude_code`` out of the model's schema,
  which is the whole point: a tool the host cannot run is never advertised.

Providers other than the local CLI live in their own modules and register
themselves on import. They are loaded defensively because a build may not ship
them — a missing optional provider is "not installed here", never an ImportError
during tool-schema building.
"""

from __future__ import annotations

import importlib
import logging
from typing import Dict, List, Optional, Tuple

from src.delegation.base import DelegationProvider, DelegationResult

logger = logging.getLogger(__name__)

__all__ = [
    "DelegationProvider",
    "DelegationResult",
    "register",
    "unregister",
    "providers",
    "get",
    "select",
    "selection",
    "availability",
]

# Insertion-ordered: ``auto`` walks this, so registration order is the
# preference order. The local CLI is first because it needs no network.
_REGISTRY: Dict[str, DelegationProvider] = {}

# Optional provider modules, tried once. Each is expected to call ``register``.
# Named rather than discovered so the order stays deterministic.
_OPTIONAL_MODULES: Tuple[str, ...] = (
    "src.delegation.claude_subscription",
    "src.delegation.mcp",
)
_optional_loaded = False

# Values of ``delegation_provider`` that are not provider ids.
AUTO = "auto"
NONE = "none"


def register(provider: DelegationProvider) -> DelegationProvider:
    """Add a provider. Re-registering an id replaces it, so import order in
    tests cannot depend on who got there first."""
    if not provider.id:
        raise ValueError("a delegation provider needs an id")
    _REGISTRY[provider.id] = provider
    return provider


def unregister(provider_id: str) -> None:
    """Remove a provider — for a module that discovers at import time that it
    cannot work here, and for tests that install fakes."""
    _REGISTRY.pop(provider_id, None)


def _load_optional() -> None:
    global _optional_loaded
    if _optional_loaded:
        return
    _optional_loaded = True
    for name in _OPTIONAL_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            # Not shipped in this build. Expected, not a problem.
            logger.debug("delegation: optional provider %s is not installed", name)
        except Exception:
            # Present but broken: loud, because the operator configured it and
            # would otherwise see only "no provider available".
            logger.warning("delegation: provider %s failed to load", name, exc_info=True)


def providers() -> Tuple[DelegationProvider, ...]:
    """Every registered provider, in preference order."""
    _load_optional()
    return tuple(_REGISTRY.values())


def get(provider_id: str) -> Optional[DelegationProvider]:
    """The provider with this id, or None when it is not installed here."""
    _load_optional()
    return _REGISTRY.get(str(provider_id or "").strip())


def _probe(provider: DelegationProvider) -> Tuple[bool, str]:
    """``is_available`` with the contract enforced: a provider that raises is
    unusable, not fatal to the caller building a tool schema."""
    try:
        ok, detail = provider.is_available()
    except Exception as exc:
        logger.debug("delegation: provider %s probe raised", provider.id, exc_info=True)
        return False, f"probe failed: {type(exc).__name__}: {exc}"
    return bool(ok), str(detail or "")


def availability() -> List[Tuple[str, bool, str]]:
    """``(id, ok, detail)`` per provider — what the admin UI lists."""
    return [(p.id, *_probe(p)) for p in providers()]


def _preference() -> str:
    try:
        from src.settings import get_setting

        return str(get_setting("delegation_provider", AUTO) or AUTO).strip().lower()
    except Exception:
        # Settings unreadable (fresh container, test import): auto is the
        # behaviour that needs no configuration.
        logger.debug("delegation: could not read delegation_provider", exc_info=True)
        return AUTO


def selection(preference: Optional[str] = None) -> Tuple[Optional[DelegationProvider], str]:
    """Resolve the setting to ``(provider, reason)``.

    ``reason`` is always populated, including on success, because the capability
    hint and the admin UI both need to say *why* delegation is on or off.
    """
    pref = str(preference).strip().lower() if preference is not None else _preference()
    if pref == NONE:
        return None, "delegation is switched off (delegation_provider=none)"

    known = providers()
    if pref and pref != AUTO:
        chosen = _REGISTRY.get(pref)
        if chosen is None:
            return None, (
                f"provider {pref!r} is not installed in this build "
                f"(available: {', '.join(p.id for p in known) or 'none'})"
            )
        ok, detail = _probe(chosen)
        if ok:
            return chosen, f"{chosen.title}: {detail}"
        # An explicit choice is honoured even when it fails. Substituting a
        # different vendor here would be a billing decision made on the
        # operator's behalf.
        return None, f"{chosen.title} is selected but unusable: {detail}"

    for provider in known:
        ok, detail = _probe(provider)
        if ok:
            return provider, f"auto-selected {provider.title}: {detail}"
    reasons = "; ".join(f"{p}: {d}" for p, ok, d in availability() if not ok)
    return None, f"no usable delegation provider ({reasons or 'none registered'})"


def select(preference: Optional[str] = None) -> Optional[DelegationProvider]:
    """The provider that should serve delegation, or None when nothing can."""
    return selection(preference)[0]


# The local CLI is always registered: it is part of this build, and whether it
# can run is a question for its own probe, not for import time.
from src.delegation.claude_cli import ClaudeCliProvider  # noqa: E402  (cycle-free by design)

register(ClaudeCliProvider())
