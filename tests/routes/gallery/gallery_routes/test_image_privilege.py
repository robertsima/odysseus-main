"""The local image tools run only for accounts allowed to generate images.

Denoise, local upscale, background removal and face enhancement run models on
the server's GPU. An account whose can_generate_images privilege is off must
get 403 before any image is read. (Upscale, style transfer, inpaint and
harmonize have the same check in test_image_endpoints.)
"""
import base64

import pytest

pytestmark = pytest.mark.security

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PATHS = [
    "/api/image/denoise",
    "/api/image/upscale-local",
    "/api/image/remove-bg",
    "/api/image/enhance-face",
]


@pytest.mark.parametrize("path", PATHS)
def test_an_account_without_the_image_privilege_is_refused(api, path):
    assert api.auth.set_privileges("bob", {"can_generate_images": False})

    response = api.as_user("bob").post(path, json={"image": base64.b64encode(PNG).decode()})

    assert response.status_code == 403

