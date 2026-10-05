"""Every gallery and image-tool endpoint turns away a request with no session cookie."""
import pytest

pytestmark = pytest.mark.security

IMG = "00000000-0000-0000-0000-000000000000"
ALBUM = "11111111-1111-1111-1111-111111111111"

ENDPOINTS = [
    ("POST", "/api/gallery/upload"),
    ("POST", f"/api/gallery/{IMG}/replace"),
    ("POST", f"/api/gallery/{IMG}/rename"),
    ("POST", f"/api/gallery/{IMG}/rotate"),
    ("POST", "/api/gallery/ai-upscale"),
    ("POST", "/api/gallery/style-transfer"),
    ("GET", "/api/gallery/tags"),
    ("GET", "/api/gallery/library"),
    ("GET", "/api/gallery/albums"),
    ("POST", "/api/gallery/albums"),
    ("GET", "/api/gallery/stats"),
    ("POST", "/api/gallery/ai-tag-batch"),
    ("GET", f"/api/gallery/{IMG}"),
    ("PATCH", f"/api/gallery/{IMG}"),
    ("POST", "/api/gallery/download-zip"),
    ("POST", "/api/gallery/clear-user-tags"),
    ("POST", "/api/gallery/clear-ai-tags"),
    ("POST", "/api/gallery/dedupe-tags"),
    ("DELETE", f"/api/gallery/{IMG}"),
    ("POST", "/api/image/inpaint"),
    ("POST", "/api/image/harmonize"),
    ("POST", "/api/image/sharpen"),
    ("POST", "/api/image/denoise"),
    ("POST", "/api/image/upscale-local"),
    ("POST", "/api/image/mask"),
    ("POST", "/api/image/remove-bg"),
    ("POST", "/api/image/enhance-face"),
    ("PUT", f"/api/gallery/albums/{ALBUM}"),
    ("DELETE", f"/api/gallery/albums/{ALBUM}"),
    ("POST", f"/api/gallery/albums/{ALBUM}/add"),
    ("POST", f"/api/gallery/albums/{ALBUM}/remove"),
    ("POST", f"/api/gallery/{IMG}/favorite"),
    ("POST", f"/api/gallery/{IMG}/ai-tag"),
]


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_request_without_a_session_is_refused(api, method, path):
    response = api.anonymous().request(method, path)

    assert response.status_code == 401
