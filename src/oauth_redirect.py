"""The redirect address Google OAuth sends the browser back to.

Google only accepts a redirect URI that is ``https`` (plain ``http`` only for
localhost), names a domain rather than an IP address, and ends in a public
top-level domain. The callback is reached by the user's own browser, never by
Google's servers, so the address does not have to be reachable from the
internet: an HTTPS name private to a tailnet (Tailscale Serve,
``https://<machine>.<tailnet>.ts.net``) is enough.

Until 2026-09-28 the Calendar flow always built ``http://<Host>/...`` and both
Google flows ignored the ``X-Forwarded-Proto`` an HTTPS proxy sends, so behind
Tailscale Serve the redirect still said ``http`` and Google refused it unless
the ``*_OAUTH_REDIRECT_URI`` variable was set by hand. This derives the
address the browser actually used, and says up front when Google will refuse
it instead of sending the user to Google's error page.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Optional
from urllib.parse import urlsplit

# Suffixes that are never public top-level domains (RFC 6762/8375, common
# home-router and NAS defaults). A redirect on one of them is refused by
# Google even over HTTPS.
_PRIVATE_SUFFIXES = (
    ".local", ".lan", ".home", ".home.arpa", ".internal", ".intranet", ".corp",
    ".localdomain", ".nas", ".private", ".test", ".example", ".invalid",
)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _first(value: str) -> str:
    # A chained proxy sends a list; the client-facing hop comes first.
    return (value or "").split(",")[0].strip()


def google_redirect_uri(request, callback_path: str, env_var: str) -> str:
    """The OAuth redirect URI for ``callback_path`` (e.g. ``/api/calendar/oauth/google/callback``).

    ``env_var`` (when set and non-empty) wins outright; then the install's
    public URL (Settings > Public URL, else ``APP_PUBLIC_URL``) plus
    ``callback_path``, so one origin serves the Gmail and Calendar flows
    alike; otherwise the scheme and host the browser used, honouring
    ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` from an HTTPS proxy.
    """
    configured = (os.environ.get(env_var) or "").strip()
    if configured:
        return configured
    from src.settings import get_setting_or_env

    public = str(get_setting_or_env("app_public_url", "APP_PUBLIC_URL", "") or "").strip().rstrip("/")
    if public:
        return f"{public}{callback_path}"
    headers = request.headers
    forwarded_proto = _first(headers.get("x-forwarded-proto", "")).lower()
    scheme = "https" if (request.url.scheme == "https" or forwarded_proto == "https") else "http"
    host = _first(headers.get("x-forwarded-host", "")) or headers.get("host") or "localhost:7000"
    return f"{scheme}://{host}{callback_path}"


def google_redirect_problem(uri: str) -> Optional[str]:
    """Why Google will refuse ``uri`` as a redirect, or None.

    ``"needs_https"``, ``"ip_address"`` or ``"private_hostname"``.
    """
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return None
    if parts.scheme != "https":
        return "needs_https"
    try:
        ipaddress.ip_address(host)
        return "ip_address"
    except ValueError:
        pass
    if "." not in host or host.endswith(_PRIVATE_SUFFIXES):
        return "private_hostname"
    return None
