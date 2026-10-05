"""Every document endpoint turns away a request with no session cookie."""
import pytest

pytestmark = pytest.mark.security

DOC = "00000000-0000-0000-0000-000000000000"

ENDPOINTS = [
    ("POST", "/api/document"),
    ("POST", "/api/documents/import-pdf"),
    ("GET", "/api/documents/library"),
    ("GET", f"/api/documents/{DOC}"),
    ("GET", f"/api/document/{DOC}"),
    ("POST", f"/api/document/{DOC}/archive"),
    ("POST", f"/api/document/{DOC}/extract-pdf-text"),
    ("POST", "/api/documents/export-zip"),
    ("PUT", f"/api/document/{DOC}"),
    ("PATCH", f"/api/document/{DOC}"),
    ("DELETE", f"/api/document/{DOC}"),
    ("GET", f"/api/document/{DOC}/versions"),
    ("GET", f"/api/document/{DOC}/version/1"),
    ("POST", f"/api/document/{DOC}/restore/1"),
    ("POST", "/api/documents/tidy"),
    ("POST", "/api/documents/ai-tidy"),
    ("POST", f"/api/document/{DOC}/export-pdf/preview"),
    ("GET", f"/api/document/{DOC}/render-pages"),
    ("GET", f"/api/document/{DOC}/page/1.png"),
    ("POST", f"/api/document/{DOC}/ai-fill-annotations"),
    ("GET", f"/api/document/{DOC}/render-pdf"),
    ("GET", f"/api/document/{DOC}/export-pdf"),
    ("POST", f"/api/document/{DOC}/prepare-signed-reply"),
]


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_request_without_a_session_is_refused(api, method, path):
    response = api.anonymous().request(method, path)

    assert response.status_code == 401
