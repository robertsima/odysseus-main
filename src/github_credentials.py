"""Shared validation for credentials handed to GitHub integrations."""

from __future__ import annotations

import os
import re


GITHUB_TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"
_PUBLIC_HOSTS = {"", "github.com", "https://github.com"}
_TOKEN_SHAPE = re.compile(
    r"^(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+$|^[0-9a-fA-F]{40}$"
)


def configured_github_host() -> str:
    return os.environ.get("GITHUB_HOST", "").strip().lower().rstrip("/")


def is_public_github_host() -> bool:
    return configured_github_host() in _PUBLIC_HOSTS


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
