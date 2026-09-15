"""ChatGPT subscription, behind the provider-neutral interface.

This is an adapter, not an implementation. :mod:`src.chatgpt_subscription` is the
working OAuth device-flow provider and stays the single source of truth: routes,
``llm_core`` and ``endpoint_resolver`` import it directly today and keep doing so.
Every method here forwards to that module, so there is exactly one copy of the
device-flow, refresh and resolve logic to get wrong.

The one thing the adapter adds is error translation. The underlying module raises
``ChatGPTSubscription*`` classes, which a provider-agnostic caller cannot catch
without knowing which provider it is talking to -- the whole point of the
interface. So calls made *through* this adapter surface the neutral taxonomy from
:mod:`src.subscription.base`, with the message text carried across unchanged and
the native error chained as ``__cause__``. Direct callers of
``src.chatgpt_subscription`` see no change at all, and
:func:`to_http_exception` here accepts either taxonomy so a route can be migrated
one call at a time.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from src import chatgpt_subscription as _impl
from src.subscription.base import (
    DeviceCodeBundle,
    RuntimeCredentials,
    SubscriptionAuthNotFound,
    SubscriptionError,
    SubscriptionProvider,
    SubscriptionRateLimited,
    SubscriptionReauthRequired,
    TokenBundle,
    map_error,
    stored_auth_row_exists,
)

PROVIDER_ID = _impl.CHATGPT_SUBSCRIPTION_PROVIDER

# Specific classes first: ChatGPTSubscriptionError is the base of the other three,
# and map_error matches in declaration order.
_NATIVE_TO_NEUTRAL: Dict[type, type] = {
    _impl.ChatGPTSubscriptionRateLimited: SubscriptionRateLimited,
    _impl.ChatGPTSubscriptionReauthRequired: SubscriptionReauthRequired,
    _impl.ChatGPTSubscriptionAuthNotFound: SubscriptionAuthNotFound,
    _impl.ChatGPTSubscriptionError: SubscriptionError,
}

_NEUTRAL_TO_NATIVE: Dict[type, type] = {
    SubscriptionRateLimited: _impl.ChatGPTSubscriptionRateLimited,
    SubscriptionReauthRequired: _impl.ChatGPTSubscriptionReauthRequired,
    SubscriptionAuthNotFound: _impl.ChatGPTSubscriptionAuthNotFound,
    SubscriptionError: _impl.ChatGPTSubscriptionError,
}


def to_neutral_error(exc: BaseException) -> BaseException:
    """Translate a ``ChatGPTSubscription*`` error into the neutral taxonomy."""
    return map_error(exc, _NATIVE_TO_NEUTRAL, SubscriptionError, provider=PROVIDER_ID)


def to_native_error(exc: BaseException) -> BaseException:
    """Translate a neutral subscription error back into a ChatGPT-specific one.

    For the reverse direction: code that already catches the provider's classes
    can consume a neutral error raised by generic machinery without being
    rewritten first.
    """
    return map_error(exc, _NEUTRAL_TO_NATIVE, _impl.ChatGPTSubscriptionError)


def to_http_exception(exc: BaseException):
    """Map either taxonomy onto an HTTP status.

    Delegates to the existing module for native errors so the statuses and
    wording stay byte-identical to what the admin UI already handles, and uses
    the neutral mapping otherwise.
    """
    if isinstance(exc, _impl.ChatGPTSubscriptionError):
        return _impl.to_http_exception(exc)
    from src.subscription.base import to_http_exception as _neutral

    return _neutral(exc)


class ChatGPTSubscriptionProvider(SubscriptionProvider):
    """The existing ChatGPT/Codex subscription, exposed through the interface."""

    provider_id = PROVIDER_ID
    title = "ChatGPT Subscription"
    # The value routes already write to ProviderAuthSession.auth_mode. Changing
    # it would orphan every row provisioned before this package existed.
    auth_mode = "chatgpt"
    refresh_skew_seconds = _impl.CHATGPT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @property
    def default_base_url(self) -> str:
        # Read through rather than copied at import: the underlying constant is
        # env-driven, and an operator who points CHATGPT_SUBSCRIPTION_BASE_URL
        # elsewhere expects both paths to agree.
        return _impl.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL

    # -- routing ----------------------------------------------------------

    def owns_base_url(self, url: str) -> bool:
        return _impl.is_chatgpt_subscription_base(url)

    # -- login ------------------------------------------------------------

    def request_device_code(self, timeout: float = 15.0) -> DeviceCodeBundle:
        try:
            return _impl.request_device_code(timeout=timeout)
        except _impl.ChatGPTSubscriptionError as exc:
            raise to_neutral_error(exc) from exc

    def poll_device_auth(
        self, device_auth_id: str, user_code: str, timeout: float = 15.0
    ) -> Dict[str, Any]:
        try:
            return _impl.poll_device_auth(device_auth_id, user_code, timeout=timeout)
        except _impl.ChatGPTSubscriptionError as exc:
            raise to_neutral_error(exc) from exc

    def exchange_authorization_code(
        self, authorization_code: str, code_verifier: str, timeout: float = 15.0
    ) -> TokenBundle:
        try:
            return _impl.exchange_authorization_code(
                authorization_code, code_verifier, timeout=timeout
            )
        except _impl.ChatGPTSubscriptionError as exc:
            raise to_neutral_error(exc) from exc

    # -- tokens -----------------------------------------------------------

    def refresh_oauth_tokens(
        self, access_token: str, refresh_token: str, timeout: float = 20.0
    ) -> TokenBundle:
        try:
            return _impl.refresh_oauth_tokens(access_token, refresh_token, timeout=timeout)
        except _impl.ChatGPTSubscriptionError as exc:
            raise to_neutral_error(exc) from exc

    def access_token_is_expiring(
        self, access_token: str, skew_seconds: Optional[int] = None
    ) -> bool:
        # Delegated rather than using the base implementation, even though the
        # two are currently identical: if this provider ever needs a different
        # notion of "expiring" (a non-JWT token, a vendor-supplied hint), the
        # interface must not quietly disagree with the module doing the refresh.
        skew = self.refresh_skew_seconds if skew_seconds is None else skew_seconds
        return _impl.access_token_is_expiring(access_token, skew)

    def resolve_runtime_credentials(
        self, auth_id: str, owner: Optional[str] = None, *, force_refresh: bool = False
    ) -> RuntimeCredentials:
        try:
            return _impl.resolve_runtime_credentials(
                auth_id, owner, force_refresh=force_refresh
            )
        except _impl.ChatGPTSubscriptionError as exc:
            raise to_neutral_error(exc) from exc

    # -- requests ---------------------------------------------------------

    def headers(self, access_token: Optional[str]) -> Dict[str, str]:
        return _impl.chatgpt_headers(access_token)

    # -- status -----------------------------------------------------------

    def is_linked(self, owner: Optional[str] = None) -> Tuple[bool, str]:
        """Local-state only: is there a stored credential for this provider?

        A network check would be more truthful but this runs inside capability
        probes on request paths, and a hanging probe is worse than a stale
        answer. A credential that has gone bad surfaces as a 401 with a
        reconnect prompt, which is the same outcome by a slower route.
        """
        if stored_auth_row_exists(PROVIDER_ID, owner):
            return True, "ChatGPT subscription is connected"
        return False, "no ChatGPT subscription connected — connect one in Settings › Models"

    # -- errors -----------------------------------------------------------

    def to_http_exception(self, exc: BaseException):
        return to_http_exception(exc)


_PROVIDER = ChatGPTSubscriptionProvider()


def provider() -> ChatGPTSubscriptionProvider:
    """The singleton instance. Stateless, so sharing it is free."""
    return _PROVIDER


def is_linked(owner: Optional[str] = None) -> Tuple[bool, str]:
    """Module-level shorthand, for capability probes that import the module."""
    return _PROVIDER.is_linked(owner)
