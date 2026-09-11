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


# ── Pull request feedback (Workbench) ────────────────────────────────────────
#
# The same App/PAT credential that opens draft PRs can read them back and post
# reviews, which is what lets the operator review an agent's change from the
# Odysseus UI instead of switching to GitHub. Everything here is read-only
# except the two explicit write calls at the bottom, and every response is
# reduced to the fields the UI shows so a PR body cannot smuggle anything
# oversized through.

MAX_PATCH_CHARS = 20_000
MAX_PR_DIFF_CHARS = 400_000
REVIEW_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")


def _api_headers(token: str, accept: str = "application/vnd.github+json") -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def pr_access_blockers(cfg: WorktreeConfig) -> list:
    """Why PR feedback is unavailable (empty list = usable). Unlike publishing,
    reading and reviewing PRs does not need the publish flag."""
    import os

    blockers = []
    if not cfg.repo_slug:
        blockers.append("ODYSSEUS_AGENT_REPO is unset or not a valid owner/name slug")
    if not cfg.has_any_credential:
        blockers.append(
            "no GitHub credential configured (GitHub App triple, or "
            f"{cfg.fallback_token_env} as a fallback)"
        )
    elif cfg.has_github_app and not os.path.isfile(cfg.private_key_path):
        blockers.append("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH does not point at a file")
    return blockers


async def _api(cfg: WorktreeConfig, token: str, method: str, path: str, *,
               params: Optional[Dict] = None, json_body: Optional[Dict] = None,
               accept: str = "application/vnd.github+json"):
    if not is_valid_repo_slug(cfg.repo_slug):
        raise GitHubError("repository slug is not valid")
    import httpx

    url = f"{cfg.api_base}{path}"
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
        resp = await client.request(method, url, params=params, json=json_body,
                                    headers=_api_headers(token, accept))
    if resp.status_code >= 400:
        raise GitHubError(
            f"GitHub {method} {path.split('?', 1)[0]} failed ({resp.status_code}): "
            f"{scrub(resp.text, token)[:300]}"
        )
    if accept != "application/vnd.github+json":
        return resp.text
    try:
        return resp.json()
    except ValueError:
        raise GitHubError("GitHub returned a non-JSON response")


def _user(obj) -> str:
    return str((obj or {}).get("login") or "") if isinstance(obj, dict) else ""


def _pr_row(pr: Dict) -> Dict:
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "state": pr.get("state"),
        "draft": bool(pr.get("draft")),
        "merged": bool(pr.get("merged_at")),
        "url": pr.get("html_url"),
        "user": _user(pr.get("user")),
        "head": {"ref": head.get("ref"), "sha": head.get("sha")},
        "base": {"ref": base.get("ref")},
        "labels": [l.get("name") for l in (pr.get("labels") or []) if isinstance(l, dict) and l.get("name")],
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "merged_at": pr.get("merged_at"),
        "body_excerpt": str(pr.get("body") or "")[:400],
    }


async def list_pull_requests(cfg: WorktreeConfig, token: str, *, state: str = "open", limit: int = 30) -> list:
    state = state if state in ("open", "closed", "all") else "open"
    items = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/pulls",
                       params={"state": state, "per_page": max(1, min(int(limit or 30), 100)), "sort": "updated",
                               "direction": "desc"})
    return [_pr_row(pr) for pr in (items if isinstance(items, list) else []) if isinstance(pr, dict)]


async def get_pull_request(cfg: WorktreeConfig, token: str, number: int) -> Dict:
    pr = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/pulls/{int(number)}")
    row = _pr_row(pr)
    row.update({
        "body": str(pr.get("body") or "")[:60000],
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "commits": pr.get("commits"),
        "comments": pr.get("comments"),
        "review_comments": pr.get("review_comments"),
    })
    return row


async def pull_request_files(cfg: WorktreeConfig, token: str, number: int) -> list:
    items = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/pulls/{int(number)}/files",
                       params={"per_page": 100})
    out = []
    for f in (items if isinstance(items, list) else []):
        if not isinstance(f, dict):
            continue
        patch = str(f.get("patch") or "")
        out.append({
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "patch": patch[:MAX_PATCH_CHARS],
            "patch_truncated": len(patch) > MAX_PATCH_CHARS,
            "previous_filename": f.get("previous_filename"),
        })
    return out


async def pull_request_reviews(cfg: WorktreeConfig, token: str, number: int) -> list:
    items = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/pulls/{int(number)}/reviews",
                       params={"per_page": 100})
    return [{
        "id": r.get("id"), "user": _user(r.get("user")), "state": r.get("state"),
        "body": str(r.get("body") or "")[:8000], "submitted_at": r.get("submitted_at"), "url": r.get("html_url"),
    } for r in (items if isinstance(items, list) else []) if isinstance(r, dict)]


async def pull_request_review_comments(cfg: WorktreeConfig, token: str, number: int) -> list:
    items = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/pulls/{int(number)}/comments",
                       params={"per_page": 100})
    return [{
        "id": c.get("id"), "user": _user(c.get("user")), "body": str(c.get("body") or "")[:8000],
        "path": c.get("path"), "line": c.get("line") or c.get("original_line"), "side": c.get("side"),
        "created_at": c.get("created_at"), "url": c.get("html_url"), "in_reply_to_id": c.get("in_reply_to_id"),
    } for c in (items if isinstance(items, list) else []) if isinstance(c, dict)]


async def pull_request_comments(cfg: WorktreeConfig, token: str, number: int) -> list:
    items = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/issues/{int(number)}/comments",
                       params={"per_page": 100})
    return [{
        "id": c.get("id"), "user": _user(c.get("user")), "body": str(c.get("body") or "")[:8000],
        "created_at": c.get("created_at"), "url": c.get("html_url"),
    } for c in (items if isinstance(items, list) else []) if isinstance(c, dict)]


async def pull_request_checks(cfg: WorktreeConfig, token: str, head_sha: str) -> list:
    if not head_sha or not all(ch in "0123456789abcdefABCDEF" for ch in head_sha):
        return []
    data = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/commits/{head_sha}/check-runs",
                      params={"per_page": 100})
    runs = data.get("check_runs") if isinstance(data, dict) else None
    return [{
        "name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"),
        "url": r.get("html_url"), "app": _user(r.get("app")) or ((r.get("app") or {}).get("slug") if isinstance(r.get("app"), dict) else None),
        "started_at": r.get("started_at"), "completed_at": r.get("completed_at"),
    } for r in (runs or []) if isinstance(r, dict)]


async def pull_request_diff(cfg: WorktreeConfig, token: str, number: int) -> Dict:
    text = await _api(cfg, token, "GET", f"/repos/{cfg.repo_slug}/pulls/{int(number)}",
                      accept="application/vnd.github.diff")
    text = str(text or "")
    return {"diff": text[:MAX_PR_DIFF_CHARS], "truncated": len(text) > MAX_PR_DIFF_CHARS}


async def create_issue_comment(cfg: WorktreeConfig, token: str, number: int, body: str) -> Dict:
    text = (body or "").strip()
    if not text:
        raise GitHubError("a comment body is required")
    data = await _api(cfg, token, "POST", f"/repos/{cfg.repo_slug}/issues/{int(number)}/comments",
                      json_body={"body": text[:60000]})
    return {"id": data.get("id"), "url": data.get("html_url")}


async def create_review(cfg: WorktreeConfig, token: str, number: int, *, event: str,
                        body: str = "", comments: Optional[list] = None) -> Dict:
    """Submit a review. ``event`` is COMMENT, APPROVE or REQUEST_CHANGES;
    ``comments`` are inline ``{path, line, body[, side]}`` entries."""
    event = (event or "COMMENT").upper()
    if event not in REVIEW_EVENTS:
        raise GitHubError(f"review event must be one of {', '.join(REVIEW_EVENTS)}")
    inline = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        path = str(c.get("path") or "").strip()
        text = str(c.get("body") or "").strip()
        try:
            line = int(c.get("line"))
        except (TypeError, ValueError):
            line = 0
        if not path or not text or line <= 0:
            continue
        entry = {"path": path[:1000], "line": line, "body": text[:20000]}
        side = str(c.get("side") or "").upper()
        if side in ("LEFT", "RIGHT"):
            entry["side"] = side
        inline.append(entry)
    text = (body or "").strip()
    if event != "APPROVE" and not text and not inline:
        raise GitHubError("a review body or at least one inline comment is required")
    payload: Dict = {"event": event, "body": text[:60000]}
    if inline:
        payload["comments"] = inline[:100]
    data = await _api(cfg, token, "POST", f"/repos/{cfg.repo_slug}/pulls/{int(number)}/reviews", json_body=payload)
    return {"id": data.get("id"), "state": data.get("state"), "url": data.get("html_url")}
