"""OpenMoji pads codepoints to four hex digits; `AE.svg` was a CDN 404."""
import asyncio

from routes import emoji_routes


def test_openmoji_filename_pads_each_codepoint():
    assert emoji_routes._openmoji_filename("ae") == "00AE.svg"
    assert emoji_routes._openmoji_filename("a9") == "00A9.svg"
    assert emoji_routes._openmoji_filename("1f600") == "1F600.svg"
    assert emoji_routes._openmoji_filename("1f468-200d-1f469") == "1F468-200D-1F469.svg"
    assert emoji_routes._openmoji_filename("23-fe0f-20e3") == "0023-FE0F-20E3.svg"


def test_route_fetches_the_padded_filename(tmp_path, monkeypatch):
    fetched = []
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'

    class _Resp:
        status_code = 200
        content = svg

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            fetched.append(url)
            return _Resp()

    monkeypatch.setattr(emoji_routes, "_CACHE_DIR", tmp_path / "emoji")
    monkeypatch.setattr(emoji_routes.httpx, "AsyncClient", _Client)

    router = emoji_routes.setup_emoji_routes()
    endpoint = next(r.endpoint for r in router.routes if r.path == "/api/emoji/{code}.svg")
    response = asyncio.run(endpoint("ae"))

    assert fetched == [f"{emoji_routes._OPENMOJI_BASE}/00AE.svg"]
    assert response.body == svg
    assert (tmp_path / "emoji" / "ae.svg").exists(), "cache key stays the requested code"
