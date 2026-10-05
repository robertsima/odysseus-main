"""Every email endpoint turns away a request with no session cookie."""
import pytest

pytestmark = pytest.mark.security

ENDPOINTS = [
    ("GET", "/api/email/list"),
    ("GET", "/api/email/unread-state"),
    ("GET", "/api/email/search"),
    ("GET", "/api/email/contacts"),
    ("GET", "/api/email/folders"),
    ("GET", "/api/email/read/1"),
    ("GET", "/api/email/attachments/1"),
    ("GET", "/api/email/attachment/1/0"),
    ("GET", "/api/email/attachments-download/1"),
    ("POST", "/api/email/attachment-as-doc/1/0"),
    ("POST", "/api/email/mark-read/1"),
    ("POST", "/api/email/mark-unread/1"),
    ("POST", "/api/email/flag/1"),
    ("POST", "/api/email/archive/1"),
    ("DELETE", "/api/email/delete/1"),
    ("DELETE", "/api/email/delete-permanent/1"),
    ("POST", "/api/email/move/1?dest=Trash"),
    ("POST", "/api/email/send"),
    ("POST", "/api/email/draft"),
    ("POST", "/api/email/schedule"),
    ("GET", "/api/email/scheduled"),
    ("DELETE", "/api/email/scheduled/abc"),
    ("GET", "/api/email/pending"),
    ("POST", "/api/email/pending/abc/approve"),
    ("DELETE", "/api/email/pending/abc"),
    ("POST", "/api/email/summarize"),
    ("POST", "/api/email/translate"),
    ("POST", "/api/email/ai-reply"),
    ("GET", "/api/email/style"),
    ("PUT", "/api/email/style"),
    ("GET", "/api/email/config"),
    ("PUT", "/api/email/config"),
    ("GET", "/api/email/urgency-state"),
    ("GET", "/api/email/accounts"),
    ("POST", "/api/email/accounts"),
    ("PUT", "/api/email/accounts/abc"),
    ("DELETE", "/api/email/accounts/abc"),
    ("POST", "/api/email/accounts/test"),
    ("POST", "/api/email/accounts/abc/set-default"),
    ("GET", "/api/email/oauth/google/authorize?account_id=abc"),
    ("POST", "/api/email/unsubscribe/execute"),
    ("POST", "/api/email/compose-upload"),
]


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_request_without_a_session_is_refused(api, method, path):
    response = api.anonymous().request(method, path, json={})

    assert response.status_code == 401
