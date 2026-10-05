"""Image tools use only image endpoints the caller may see, and need the image privilege.

A private image endpoint carries its owner's API key and runs on their
hardware. Bob's image tools must not reach Alice's endpoint, whether he names
it or leaves the choice to the server.
"""
import base64

import pytest

from tests.routes._support.images import FakeModelServer, png_bytes

pytestmark = pytest.mark.security

ALICE_BASE = "http://127.0.0.1:7860"
PNG_B64 = base64.b64encode(png_bytes()).decode()


@pytest.fixture
def alice_endpoint(api, monkeypatch):
    import core.database as database

    monkeypatch.delenv("IMAGE_BLOCK_PRIVATE_IPS", raising=False)
    db = database.SessionLocal()
    try:
        db.add(database.ModelEndpoint(
            id="alice-diffusion", name="alice's diffusion box", base_url=ALICE_BASE,
            api_key="alice-secret-key", is_enabled=True, owner="alice", model_type="image",
        ))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def image_server(monkeypatch):
    return FakeModelServer({"data": [{"b64_json": "RESULT"}]}).install(monkeypatch)


def _upscale(client):
    return client.post("/api/gallery/ai-upscale", files={"image": ("in.png", png_bytes(), "image/png")})


def _style_transfer(client):
    return client.post("/api/gallery/style-transfer",
                       files={"image": ("in.png", png_bytes(), "image/png")}, data={"prompt": "oil paint"})


@pytest.mark.parametrize("call", [_upscale, _style_transfer], ids=["ai-upscale", "style-transfer"])
def test_another_users_private_endpoint_is_not_picked_for_image_tools(api, alice_endpoint, image_server, call):
    response = call(api.as_user("bob"))

    assert response.status_code == 400
    assert image_server.calls == []


@pytest.mark.parametrize("path, body", [
    ("/api/image/inpaint", {"image": PNG_B64, "mask": PNG_B64}),
    ("/api/image/harmonize", {"image": PNG_B64}),
])
def test_naming_another_users_private_endpoint_is_refused(api, alice_endpoint, image_server, path, body):
    bob = api.as_user("bob")

    on_alices = bob.post(path, json={**body, "_endpoint": ALICE_BASE})
    on_unregistered = bob.post(path, json={**body, "_endpoint": "http://127.0.0.1:7861"})
    picked_for_him = bob.post(path, json=body)

    assert on_alices.status_code == 403
    assert on_alices.json() == on_unregistered.json()
    assert picked_for_him.status_code == 400
    assert image_server.calls == []


def test_the_owner_reaches_their_private_endpoint(api, alice_endpoint, image_server):
    alice = api.as_user("alice")

    upscaled = _upscale(alice)
    inpainted = alice.post("/api/image/inpaint", json={"image": PNG_B64, "mask": PNG_B64, "_endpoint": ALICE_BASE})

    assert (upscaled.json(), inpainted.json()) == ({"image": "RESULT"}, {"image": "RESULT"})
    assert [c["url"] for c in image_server.calls] == [
        ALICE_BASE + "/v1/images/upscale",
        ALICE_BASE + "/v1/images/edits",
    ]


PRIVILEGED = [
    "/api/gallery/ai-upscale",
    "/api/gallery/style-transfer",
    "/api/image/inpaint",
    "/api/image/harmonize",
    "/api/image/sharpen",
    "/api/image/denoise",
    "/api/image/upscale-local",
    "/api/image/mask",
    "/api/image/remove-bg",
    "/api/image/enhance-face",
]


@pytest.mark.parametrize("path", PRIVILEGED)
def test_a_user_without_the_image_privilege_is_refused(api, path):
    assert api.auth.set_privileges("alice", {"can_generate_images": False})

    response = api.as_user("alice").post(path, json={})

    assert response.status_code == 403
    assert "can generate images" in response.json()["detail"]
