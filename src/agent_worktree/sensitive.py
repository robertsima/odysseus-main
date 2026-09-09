"""Classify changed paths that need a second, explicit human acknowledgement.

A normal code change gets one approval. A change that touches CI, container or
deployment definitions, authentication, secrets, or the MCP/tool permission
surface gets a second one: those files decide what runs, as whom, and with which
capabilities, so a plausible-looking edit there is the highest-value target for
prompt injection. The operator has to say `--allow-sensitive` and is shown the
exact file list before doing so.

Matching is on the repo-relative POSIX path, case-folded, and rules are prefix /
suffix / substring tests rather than globs so a path cannot slip past by
containing glob metacharacters.
"""

from __future__ import annotations

import hashlib
import posixpath
from typing import Dict, Iterable, List, Tuple

WORKFLOWS = "workflows"
DOCKER = "docker"
DEPLOYMENT = "deployment"
AUTH = "auth"
SECRETS = "secrets"
MCP_PERMISSIONS = "mcp_permissions"

CATEGORY_ORDER: Tuple[str, ...] = (
    WORKFLOWS,
    DOCKER,
    DEPLOYMENT,
    AUTH,
    SECRETS,
    MCP_PERMISSIONS,
)

CATEGORY_LABELS: Dict[str, str] = {
    WORKFLOWS: "CI workflows and actions",
    DOCKER: "container build and compose definitions",
    DEPLOYMENT: "deployment, service and dependency manifests",
    AUTH: "authentication and session handling",
    SECRETS: "secrets, keys and credential storage",
    MCP_PERMISSIONS: "MCP servers and tool permission policy",
}

# (category, kind, needle). kind is one of "prefix", "suffix", "contains",
# "basename", "basename_prefix".
_RULES: Tuple[Tuple[str, str, str], ...] = (
    (WORKFLOWS, "prefix", ".github/"),
    (WORKFLOWS, "basename", ".gitlab-ci.yml"),
    (WORKFLOWS, "basename", "jenkinsfile"),

    (DOCKER, "prefix", "docker/"),
    (DOCKER, "basename_prefix", "dockerfile"),
    (DOCKER, "basename_prefix", "docker-compose"),
    (DOCKER, "basename", ".dockerignore"),
    (DOCKER, "contains", "/dockerfile"),

    (DEPLOYMENT, "suffix", ".service"),
    (DEPLOYMENT, "prefix", "deploy/"),
    (DEPLOYMENT, "prefix", "k8s/"),
    (DEPLOYMENT, "prefix", "helm/"),
    (DEPLOYMENT, "prefix", "charts/"),
    (DEPLOYMENT, "basename", "install-service.sh"),
    (DEPLOYMENT, "basename", "setup.py"),
    (DEPLOYMENT, "basename", "pyproject.toml"),
    (DEPLOYMENT, "basename", "package.json"),
    (DEPLOYMENT, "basename", "package-lock.json"),
    (DEPLOYMENT, "basename_prefix", "requirements"),
    (DEPLOYMENT, "basename_prefix", "start-"),
    (DEPLOYMENT, "basename_prefix", "launch-"),
    (DEPLOYMENT, "basename_prefix", "build-"),
    (DEPLOYMENT, "suffix", ".spec"),

    (AUTH, "basename", "auth.py"),
    (AUTH, "basename", "middleware.py"),
    (AUTH, "basename", "api_key_manager.py"),
    (AUTH, "basename", "auth_helpers.py"),
    (AUTH, "basename", "auth_routes.py"),
    (AUTH, "basename", "api_token_routes.py"),
    (AUTH, "basename", "device_flow.py"),
    (AUTH, "basename", "session_manager.py"),
    (AUTH, "contains", "/auth/"),
    (AUTH, "contains", "oauth"),

    (SECRETS, "basename_prefix", ".env"),
    (SECRETS, "suffix", ".pem"),
    (SECRETS, "suffix", ".key"),
    (SECRETS, "suffix", ".p12"),
    (SECRETS, "suffix", ".pfx"),
    (SECRETS, "suffix", ".jks"),
    (SECRETS, "contains", "secrets/"),
    (SECRETS, "contains", "credential"),
    (SECRETS, "basename", "secret_storage.py"),
    (SECRETS, "basename", "vault_routes.py"),
    (SECRETS, "basename", ".npmrc"),
    (SECRETS, "basename", ".netrc"),

    (MCP_PERMISSIONS, "prefix", "mcp_servers/"),
    (MCP_PERMISSIONS, "prefix", ".claude/"),
    (MCP_PERMISSIONS, "basename", ".mcp.json"),
    (MCP_PERMISSIONS, "basename", "mcp_manager.py"),
    (MCP_PERMISSIONS, "basename", "mcp_routes.py"),
    (MCP_PERMISSIONS, "basename", "mcp_oauth.py"),
    (MCP_PERMISSIONS, "basename", "tool_security.py"),
    (MCP_PERMISSIONS, "basename", "tool_policy.py"),
    (MCP_PERMISSIONS, "contains", "permissions"),
    (MCP_PERMISSIONS, "contains", "lotus-mcp/config/"),
)


def normalize_path(raw: str) -> str:
    """Repo-relative POSIX path, case-folded, for matching only.

    Only a leading "./" or "/" is stripped. A blanket ``lstrip("./")`` would eat
    the dot of ``.github/workflows/ci.yml`` and quietly declassify every CI file.
    """
    text = (raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    return posixpath.normpath(text).casefold() if text else ""


def categories_for(path: str) -> List[str]:
    """Sensitive categories a single path falls into (possibly none)."""
    norm = normalize_path(path)
    if not norm or norm == ".":
        return []
    base = posixpath.basename(norm)
    hits: List[str] = []
    for category, kind, needle in _RULES:
        if category in hits:
            continue
        matched = (
            (kind == "prefix" and norm.startswith(needle))
            or (kind == "suffix" and norm.endswith(needle))
            or (kind == "contains" and needle in norm)
            or (kind == "basename" and base == needle)
            or (kind == "basename_prefix" and base.startswith(needle))
        )
        if matched:
            hits.append(category)
    return [c for c in CATEGORY_ORDER if c in hits]


def classify(paths: Iterable[str]) -> Dict[str, List[str]]:
    """Map category -> sorted paths, for every sensitive path in `paths`."""
    found: Dict[str, List[str]] = {}
    for path in paths:
        for category in categories_for(path):
            found.setdefault(category, []).append(path)
    return {
        category: sorted(set(found[category]))
        for category in CATEGORY_ORDER
        if category in found
    }


def digest(findings: Dict[str, List[str]]) -> str:
    """Stable digest of a classification.

    An approval is bound to this value, so re-classifying a *different* set of
    sensitive files after the operator looked at the list invalidates the grant
    even in the unlikely event the commit SHA were to match.
    """
    hasher = hashlib.sha256()
    for category in CATEGORY_ORDER:
        for path in findings.get(category, []):
            hasher.update(f"{category}:{normalize_path(path)}\n".encode("utf-8"))
    return hasher.hexdigest()


def summarize(findings: Dict[str, List[str]]) -> str:
    """One-line-per-category human summary for the approval prompt."""
    if not findings:
        return "no sensitive paths changed"
    lines = []
    for category, paths in findings.items():
        label = CATEGORY_LABELS.get(category, category)
        lines.append(f"{category} ({label}): " + ", ".join(paths))
    return "\n".join(lines)
