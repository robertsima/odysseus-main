"""Approval requests and single-use, binding approval grants.

A boolean "approved" flag is not approval: it survives the change it was meant
to bless, and anything that can set it once can set it again. An approval here
is a random secret the operator receives out-of-band, hashed at rest, and bound
to five facts at the moment it is granted:

    repository slug, branch, head commit SHA, changed-file digest,
    and the digest of the sensitive paths in that change

It also carries a short expiry and is consumed exactly once. Re-running the
publish, amending the commit, adding a file, or pointing at a different repo all
invalidate it, because each changes one of the bound facts.

The plaintext code exists only in the operator's terminal. Only
``sha256(salt || code)`` is persisted, so *reading* the state directory is not
enough to publish.

*Writing* it would be, which is why the grant is additionally authenticated: a
record carries an HMAC over every field the decision depends on, keyed by a file
that the agent's file tools refuse to open (the state directory is on the
sensitive-path deny list in tool_execution). A hand-written or edited grant fails
verification and is treated as no approval at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from typing import Dict, List, Optional, Tuple

from core.atomic_io import atomic_write_json
from src.agent_worktree.config import WorktreeConfig, load_config
from src.agent_worktree.locking import file_lock

logger = logging.getLogger(__name__)

REQUEST_TTL_S = 24 * 3600  # a request the operator never looks at goes stale
CODE_BYTES = 32

def _now() -> float:
    """Current wall-clock time.

    A single indirection so expiry behaviour is testable without moving the
    process clock or sleeping through a real TTL.
    """
    return time.time()


STATUS_PENDING = "pending"
STATUS_GRANTED = "granted"
STATUS_USED = "used"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"


class ApprovalError(RuntimeError):
    """Approval is missing, expired, mismatched, or already spent."""


def _requests_dir(cfg: WorktreeConfig) -> str:
    return os.path.join(cfg.state_dir, "requests")


def _lock_path(cfg: WorktreeConfig) -> str:
    return os.path.join(cfg.state_dir, "locks", "approvals.lock")


def _record_path(cfg: WorktreeConfig, request_id: str) -> str:
    # request ids are generated here, never supplied by a caller, but the guard
    # keeps a future caller from turning an id into a path traversal.
    if not request_id or not all(c.isalnum() or c == "-" for c in request_id):
        raise ApprovalError("invalid approval request id")
    return os.path.join(_requests_dir(cfg), f"{request_id}.json")


def _hash_code(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


# Fields a grant's authenticity depends on. Any change to any of them must
# invalidate the MAC, so the list is explicit rather than "whatever is in the
# dict" — a future field added without thought then fails loudly here instead of
# silently falling outside the protection.
_MAC_FIELDS = ("expires_at", "allow_sensitive", "code_hash", "salt")
_BOUND_FIELDS = ("repo", "branch", "head_sha", "files_digest", "sensitive_digest")


def _key_path(cfg: WorktreeConfig) -> str:
    return os.path.join(cfg.state_dir, ".approval_key")


def _mac_key(cfg: WorktreeConfig) -> bytes:
    """Per-installation MAC key, created on first use at mode 0600.

    Kept inside the state directory, which the agent's file tools deny-list, so
    the agent can neither read the key nor rewrite a record's MAC. An operator
    who deletes it invalidates every outstanding grant — the safe direction.
    """
    path = _key_path(cfg)
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode("ascii")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _grant_mac(cfg: WorktreeConfig, request_id: str, grant_rec: Dict) -> str:
    """MAC over the request id, the grant's decision fields, and its bindings."""
    bound = grant_rec.get("bound") or {}
    payload = {
        "id": request_id,
        "grant": {field: grant_rec.get(field) for field in _MAC_FIELDS},
        "bound": {field: bound.get(field) for field in _BOUND_FIELDS},
    }
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_mac_key(cfg), message, hashlib.sha256).hexdigest()


def _verified_grant(cfg: WorktreeConfig, record: Dict) -> Dict:
    """Return the record's grant, or raise if it is absent, malformed or forged.

    Fails closed on every incomplete shape. In particular a missing or empty
    ``bound`` is rejected rather than skipped: iterating an empty mapping would
    silently turn "this exact commit" into "any commit".
    """
    grant_rec = record.get("grant")
    if not isinstance(grant_rec, dict):
        raise ApprovalError("approval record carries no grant")
    for field in _MAC_FIELDS:
        if grant_rec.get(field) in (None, ""):
            raise ApprovalError("approval record is incomplete")
    bound = grant_rec.get("bound")
    if not isinstance(bound, dict) or set(bound) != set(_BOUND_FIELDS):
        raise ApprovalError("approval record is not bound to a specific change")
    for field in _BOUND_FIELDS:
        if not isinstance(bound.get(field), str) or not bound[field]:
            raise ApprovalError("approval record is not bound to a specific change")
    expected = str(grant_rec.get("mac") or "")
    if not expected or not hmac.compare_digest(
        expected, _grant_mac(cfg, str(record.get("id") or ""), grant_rec)
    ):
        logger.warning(
            "agent worktree: approval record %s failed integrity check",
            record.get("id"),
        )
        raise ApprovalError(
            "approval record failed its integrity check; it was not produced by "
            "the approval command"
        )
    return grant_rec


def _load(cfg: WorktreeConfig, request_id: str) -> Dict:
    path = _record_path(cfg, request_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ApprovalError(f"no approval request {request_id}")
    except (OSError, ValueError) as exc:
        raise ApprovalError(f"approval request {request_id} is unreadable: {exc}")
    if not isinstance(data, dict):
        raise ApprovalError(f"approval request {request_id} is malformed")
    return data


def _save(cfg: WorktreeConfig, record: Dict) -> None:
    atomic_write_json(_record_path(cfg, record["id"]), record, indent=2)


def _effective_status(
    record: Dict, now: Optional[float] = None, cfg: Optional[WorktreeConfig] = None
) -> str:
    """Status with expiry applied, without mutating the stored record.

    When `cfg` is supplied, a "granted" status is only believed if the grant
    itself verifies. Reporting `granted` for a record whose grant is forged or
    incomplete would be a misleading answer everywhere it is displayed, and the
    consuming path would have to reject it a second time anyway.
    """
    now = _now() if now is None else now
    status = str(record.get("status") or STATUS_PENDING)
    if status in (STATUS_USED, STATUS_REVOKED):
        return status
    grant = record.get("grant") or {}
    if status == STATUS_GRANTED:
        if cfg is not None:
            try:
                _verified_grant(cfg, record)
            except ApprovalError:
                return STATUS_PENDING
        if float(grant.get("expires_at") or 0) <= now:
            return STATUS_EXPIRED
        return STATUS_GRANTED
    if float(record.get("expires_at") or 0) <= now:
        return STATUS_EXPIRED
    return status


def public_view(record: Dict, cfg: Optional[WorktreeConfig] = None) -> Dict:
    """Record fields safe to show the agent, an operator, or a log.

    The grant sub-object is reduced to metadata: the code hash and salt never
    leave this module, so a leaked transcript cannot be replayed offline.
    """
    grant = record.get("grant") or {}
    return {
        "id": record.get("id"),
        "status": _effective_status(record, cfg=cfg),
        "repo": record.get("repo"),
        "branch": record.get("branch"),
        "head_sha": record.get("head_sha"),
        "base_branch": record.get("base_branch"),
        "title": record.get("title"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "changed_files": record.get("changed_files") or [],
        "sensitive": record.get("sensitive") or {},
        "remote_state": record.get("remote_state"),
        "requested_by": record.get("requested_by"),
        "grant": (
            {
                "granted_at": grant.get("granted_at"),
                "expires_at": grant.get("expires_at"),
                "allow_sensitive": bool(grant.get("allow_sensitive")),
                "granted_by": grant.get("granted_by"),
                "used_at": grant.get("used_at"),
            }
            if grant
            else None
        ),
        "published": record.get("published"),
    }


def create_request(
    *,
    repo: str,
    branch: str,
    head_sha: str,
    base_branch: str,
    title: str,
    body: str,
    changed_files: List[str],
    files_digest: str,
    sensitive: Dict[str, List[str]],
    sensitive_digest: str,
    remote_state: str = "unknown",
    requested_by: Optional[str] = None,
    cfg: Optional[WorktreeConfig] = None,
) -> Dict:
    cfg = cfg or load_config()
    now = _now()
    record = {
        "id": uuid.uuid4().hex,
        "status": STATUS_PENDING,
        "created_at": now,
        "expires_at": now + REQUEST_TTL_S,
        "repo": repo,
        "branch": branch,
        "head_sha": head_sha,
        "base_branch": base_branch,
        "title": title,
        "body": body,
        "changed_files": list(changed_files),
        "files_digest": files_digest,
        "sensitive": sensitive,
        "sensitive_digest": sensitive_digest,
        # Remote branch state observed when the change was frozen: a SHA, the
        # literal "absent", or "unknown" when the lookup failed. publish() binds
        # to this so a branch that moved underneath the approval is rejected.
        "remote_state": remote_state,
        "requested_by": requested_by,
        "grant": None,
        "published": None,
    }
    with file_lock(_lock_path(cfg)):
        _save(cfg, record)
    logger.info(
        "agent worktree: approval requested id=%s branch=%s sha=%s sensitive=%s",
        record["id"], branch, head_sha[:12], sorted(sensitive.keys()),
    )
    return record


def list_requests(cfg: Optional[WorktreeConfig] = None) -> List[Dict]:
    cfg = cfg or load_config()
    directory = _requests_dir(cfg)
    out: List[Dict] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            out.append(public_view(_load(cfg, name[: -len(".json")]), cfg=cfg))
        except ApprovalError:
            continue
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out


def get_request(request_id: str, cfg: Optional[WorktreeConfig] = None) -> Dict:
    cfg = cfg or load_config()
    return public_view(_load(cfg, request_id), cfg=cfg)


def grant(
    request_id: str,
    *,
    allow_sensitive: bool = False,
    granted_by: Optional[str] = None,
    cfg: Optional[WorktreeConfig] = None,
) -> Tuple[str, Dict]:
    """Approve a pending request. Returns (plaintext code, public record).

    The caller is the operator CLI, which prints the code once. This function
    refuses a sensitive change unless the operator passed ``allow_sensitive``
    after seeing the file list — the second acknowledgement.
    """
    cfg = cfg or load_config()
    with file_lock(_lock_path(cfg)):
        record = _load(cfg, request_id)
        status = _effective_status(record, cfg=cfg)
        if status == STATUS_USED:
            raise ApprovalError("this request was already published")
        if status == STATUS_REVOKED:
            raise ApprovalError("this request was revoked")
        if status == STATUS_EXPIRED:
            raise ApprovalError("this request has expired; ask the agent for a new one")
        if record.get("sensitive") and not allow_sensitive:
            raise ApprovalError(
                "change touches sensitive paths; re-run with --allow-sensitive "
                "after reviewing them"
            )
        code = secrets.token_urlsafe(CODE_BYTES)
        salt = secrets.token_hex(16)
        now = _now()
        bound = {field: record.get(field) for field in _BOUND_FIELDS}
        for field, value in bound.items():
            if not isinstance(value, str) or not value:
                raise ApprovalError(f"request is missing {field}; cannot approve it")
        grant_rec = {
            "salt": salt,
            "code_hash": _hash_code(salt, code),
            "granted_at": now,
            "expires_at": now + cfg.approval_ttl_s,
            "allow_sensitive": bool(allow_sensitive),
            "granted_by": granted_by,
            "used_at": None,
            # Re-bound at grant time so a record edited between request and
            # grant cannot smuggle a different change past the operator.
            "bound": bound,
        }
        # Authenticates the grant against tampering by anything that can write
        # the state directory. Computed last, over the finished object.
        grant_rec["mac"] = _grant_mac(cfg, request_id, grant_rec)
        record["status"] = STATUS_GRANTED
        record["grant"] = grant_rec
        _save(cfg, record)
    logger.info(
        "agent worktree: approval granted id=%s ttl=%ss allow_sensitive=%s",
        request_id, cfg.approval_ttl_s, bool(allow_sensitive),
    )
    return code, public_view(record, cfg=cfg)


def revoke(request_id: str, cfg: Optional[WorktreeConfig] = None) -> Dict:
    cfg = cfg or load_config()
    with file_lock(_lock_path(cfg)):
        record = _load(cfg, request_id)
        if _effective_status(record, cfg=cfg) == STATUS_USED:
            raise ApprovalError("this request was already published")
        record["status"] = STATUS_REVOKED
        record["grant"] = None
        _save(cfg, record)
    logger.info("agent worktree: approval revoked id=%s", request_id)
    return public_view(record, cfg=cfg)


def consume(
    request_id: str,
    code: str,
    *,
    repo: str,
    branch: str,
    head_sha: str,
    files_digest: str,
    sensitive_digest: str,
    has_sensitive: bool,
    cfg: Optional[WorktreeConfig] = None,
) -> Dict:
    """Spend an approval, or raise.

    The record is marked used *before* the push runs. A crash mid-push therefore
    costs a second approval rather than leaving a live grant behind — the safe
    direction for a capability that pushes code.
    """
    cfg = cfg or load_config()
    if not isinstance(code, str) or not code.strip():
        raise ApprovalError("approval code required")
    with file_lock(_lock_path(cfg)):
        record = _load(cfg, request_id)
        status = _effective_status(record, cfg=cfg)
        if status == STATUS_USED:
            raise ApprovalError("approval already used; request a new one")
        if status == STATUS_REVOKED:
            raise ApprovalError("approval was revoked")
        if status == STATUS_EXPIRED:
            raise ApprovalError("approval expired; request a new one")
        if status != STATUS_GRANTED:
            raise ApprovalError("this request has not been approved by a human yet")

        # Raises unless the grant is complete, fully bound, and authentic.
        grant_rec = _verified_grant(cfg, record)
        if not hmac.compare_digest(
            str(grant_rec["code_hash"]), _hash_code(str(grant_rec["salt"]), code.strip())
        ):
            logger.warning("agent worktree: bad approval code for id=%s", request_id)
            raise ApprovalError("approval code does not match")

        bound = grant_rec["bound"]
        actual = {
            "repo": repo,
            "branch": branch,
            "head_sha": head_sha,
            "files_digest": files_digest,
            "sensitive_digest": sensitive_digest,
        }
        # Iterate the expected field list, not the record's keys: a record with
        # fields removed must fail, not skip the checks it no longer contains.
        for key in _BOUND_FIELDS:
            if not hmac.compare_digest(str(actual.get(key) or ""), str(bound.get(key) or "")):
                raise ApprovalError(
                    f"approval is bound to a different {key}; the change moved "
                    "since it was approved — request a new approval"
                )
        if has_sensitive and not grant_rec.get("allow_sensitive"):
            raise ApprovalError(
                "change touches sensitive paths but the approval did not "
                "acknowledge them"
            )

        grant_rec["used_at"] = _now()
        record["grant"] = grant_rec
        record["status"] = STATUS_USED
        _save(cfg, record)
    logger.info("agent worktree: approval consumed id=%s sha=%s", request_id, head_sha[:12])
    return record


def mark_published(request_id: str, details: Dict, cfg: Optional[WorktreeConfig] = None) -> Dict:
    cfg = cfg or load_config()
    with file_lock(_lock_path(cfg)):
        record = _load(cfg, request_id)
        record["published"] = details
        _save(cfg, record)
    return public_view(record, cfg=cfg)


def purge_expired(cfg: Optional[WorktreeConfig] = None, *, keep_days: int = 30) -> int:
    """Delete request records that are long finished. Returns the count."""
    cfg = cfg or load_config()
    cutoff = _now() - keep_days * 86400
    removed = 0
    with file_lock(_lock_path(cfg)):
        try:
            names = os.listdir(_requests_dir(cfg))
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(_requests_dir(cfg), name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                created = float(data.get("created_at") or 0)
            except (OSError, ValueError, TypeError):
                created = 0
            if created < cutoff:
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    pass
    return removed
