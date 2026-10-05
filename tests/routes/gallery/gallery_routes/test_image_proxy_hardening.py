"""Inpaint and harmonize call the user's registered image endpoint, and tell the client nothing about it.

Both routes forward the user's image to a server the user registered, with
that endpoint's stored key. The outbound address must come from the stored
endpoint, only the known image paths may be appended to it, and a failure
upstream must not hand the client the upstream's error text, its address or
the exception, which would turn the proxy into a way to probe the network.
Alice owns one image endpoint; the upstream is faked at the HTTP client.
"""
import base64

import httpx
import pytest

import src.database
from routes.gallery import gallery_routes

pytestmark = pytest.mark.security

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
BASE = "http://127.0.0.1:9/v1"
LEAK = "UPSTREAM-SECRET-DETAIL"


@pytest.fixture
def upstream(api, monkeypatch):
    """Alice's registered endpoint; ``answer`` decides every upstream reply."""
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="alice-img", name="alice images", base_url=BASE, api_key="alice-key",
            is_enabled=True, model_type="image", owner="alice",
        ))
        db.commit()
    finally:
        db.close()

    class Upstream:
        requests = []
        answer = staticmethod(lambda request: httpx.Response(404, text=LEAK, request=request))

    async def send(self, request, **kwargs):
        Upstream.requests.append(request)
        response = Upstream.answer(request)
        if isinstance(response, Exception):
            raise response
        return response

    Upstream.requests = []
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return Upstream


def _inpaint(client, **extra):
    image = base64.b64encode(PNG).decode()
    return client.post("/api/image/inpaint", json={
        "image": image, "mask": image, "prompt": "a hat", "_endpoint": BASE, **extra})


def _harmonize(client, **extra):
    return client.post("/api/image/harmonize", json={
        "image": base64.b64encode(PNG).decode(), "_endpoint": BASE, **extra})


def _assert_nothing_leaked(response):
    for fragment in (LEAK, "127.0.0.1", "last_err", "404", "boom"):
        assert fragment not in response.text, (fragment, response.text)


def test_harmonize_tries_only_the_known_paths_on_the_registered_base(api, upstream):
    response = _harmonize(api.as_user("alice"))

    assert response.status_code == 502
    urls = [str(r.url) for r in upstream.requests]
    assert urls, "nothing reached the endpoint"
    for url in urls:
        assert url.startswith("http://127.0.0.1:9/")
        path = url[len("http://127.0.0.1:9"):]
        if path.startswith("/v1"):
            path = path[len("/v1"):]
        assert path in gallery_routes._GALLERY_ENDPOINT_PATHS, url
    assert upstream.requests[0].headers["authorization"] == "Bearer alice-key"
    _assert_nothing_leaked(response)


def test_harmonize_does_not_pass_on_the_upstreams_error_field(api, upstream):
    upstream.answer = staticmethod(lambda request: httpx.Response(200, json={"error": LEAK}, request=request))

    response = _harmonize(api.as_user("alice"))

    assert response.status_code == 502
    _assert_nothing_leaked(response)


@pytest.mark.parametrize("tool", [_inpaint, _harmonize], ids=["inpaint", "harmonize"])
def test_a_connection_failure_does_not_reach_the_client(api, upstream, tool):
    upstream.answer = staticmethod(lambda request: httpx.ConnectError("boom internal-host:1234", request=request))

    response = tool(api.as_user("alice"))

    assert response.status_code >= 400
    assert "internal-host" not in response.text
    _assert_nothing_leaked(response)


def test_inpaint_falls_back_to_the_inpaint_path_and_hides_its_failure(api, upstream):
    def answer(request):
        if request.url.path.endswith("/images/edits"):
            return httpx.Response(404, request=request)
        return httpx.Response(500, text=LEAK, request=request)

    upstream.answer = staticmethod(answer)

    response = _inpaint(api.as_user("alice"))

    assert [r.url.path for r in upstream.requests] == ["/v1/images/edits", "/v1/images/inpaint"]
    assert upstream.requests[1].url.host == "127.0.0.1"
    assert response.status_code == 500
    _assert_nothing_leaked(response)


@pytest.mark.parametrize("tool", [_inpaint, _harmonize], ids=["inpaint", "harmonize"])
def test_an_admin_is_held_to_the_registered_endpoints_too(api, upstream, tool):
    response = tool(api.as_admin(), _endpoint="http://127.0.0.1:9999/v1")

    assert response.status_code == 403
    assert upstream.requests == []
