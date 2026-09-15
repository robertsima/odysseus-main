"""The provider-neutral contract for subscription-backed model access.

A *subscription* provider is not a transport detail, it is a billing model. The
account holder already pays a flat monthly fee, and the only way to spend that
fee instead of metered per-token API credit is to present the credential the
subscription itself issues -- an OAuth access token minted for the consumer
account, refreshed from a long-lived refresh token kept server-side. An API key
travels the same wire and bills from a different pot, so it is *not* a fallback
for anything in this package and nothing here accepts one.

:mod:`src.chatgpt_subscription` already implements the whole shape for one
provider: device authorization, a token exchange, a refresh with skew, and a
runtime resolve that hands the caller a token that is good *right now*. This
module lifts that shape out so a second subscription (Claude) can be connected
the same way rather than by shelling out to a vendor CLI -- which is what
Odysseus does today, for exactly the billing reason above.

Everything provider-specific -- endpoints, client ids, header names, which hosts
a provider owns -- stays in the provider module. What lives here is the contract,
the error taxonomy, and the two pieces of machinery that are genuinely identical
across providers: JWT expiry-with-skew, and the refresh-on-read resolve against
``ProviderAuthSession``.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Type

# A token response, a device-code response and a resolved runtime credential are
# all plain JSON objects. They stay dicts rather than dataclasses because the
# existing provider hands provider-shaped dicts to routes that index them
# directly, and this package must not change those payloads.
TokenBundle = Dict[str, Any]
DeviceCodeBundle = Dict[str, Any]
RuntimeCredentials = Dict[str, Any]

# How early to treat an access token as spent. Refreshing a token that is about
# to expire costs one round trip; discovering mid-request that it expired is a
# 401 the user sees. Providers may raise this, not drop it below a round trip.
DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120


# --- error taxonomy -------------------------------------------------------
#
# Three failure kinds have to be distinguishable by *callers*, because the right
# response differs: reconnect the account, back off, or say nothing is connected
# yet. Everything else is an upstream fault and stays generic. Provider modules
# map their own exception classes onto these (see :func:`map_error`) so a caller
# can catch one taxonomy regardless of which subscription served the request.


class SubscriptionError(RuntimeError):
    """Base error for any subscription provider failure.

    ``provider`` carries the provider id when known, so a handler that caught the
    neutral class can still say *which* account needs attention.
    """

    def __init__(self, message: str, *, provider: Optional[str] = None) -> None:
        super().__init__(message)
        self.provider = provider


class SubscriptionReauthRequired(SubscriptionError):
    """Stored OAuth credentials are invalid or expired beyond refresh."""


class SubscriptionRateLimited(SubscriptionError):
    """Upstream quota/rate limit. Credentials are fine; reconnecting will not help."""


class SubscriptionAuthNotFound(SubscriptionError):
    """No matching owner-scoped auth session exists."""


class SubscriptionNotConfigured(SubscriptionError):
    """The provider ships in this build but has no verified OAuth configuration.

    Kept distinct from :class:`SubscriptionAuthNotFound`: "nobody has connected
    an account" is one user action away, while "this build does not know the
    provider's OAuth endpoints" is a code or deployment gap. Reporting the second
    as the first sends the user round a login flow that cannot possibly succeed.
    """


def map_error(
    exc: BaseException,
    table: Mapping[Type[BaseException], Type[BaseException]],
    default: Type[BaseException],
    *,
    provider: Optional[str] = None,
) -> BaseException:
    """Translate ``exc`` into the class ``table`` names, falling back to ``default``.

    Used in both directions: a provider module maps its native exceptions onto
    the neutral taxonomy for callers that speak this interface, and back again for
    callers that still catch the provider's own classes. The message crosses over
    verbatim -- error text is user-visible and already tuned per provider -- and
    the original is chained so a traceback still shows where it began.

    ``table`` is scanned in declaration order and matched with ``isinstance``, so
    it may list a base class after the subclasses it would otherwise shadow.
    """
    mapped: BaseException
    for source, target in table.items():
        if isinstance(exc, source):
            mapped = target(str(exc))
            break
    else:
        mapped = default(str(exc))
    if provider is not None and isinstance(mapped, SubscriptionError):
        mapped.provider = provider
    mapped.__cause__ = exc
    return mapped


def to_http_exception(exc: BaseException):
    """Map a subscription failure onto the HTTP status a route should return.

    Statuses match :func:`src.chatgpt_subscription.to_http_exception` exactly --
    429 for quota, 401 for anything the user fixes by reconnecting, 502 otherwise
    -- because the admin UI keys its messaging off them and this package must not
    change what that UI sees.

    FastAPI is imported lazily: this module is otherwise pure and is imported by
    capability probes that run long before any web framework is needed.
    """
    from fastapi import HTTPException

    if isinstance(exc, SubscriptionRateLimited):
        return HTTPException(429, str(exc))
    if isinstance(exc, (SubscriptionReauthRequired, SubscriptionAuthNotFound)):
        return HTTPException(401, f"{exc} Reconnect the provider.")
    if isinstance(exc, SubscriptionNotConfigured):
        # 503 rather than 401: no credential the user could supply helps yet.
        return HTTPException(503, str(exc))
    return HTTPException(502, str(exc))


# --- shared machinery -----------------------------------------------------

_AUTH_REFRESH_LOCKS: Dict[str, threading.Lock] = {}
_AUTH_REFRESH_LOCKS_GUARD = threading.Lock()


def refresh_lock_for(auth_id: str) -> threading.Lock:
    """One lock per stored credential.

    Two concurrent requests on the same account would otherwise both refresh, and
    against a provider that rotates refresh tokens the second rotation
    invalidates the first -- the account then needs a full reconnect. Keyed by
    ``auth_id`` so unrelated accounts never serialise against each other.
    """
    with _AUTH_REFRESH_LOCKS_GUARD:
        lock = _AUTH_REFRESH_LOCKS.get(auth_id)
        if lock is None:
            lock = threading.Lock()
            _AUTH_REFRESH_LOCKS[auth_id] = lock
        return lock


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    """Decode a JWT's claim set without verifying it.

    Verification is the issuer's job; all we want is ``exp`` so we can refresh
    before a request fails. Padding is re-added because JWT segments are
    base64url with the padding stripped.
    """
    parts = (token or "").split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def jwt_access_token_is_expiring(
    access_token: str,
    skew_seconds: int = DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> bool:
    """Whether ``access_token`` expires within ``skew_seconds``.

    Anything unreadable counts as expiring: a token we cannot inspect is a token
    we cannot trust, and refreshing costs one round trip while guessing wrong
    costs the user their request.
    """
    try:
        exp = int(decode_jwt_payload(access_token).get("exp") or 0)
    except Exception:
        return True
    return exp <= int(time.time()) + int(skew_seconds)


def database_handles():
    """Import the ORM lazily.

    ``core.database`` runs ``init_db()`` at import time, so importing it from a
    module body would drag a database connection into every capability probe and
    unit test that only wants to ask a provider whether it is linked.
    """
    from core.database import ProviderAuthSession, SessionLocal, utcnow_naive

    return ProviderAuthSession, SessionLocal, utcnow_naive


def stored_auth_row_exists(provider_id: str, owner: Optional[str] = None) -> bool:
    """Whether any credential is on file for ``provider_id``.

    Deliberately swallows every failure and reports ``False``: this feeds
    ``is_linked()``, which capability probes call on request paths. A missing
    table on a fresh install, or a database that is not up yet, means "not
    linked" -- never a 500.
    """
    try:
        ProviderAuthSession, SessionLocal, _utcnow = database_handles()
    except Exception:
        return False
    db = None
    try:
        db = SessionLocal()
        q = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.provider == provider_id,
        )
        if owner:
            q = q.filter(ProviderAuthSession.owner == owner)
        return q.first() is not None
    except Exception:
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def resolve_runtime_credentials_via_db(
    auth_id: str,
    owner: Optional[str],
    *,
    provider_id: str,
    default_base_url: str,
    auth_mode: str,
    refresh: Callable[[str, str], TokenBundle],
    is_expiring: Callable[[str], bool],
    force_refresh: bool = False,
) -> RuntimeCredentials:
    """Read a stored credential and return one that is valid *now*.

    This is the runtime entry point every request path funnels through, and it is
    the same for every provider: look the row up scoped to its owner, refresh it
    if spent, persist the rotation, hand back a bearer. Only ``refresh`` and
    ``is_expiring`` differ per provider, so they arrive as callables.

    The double expiry check is not redundant. The first is unlocked so the common
    case (token still good) costs no contention; the second runs under the
    per-credential lock after re-reading the row, so a request that queued behind
    someone else's refresh uses their new token instead of spending the refresh
    token a second time.
    """
    ProviderAuthSession, SessionLocal, utcnow_naive = database_handles()
    db = SessionLocal()
    try:
        q = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.id == auth_id,
            ProviderAuthSession.provider == provider_id,
        )
        if owner:
            q = q.filter(ProviderAuthSession.owner == owner)
        row = q.first()
        if row is None:
            raise SubscriptionAuthNotFound(
                "Subscription credentials were not found for this user.",
                provider=provider_id,
            )

        access_token = row.access_token or ""
        if force_refresh or is_expiring(access_token):
            with refresh_lock_for(auth_id):
                db.refresh(row)
                access_token = row.access_token or ""
                refresh_token = row.refresh_token or ""
                if force_refresh or is_expiring(access_token):
                    refreshed = refresh(access_token, refresh_token)
                    row.access_token = refreshed["access_token"]
                    # Providers that rotate refresh tokens send a new one with
                    # every refresh; providers that do not omit the field
                    # entirely. Only overwrite on a value, or the next refresh
                    # has nothing to present.
                    if refreshed.get("refresh_token"):
                        row.refresh_token = refreshed["refresh_token"]
                    row.last_refresh = utcnow_naive()
                    db.commit()
                    db.refresh(row)
            access_token = row.access_token or ""

        return {
            "provider": provider_id,
            "base_url": (row.base_url or default_base_url).rstrip("/"),
            # Named "api_key" because that is the key src.endpoint_resolver
            # reads, not because it is one: this is a short-lived OAuth access
            # token minted against the user's subscription.
            "api_key": access_token,
            "auth_mode": row.auth_mode or auth_mode,
        }
    finally:
        db.close()


# --- the contract ---------------------------------------------------------


class SubscriptionProvider(ABC):
    """One subscription an account holder can connect and spend against.

    Implementations are stateless singletons -- everything mutable lives in
    ``ProviderAuthSession`` -- so the registry can hand the same instance to
    every caller.

    A provider may legitimately not support every step: a vendor whose login is
    authorization-code-with-PKCE rather than RFC 8628 device authorization has no
    device code to hand out. Such a provider reports
    :attr:`supports_device_flow` as ``False`` and raises
    :class:`SubscriptionNotConfigured` from the steps it cannot serve. That is
    honest; returning a fabricated device code is not.
    """

    #: Stable identifier, also the ``ProviderAuthSession.provider`` value.
    provider_id: str = ""
    #: Human-facing name, as the admin UI renders it.
    title: str = ""
    #: ``ProviderAuthSession.auth_mode`` value for rows this provider owns.
    auth_mode: str = ""
    #: Base URL new endpoints are provisioned against.
    default_base_url: str = ""
    refresh_skew_seconds: int = DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @property
    def supports_device_flow(self) -> bool:
        """Whether :meth:`request_device_code` can actually serve a login."""
        return True

    # -- routing ----------------------------------------------------------

    @abstractmethod
    def owns_base_url(self, url: str) -> bool:
        """Whether requests to ``url`` are served by this subscription.

        Routing is by URL because that is all the request path has: an endpoint
        row carries a base URL, and whether it needs a subscription bearer (plus
        this provider's headers) must be answerable from that alone.
        """

    # -- login ------------------------------------------------------------

    @abstractmethod
    def request_device_code(self, timeout: float = 15.0) -> DeviceCodeBundle:
        """Begin a login and return the code and URI the user is shown."""

    @abstractmethod
    def poll_device_auth(
        self, device_auth_id: str, user_code: str, timeout: float = 15.0
    ) -> Dict[str, Any]:
        """Poll a pending login. Returns a pending marker until the user finishes."""

    @abstractmethod
    def exchange_authorization_code(
        self, authorization_code: str, code_verifier: str, timeout: float = 15.0
    ) -> TokenBundle:
        """Trade an authorization code (plus its PKCE verifier) for tokens."""

    # -- tokens -----------------------------------------------------------

    @abstractmethod
    def refresh_oauth_tokens(
        self, access_token: str, refresh_token: str, timeout: float = 20.0
    ) -> TokenBundle:
        """Mint a new access token from the stored refresh token."""

    def access_token_is_expiring(
        self, access_token: str, skew_seconds: Optional[int] = None
    ) -> bool:
        """Whether ``access_token`` should be refreshed before it is used."""
        skew = self.refresh_skew_seconds if skew_seconds is None else skew_seconds
        return jwt_access_token_is_expiring(access_token, skew)

    @abstractmethod
    def resolve_runtime_credentials(
        self, auth_id: str, owner: Optional[str] = None, *, force_refresh: bool = False
    ) -> RuntimeCredentials:
        """Return ``{provider, base_url, api_key, auth_mode}`` valid right now."""

    # -- requests ---------------------------------------------------------

    @abstractmethod
    def headers(self, access_token: Optional[str]) -> Dict[str, str]:
        """Headers a request on this subscription must carry, bearer included.

        Subscription endpoints generally police more than the bearer (origin,
        client identification, API version), so header construction belongs to
        the provider rather than to a generic ``Authorization``-only builder.
        """

    # -- status -----------------------------------------------------------

    @abstractmethod
    def is_linked(self, owner: Optional[str] = None) -> Tuple[bool, str]:
        """Whether an account is connected, plus a sentence saying why not.

        The capability registry calls this on request paths, so it must never
        raise and never block: probe local state only, no network. The reason
        string is shown to an operator, so it says what to do about it.
        """

    # -- errors -----------------------------------------------------------

    def to_http_exception(self, exc: BaseException):
        """Map a failure from this provider onto an HTTP status."""
        return to_http_exception(exc)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.provider_id}>"
