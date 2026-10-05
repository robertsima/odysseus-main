"""Every chat-session endpoint turns away a request with no session cookie."""
import pytest

pytestmark = pytest.mark.security

SID = "00000000-0000-0000-0000-000000000000"

ENDPOINTS = [
    ("GET", "/api/sessions"),
    ("POST", "/api/session"),
    ("PATCH", f"/api/session/{SID}"),
    ("POST", f"/api/session/{SID}/inject_messages"),
    ("POST", f"/api/session/{SID}/delete"),
    ("POST", "/api/sessions/bulk-delete"),
    ("DELETE", f"/api/session/{SID}"),
    ("DELETE", "/api/sessions/all"),
    ("POST", f"/api/session/{SID}/archive"),
    ("POST", f"/api/session/{SID}/unarchive"),
    ("GET", "/api/sessions/archived"),
    ("GET", f"/api/session/{SID}/export"),
    ("POST", "/api/sessions/save"),
    ("POST", "/api/session/openai"),
    ("POST", f"/api/session/{SID}/important"),
    ("GET", f"/api/session/{SID}/settings"),
    ("PATCH", f"/api/session/{SID}/settings"),
    ("POST", f"/api/session/{SID}/compact"),
    ("POST", "/api/chats/tidy"),
    ("POST", "/api/sessions/auto-sort"),
    ("GET", f"/api/session/{SID}/context_info"),
]


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_request_without_a_session_is_refused(api, method, path):
    response = api.anonymous().request(method, path)

    assert response.status_code == 401
