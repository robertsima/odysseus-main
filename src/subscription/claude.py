"""Disabled Claude consumer-subscription OAuth placeholder.

Odysseus reaches Claude today by shelling out to a locally installed Claude Code
CLI. That looks like sloppy coupling and is not: the CLI is the only path that
rides a Claude *subscription*. An API key would be a one-line change and the
wrong one -- it moves the account onto metered per-token billing, which is
precisely what the subscription was bought to avoid. So this module exists to
make a Claude subscription connectable the way a ChatGPT one already is, over
account OAuth with a server-side refresh token, and it has no API-key path.

**This provider is deliberately shipped unconfigured.** The device-flow shape is
implemented (it mirrors :mod:`src.chatgpt_subscription`, whose flow is known to
work), but the real-world OAuth constants -- client id, issuer, token URL,
scopes, device endpoints -- could not be verified from inside this change, and a
plausible-looking guess is worse than an honest blank: it would send the account
holder round a login that fails at the vendor with an opaque error, or worse,
transmit a credential to a host we invented. Every unverified value below is an
empty constant with a ``TODO(verify)`` note saying exactly what to confirm and
where from. Until they are filled in, :func:`is_linked` reports "not configured"
cleanly, :meth:`ClaudeSubscriptionProvider.owns_base_url` claims nothing, and
every network step raises :class:`SubscriptionNotConfigured` instead of calling
out.

This module is intentionally disabled. Anthropic's consumer terms reserve
Claude.ai subscription login for the unmodified Claude Code client and do not
permit third-party applications to collect or intermediate those credentials.
Claude delegation therefore belongs in the approved Claude Code/MCP provider,
not in a copied OAuth flow here. The placeholder remains only so old imports can
fail closed with an actionable message instead of an ImportError.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import httpx

from src.subscription.base import (
    DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    DeviceCodeBundle,
    RuntimeCredentials,
    SubscriptionError,
    SubscriptionNotConfigured,
    SubscriptionProvider,
    SubscriptionRateLimited,
    SubscriptionReauthRequired,
    TokenBundle,
    jwt_access_token_is_expiring,
    resolve_runtime_credentials_via_db,
    stored_auth_row_exists,
)

logger = logging.getLogger(__name__)

CLAUDE_SUBSCRIPTION_PROVIDER = "claude-subscription"
CLAUDE_SUBSCRIPTION_TITLE = "Claude Subscription"
CLAUDE_SUBSCRIPTION_DISABLED = True

# --- unverified OAuth configuration ---------------------------------------
#
# Each value is overridable by environment variable so a deployment that *has*
# confirmed it can run this provider without a code change, and so the blanks
# below never have to be filled in from memory.
#
# TODO(verify): every constant in this block. All of them are obtainable from one
# place -- the OAuth request an installed Claude Code CLI makes when it logs a
# subscriber in. Capture it (a local HTTPS proxy, the CLI's own verbose/debug
# output, or the authorize URL it opens in the browser, whose query string
# carries client_id, redirect_uri, scope and the PKCE challenge) and paste the
# values here, or publish them via the env vars. Cross-check against Anthropic's
# own published OAuth documentation before trusting a captured value; do not
# copy constants out of third-party blog posts or unofficial clients.

# The OAuth client the subscription login is registered as.
# TODO(verify): read client_id from the CLI's authorize URL. Unknown here.
CLAUDE_OAUTH_CLIENT_ID = os.getenv("CLAUDE_OAUTH_CLIENT_ID", "").strip()

# Authorization server origin, used to build the URLs below when they are not
# given explicitly.
# TODO(verify): the issuer origin the CLI authorizes against. Unknown here.
CLAUDE_OAUTH_ISSUER = os.getenv("CLAUDE_OAUTH_ISSUER", "").strip().rstrip("/")

# TODO(verify): the token endpoint that serves authorization_code and
# refresh_token grants. Unknown here; NOT assumed to live under the issuer,
# because the ChatGPT provider is itself a counter-example (its device endpoints
# and its token endpoint sit on different paths).
CLAUDE_OAUTH_TOKEN_URL = os.getenv("CLAUDE_OAUTH_TOKEN_URL", "").strip()

# TODO(verify): the redirect URI registered for this client. It is echoed in the
# authorization_code grant and a mismatch is rejected, so it must be exact.
CLAUDE_OAUTH_REDIRECT_URI = os.getenv("CLAUDE_OAUTH_REDIRECT_URI", "").strip()

# TODO(verify): the scopes the subscription login requests. Space-separated in
# the authorize URL. Requesting the wrong set yields a token that authenticates
# but is not permitted to run inference, which fails later and confusingly.
CLAUDE_OAUTH_SCOPES: List[str] = [
    scope for scope in os.getenv("CLAUDE_OAUTH_SCOPES", "").replace(",", " ").split() if scope
]

# Device-authorization endpoints (RFC 8628). Separate constants because it is not
# established that the Claude login offers a device flow at all -- the CLI may
# only do authorization-code-with-PKCE against a loopback or console callback, in
# which case these stay empty forever and the login is driven through
# :func:`build_authorization_url` instead.
# TODO(verify): whether a device-code flow exists for this client, and its URLs.
CLAUDE_OAUTH_DEVICE_CODE_URL = os.getenv("CLAUDE_OAUTH_DEVICE_CODE_URL", "").strip()
CLAUDE_OAUTH_DEVICE_TOKEN_URL = os.getenv("CLAUDE_OAUTH_DEVICE_TOKEN_URL", "").strip()

# The base URL a provisioned endpoint points at. Left empty on purpose: the
# Anthropic *API* hostname is well known, but which base a *subscription* bearer
# is accepted against is not, and claiming the API host here would make this
# provider capture ordinary API-key endpoints during routing -- silently
# rerouting a metered endpoint through subscription auth, or the reverse.
# TODO(verify): the base URL the CLI sends subscriber inference to.
DEFAULT_CLAUDE_SUBSCRIPTION_BASE_URL = (
    os.getenv("CLAUDE_SUBSCRIPTION_BASE_URL", "").strip().rstrip("/")
)

# Anthropic's API version header. This one *is* verified -- it is the value this
# repo already sends for Anthropic endpoints (see src/endpoint_resolver.py).
ANTHROPIC_VERSION_HEADER = "2023-06-01"

# TODO(verify): whether the subscription path additionally requires a beta
# opt-in header (an `anthropic-beta` value) and/or a specific client
# identification header. Omitted rather than guessed: a wrong beta string is
# rejected outright, and an invented client id misrepresents this software to the
# vendor.

CLAUDE_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = int(
    os.getenv("CLAUDE_ACCESS_TOKEN_REFRESH_SKEW_SECONDS", "")
    or DEFAULT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
)

_REQUIRED_FOR_LOGIN = (
    ("client id", "CLAUDE_OAUTH_CLIENT_ID"),
    ("token URL", "CLAUDE_OAUTH_TOKEN_URL"),
    ("redirect URI", "CLAUDE_OAUTH_REDIRECT_URI"),
)


def _missing_configuration() -> List[str]:
    """Which unverified constants are still blank, in operator-facing words."""
    values = {
        "CLAUDE_OAUTH_CLIENT_ID": CLAUDE_OAUTH_CLIENT_ID,
        "CLAUDE_OAUTH_TOKEN_URL": CLAUDE_OAUTH_TOKEN_URL,
        "CLAUDE_OAUTH_REDIRECT_URI": CLAUDE_OAUTH_REDIRECT_URI,
    }
    return [label for label, key in _REQUIRED_FOR_LOGIN if not values[key]]


def is_configured() -> Tuple[bool, str]:
    """Always fail closed: direct Claude consumer OAuth is unsupported here."""
    if CLAUDE_SUBSCRIPTION_DISABLED:
        return False, (
            "Direct Claude consumer-subscription OAuth is disabled. Use the "
            "approved Claude Code or MCP delegation provider instead."
        )
    missing = _missing_configuration()
    if missing:
        return False, (
            "Claude subscription OAuth is not configured: missing "
            + ", ".join(missing)
            + ". These values are unverified in this build — capture them from the "
            "Claude Code CLI's own login request (see src/subscription/claude.py) "
            "or set the CLAUDE_OAUTH_* environment variables."
        )
    return True, "Claude subscription OAuth is configured"


def _require_configured() -> None:
    ok, reason = is_configured()
    if not ok:
        raise SubscriptionNotConfigured(reason, provider=CLAUDE_SUBSCRIPTION_PROVIDER)


# --- PKCE -----------------------------------------------------------------
#
# RFC 7636 is a standard, so unlike the endpoints above this can be implemented
# correctly without verifying anything against the vendor.


def generate_pkce_pair() -> Tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for an S256 PKCE exchange.

    ``secrets`` rather than ``random``: the verifier is the only thing stopping an
    intercepted authorization code from being redeemed by someone else.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorization_url(code_challenge: str, state: str) -> str:
    """The URL the user visits to approve the connection.

    Present for the authorization-code path, which is the flow the CLI is
    believed to use. It raises rather than returning a URL built from blank
    constants -- an authorize link pointing at nowhere is worse than an error.
    """
    _require_configured()
    authorize_url = os.getenv("CLAUDE_OAUTH_AUTHORIZE_URL", "").strip()
    if not authorize_url:
        # TODO(verify): the authorize endpoint path on the issuer.
        if not CLAUDE_OAUTH_ISSUER:
            raise SubscriptionNotConfigured(
                "Claude subscription authorize URL is unknown in this build. Set "
                "CLAUDE_OAUTH_AUTHORIZE_URL once it has been confirmed.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        raise SubscriptionNotConfigured(
            "Claude subscription authorize path is unverified; only the issuer "
            "origin is known. Set CLAUDE_OAUTH_AUTHORIZE_URL explicitly.",
            provider=CLAUDE_SUBSCRIPTION_PROVIDER,
        )
    params = {
        "response_type": "code",
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if CLAUDE_OAUTH_SCOPES:
        params["scope"] = " ".join(CLAUDE_OAUTH_SCOPES)
    return f"{authorize_url}?{urlencode(params)}"


# --- HTTP plumbing --------------------------------------------------------


def _raise_for_oauth_response(response: httpx.Response, action: str) -> None:
    """Turn a failed OAuth response into the right neutral error.

    Mirrors the ChatGPT provider's triage, because the distinction it draws is
    the one callers act on: 429 means wait (the credential is fine), an auth
    failure or a spent/reused refresh token means the account must be reconnected,
    anything else is an upstream fault. Response bodies are parsed defensively --
    an error page is not always JSON -- and only the vendor's own message is
    surfaced, never a token.
    """
    if response.status_code < 400:
        return
    code = ""
    message = f"Claude Subscription {action} failed with HTTP {response.status_code}."
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "").strip()
            msg = err.get("message")
            if msg:
                message = f"Claude Subscription {action} failed: {msg}"
        elif isinstance(err, str):
            code = err.strip()
            desc = payload.get("error_description") or payload.get("message")
            if desc:
                message = f"Claude Subscription {action} failed: {desc}"
    except Exception:
        # A non-JSON error body tells us nothing extra; the status line already
        # decided the outcome below.
        pass
    if response.status_code == 429:
        raise SubscriptionRateLimited(
            "Claude Subscription quota or rate limit was reached. Credentials are still valid.",
            provider=CLAUDE_SUBSCRIPTION_PROVIDER,
        )
    if response.status_code in (401, 403) or code in {
        "invalid_grant",
        "invalid_token",
        "invalid_request",
        "invalid_client",
        "refresh_token_reused",
    }:
        raise SubscriptionReauthRequired(message, provider=CLAUDE_SUBSCRIPTION_PROVIDER)
    raise SubscriptionError(message, provider=CLAUDE_SUBSCRIPTION_PROVIDER)


def _json_or_error(response: httpx.Response, action: str) -> Dict[str, Any]:
    _raise_for_oauth_response(response, action)
    try:
        data = response.json()
    except Exception as exc:
        raise SubscriptionError(
            f"Claude Subscription {action} returned invalid JSON.",
            provider=CLAUDE_SUBSCRIPTION_PROVIDER,
        ) from exc
    if not isinstance(data, dict):
        raise SubscriptionError(
            f"Claude Subscription {action} returned an unexpected response.",
            provider=CLAUDE_SUBSCRIPTION_PROVIDER,
        )
    return data


class ClaudeSubscriptionProvider(SubscriptionProvider):
    """A Claude subscription, connected by account OAuth rather than an API key."""

    provider_id = CLAUDE_SUBSCRIPTION_PROVIDER
    title = CLAUDE_SUBSCRIPTION_TITLE
    auth_mode = "claude"
    refresh_skew_seconds = CLAUDE_ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @property
    def default_base_url(self) -> str:
        return DEFAULT_CLAUDE_SUBSCRIPTION_BASE_URL

    @property
    def supports_device_flow(self) -> bool:
        """False until the device endpoints are confirmed to exist for this client."""
        return not CLAUDE_SUBSCRIPTION_DISABLED and bool(
            CLAUDE_OAUTH_DEVICE_CODE_URL
            and CLAUDE_OAUTH_DEVICE_TOKEN_URL
            and not _missing_configuration()
        )

    # -- routing ----------------------------------------------------------

    def owns_base_url(self, url: str) -> bool:
        """Match only the configured subscription base, host and path prefix both.

        It would be easy to claim every Anthropic API host here, and wrong: those
        URLs are ordinary API-key endpoints. Capturing them would route a metered
        endpoint through subscription auth (401, since the bearer is not for that
        host) or hide a genuine subscription behind API-key headers. With no
        configured base this owns nothing, which is the correct answer for an
        unconfigured provider.
        """
        if CLAUDE_SUBSCRIPTION_DISABLED:
            return False
        base = DEFAULT_CLAUDE_SUBSCRIPTION_BASE_URL
        if not base:
            return False
        try:
            expected = urlparse(base)
            parsed = urlparse(url or "")
        except Exception:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        expected_host = (expected.hostname or "").lower().rstrip(".")
        if not expected_host or host != expected_host:
            return False
        path = (parsed.path or "").rstrip("/")
        expected_path = (expected.path or "").rstrip("/")
        if not expected_path:
            return True
        return path == expected_path or path.startswith(expected_path + "/")

    # -- login ------------------------------------------------------------

    def request_device_code(self, timeout: float = 15.0) -> DeviceCodeBundle:
        if not self.supports_device_flow:
            raise SubscriptionNotConfigured(
                "Claude subscription device-code login is not available in this "
                "build: the device-authorization endpoints are unverified. See "
                "src/subscription/claude.py.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        response = httpx.post(
            CLAUDE_OAUTH_DEVICE_CODE_URL,
            json={"client_id": CLAUDE_OAUTH_CLIENT_ID, "scope": " ".join(CLAUDE_OAUTH_SCOPES)},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        data = _json_or_error(response, "device-code request")
        # RFC 8628 names the handle device_code; the ChatGPT provider calls it
        # device_auth_id. Normalise to the latter so the shared device-flow
        # routes and pending-store keep one vocabulary.
        if not data.get("device_auth_id") and data.get("device_code"):
            data["device_auth_id"] = data["device_code"]
        if not data.get("device_auth_id") or not data.get("user_code"):
            raise SubscriptionError(
                "Claude device-code response was missing required fields.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        data.setdefault("interval", 5)
        data.setdefault("expires_in", 900)
        return data

    def poll_device_auth(
        self, device_auth_id: str, user_code: str, timeout: float = 15.0
    ) -> Dict[str, Any]:
        if not self.supports_device_flow:
            raise SubscriptionNotConfigured(
                "Claude subscription device-code login is not available in this build.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        response = httpx.post(
            CLAUDE_OAUTH_DEVICE_TOKEN_URL,
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if response.status_code in (403, 404):
            # The ChatGPT flow answers an un-approved code this way rather than
            # with RFC 8628's authorization_pending body; treat the same shape as
            # "still waiting" so the polling loop does not abort a live login.
            return {"status": "pending", "error": "authorization_pending"}
        return _json_or_error(response, "device-code poll")

    def exchange_authorization_code(
        self, authorization_code: str, code_verifier: str, timeout: float = 15.0
    ) -> TokenBundle:
        _require_configured()
        response = httpx.post(
            CLAUDE_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": CLAUDE_OAUTH_REDIRECT_URI,
                "client_id": CLAUDE_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            timeout=timeout,
        )
        data = _json_or_error(response, "token exchange")
        if not data.get("access_token"):
            raise SubscriptionReauthRequired(
                "Claude token exchange did not return an access token.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        return data

    # -- tokens -----------------------------------------------------------

    def refresh_oauth_tokens(
        self, access_token: str, refresh_token: str, timeout: float = 20.0
    ) -> TokenBundle:
        # The access token plays no part in a refresh_token grant; it is in the
        # signature because the interface passes both (some providers bind the
        # two) and dropping it would make the call sites diverge per provider.
        del access_token
        _require_configured()
        if not refresh_token:
            raise SubscriptionReauthRequired(
                "Claude Subscription is missing a refresh token. Reconnect the provider.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        response = httpx.post(
            CLAUDE_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLAUDE_OAUTH_CLIENT_ID,
            },
            timeout=timeout,
        )
        data = _json_or_error(response, "token refresh")
        if not data.get("access_token"):
            raise SubscriptionReauthRequired(
                "Claude token refresh did not return an access token.",
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
            )
        return data

    def access_token_is_expiring(
        self, access_token: str, skew_seconds: Optional[int] = None
    ) -> bool:
        skew = self.refresh_skew_seconds if skew_seconds is None else skew_seconds
        return jwt_access_token_is_expiring(access_token, skew)

    def resolve_runtime_credentials(
        self, auth_id: str, owner: Optional[str] = None, *, force_refresh: bool = False
    ) -> RuntimeCredentials:
        return resolve_runtime_credentials_via_db(
            auth_id,
            owner,
            provider_id=self.provider_id,
            default_base_url=DEFAULT_CLAUDE_SUBSCRIPTION_BASE_URL,
            auth_mode=self.auth_mode,
            refresh=self.refresh_oauth_tokens,
            is_expiring=self.access_token_is_expiring,
            force_refresh=force_refresh,
        )

    # -- requests ---------------------------------------------------------

    def headers(self, access_token: Optional[str]) -> Dict[str, str]:
        """Auth headers for a subscription-backed Claude request.

        A subscription bearer goes in ``Authorization``, *not* in ``x-api-key``:
        the two are different credential types on different billing paths, and
        putting an OAuth token in the API-key header is how a subscription
        silently becomes a metered call (or, more likely, a 401).
        """
        headers = {
            "Accept": "application/json",
            "anthropic-version": ANTHROPIC_VERSION_HEADER,
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        beta = os.getenv("CLAUDE_SUBSCRIPTION_ANTHROPIC_BETA", "").strip()
        if beta:
            # See the TODO(verify) on the beta header above: supplied by the
            # operator only, never guessed here.
            headers["anthropic-beta"] = beta
        return headers

    # -- status -----------------------------------------------------------

    def is_linked(self, owner: Optional[str] = None) -> Tuple[bool, str]:
        """Local-state only, and never raises: capability probes call this.

        Reports the configuration gap before the credential gap, because they
        need different fixes and telling an operator to "connect an account" when
        the login cannot run yet wastes their time.
        """
        configured, reason = is_configured()
        if not configured:
            return False, reason
        if stored_auth_row_exists(CLAUDE_SUBSCRIPTION_PROVIDER, owner):
            return True, "Claude subscription is connected"
        return False, "no Claude subscription connected — connect one in Settings › Models"


_PROVIDER = ClaudeSubscriptionProvider()


def provider() -> ClaudeSubscriptionProvider:
    """The singleton instance. Stateless, so sharing it is free."""
    return _PROVIDER


def is_linked(owner: Optional[str] = None) -> Tuple[bool, str]:
    """Module-level shorthand used by the capability registry.

    ``src.capabilities_builtin`` imports this module and calls this directly, so
    the signature is part of that contract: return ``(ok, reason)``, never raise.
    """
    try:
        return _PROVIDER.is_linked(owner)
    except Exception as exc:  # pragma: no cover - defensive; probe must not raise
        logger.debug("Claude subscription is_linked probe failed: %s", exc)
        return False, f"could not check Claude subscription: {type(exc).__name__}"
