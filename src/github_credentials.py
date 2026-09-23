"""Shared validation for credentials handed to GitHub integrations."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse


GITHUB_TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"
_PUBLIC_HOSTS = {"", "github.com", "https://github.com"}
_TOKEN_SHAPE = re.compile(
    r"^(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+$|^[0-9a-fA-F]{40}$"
)


def configured_github_host() -> str:
    return os.environ.get("GITHUB_HOST", "").strip().lower().rstrip("/")


def is_public_github_host() -> bool:
    return configured_github_host() in _PUBLIC_HOSTS


_HOSTNAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
)


def github_git_host() -> str | None:
    """The hostname the shared credential belongs to, for Git over HTTPS.

    github.com unless GITHUB_HOST names an Enterprise install -- the variable
    the GitHub MCP server honours, so Git and MCP reach the same host with the
    same token. A bare host or an https:// URL is accepted; plain http, a port,
    a path or userinfo yields None, and then no remote is sent the token.
    """
    raw = configured_github_host()
    if raw in _PUBLIC_HOSTS:
        return "github.com"
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not _HOSTNAME.fullmatch(host)
    ):
        return None
    return host


def token_for_git_host(host: str | None, token: str | None) -> str | None:
    """`token` only when `host` is the one the shared credential was issued for.

    The single decision a Git transport makes before authenticating: an
    Enterprise token must never reach github.com (nor a github.com token an
    Enterprise host), whatever remote a repository's config happens to name.
    """
    expected = github_git_host()
    if not token or not host or not expected:
        return None
    return token if host.casefold() == expected else None


def token_looks_valid(token: str | None, *, public_host: bool | None = None) -> bool:
    """Reject obvious cross-service secrets without over-validating GHE tokens."""
    value = str(token or "").strip()
    if not value:
        return False
    if public_host is None:
        public_host = is_public_github_host()
    return bool(_TOKEN_SHAPE.fullmatch(value)) if public_host else True


def github_token_from_env(*, public_only: bool = False) -> str | None:
    if public_only and not is_public_github_host():
        return None
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    return token if token_looks_valid(token) else None
