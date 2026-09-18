"""Shared OAuth token-failure taxonomy.

Two subsystems refresh Google OAuth tokens — the calendar sync
(:mod:`src.caldav_sync`) and the mail transport (:mod:`routes.email_helpers`) —
and both need the same answer to one question: is this grant dead, or was the
request merely unlucky? Getting it wrong in either direction is user-visible.
Call it terminal when it was a blip and the UI nags about reconnecting a working
account; call it transient when the grant is revoked and the account retries a
doomed refresh forever while reporting success.

The two grew independent copies of this logic and had already diverged before
either shipped: one classified a parsed-but-unknown error code as transient, the
other fell through to the status and called the same response terminal. So a
``400 {"error": "invalid_request"}`` — a malformed request, i.e. *our* bug —
told calendar users to retry and mail users to re-authorize. One definition,
imported by both.
"""

from __future__ import annotations

TOKEN_OK = "ok"
TOKEN_UNCONFIGURED = "unconfigured"   # the server itself has no OAuth client set up
TOKEN_TERMINAL = "terminal"           # grant is dead; only re-authorization fixes it
TOKEN_TRANSIENT = "transient"         # blip; the next attempt may well succeed

# RFC 6749 §5.2 error codes that mean the stored *grant* is gone, not that the
# request was unlucky. Google answers a revoked or expired refresh token with
# ``invalid_grant``; the others show up when the OAuth client itself has been
# deleted or had the scope withdrawn. All of them survive a retry, so we stop
# retrying and tell the user to reconnect.
#
# ``invalid_request`` is deliberately absent: Google returns it for a malformed
# request, which is a defect on our side. Treating it as terminal would tell a
# user to reconnect a perfectly good account to work around our own bug.
GOOGLE_TERMINAL_TOKEN_ERRORS = frozenset({
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "invalid_scope",
})


def classify_google_token_failure(resp) -> tuple[str, str]:
    """Classify a failed Google token-endpoint response as terminal or transient.

    Returns ``(TOKEN_TERMINAL | TOKEN_TRANSIENT, oauth_error_code)``.

    ``resp`` is None when the request never produced a response at all (DNS,
    TLS, connect timeout) — always transient.

    We read the OAuth error code out of the JSON body rather than trusting the
    status alone, because Google answers *both* "your refresh token is dead"
    and "you sent a malformed request" with a bare 400. The body can also be
    missing or not JSON at all (a proxy's HTML error page), so we fall back to
    the status: this endpoint only answers 400/401 over credentials, never as a
    transient blip, while 429/5xx are retryable by definition.

    Only the short error *code* is returned — never ``error_description``,
    which is free-form upstream text, and never any part of the request, which
    carries the client secret and the refresh token.
    """
    if resp is None:
        return TOKEN_TRANSIENT, ""
    status = getattr(resp, "status_code", 0) or 0
    code = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            # Google sends a bare string here; some proxies wrap it in an object.
            if isinstance(err, str):
                code = err.strip().lower()
            elif isinstance(err, dict):
                code = str(err.get("status") or err.get("code") or "").strip().lower()
    except Exception:
        code = ""
    if code:
        return (TOKEN_TERMINAL if code in GOOGLE_TERMINAL_TOKEN_ERRORS else TOKEN_TRANSIENT), code
    # No usable body: 400/401 from a token endpoint is a credential verdict,
    # anything else (429, 5xx, a stray 3xx) is worth retrying.
    return (TOKEN_TERMINAL if status in (400, 401) else TOKEN_TRANSIENT), ""

