"""Explain exactly why publishing is not working.

"Unauthorized" from GitHub covers a dozen distinct mistakes, and the ones that
actually happen are boring: the Client ID pasted where the App ID goes, the App
ID pasted where the Installation ID goes, a private key that never got mounted
into the container, or an App that was created but never installed on the
repository. This module distinguishes them by asking GitHub.

Nothing here prints or returns credential material. Secrets are reported as a
shape and a length, never a value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent_worktree.config import WorktreeConfig, load_config, publish_blockers
from src.agent_worktree.validation import is_valid_repo_slug

API_TIMEOUT_S = 20.0

OK = "ok"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            out["hint"] = self.hint
        if self.data:
            out["data"] = self.data
        return out


def classify_token(token: str) -> str:
    """Name the shape of a token so a wrong paste is obvious without printing it."""
    if not token:
        return "empty"
    if token.startswith("github_pat_"):
        return "fine-grained personal access token"
    if token.startswith("ghp_"):
        return "classic personal access token"
    if token.startswith("ghs_"):
        return "installation token (short-lived; not a durable credential)"
    if token.startswith(("gho_", "ghu_")):
        return "OAuth/user token"
    if token.startswith("Iv1.") or token.startswith("Iv23"):
        return "GitHub App CLIENT ID — not a token"
    if len(token) == 40 and all(c in "0123456789abcdef" for c in token.lower()):
        return "40-char hex — this is probably an App client SECRET, not a token"
    return "unrecognized shape"


def _check_config(cfg: WorktreeConfig) -> List[Check]:
    checks: List[Check] = []

    checks.append(
        Check(
            "publish flag",
            OK if cfg.publish_enabled else FAIL,
            "ODYSSEUS_AGENT_PUBLISH_ENABLED is on" if cfg.publish_enabled
            else "ODYSSEUS_AGENT_PUBLISH_ENABLED is not set to 1",
            hint="" if cfg.publish_enabled else "Set it to 1 in the container environment.",
        )
    )

    raw_repo = (os.getenv("ODYSSEUS_AGENT_REPO") or "").strip()
    if cfg.repo_slug:
        checks.append(Check("repository", OK, cfg.repo_slug))
    elif raw_repo:
        checks.append(
            Check(
                "repository", FAIL, f"{raw_repo!r} is not a valid owner/name slug",
                hint="Use owner/name with no URL, no .git suffix, e.g. robertsima/odysseus-main.",
            )
        )
    else:
        checks.append(Check("repository", FAIL, "ODYSSEUS_AGENT_REPO is unset"))

    checks.append(
        Check(
            "source repository",
            OK if os.path.exists(os.path.join(cfg.source_repo, ".git")) else FAIL,
            cfg.source_repo,
            hint=""
            if os.path.exists(os.path.join(cfg.source_repo, ".git"))
            else "The container needs a real git checkout here. Set ODYSSEUS_AGENT_SOURCE_REPO.",
        )
    )

    for label, path in (("worktree root", cfg.worktree_root), ("state directory", cfg.state_dir)):
        try:
            os.makedirs(path, exist_ok=True)
            writable = os.access(path, os.W_OK)
        except OSError as exc:
            checks.append(Check(label, FAIL, f"{path}: {exc}",
                                hint="Must be inside a mounted, writable volume."))
            continue
        checks.append(
            Check(label, OK if writable else FAIL, path,
                  hint="" if writable else "Not writable by the container user.")
        )
    return checks


def _check_credentials(cfg: WorktreeConfig) -> List[Check]:
    checks: List[Check] = []
    fallback = (os.getenv(cfg.fallback_token_env) or "").strip()

    if not cfg.has_github_app and not fallback:
        checks.append(
            Check(
                "credential", FAIL, "no GitHub App and no fallback token",
                hint="Set the three ODYSSEUS_GITHUB_APP_* variables, or "
                     f"{cfg.fallback_token_env}.",
            )
        )
        return checks

    if cfg.has_github_app:
        checks.append(Check("credential mode", OK, "GitHub App installation token"))

        if cfg.app_id.isdigit():
            checks.append(Check("app id", OK, cfg.app_id))
        else:
            checks.append(
                Check(
                    "app id", FAIL, f"{cfg.app_id!r} is not numeric",
                    hint="ODYSSEUS_GITHUB_APP_ID is the numeric App ID from the app's "
                         "settings page, not the Client ID (Iv1./Iv23...) and not the app name.",
                )
            )
        if cfg.installation_id.isdigit():
            checks.append(Check("installation id", OK, cfg.installation_id))
        else:
            checks.append(
                Check(
                    "installation id", FAIL, f"{cfg.installation_id!r} is not numeric",
                    hint="The Installation ID is a different number from the App ID. It is "
                         "the last path segment of the app's 'Configure' URL, or read it "
                         "from the installations listed below.",
                )
            )

        checks.append(_check_private_key(cfg))
    else:
        shape = classify_token(fallback)
        status = FAIL if ("not a token" in shape or "SECRET" in shape) else WARN
        checks.append(
            Check(
                "credential mode", status,
                f"fallback {cfg.fallback_token_env} ({shape}, {len(fallback)} chars)",
                hint="A GitHub App installation token is preferred; this one is long-lived."
                if status == WARN
                else "This value is not a personal access token. Check what you pasted.",
            )
        )
    return checks


def _check_private_key(cfg: WorktreeConfig) -> Check:
    path = cfg.private_key_path
    if not os.path.exists(path):
        return Check(
            "private key", FAIL, f"{path} does not exist",
            hint="In a container the key must be inside a mounted volume. Put it under "
                 "the data mount (e.g. /app/data/github-app.pem) and point the variable there.",
        )
    try:
        mode = os.stat(path).st_mode & 0o777
        with open(path, "rb") as fh:
            pem = fh.read()
    except OSError as exc:
        return Check("private key", FAIL, f"cannot read {path}: {exc}",
                     hint="Check ownership and mode for the container user.")

    if b"PUBLIC KEY" in pem or b"CERTIFICATE" in pem:
        return Check("private key", FAIL, "file contains a public key or certificate",
                     hint="Download the app's private key (.pem) from its settings page.")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:
        return Check("private key", FAIL, f"cannot be parsed: {exc}",
                     hint="Must be an unencrypted PEM RSA private key, exactly as GitHub "
                          "issues it. A key converted to PKCS#8 is fine; an encrypted one is not.")
    if not isinstance(key, rsa.RSAPrivateKey):
        return Check("private key", FAIL, "not an RSA key",
                     hint="GitHub App keys are RSA.")
    return Check(
        "private key", OK if mode <= 0o600 else WARN,
        f"RSA {key.key_size}-bit, mode {oct(mode)}",
        hint="" if mode <= 0o600 else "Tighten to 0600; it is readable by others.",
        data={"bits": key.key_size},
    )


async def _github_checks(cfg: WorktreeConfig) -> List[Check]:
    """Live calls to GitHub. These are what distinguish 'wrong id' from 'not installed'."""
    from src.agent_worktree.github import GitHubError, installation_token

    checks: List[Check] = []
    import httpx

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if cfg.has_github_app:
        try:
            from src.agent_worktree.github import _app_jwt

            with open(cfg.private_key_path, "rb") as fh:
                jwt = _app_jwt(cfg.app_id, fh.read())
        except (GitHubError, OSError) as exc:
            checks.append(Check("app identity", FAIL, str(exc)))
            return checks

        async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
            resp = await client.get(
                f"{cfg.api_base}/app", headers={**headers, "Authorization": f"Bearer {jwt}"}
            )
            if resp.status_code == 200:
                app = resp.json()
                checks.append(
                    Check("app identity", OK,
                          f"{app.get('name')} (app id {app.get('id')}, slug {app.get('slug')})")
                )
            elif resp.status_code == 401:
                checks.append(
                    Check("app identity", FAIL, "GitHub rejected the app JWT (401)",
                          hint="The App ID and the private key do not belong together, or the "
                               "container clock is off by more than a minute. Re-check "
                               "ODYSSEUS_GITHUB_APP_ID against the key you downloaded.")
                )
                return checks
            else:
                checks.append(Check("app identity", FAIL,
                                    f"GET /app returned {resp.status_code}"))
                return checks

            resp = await client.get(
                f"{cfg.api_base}/app/installations",
                headers={**headers, "Authorization": f"Bearer {jwt}"},
            )
            installs = resp.json() if resp.status_code == 200 else []
            listed = [
                {
                    "installation_id": item.get("id"),
                    "account": (item.get("account") or {}).get("login"),
                    "repository_selection": item.get("repository_selection"),
                }
                for item in installs
                if isinstance(item, dict)
            ]
            if not listed:
                checks.append(
                    Check("installations", FAIL, "the app is not installed anywhere",
                          hint="Install the app on the repository from its settings page, "
                               "then read the installation id from the resulting URL.")
                )
                return checks
            match = [i for i in listed if str(i["installation_id"]) == cfg.installation_id]
            checks.append(
                Check(
                    "installations",
                    OK if match else FAIL,
                    f"{len(listed)} installation(s): "
                    + ", ".join(f"{i['installation_id']} ({i['account']})" for i in listed),
                    hint="" if match else
                    f"ODYSSEUS_GITHUB_APP_INSTALLATION_ID={cfg.installation_id!r} matches none "
                    "of these. Use one of the ids above.",
                    data={"installations": listed},
                )
            )
            if not match:
                return checks

        try:
            token = await installation_token(cfg)
        except GitHubError as exc:
            checks.append(Check("installation token", FAIL, str(exc)))
            return checks
        checks.append(Check("installation token", OK, "minted successfully"))
    else:
        token = (os.getenv(cfg.fallback_token_env) or "").strip()
        if not token:
            return checks

    if not is_valid_repo_slug(cfg.repo_slug):
        return checks

    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
        resp = await client.get(
            f"{cfg.api_base}/repos/{cfg.repo_slug}",
            headers={**headers, "Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 200:
        repo = resp.json()
        perms = repo.get("permissions") or {}
        can_push = bool(perms.get("push")) or bool(perms.get("maintain")) or bool(perms.get("admin"))
        checks.append(
            Check(
                "repository access", OK if can_push else FAIL,
                f"{cfg.repo_slug}: "
                + (", ".join(k for k, v in perms.items() if v) or "no permissions reported"),
                hint="" if can_push else
                "The credential can see the repository but cannot write to it. Grant the app "
                "Contents: read and write, and Pull requests: read and write.",
                data={"default_branch": repo.get("default_branch"), "private": repo.get("private")},
            )
        )
    elif resp.status_code == 404:
        checks.append(
            Check("repository access", FAIL, f"{cfg.repo_slug} not visible to this credential",
                  hint="Either the slug is wrong, or the app is installed on a different "
                       "account, or its repository access does not include this repo.")
        )
    elif resp.status_code in (401, 403):
        checks.append(
            Check("repository access", FAIL,
                  f"GitHub returned {resp.status_code} for {cfg.repo_slug}",
                  hint="The credential is not authorized for this repository.")
        )
    else:
        checks.append(Check("repository access", FAIL,
                            f"unexpected status {resp.status_code}"))
    return checks


async def run_diagnostics(
    cfg: Optional[WorktreeConfig] = None, *, offline: bool = False
) -> Dict[str, Any]:
    """Every check, in the order an operator should read them."""
    cfg = cfg or load_config()
    checks: List[Check] = []
    checks.extend(_check_config(cfg))
    checks.extend(_check_credentials(cfg))

    creds_ok = all(c.status != FAIL for c in checks if c.name in
                   ("credential", "credential mode", "app id", "installation id", "private key"))
    if offline:
        checks.append(Check("github", SKIP, "network checks skipped (--offline)"))
    elif not creds_ok:
        checks.append(Check("github", SKIP, "skipped: fix the credential problems above first"))
    else:
        try:
            checks.extend(await _github_checks(cfg))
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not crash
            checks.append(Check("github", FAIL, f"live check failed: {exc}"))

    failures = [c for c in checks if c.status == FAIL]
    return {
        "ok": not failures,
        "blockers": publish_blockers(cfg),
        "checks": [c.as_dict() for c in checks],
        "failures": len(failures),
    }
