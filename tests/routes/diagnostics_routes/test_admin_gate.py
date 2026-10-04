"""The diagnostics routes are for admins.

They expose database and vector-store statistics across every user, and the
test endpoints make outbound calls on the server's behalf.
"""
import pytest

pytestmark = pytest.mark.security


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/db/stats"),
    ("GET", "/api/rag/stats"),
    ("GET", "/api/test/youtube?url=https://youtu.be/dQw4w9WgXcQ"),
    ("POST", "/api/test-research"),
])
def test_a_user_who_is_not_an_admin_cannot_use_diagnostics(api, method, path):
    assert api.as_user("alice").request(method, path).status_code == 403


def test_an_admin_can_read_the_database_stats(api):
    assert api.as_admin().get("/api/db/stats").status_code == 200
