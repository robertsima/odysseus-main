"""Registry of subscription-backed providers.

Subscription auth started as one provider, so the code that needed it named that
provider directly -- ``is_chatgpt_subscription_base(url)`` in the routing path,
``from src.chatgpt_subscription import resolve_runtime_credentials`` in the
resolver. Every such call site is a place a second subscription has to be
special-cased again. This module is the seam: ask it which provider owns a URL, or
for one by id, and adding a third compliant provider is a registration rather than
an edit to the request path.

Providers are imported lazily on first lookup. A capability probe asking whether a
Claude subscription is linked must not drag ``httpx``, FastAPI or the ORM into the
process just by importing this package.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.subscription.base import (
    DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    DeviceCodeBundle,
    RuntimeCredentials,
    SubscriptionAuthNotFound,
    SubscriptionError,
    SubscriptionNotConfigured,
    SubscriptionProvider,
    SubscriptionRateLimited,
    SubscriptionReauthRequired,
    TokenBundle,
    to_http_exception,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS",
    "DeviceCodeBundle",
    "RuntimeCredentials",
    "SubscriptionAuthNotFound",
    "SubscriptionError",
    "SubscriptionNotConfigured",
    "SubscriptionProvider",
    "SubscriptionRateLimited",
    "SubscriptionReauthRequired",
    "TokenBundle",
    "get",
    "provider_for_auth_id",
    "provider_for_url",
    "providers",
    "to_http_exception",
]

# Module path -> factory attribute. Order is the order the admin UI lists them in
# and the order :func:`provider_for_url` consults. Only providers with a verified
# and policy-compliant authentication path belong here. In particular, do not
# register a Claude.ai OAuth adapter: Claude consumer subscription login must use
# the unmodified Claude Code/MCP path, not a copied credential flow.
_PROVIDER_MODULES = (
    ("src.subscription.chatgpt", "provider"),
)

_cache: Dict[str, SubscriptionProvider] = {}
_loaded = False


def _load() -> Dict[str, SubscriptionProvider]:
    """Import and instantiate the registered providers once.

    A provider whose module fails to import is skipped with a warning rather than
    breaking the registry: one unusable provider must not take down a working
    subscription the user is relying on right now.
    """
    global _loaded
    if _loaded:
        return _cache
    for module_path, factory_name in _PROVIDER_MODULES:
        try:
            module = __import__(module_path, fromlist=[factory_name])
            instance = getattr(module, factory_name)()
        except Exception as exc:
            logger.warning("Subscription provider %s is unavailable: %s", module_path, exc)
            continue
        _cache[instance.provider_id] = instance
    _loaded = True
    return _cache


def providers() -> List[SubscriptionProvider]:
    """Every provider this build ships, registration order preserved.

    Includes providers that are present but unconfigured -- the admin UI needs to
    render "not connected" for them, which it cannot do for a provider it is never
    told about.
    """
    return list(_load().values())


def get(provider_id: str) -> Optional[SubscriptionProvider]:
    """The provider with this id, or ``None``.

    ``None`` rather than a raise: callers resolve ids that came out of a database
    row, and a row written by a build that had a provider this one does not is a
    normal condition, not an error.
    """
    return _load().get((provider_id or "").strip())


def provider_for_auth_id(
    auth_id: str, owner: Optional[str] = None
) -> Optional[SubscriptionProvider]:
    """Return the registered provider for a stored auth-session id.

    The auth-session row is authoritative when it exists. URL matching remains
    a compatibility fallback for old/test rows with no database record, but an
    unknown provider id fails closed instead of being treated as ChatGPT.
    """
    if not (auth_id or "").strip():
        return None
    try:
        from core.database import ProviderAuthSession, SessionLocal
    except Exception:
        return None
    db = None
    try:
        db = SessionLocal()
        query = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.id == auth_id,
        )
        if owner:
            query = query.filter(ProviderAuthSession.owner == owner)
        row = query.first()
        if row is None:
            return None
        provider_id = (getattr(row, "provider", None) or "").strip()
        provider = get(provider_id)
        if provider is None:
            raise SubscriptionNotConfigured(
                f"Subscription provider {provider_id or '(empty)'} is not installed.",
                provider=provider_id or None,
            )
        return provider
    except SubscriptionNotConfigured:
        raise
    except Exception:
        # Capability probes and legacy/test environments may not have the auth
        # table or a full ORM query double. Treat that as an absent row so URL
        # matching can preserve the established ChatGPT fallback.
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def provider_for_url(url: str) -> Optional[SubscriptionProvider]:
    """The provider that serves ``url``, or ``None`` if no subscription owns it.

    ``None`` is the common case -- most endpoints are ordinary API-key or local
    endpoints -- so callers treat it as "not a subscription URL", never as a
    failure. A provider that raises while matching is treated as not matching, so
    a broken matcher cannot break routing for the others.
    """
    if not url:
        return None
    for instance in _load().values():
        try:
            if instance.owns_base_url(url):
                return instance
        except Exception as exc:
            logger.debug("Provider %s failed URL match: %s", instance.provider_id, exc)
    return None
