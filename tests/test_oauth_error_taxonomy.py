"""One definition of "is this OAuth grant dead, or was the request unlucky?".

The calendar sync and the mail transport both refresh Google tokens, and both
grew their own copy of this logic. They had already diverged before either
shipped: given `400 {"error": "invalid_request"}` — a malformed request, i.e.
our own bug — one said transient and the other said terminal, so the same
response told calendar users to retry and mail users to re-authorize a working
account.

Getting it wrong is user-visible in both directions. Terminal-when-transient
nags about reconnecting an account that is fine; transient-when-terminal retries
a revoked grant forever while reporting success — which is the bug that started
all of this.
"""

import pytest

from src import oauth_errors
from src.oauth_errors import (
    GOOGLE_TERMINAL_TOKEN_ERRORS,
    TOKEN_TERMINAL,
    TOKEN_TRANSIENT,
    classify_google_token_failure,
)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


@pytest.mark.parametrize("body, expected, why", [
    ({"error": "invalid_grant"}, TOKEN_TERMINAL, "revoked or expired refresh token"),
    ({"error": "invalid_client"}, TOKEN_TERMINAL, "the OAuth client is gone"),
    ({"error": "unauthorized_client"}, TOKEN_TERMINAL, "client no longer permitted"),
    ({"error": "invalid_scope"}, TOKEN_TERMINAL, "scope withdrawn"),
    ({"error": {"status": "invalid_grant"}}, TOKEN_TERMINAL, "Google's dict-shaped error body"),
    ({"error": "rate_limit_exceeded"}, TOKEN_TRANSIENT, "known code, not a grant failure"),
    ({"error": "some_future_code"}, TOKEN_TRANSIENT, "unknown code: do not assume the worst"),
])
def test_a_parsed_error_code_decides_it(body, expected, why):
    assert classify_google_token_failure(_Resp(400, body))[0] == expected, why


def test_a_malformed_request_is_our_bug_not_a_dead_grant():
    """`invalid_request` at 400 is the case the two copies disagreed on. It
    means we sent Google something wrong; telling the user to reconnect a
    working account to route around our defect is the wrong answer."""
    assert "invalid_request" not in GOOGLE_TERMINAL_TOKEN_ERRORS
    assert classify_google_token_failure(_Resp(400, {"error": "invalid_request"}))[0] == TOKEN_TRANSIENT


@pytest.mark.parametrize("status, expected", [(400, TOKEN_TERMINAL), (401, TOKEN_TERMINAL),
                                              (429, TOKEN_TRANSIENT), (500, TOKEN_TRANSIENT),
                                              (503, TOKEN_TRANSIENT)])
def test_an_unreadable_body_falls_back_to_the_status(status, expected):
    """A proxy's HTML error page, say. This endpoint only answers 400/401 over
    credentials; 429/5xx are retryable by definition."""
    assert classify_google_token_failure(_Resp(status, None))[0] == expected


def test_no_response_at_all_is_always_transient():
    """DNS, TLS, connect timeout. An unreachable Google says nothing about
    whether the grant is still good."""
    assert classify_google_token_failure(None)[0] == TOKEN_TRANSIENT


def test_the_error_description_is_never_returned():
    """Only the short code travels — `error_description` is free-form upstream
    text and the request body carries the client secret and refresh token."""
    resp = _Resp(400, {"error": "invalid_grant",
                       "error_description": "Token has been expired or revoked."})
    verdict, code = classify_google_token_failure(resp)
    assert (verdict, code) == (TOKEN_TERMINAL, "invalid_grant")


def test_both_subsystems_use_the_one_definition():
    """The point of the module. If either grows a local copy again, they will
    drift again."""
    import routes.email_helpers as email
    import src.caldav_sync as caldav

    assert email.GOOGLE_TERMINAL_TOKEN_ERRORS is oauth_errors.GOOGLE_TERMINAL_TOKEN_ERRORS
    assert caldav.GOOGLE_TERMINAL_TOKEN_ERRORS is oauth_errors.GOOGLE_TERMINAL_TOKEN_ERRORS
    assert caldav.TOKEN_TERMINAL == email.TOKEN_TERMINAL == TOKEN_TERMINAL


def test_the_mail_wrapper_keeps_its_verdict_only_contract():
    """Mail call sites never needed the error code; the wrapper returns a bare
    string so they are untouched by the consolidation."""
    import routes.email_helpers as email

    verdict = email._classify_google_token_failure(_Resp(400, {"error": "invalid_grant"}))
    assert verdict == TOKEN_TERMINAL and isinstance(verdict, str)
