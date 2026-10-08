"""ChatGPT subscription / Codex backend OAuth helpers.

This provider is intentionally separate from OpenAI API-key endpoints. It uses
OpenAI account OAuth device authorization, stores refresh tokens server-side,
and resolves a fresh bearer token at request time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL = (
    os.getenv("CHATGPT_SUBSCRIPTION_BASE_URL", "").strip().rstrip("/")
    or "https://chatgpt.com/backend-api/codex"
)
CHATGPT_SUBSCRIPTION_PROVIDER = "chatgpt-subscription"
CHATGPT_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_OAUTH_ISSUER = "https://auth.openai.com"
CHATGPT_OAUTH_REDIRECT_URI = f"{CHATGPT_OAUTH_ISSUER}/deviceauth/callback"
# Refresh this long before ``exp``. A worker resolves its bearer once at launch
# and then streams for minutes, so a token with only a couple of minutes left
# at launch can lapse mid-run; five minutes covers a normal round with margin.
CHATGPT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 300
# After a 401, a refresh this recent means the rejected token is already brand
# new: minting another one cannot help and only rotates the refresh token again.
CHATGPT_UNAUTHORIZED_REFRESH_COOLDOWN_SECONDS = 30
_AUTH_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_AUTH_REFRESH_LOCKS_GUARD = threading.Lock()
# auth_id -> time.monotonic() of this process's last successful refresh.
_LAST_REFRESH_AT: dict[str, float] = {}
# sha256(old access token) -> replacement access token, recorded on every
# refresh. Headers snapshotted before a refresh (a worker's session headers, a
# chat turn's resolved bearer) carry the old token; this lets the request path
# swap it without a database round trip. Keyed by digest so superseded tokens
# are never retained; bounded so it cannot grow without limit.
_SUPERSEDED_TOKENS: "OrderedDict[str, str]" = OrderedDict()
_SUPERSEDED_TOKENS_MAX = 64
_SUPERSEDED_GUARD = threading.Lock()


def _token_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _auth_ref(auth_id: str) -> str:
    """Short, non-secret handle for log lines."""
    return str(auth_id or "?")[:8]


def _expires_in(token: str) -> str:
    """Seconds until ``token`` expires, for logs; never the token itself."""
    try:
        exp = int(_decode_jwt_payload(token).get("exp") or 0)
    except Exception:
        return "unknown"
    if not exp:
        return "unknown"
    return str(exp - int(time.time()))


def _record_superseded(old_token: str, new_token: str) -> None:
    if not old_token or not new_token or old_token == new_token:
        return
    key = _token_digest(old_token)
    with _SUPERSEDED_GUARD:
        _SUPERSEDED_TOKENS[key] = new_token
        _SUPERSEDED_TOKENS.move_to_end(key)
        while len(_SUPERSEDED_TOKENS) > _SUPERSEDED_TOKENS_MAX:
            _SUPERSEDED_TOKENS.popitem(last=False)


def superseded_access_token(access_token: Optional[str]) -> Optional[str]:
    """The newest token that replaced ``access_token`` in this process, if any.

    Follows the chain (a token refreshed twice since it was snapshotted) and
    returns ``None`` when the token was never superseded.
    """
    if not access_token:
        return None
    current = access_token
    with _SUPERSEDED_GUARD:
        for _ in range(_SUPERSEDED_TOKENS_MAX):
            nxt = _SUPERSEDED_TOKENS.get(_token_digest(current))
            if not nxt or nxt == current:
                break
            current = nxt
    return current if current != access_token else None


def _database_handles():
    from core.database import ProviderAuthSession, SessionLocal, utcnow_naive
    return ProviderAuthSession, SessionLocal, utcnow_naive


def _refresh_lock_for(auth_id: str) -> threading.Lock:
    with _AUTH_REFRESH_LOCKS_GUARD:
        lock = _AUTH_REFRESH_LOCKS.get(auth_id)
        if lock is None:
            lock = threading.Lock()
            _AUTH_REFRESH_LOCKS[auth_id] = lock
        return lock


class ChatGPTSubscriptionError(RuntimeError):
    """Base error for ChatGPT subscription provider failures."""


class ChatGPTSubscriptionReauthRequired(ChatGPTSubscriptionError):
    """Stored OAuth credentials are invalid or expired beyond refresh."""


class ChatGPTSubscriptionRateLimited(ChatGPTSubscriptionError):
    """Upstream quota/rate limit; reconnecting will not fix it."""


class ChatGPTSubscriptionAuthNotFound(ChatGPTSubscriptionError):
    """No matching owner-scoped auth session exists."""


def is_chatgpt_subscription_base(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").rstrip("/")
    except Exception:
        return False
    return host == "chatgpt.com" and (
        path == "/backend-api/codex" or path.startswith("/backend-api/codex/")
    )


def chatgpt_headers(access_token: Optional[str]) -> Dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/codex",
        "User-Agent": "Agamemnon ChatGPT Subscription",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def fetch_available_models(access_token: str, timeout: float = 10.0) -> list[str]:
    if not access_token:
        return []
    try:
        response = httpx.get(
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
            headers=chatgpt_headers(access_token),
            timeout=timeout,
        )
        if response.status_code != 200:
            return []
        data = response.json()
    except Exception:
        return []
    entries = data.get("models", []) if isinstance(data, dict) else []
    sortable: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug.strip()))
    sortable.sort(key=lambda item: (item[0], item[1]))
    ordered: list[str] = []
    seen: set[str] = set()
    for _, slug in sortable:
        if slug not in seen:
            ordered.append(slug)
            seen.add(slug)
    return ordered


def _raise_for_oauth_response(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    code = ""
    message = f"ChatGPT Subscription {action} failed with HTTP {response.status_code}."
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "").strip()
            msg = err.get("message")
            if msg:
                message = f"ChatGPT Subscription {action} failed: {msg}"
        elif isinstance(err, str):
            code = err.strip()
            desc = payload.get("error_description") or payload.get("message")
            if desc:
                message = f"ChatGPT Subscription {action} failed: {desc}"
    except Exception:
        pass
    if response.status_code == 429:
        raise ChatGPTSubscriptionRateLimited(
            "ChatGPT Subscription quota or rate limit was reached. Credentials are still valid."
        )
    if response.status_code in (401, 403) or code in {"invalid_grant", "invalid_token", "invalid_request", "refresh_token_reused"}:
        raise ChatGPTSubscriptionReauthRequired(message)
    raise ChatGPTSubscriptionError(message)


def _json_or_error(response: httpx.Response, action: str) -> Dict[str, Any]:
    _raise_for_oauth_response(response, action)
    try:
        data = response.json()
    except Exception as exc:
        raise ChatGPTSubscriptionError(f"ChatGPT Subscription {action} returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise ChatGPTSubscriptionError(f"ChatGPT Subscription {action} returned an unexpected response.")
    return data


def request_device_code(timeout: float = 15.0) -> Dict[str, Any]:
    response = httpx.post(
        f"{CHATGPT_OAUTH_ISSUER}/api/accounts/deviceauth/usercode",
        json={"client_id": CHATGPT_OAUTH_CLIENT_ID},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    data = _json_or_error(response, "device-code request")
    if not data.get("device_auth_id") or not data.get("user_code"):
        raise ChatGPTSubscriptionError("ChatGPT device-code response was missing required fields.")
    data.setdefault("verification_uri", f"{CHATGPT_OAUTH_ISSUER}/codex/device")
    data.setdefault("interval", 5)
    data.setdefault("expires_in", 900)
    return data


def poll_device_auth(device_auth_id: str, user_code: str, timeout: float = 15.0) -> Dict[str, Any]:
    response = httpx.post(
        f"{CHATGPT_OAUTH_ISSUER}/api/accounts/deviceauth/token",
        json={"device_auth_id": device_auth_id, "user_code": user_code},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if response.status_code in (403, 404):
        return {"status": "pending", "error": "authorization_pending"}
    return _json_or_error(response, "device-code poll")


def exchange_authorization_code(authorization_code: str, code_verifier: str, timeout: float = 15.0) -> Dict[str, Any]:
    response = httpx.post(
        CHATGPT_OAUTH_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": CHATGPT_OAUTH_REDIRECT_URI,
            "client_id": CHATGPT_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        timeout=timeout,
    )
    data = _json_or_error(response, "token exchange")
    if not data.get("access_token"):
        raise ChatGPTSubscriptionReauthRequired("ChatGPT token exchange did not return an access token.")
    return data


def refresh_oauth_tokens(access_token: str, refresh_token: str, timeout: float = 20.0) -> Dict[str, Any]:
    del access_token
    if not refresh_token:
        raise ChatGPTSubscriptionReauthRequired("ChatGPT Subscription is missing a refresh token. Reconnect the provider.")
    response = httpx.post(
        CHATGPT_OAUTH_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CHATGPT_OAUTH_CLIENT_ID,
        },
        timeout=timeout,
    )
    data = _json_or_error(response, "token refresh")
    if not data.get("access_token"):
        raise ChatGPTSubscriptionReauthRequired("ChatGPT token refresh did not return an access token.")
    return data


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def access_token_is_expiring(access_token: str, skew_seconds: int = CHATGPT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
    try:
        exp = int(_decode_jwt_payload(access_token).get("exp") or 0)
    except Exception:
        return True
    return exp <= int(time.time()) + int(skew_seconds)


def resolve_runtime_credentials(
    auth_id: str,
    owner: Optional[str] = None,
    *,
    force_refresh: bool = False,
    rejected_access_token: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``{provider, base_url, api_key, auth_mode}`` with a bearer good now.

    Refreshes are single-flight per credential: the check-and-refresh runs
    under a per-``auth_id`` lock after re-reading the row, so requests that
    queue behind someone else's refresh use its result instead of spending the
    (rotating) refresh token a second time.

    ``rejected_access_token`` is the 401 recovery path: refresh only if the
    stored token is still the one upstream just rejected. If another request
    already replaced it, that replacement is returned without a refresh; if it
    was minted within the cooldown, it is returned as-is (a brand-new token
    that was rejected will not be helped by minting another).
    """
    ProviderAuthSession, SessionLocal, utcnow_naive = _database_handles()
    ref = _auth_ref(auth_id)

    def _read_stored() -> Dict[str, str]:
        db = SessionLocal()
        try:
            q = db.query(ProviderAuthSession).filter(
                ProviderAuthSession.id == auth_id,
                ProviderAuthSession.provider == CHATGPT_SUBSCRIPTION_PROVIDER,
            )
            if owner:
                q = q.filter(ProviderAuthSession.owner == owner)
            row = q.first()
            if row is None:
                raise ChatGPTSubscriptionAuthNotFound(
                    "ChatGPT Subscription credentials were not found for this user."
                )
            return {
                "access_token": row.access_token or "",
                "refresh_token": row.refresh_token or "",
                "base_url": row.base_url or DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL,
                "auth_mode": row.auth_mode or "chatgpt",
            }
        finally:
            db.close()

    def _refresh_reason(token: str) -> Optional[str]:
        """Why this token must be refreshed now, or ``None`` to use it as-is."""
        if access_token_is_expiring(token):
            return "expiring"
        if force_refresh:
            return reason or "forced"
        if rejected_access_token is not None:
            if token != rejected_access_token:
                logger.info(
                    "[chatgpt-auth] auth=%s %s: stored token was already replaced "
                    "by another refresh; reusing it without refreshing again",
                    ref, reason or "unauthorized",
                )
                _record_superseded(rejected_access_token, token)
                return None
            last = _LAST_REFRESH_AT.get(auth_id)
            if last is not None and time.monotonic() - last < CHATGPT_UNAUTHORIZED_REFRESH_COOLDOWN_SECONDS:
                logger.info(
                    "[chatgpt-auth] auth=%s %s: token was refreshed %.1fs ago; "
                    "not refreshing again, retrying with it",
                    ref, reason or "unauthorized", time.monotonic() - last,
                )
                return None
            return reason or "unauthorized"
        return None

    stored = _read_stored()
    access_token = stored["access_token"]
    if force_refresh or rejected_access_token is not None or access_token_is_expiring(access_token):
        # Waiting for the per-account refresh lock and calling OAuth can both
        # take seconds. Neither phase may pin a connection from the shared app
        # QueuePool, especially when many agents resolve the same endpoint.
        with _refresh_lock_for(auth_id):
            stored = _read_stored()
            access_token = stored["access_token"]
            why = _refresh_reason(access_token)
            if why:
                logger.info(
                    "[chatgpt-auth] refresh start auth=%s reason=%s token_expires_in=%s",
                    ref, why, _expires_in(access_token),
                )
                started = time.monotonic()
                try:
                    refreshed = refresh_oauth_tokens(access_token, stored["refresh_token"])
                except Exception as exc:
                    # The message is the OAuth server's error text (or ours);
                    # it never contains a token.
                    logger.warning(
                        "[chatgpt-auth] refresh failed auth=%s reason=%s elapsed=%.2fs "
                        "error=%s: %s",
                        ref, why, time.monotonic() - started, type(exc).__name__, exc,
                    )
                    raise
                db = SessionLocal()
                try:
                    q = db.query(ProviderAuthSession).filter(
                        ProviderAuthSession.id == auth_id,
                        ProviderAuthSession.provider == CHATGPT_SUBSCRIPTION_PROVIDER,
                    )
                    if owner:
                        q = q.filter(ProviderAuthSession.owner == owner)
                    row = q.first()
                    if row is None:
                        raise ChatGPTSubscriptionAuthNotFound(
                            "ChatGPT Subscription credentials were not found for this user."
                        )
                    row.access_token = refreshed["access_token"]
                    if refreshed.get("refresh_token"):
                        row.refresh_token = refreshed["refresh_token"]
                    row.last_refresh = utcnow_naive()
                    db.commit()
                    stored = {
                        "access_token": row.access_token or "",
                        "refresh_token": row.refresh_token or "",
                        "base_url": row.base_url or DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL,
                        "auth_mode": row.auth_mode or "chatgpt",
                    }
                finally:
                    db.close()
                _LAST_REFRESH_AT[auth_id] = time.monotonic()
                _record_superseded(access_token, stored["access_token"])
                if rejected_access_token and rejected_access_token != access_token:
                    _record_superseded(rejected_access_token, stored["access_token"])
                logger.info(
                    "[chatgpt-auth] refresh ok auth=%s reason=%s elapsed=%.2fs "
                    "token_expires_in=%s refresh_token_rotated=%s",
                    ref, why, time.monotonic() - started, _expires_in(stored["access_token"]),
                    "yes" if refreshed.get("refresh_token") else "no",
                )
            access_token = stored["access_token"]

    return {
        "provider": CHATGPT_SUBSCRIPTION_PROVIDER,
        "base_url": stored["base_url"].rstrip("/"),
        "api_key": access_token,
        "auth_mode": stored["auth_mode"],
    }


def _jwt_exp(token: str) -> Optional[int]:
    try:
        exp = int(_decode_jwt_payload(token).get("exp") or 0)
    except Exception:
        return None
    return exp or None


def _token_identity(token: str) -> Optional[tuple]:
    """The ChatGPT account a JWT was minted for (subject + account id), if readable."""
    try:
        claims = _decode_jwt_payload(token)
    except Exception:
        return None
    auth_claims = claims.get("https://api.openai.com/auth")
    account = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
    sub = claims.get("sub")
    if not sub and not account:
        return None
    return (str(sub or ""), str(account or ""))


def _find_auth_for_token(access_token: str) -> Optional[tuple]:
    """``(auth_id, owner)`` of the stored credential ``access_token`` belongs to.

    The request path only has the bearer from its headers, not the auth row it
    came from. Match on the exact token (or the one that superseded it) first;
    failing that, on the ChatGPT account the JWT names, but only when exactly
    one stored credential is for that account, so a request is never recovered
    onto somebody else's connection.
    """
    if not access_token:
        return None
    ProviderAuthSession, SessionLocal, _utcnow = _database_handles()
    db = SessionLocal()
    try:
        rows = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.provider == CHATGPT_SUBSCRIPTION_PROVIDER,
        ).all()
        candidates = [(row.id, row.owner, row.access_token or "") for row in rows]
    finally:
        db.close()
    probes = {access_token}
    replacement = superseded_access_token(access_token)
    if replacement:
        probes.add(replacement)
    for auth_id, owner, token in candidates:
        if token and token in probes:
            return auth_id, owner
    identity = _token_identity(access_token)
    if identity:
        matches = [(a, o) for a, o, t in candidates if t and _token_identity(t) == identity]
        if len(matches) == 1:
            return matches[0]
    return None


def recover_rejected_access_token(access_token: str) -> Optional[str]:
    """After a 401 on ``access_token``: a bearer worth exactly one retry.

    Single-flight with every other refresh of the same credential (see
    :func:`resolve_runtime_credentials`). Returns ``None`` when no stored
    credential can be identified for the token. Raises
    :class:`ChatGPTSubscriptionReauthRequired` when the refresh itself is
    refused -- the one case where reconnecting is the right advice.
    """
    found = _find_auth_for_token(access_token)
    if not found:
        logger.warning("[chatgpt-auth] 401 recovery: no stored credential matches the rejected token "
                       "(token_expires_in=%s)", _expires_in(access_token))
        return None
    auth_id, owner = found
    creds = resolve_runtime_credentials(
        auth_id, owner, rejected_access_token=access_token, reason="unauthorized",
    )
    fresh = creds.get("api_key") or None
    if not fresh:
        logger.warning("[chatgpt-auth] 401 recovery auth=%s: the stored credential has no access token",
                       _auth_ref(auth_id))
    return fresh


def _session_credential_ref(session_id: str) -> Optional[tuple]:
    """``(auth_id, owner)`` of the ChatGPT credential behind a chat's endpoint.

    The same lookup ``routes.chat_helpers.resolve_session_auth`` uses to give
    a chat its bearer in the first place: the chat row's owner and endpoint
    URL, then that owner's enabled, session-backed endpoint serving the URL.
    It never crosses owners, so a request is only ever recovered onto the
    credential its own chat was configured with.
    """
    if not session_id:
        return None
    from core.database import ModelEndpoint, Session as DbSession, SessionLocal
    from src.auth_helpers import owner_filter

    try:
        from routes.chat_helpers import _session_url_matches_endpoint as _matches
    except Exception:  # pragma: no cover - routes always import in the app
        def _matches(session_url, base):
            return is_chatgpt_subscription_base(session_url) and is_chatgpt_subscription_base(base)

    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == str(session_id)).first()
        if row is None:
            return None
        owner = getattr(row, "owner", None)
        session_url = getattr(row, "endpoint_url", "") or ""
        query = db.query(ModelEndpoint).filter(
            ModelEndpoint.is_enabled == True,  # noqa: E712
            ModelEndpoint.provider_auth_id.isnot(None),
        )
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        for endpoint in query.all():
            base = getattr(endpoint, "base_url", "") or ""
            if not is_chatgpt_subscription_base(base) or not _matches(session_url, base):
                continue
            auth_owner = owner if owner is not None else getattr(endpoint, "owner", None)
            return endpoint.provider_auth_id, auth_owner
        return None
    finally:
        db.close()


def access_token_for_session(session_id: Optional[str], *, rejected_access_token: Optional[str] = None,
                             reason: str = "missing") -> Optional[str]:
    """The chat owner's current ChatGPT bearer, resolved from the store.

    For a request whose headers carry no usable bearer (``reason="missing"``)
    or one that matches no stored credential (``reason="unmatched"``). Returns
    ``None`` -- and says why in the log -- when the chat has no ChatGPT
    credential or the store only holds the very token upstream just rejected.
    Raises what :func:`resolve_runtime_credentials` raises when a needed
    refresh is refused.
    """
    sref = str(session_id or "-")[:8]
    if not session_id:
        logger.warning("[chatgpt-auth] %s bearer: no chat session on the request, so no owner "
                       "credential to resolve", reason)
        return None
    ref = _session_credential_ref(session_id)
    if not ref:
        logger.warning("[chatgpt-auth] %s bearer session=%s: no enabled ChatGPT Subscription endpoint "
                       "with a stored credential serves this chat", reason, sref)
        return None
    auth_id, owner = ref
    creds = resolve_runtime_credentials(auth_id, owner)
    token = creds.get("api_key") or ""
    if not token:
        logger.warning("[chatgpt-auth] %s bearer session=%s auth=%s: the stored credential has no "
                       "access token", reason, sref, _auth_ref(auth_id))
        return None
    if rejected_access_token and token == rejected_access_token:
        logger.warning("[chatgpt-auth] %s bearer session=%s auth=%s: the stored token is the one "
                       "upstream rejected; nothing newer to retry with", reason, sref, _auth_ref(auth_id))
        return None
    logger.info("[chatgpt-auth] %s bearer session=%s: resolved the chat owner's stored credential "
                "auth=%s token_expires_in=%s", reason, sref, _auth_ref(auth_id), _expires_in(token))
    return token


def token_needs_presend_refresh(access_token: Optional[str]) -> bool:
    """Cheap, local: is this a readable JWT inside the refresh skew?

    Lets the async request path decide whether :func:`current_access_token`
    (database and possibly network) is worth a thread hop at all.
    """
    return bool(access_token) and _jwt_exp(access_token) is not None and access_token_is_expiring(access_token)


def current_access_token(access_token: Optional[str]) -> Optional[str]:
    """Pre-send check for a bearer snapshotted into headers earlier.

    Returns a replacement when the token was superseded by a refresh in this
    process, or is inside the refresh skew (refreshing it single-flight), and
    ``None`` when the caller's token is fine as it is. Tokens that are not
    readable JWTs are left alone: without ``exp`` there is nothing to check.
    """
    if not access_token:
        return None
    replacement = superseded_access_token(access_token)
    candidate = replacement or access_token
    if _jwt_exp(candidate) is None or not access_token_is_expiring(candidate):
        return replacement
    found = _find_auth_for_token(candidate)
    if not found:
        return replacement
    auth_id, owner = found
    fresh = resolve_runtime_credentials(auth_id, owner).get("api_key") or ""
    if fresh and fresh != access_token:
        _record_superseded(access_token, fresh)
        return fresh
    return replacement


def to_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ChatGPTSubscriptionRateLimited):
        return HTTPException(429, str(exc))
    if isinstance(exc, (ChatGPTSubscriptionReauthRequired, ChatGPTSubscriptionAuthNotFound)):
        return HTTPException(401, f"{exc} Reconnect the provider.")
    return HTTPException(502, str(exc))


def _arguments_to_str(arguments) -> str:
    """Responses wants function-call arguments as a JSON string.

    Callers may hand us either the raw string the model streamed or a decoded
    dict (``_normalize_messages_for_provider`` converts them for some
    providers), so accept both.
    """
    if isinstance(arguments, str):
        return arguments or "{}"
    if arguments is None:
        return "{}"
    try:
        return json.dumps(arguments)
    except (TypeError, ValueError):
        return "{}"


def _message_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
        )
    return "" if content is None else str(content)


def _message_image_urls(content) -> list[str]:
    """Image URLs (data URLs included) in an OpenAI-style multimodal content list."""
    urls: list[str] = []
    if not isinstance(content, list):
        return urls
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in ("image_url", "input_image"):
            continue
        ref = part.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else ref
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def build_responses_input(messages: list[dict], *, replay_phase: bool = True) -> list[dict]:
    """Convert OpenAI chat messages to Responses API input items.

    Tool calls and their results are first-class item types here, not messages:
    an assistant turn that called a tool becomes ``function_call`` items, and
    the matching result becomes a ``function_call_output`` keyed by the same
    ``call_id``. Flattening those into plain text (as this did originally) makes
    the model see its own tool call as prose with no result attached, so it
    re-plans the same call forever instead of continuing.

    An assistant message that carries ``responses_phase`` (commentary /
    final_answer, captured from the stream) goes back with ``phase`` and the
    explicit ``type: message``, so a phase-emitting model can tell its interim
    commentary from a final answer. Messages without one keep the original
    shape exactly, so old history's prompt-cache prefix does not change.
    Order within a round is reasoning, then the message, then its calls --
    the order the model's ``output`` array produced them.

    A user message whose content list has ``image_url`` parts (the images a tool
    returned, or an attachment) goes out with ``input_image`` items beside its
    ``input_text`` -- this used to flatten to text and drop the picture. A
    message with no image keeps the exact shape above for the same cache reason.
    """
    input_items: list[dict] = []
    for msg in messages or []:
        role = msg.get("role") or "user"
        content = msg.get("content")

        # A reasoning model's own thinking is a first-class output item here,
        # and it is the thread between one tool call and the next. Dropped, the
        # model re-derives its whole plan from the transcript every round --
        # which is what an agent loop that "goes flat" after a couple of rounds
        # actually looks like. Replay the items verbatim, ahead of the text and
        # calls they produced, so the order matches the `output` array they
        # arrived in.
        for item in (msg.get("reasoning_items") or []):
            if isinstance(item, dict) and item.get("type") == "reasoning":
                input_items.append(item)

        if role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                input_items.append({
                    "type": "function_call_output",
                    "call_id": str(call_id),
                    "output": _message_text(content),
                })
            else:
                # No id to correlate with — fall back to plain text rather than
                # emitting an item the API will reject.
                input_items.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": _message_text(content)}],
                })
            continue

        tool_calls = msg.get("tool_calls") if role == "assistant" else None

        text = _message_text(content)
        image_urls = _message_image_urls(content) if role == "user" else []
        if image_urls:
            text = text.strip()
            blocks = ([{"type": "input_text", "text": text}] if text else [])
            blocks += [{"type": "input_image", "image_url": url} for url in image_urls]
            input_items.append({"role": role, "content": blocks})
        elif text:
            input_type = "output_text" if role == "assistant" else "input_text"
            phase = msg.get("responses_phase") if role == "assistant" and replay_phase else None
            if phase:
                input_items.append({
                    "type": "message",
                    "role": role,
                    "phase": str(phase),
                    "content": [{"type": input_type, "text": text}],
                })
            else:
                input_items.append({"role": role, "content": [{"type": input_type, "text": text}]})

        for call in tool_calls or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            name = fn.get("name") or call.get("name")
            if not name:
                continue
            call_id = call.get("id") or call.get("call_id")
            if not call_id:
                continue
            input_items.append({
                "type": "function_call",
                "call_id": str(call_id),
                "name": str(name),
                "arguments": _arguments_to_str(fn.get("arguments", call.get("arguments"))),
            })

    return input_items


def build_responses_tools(tools: list[dict] | None) -> list[dict]:
    """Convert OpenAI chat tool schemas to the Responses API shape.

    Chat Completions nests the definition under a ``function`` key; Responses
    flattens it onto the item. Sending the nested form is rejected, which is why
    this endpoint shipped with tools disabled entirely.
    """
    converted: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        name = (fn or tool).get("name")
        if not name:
            continue
        entry = {
            "type": "function",
            "name": str(name),
            "parameters": (fn or tool).get("parameters") or {"type": "object", "properties": {}},
        }
        description = (fn or tool).get("description")
        if description:
            entry["description"] = str(description)
        # Responses may normalize omitted strict settings into an all-required
        # schema. Preserve explicit opt-outs for action-dependent parameters.
        strict = (fn or tool).get("strict")
        if isinstance(strict, bool):
            entry["strict"] = strict
        converted.append(entry)
    return converted
