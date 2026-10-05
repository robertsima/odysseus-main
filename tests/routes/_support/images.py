"""Image bytes for upload tests, and a stand-in for an image or vision model server."""
import io


def png_bytes(size=(4, 2), color=(200, 30, 30)) -> bytes:
    """A small, valid PNG of the given size and colour."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeModelServer:
    """Answers every ``httpx.AsyncClient.post`` with ``reply`` and records the call.

    Routes that proxy to an image or vision model build their own AsyncClient,
    so the stand-in replaces the method rather than a client instance.
    """

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def install(self, monkeypatch):
        import httpx

        server = self

        async def post(client, url, *args, **kwargs):
            server.calls.append({"url": str(url), **kwargs})
            return httpx.Response(200, json=server.reply, request=httpx.Request("POST", str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        return self
