"""GitHub credentials and draft pull request creation.

Preferred credential is a **GitHub App installation token**: it is scoped to the
one installation, expires in an hour, and is minted on demand from a private key
that stays on disk in the operator's control. A classic personal access token is
supported only as a documented fallback because it is long-lived and broadly
scoped.

Rules that hold for every path in this module:

* tokens live in local variables and an in-memory cache with an expiry — never
  in a config file, a log line, an exception message, or a git argv;
* any text that leaves this module (errors, API bodies) is scrubbed of the token
  before it is returned;
* the JWT is signed with ``cryptography`` (already a core dependency), so no new
  package is needed to avoid embedding secrets in a URL.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Dict, Optional, Tuple

from src.agent_worktree.config import WorktreeConfig
from src.agent_worktree.validation import is_valid_repo_slug, normalize_branch

logger = logging.getLogger(__name__)

API_TIMEOUT_S = 30.0
_JWT_LIFETIME_S = 540  # GitHub caps app JWTs at 10 minutes; stay under it
_TOKEN_SKEW_S = 60

# (installation id, app id) -> (token, expires_at). Process-local only.
_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}


class GitHubError(RuntimeError):
    """A GitHub API call failed. Message is already token-scrubbed."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _app_jwt(app_id: str, private_key_pem: bytes) -> str:
    """RS256 JWT identifying the GitHub App."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - cryptography is a core dep
        raise GitHubError(f"cryptography is required to sign the app JWT: {exc}")

    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubError(f"GitHub App private key could not be loaded: {exc}")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubError("GitHub App private key must be an RSA key")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + _JWT_LIFETIME_S, "iss": app_id}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = key.sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{_b64url(signature)}"


def scrub(text: str, token: Optional[str]) -> str:
    """Strip a token (and its basic-auth encoding) from text bound for a log."""
    out = text or ""
    if token and len(token) >= 8:
        out = out.replace(token, "***")
    return out


async def installation_token(cfg: WorktreeConfig) -> str:
    """Short-lived installation token, minted or served from the cache."""
    cache_key = (cfg.installation_id, cfg.app_id)
    cached = _token_cache.get(cache_key)
    if cached and cached[1] - _TOKEN_SKEW_S > time.time():
        return cached[0]

    try:
        with open(cfg.private_key_path, "rb") as fh:
            pem = fh.read()
    except OSError as exc:
        raise GitHubError(f"cannot read GitHub App private key: {exc}")

    jwt = _app_jwt(cfg.app_id, pem)
    url = f"{cfg.api_base}/app/installations/{cfg.installation_id}/access_tokens"

    import httpx

    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code not in (200, 201):
        raise GitHubError(
            f"installation token request failed ({resp.status_code}): "
            f"{resp.text[:300]}"
        )
    data = resp.json()
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise GitHubError("installation token response contained no token")
    expires = time.time() + 3000
    raw_expiry = data.get("expires_at")
    if isinstance(raw_expiry, str):
        try:
            from datetime import datetime

            expires = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    _token_cache[cache_key] = (token, expires)
    logger.info("agent worktree: minted GitHub App installation token")
    return token


async def resolve_token(cfg: WorktreeConfig) -> str:
    """The credential to push with. App token first, PAT fallback second."""
    if cfg.has_github_app:
        return await installation_token(cfg)
    import os

    token = (os.getenv(cfg.fallback_token_env) or "").strip()
    if not token:
        raise GitHubError("no GitHub credential is configured")
    logger.warning(
        "agent worktree: using long-lived %s; a GitHub App installation token "
        "is preferred", cfg.fallback_token_env,
    )
    return token


def forget_token(cfg: WorktreeConfig) -> None:
    """Drop the cached token (used after a 401, and by tests)."""
    _token_cache.pop((cfg.installation_id, cfg.app_id), None)


async def create_draft_pr(
    cfg: WorktreeConfig,
    token: str,
    *,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> Dict:
    """Open a draft PR. Always draft — never a ready-for-review PR.

    Draft is hard-coded rather than a parameter: the whole point of the flow is
    that a human reviews before anything can merge, and a caller-supplied flag
    would be one prompt injection away from ``draft: false``.
    """
    if not is_valid_repo_slug(cfg.repo_slug):
        raise GitHubError("repository slug is not valid")
    head = normalize_branch(head_branch)
    base = normalize_branch(base_branch)
    if not head or not base:
        raise GitHubError("branch names failed validation")

    url = f"{cfg.api_base}/repos/{cfg.repo_slug}/pulls"
    payload = {
        "title": (title or head)[:250],
        "head": head,
        "base": base,
        "body": (body or "")[:60000],
        "draft": True,
        "maintainer_can_modify": True,
    }

    import httpx

    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code not in (200, 201):
        raise GitHubError(
            f"draft PR creation failed ({resp.status_code}): "
            f"{scrub(resp.text, token)[:400]}"
        )
    data = resp.json()
    return {
        "number": data.get("number"),
        "url": data.get("html_url"),
        "draft": bool(data.get("draft")),
        "state": data.get("state"),
    }


async def find_open_pr(cfg: WorktreeConfig, token: str, head_branch: str) -> Optional[Dict]:
    """Existing open PR for the branch, so a re-publish updates instead of 422s."""
    head = normalize_branch(head_branch)
    if not head or not is_valid_repo_slug(cfg.repo_slug):
        return None
    owner = cfg.repo_slug.split("/", 1)[0]

    import httpx

    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
        resp = await client.get(
            f"{cfg.api_base}/repos/{cfg.repo_slug}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code != 200:
        return None
    try:
        items = resp.json()
    except ValueError:
        return None
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    return {
        "number": first.get("number"),
        "url": first.get("html_url"),
        "draft": bool(first.get("draft")),
        "state": first.get("state"),
    }
