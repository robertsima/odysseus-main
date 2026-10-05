"""Which image endpoint the gallery's image tools may call.

The upscale, style-transfer, inpaint and harmonize tools send the user's
image to an image endpoint, with that endpoint's stored API key. Bob owns
the only one here. Alice's requests must neither use it nor reach any
address she names that is not a registered endpoint she may use.
"""
import base64

import httpx
import pytest

import src.database

pytestmark = pytest.mark.security

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
BOB_BASE = "http://127.0.0.1:9/v1"  # loopback, so the URL check itself lets it through


@pytest.fixture
def outbound(api, monkeypatch):
    """Bob's image endpoint, and a record of every host the server calls."""
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="bob-img", name="bob images", base_url=BOB_BASE, api_key="bob-key",
            is_enabled=True, model_type="image", owner="bob",
        ))
        db.commit()
    finally:
        db.close()
    hosts = []

    async def send(self, request, **kwargs):
        hosts.append(request.url.host)
        raise httpx.ConnectError("no network in tests", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return hosts


def _upscale(client):
    return client.post("/api/gallery/ai-upscale", files={"image": ("a.png", PNG, "image/png")}, data={"scale": "2"})


def _style_transfer(client):
    return client.post("/api/gallery/style-transfer", files={"image": ("a.png", PNG, "image/png")},
                       data={"prompt": "watercolor"})


def _inpaint(client, **extra):
    image = base64.b64encode(PNG).decode()
    return client.post("/api/image/inpaint", json={"image": image, "mask": image, "prompt": "a hat", **extra})


def _harmonize(client, **extra):
    return client.post("/api/image/harmonize", json={"image": base64.b64encode(PNG).decode(), **extra})


TOOLS = {"upscale": _upscale, "style-transfer": _style_transfer, "inpaint": _inpaint, "harmonize": _harmonize}
PROXIES = {"inpaint": _inpaint, "harmonize": _harmonize}


@pytest.mark.parametrize("tool", TOOLS)
def test_an_image_tool_does_not_fall_back_to_another_users_endpoint(api, outbound, tool):
    response = TOOLS[tool](api.as_user("alice"))

    assert response.status_code == 400
    assert outbound == []


@pytest.mark.parametrize("tool", PROXIES)
def test_naming_another_users_image_endpoint_is_refused(api, outbound, tool):
    response = PROXIES[tool](api.as_user("alice"), _endpoint=BOB_BASE)

    assert response.status_code == 403
    assert outbound == []


@pytest.mark.parametrize("tool", PROXIES)
def test_a_cloud_metadata_address_is_never_called(api, outbound, tool):
    response = PROXIES[tool](api.as_user("alice"), _endpoint="http://169.254.169.254/latest")

    assert response.status_code in (400, 403)
    assert outbound == []


@pytest.mark.parametrize("tool", TOOLS)
def test_an_image_tool_needs_the_image_privilege(api, outbound, tool):
    assert api.auth.set_privileges("bob", {"can_generate_images": False})

    response = TOOLS[tool](api.as_user("bob"))

    assert response.status_code == 403
    assert outbound == []
